"""统一的 Γ/FaVOR CLI 工作流：先冻结、后提交、再验证，失败候选仍占试验名额。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
import fcntl
import hashlib
import json

import polars as pl

from factor_miner.canonical import sha256_json
from factor_miner.compiler import compile_candidate, build_polars_expr, attach_market_sessions
from factor_miner.construct_validation import validate_construct
from factor_miner.favor_schema import FavorPlan, FavorDataRelease, FavorSubmission, FavorRegimePlan, FavorRegimeSubmission, parse_favor_plan
from factor_miner.favor_validation import require_panel, validate_empirical_construct
from factor_miner.favor_integration import percentile_scores, joint_signals, directional_selectivity, execute_joint, portfolio_metrics
from factor_miner.hypothesis_constraints import compile_gamma
from factor_miner.ledger import JsonlLedger, TrialEvent, EventType
from factor_miner.research_report import write_json, report_schedule, ic_diagnostics
from factor_miner.research_pool import validate_search_budget
from factor_miner.schema import TrustedCandidateFactorSpec, AvailabilitySpec, ExpectedSign
from factor_miner.errors import FactorMinerError


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def writer(root: Path):
    """全流程单写入者，检测到已有写入者立即失败。"""
    with (root/".writer.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("FaVOR 已有写入者") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def candidate_hypothesis(plan: FavorPlan, condition):
    """高层主张相同，逐条件的测量代理和预期收益方向分别冻结。"""
    return plan.hypothesis.model_copy(update={"observable_proxy": condition.observation.observation,
                                              "expected_sign": ExpectedSign(condition.expected_return_sign)})


def load_plan(root: Path) -> FavorPlan:
    plan = parse_favor_plan(json.loads((root/"plan.json").read_text()))
    registration = json.loads((root/"registration.json").read_text())
    if registration["plan_sha256"] != sha256_json(plan.model_dump(mode="json")):
        raise ValueError("已冻结 FaVOR 计划发生变化")
    for condition in plan.conditions:
        gamma = compile_gamma(candidate_hypothesis(plan, condition), condition, plan.allowed_fields, version=plan.gamma_version)
        saved = json.loads((root/"gamma"/f"{condition.condition_id}.json").read_text())
        if saved != {**gamma.model_dump(mode="json"), "gamma_sha256": gamma.identity}:
            raise ValueError("已登记 Γ 文件与计划不一致")
    JsonlLedger(root/"ledger").verify()
    return plan


def verify_inputs(plan: FavorPlan) -> FavorDataRelease:
    paths = [Path(plan.dataset_root)/name for name in ("market.parquet", "state.parquet", "label.parquet", "calendar.parquet")]
    paths.append(Path(plan.data_contract_path))
    if plan.terminal_events_path:
        paths.append(Path(plan.terminal_events_path))
    for path in paths:
        if str(path) not in plan.input_sha256 or file_sha(path) != plan.input_sha256[str(path)]:
            raise ValueError(f"发布输入未冻结或内容变化：{path.name}")
    release = FavorDataRelease.model_validate_json(Path(plan.data_contract_path).read_text())
    if release.market_id != plan.market_id or release.as_of_date < plan.splits.test_end:
        raise ValueError("发布市场不符或不能覆盖测试区间")
    if plan.terminal_events_path:
        terminal=pl.read_parquet(plan.terminal_events_path)
        from factor_miner.causal_backtest import _require_unique
        _require_unique(terminal,required={'security_id','event_date','available_at','terminal_value','source'},
            keys=['security_id','event_date'],name='终止结算')
        if terminal.filter(~(pl.col('terminal_value').is_finite()&(pl.col('terminal_value')>=0)).fill_null(False)).height:
            raise ValueError('终止结算只能包含已核实的非负有限回收值')
    return release


def register_favor(config_path: Path, root: Path, *, legacy_replay: bool = False) -> Path:
    """冻结假设、条件、预算与输入身份，不读取收益数值或候选公式。"""
    plan = parse_favor_plan(json.loads(config_path.read_text()))
    preflight = None
    if plan.run_kind == 'research' and not legacy_replay:
        if not isinstance(plan, FavorRegimePlan):
            raise ValueError('新增研究必须使用 favor-regime-v1，在生成公式前登记状态主张；旧计划仅可显式复现')
        if plan.search_budget.get('version') != 'bounded-research-v2':
            raise ValueError('新增研究必须使用 bounded-research-v2；旧合同仅可显式历史复现')
        from factor_miner.favor_preflight import inspect_measurement_contracts, inspect_data_feasibility
        preflight = inspect_measurement_contracts(plan)
        if not preflight['passed']:
            raise ValueError('事前测量合同预检未通过')
        preflight['data_feasibility'] = inspect_data_feasibility(plan)
        if preflight['data_feasibility']['needs_review']:
            response = plan.search_budget.get('feasibility_response')
            if not isinstance(response, str) or not response.strip():
                raise ValueError('事前状态锚覆盖或事件上限不足；须冻结代理与候选差异的解释，不能直接登记')
    release = verify_inputs(plan)
    root.mkdir(parents=True, exist_ok=False)
    with writer(root):
        write_json(root/"plan.json", plan.model_dump(mode="json"))
        if preflight is not None:
            write_json(root/'preflight.json', preflight)
        write_json(root/"registration.json", dict(plan_sha256=sha256_json(plan.model_dump(mode="json")),
            registered_at=datetime.now(timezone.utc).isoformat(), data_release=release.model_dump(mode="json"),
            budget=validate_search_budget(plan.search_budget, [s.model_dump(exclude={"condition_id"}) for s in plan.slots]),
            legacy_replay=legacy_replay))
        for condition in plan.conditions:
            gamma = compile_gamma(candidate_hypothesis(plan, condition), condition, plan.allowed_fields, version=plan.gamma_version)
            write_json(root/"gamma"/f"{condition.condition_id}.json", {**gamma.model_dump(mode="json"), "gamma_sha256": gamma.identity})
        from factor_miner.artifact_storage import snapshot_code
        snapshot_code(root)
        ledger = JsonlLedger(root/"ledger")
        for slot in plan.slots:
            ledger.append_event(TrialEvent(event_type=EventType.CANDIDATE_REGISTERED,
                candidate_id=f"reserved_{slot.trial_id}", status="reserved_before_expression", config_hash=sha256_json(plan.model_dump(mode="json"))))
    return root


def favor_generation_payload(root: Path, trial_id: str, producer: str) -> dict:
    """两个生成入口共享同一合同，不包含个股样本、行情或统计结果。"""
    plan = load_plan(root)
    slot = next((s for s in plan.slots if s.trial_id == trial_id), None)
    if slot is None or (root/"evaluation_started.json").exists():
        raise ValueError("候选槽不存在或研究已封存")
    condition = next(c for c in plan.conditions if c.condition_id == slot.condition_id)
    gamma = compile_gamma(candidate_hypothesis(plan, condition), condition, plan.allowed_fields, version=plan.gamma_version)
    payload = dict(instruction="根据冻结假设与 Γ 生成一个 typed AST；不得改变字段、窗口、方向、预算。只返回 submission。",
                hypothesis=candidate_hypothesis(plan, condition).model_dump(mode="json"),
                condition=condition.model_dump(mode="json"),
                submission_identity=dict(trial_id=trial_id, plan_sha256=sha256_json(plan.model_dump(mode="json")),
                                         gamma_sha256=gamma.identity, producer=producer),
                response_schema=FavorSubmission.model_json_schema())
    if isinstance(plan, FavorRegimePlan):
        state_spec = plan.regimes[slot.condition_id]
        payload['regime_hypothesis'] = state_spec.model_dump(mode='json')
        payload['submission_identity']['regime_sha256'] = state_spec.identity
        payload['response_schema'] = FavorRegimeSubmission.model_json_schema()
        payload['instruction'] += ' 状态主张已冻结，不得修改，也不得自动将状态变成交易触发。'
    if plan.version == 'favor-exploration-v1':
        payload['exploration_measurement'] = plan.exploration[slot.condition_id].model_dump(mode='json')
        payload['instruction'] += ' 本次只取得探索资格；不能将探索诊断称为正式验证通过。'
    return payload


def prepare_favor_deepseek(root: Path, trial_id: str, output: Path) -> None:
    """复用既有 DeepSeek 请求及授权执行器；这里只准备请求，不产生付费调用。"""
    from factor_miner.llm_online import build_deepseek_request, AgentRole
    payload = favor_generation_payload(root, trial_id, "deepseek_api")
    request = build_deepseek_request(campaign_id="favor_"+payload["submission_identity"]["plan_sha256"][:24],
        agent_role=AgentRole.EXPRESSION, slot_ids=(trial_id,),
        system_prompt="你是因子公式生成器。严格遵守冻结 Γ，只输出指定 JSON。不得修改假设或评价标准。",
        user_payload=payload)
    write_json(output, request.model_dump(mode="json"))


def submit_favor(root: Path, payload: dict) -> dict:
    """登记提交字节后校验；编译失败不可重写，也不释放该试验槽。"""
    with writer(root):
        plan = load_plan(root)
        if (root/"evaluation_started.json").exists():
            raise ValueError("已经开始评价，禁止追加或替换候选")
        trial_id = payload.get("trial_id")
        slot = next((s for s in plan.slots if s.trial_id == trial_id), None)
        if slot is None:
            raise ValueError("提交未登记的候选槽")
        folder = root/"submissions"/trial_id
        folder.mkdir(parents=True, exist_ok=False)
        write_json(folder/"submission.json", payload)
        ledger = JsonlLedger(root/"ledger")
        ledger.append_event(TrialEvent(event_type=EventType.CANDIDATE_REGISTERED, candidate_id=f"attempt_{trial_id}",
            status="submitted", spec_hash=sha256_json(payload), artifact_refs=(str(folder/"submission.json"),)))
        try:
            submission = (FavorRegimeSubmission if isinstance(plan,FavorRegimePlan) else FavorSubmission).model_validate(payload)
            if isinstance(plan,FavorRegimePlan) and submission.regime_sha256 != plan.regimes[slot.condition_id].identity:
                raise ValueError('提交未绑定冻结状态主张')
            condition = next(c for c in plan.conditions if c.condition_id == slot.condition_id)
            hypothesis = candidate_hypothesis(plan, condition)
            gamma = compile_gamma(hypothesis, condition, plan.allowed_fields, version=plan.gamma_version)
            if submission.plan_sha256 != sha256_json(plan.model_dump(mode="json")) or submission.gamma_sha256 != gamma.identity:
                raise ValueError("提交未绑定已冻结计划和 Γ")
            gamma.validate(submission.expression, hypothesis)
            from factor_miner.dsl import validate_ast
            metadata = validate_ast(submission.expression, plan.allowed_fields, (), limits=gamma.dsl_limits)
            spec = TrustedCandidateFactorSpec(spec_version="3" if plan.version in {"favor-gamma-v3", "favor-regime-v1", "favor-exploration-v1"} else "2", hypothesis=hypothesis, expression=submission.expression,
                required_fields=metadata.required_fields, max_lookback=metadata.lookback,
                availability=AvailabilitySpec(observation="close_t", decision="after_close_t", earliest_trade="open_t_plus_1"),
                created_at=datetime.now(timezone.utc), provenance={"producer": submission.producer,
                    "favor_plan_sha256": submission.plan_sha256, "trial_id": trial_id,
                    "gamma_json": gamma.model_dump_json(), "gamma_sha256": gamma.identity,
                    "observable_condition_json": condition.observation.model_dump_json()})
            compiled = compile_candidate(spec, plan.allowed_fields)
            ledger.register_candidate(spec)
            write_json(folder/"spec.json", spec.model_dump(mode="json"))
            if isinstance(plan, FavorRegimePlan):
                from factor_miner.regime import RegimeRecord
                record = RegimeRecord(market_id=plan.market_id, source_candidate_id=compiled.candidate_id,
                    factor_id=compiled.candidate_id, hypothesis_sha256=sha256_json(hypothesis.model_dump(mode='json')),
                    mechanism=hypothesis.mechanism, origin='before_expression',
                    registered_at=json.loads((root/'registration.json').read_text())['registered_at'],
                    results_seen=False, previously_seen=False, context_id=submission.plan_sha256,
                    context_label=f'{plan.execution.holding_sessions}交易日 · 本计划状态主张尚未检验',
                    spec=plan.regimes[slot.condition_id])
                write_json(folder/'regime_record.json',record.model_dump(mode='json'))
            receipt = dict(status="compiled", trial_id=trial_id, condition_id=slot.condition_id,
                           source_candidate_id=compiled.candidate_id, ast_hash=compiled.ast_hash)
        except (ValueError, FactorMinerError) as error:
            # 所有提交失败保存原始字节和原因；不是改公式或计算数据的兜底。
            receipt = dict(status="compile_failed", trial_id=trial_id, condition_id=slot.condition_id,
                           reason=str(error), source_candidate_id="failed_"+sha256_json(payload)[:24])
            ledger.append_event(TrialEvent(event_type=EventType.COMPILE_FAILED,
                candidate_id=receipt["source_candidate_id"], status="compile_failed", artifact_refs=(str(folder/"submission.json"),)))
        write_json(folder/"receipt.json", receipt)
        return receipt


def _load_panels(plan: FavorPlan):
    release = verify_inputs(plan)
    dataset = Path(plan.dataset_root)
    market, state, calendar = [pl.read_parquet(dataset/name) for name in ("market.parquet", "state.parquet", "calendar.parquet")]
    require_panel(market, {"date", "asset", "open", *plan.allowed_fields}, "行情")
    masks = {"valid_for_factor_compute", "valid_for_factor_rank", "can_open_long", "can_close_long"}
    require_panel(state, {"date", "asset", *masks}, "状态")
    if any(state.schema[x] != pl.Boolean or state[x].null_count() for x in masks):
        raise ValueError("计算、排名、可交易 mask 必须为非空布尔值")
    if market["date"].max() != release.as_of_date or state["date"].max() != release.state_as_of_date:
        raise ValueError("行情或状态表实际截止日与发布合同不符")
    if market.select("date", "asset").join(state.select("date", "asset"), on=["date", "asset"], how="anti").height:
        raise ValueError("行情存在缺失状态的证券日期")
    days = calendar["date"].to_list()
    if days != sorted(set(days)) or not set(market["date"].to_list()).issubset(days):
        raise ValueError("交易日历重复、乱序或缺少行情日")
    market = attach_market_sessions(market.lazy(), calendar, calendar_months=plan.version in {"favor-gamma-v3", "favor-regime-v1", "favor-exploration-v1"}).collect().sort("asset", "date")
    return market, state, days


def _pair_redundancy(left: pl.DataFrame, right: pl.DataFrame) -> dict:
    paired = left.join(right.rename({"score": "other"}), on=["date", "asset"], validate="1:1")
    daily = paired.group_by("date").agg(pl.len().alias("count"), pl.corr("score", "other", method="spearman").abs().alias("rho"))
    daily = daily.filter((pl.col("count") >= 20) & pl.col("rho").is_finite())
    if daily.height < 60:
        raise ValueError("有效代表输出冗余检验不足60日、每日20只股票")
    return dict(valid_dates=daily.height, p95_abs_spearman=daily["rho"].quantile(.95))


def _trade_inputs(plan: FavorPlan, market, state, days, start, end, boundary=None):
    schedule = report_schedule(days, start, end, plan.execution.frequency, holding_sessions=plan.execution.holding_sessions)
    if boundary is not None:
        schedule = tuple(w for w in schedule if w.exit_date < boundary)
    if not schedule:
        raise ValueError("purge 后没有完整交易窗口")
    last = max(w.exit_date for w in schedule)
    trade_market = market.filter(pl.col("date").is_between(start, last)).select(pl.col("date").alias("trade_date"), pl.col("asset").alias("security_id"), "open")
    trade_state = state.filter(pl.col("date").is_between(start, last)).select(pl.col("date").alias("trade_date"),
        pl.col("asset").alias("security_id"), "valid_for_factor_rank", "can_open_long", "can_close_long")
    return schedule, trade_market, trade_state


def run_favor(root: Path) -> Path:
    """完成共同入口全部验证与联合策略，测试结果不得反馈重新挑选。"""
    with writer(root):
        plan = load_plan(root)
        code_identity = json.loads((root/"code_identity.json").read_text())
        if any(file_sha(Path(__file__).parent/name) != digest for name, digest in code_identity.items()):
            raise ValueError("研究代码已改变，请使用冻结代码快照或登记新协议")
        write_json(root/"evaluation_started.json", dict(started_at=datetime.now(timezone.utc).isoformat()))
        ledger = JsonlLedger(root/"ledger")
        ledger.append_event(TrialEvent(event_type=EventType.RUN_STARTED, status="favor_validation"))
        try:
            from factor_miner.research_pool import ResearchResourceGuard
            guard = ResearchResourceGuard(plan.search_budget, root)
            guard.check()
            _evaluate(plan, root, ledger, guard=guard)
        except Exception as error:
            write_json(root/"failure.json", dict(status="failed", reason=str(error), retry="保留失败记录；修正数据或代码后登记新运行"))
            ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_FAILED, status="failed"))
            raise
        ledger.append_event(TrialEvent(event_type=EventType.RUN_COMPLETED, status="completed"))
    return root


def _evaluate(plan: FavorPlan, root: Path, ledger: JsonlLedger, *, guard=None) -> None:
    market, state, days = _load_panels(plan)
    split = plan.splits
    exploring = plan.version == 'favor-exploration-v1'
    if exploring:
        market = market.filter(pl.col('date') < split.test_start)
        state = state.filter(pl.col('date') < split.test_start)
    reports, raw, scores, specs = {}, {}, {}, {}
    conditions = {c.condition_id: c for c in plan.conditions}
    seen = set(plan.historical_ast_hashes)
    for slot in plan.slots:
        if guard is not None:
            guard.check()
        receipt_path = root/"submissions"/slot.trial_id/"receipt.json"
        if not receipt_path.exists():
            reports[slot.trial_id] = dict(status="not_submitted", condition_id=slot.condition_id, reason="封存时未提交；预留检验名额不缩减")
            continue
        receipt = json.loads(receipt_path.read_text())
        report = reports[slot.trial_id] = dict(receipt)
        if receipt["status"] != "compiled":
            continue
        if receipt["ast_hash"] in seen:
            report.update(status="duplicate", reason="规范 AST 已在冻结历史或本轮尝试过")
            continue
        seen.add(receipt["ast_hash"])
        spec = TrustedCandidateFactorSpec.model_validate_json((receipt_path.parent/"spec.json").read_text())
        submission_type = FavorRegimeSubmission if isinstance(plan, FavorRegimePlan) else FavorSubmission
        submission = submission_type.model_validate_json((receipt_path.parent/"submission.json").read_text())
        if isinstance(plan, FavorRegimePlan) and submission.regime_sha256 != plan.regimes[slot.condition_id].identity:
            raise ValueError("评价提交的状态主张与事前冻结状态不一致")
        expected_gamma = compile_gamma(candidate_hypothesis(plan, conditions[slot.condition_id]), conditions[slot.condition_id], plan.allowed_fields, version=plan.gamma_version)
        if (submission.expression != spec.expression or submission.gamma_sha256 != expected_gamma.identity
                or spec.provenance.get("gamma_sha256") != expected_gamma.identity):
            raise ValueError("提交或候选约束与事前冻结 Γ 不一致")
        compiled = compile_candidate(spec, plan.allowed_fields)
        if compiled.candidate_id != receipt["source_candidate_id"]:
            raise ValueError("候选 Spec 与提交回执身份不一致")
        condition = conditions[slot.condition_id]
        synthetic = validate_construct(compiled.expression, condition.observation,
            calendar_months=plan.version in {"favor-gamma-v3", "favor-regime-v1", "favor-exploration-v1"},
            response_aggregation='directional_changes' if exploring and plan.exploration[condition.condition_id].kind != 'continuous' else 'median')
        report["synthetic"] = synthetic
        if synthetic["status"] != "基础检验符合":
            report.update(status="synthetic_failed", reason="合成反例未通过")
            continue
        values = market.with_columns(build_polars_expr(spec.expression).alias("raw_factor")).join(
            state.select("date", "asset", "valid_for_factor_compute"), on=["date", "asset"], validate="1:1").select("date", "asset", "raw_factor", "valid_for_factor_compute")
        if plan.search_budget.get('version') == 'bounded-research-v2' and not exploring:
            from factor_miner.favor_validation import signal_feasibility
            feasibility = signal_feasibility(values, state, split.discovery_start, split.discovery_end, plan.construct_policy)
            report['data_feasibility'] = feasibility
            if not feasibility['feasible']:
                report.update(status='empirical_failed', reason='发现期有效覆盖或截面日期不足；未检验收益')
                write_json(root/'factors'/slot.trial_id/'construct.json', dict(synthetic=synthetic, data_feasibility=feasibility))
                continue
        if exploring:
            from factor_miner.favor_exploration import validate_exploration_construct
            empirical = validate_exploration_construct(values, market, state, condition, split.discovery_start,
                split.discovery_end, plan.exploration[condition.condition_id], days)
        else:
            empirical = validate_empirical_construct(values, market, state, condition, split.discovery_start, split.discovery_end, plan.construct_policy)
        report["empirical"] = empirical
        folder = root/"factors"/slot.trial_id
        write_json(folder/"construct.json", dict(synthetic=synthetic, empirical=empirical))
        if not empirical["passed"]:
            report.update(status=empirical['status'] if exploring else "empirical_failed",
                reason="事前类型化测量检查未通过；未检验收益" if exploring else "真实状态五分位构念检验未通过")
            continue
        values.write_parquet(folder/"raw_factor.parquet")
        raw[slot.trial_id], specs[slot.trial_id] = values, spec
        if not exploring:
            scores[slot.trial_id] = percentile_scores(values, state, condition.activation_direction)
        report.update(status="construct_passed", raw_path=str(folder/"raw_factor.parquet"))
    if exploring:
        from factor_miner.favor_exploration import finish_exploration
        finish_exploration(plan, root, ledger, reports, raw, market, state, days, guard)
        return
    if plan.search_budget.get('version') == 'bounded-research-v2':
        from factor_miner.favor_integration import selectivity_feasibility
        options = [[t for t in scores if reports[t]['condition_id'] == c.condition_id] for c in plan.conditions]
        event_checks = []
        for members in product(*options):
            ladder = {q: joint_signals(scores, members, (q,)*len(members)) for q in plan.integration.selectivity_levels}
            event_checks.append(dict(members=members, **selectivity_feasibility(ladder,
                start=split.discovery_start, end=split.discovery_end,
                min_events=plan.integration.min_events_per_ticker, min_tickers=plan.integration.min_tickers,
                support_threshold=plan.integration.ticker_support_threshold)))
        write_json(root/'event_feasibility.json', dict(combinations=event_checks, return_labels_used=False))
        if not any(item['feasible'] for item in event_checks):
            _finish_without_outcomes(plan, root, ledger, reports, len(raw), guard=guard)
            return
    if guard is not None:
        guard.check()
    # 所有构念检查完成之后才打开标签。
    ledger.append_event(TrialEvent(event_type=EventType.OUTCOME_EXPOSED, outcome_exposed=True, status="discovery"))
    labels = pl.read_parquet(Path(plan.dataset_root)/"label.parquet")
    horizon = plan.execution.holding_sessions
    label_column = f"label_o2o_{horizon}d"
    ic_lags = max(5, horizon)
    require_panel(labels, {"date", "asset", label_column, "label_entry_date", "label_exit_date"}, "标签")
    expected = pl.DataFrame([dict(date=d, entry=days[i+1], exit=days[i+horizon+1]) for i, d in enumerate(days[:-(horizon+1)])], schema={"date": pl.Date, "entry": pl.Date, "exit": pl.Date})
    check = labels.join(expected, on="date", how="left", validate="m:1")
    if check.filter(pl.col(label_column).is_finite() &
                    (pl.col("entry").is_null() | pl.col("label_entry_date").is_null() | pl.col("label_exit_date").is_null()
                     | (pl.col("label_entry_date") != pl.col("entry")) | (pl.col("label_exit_date") != pl.col("exit")))).height:
        raise ValueError(f"标签不是固定市场日历 T+1/T+{horizon+1}")
    family_size = validate_search_budget(plan.search_budget, [s.model_dump(exclude={"condition_id"}) for s in plan.slots])["diagnostic_family_size"]
    for trial, values in raw.items():
        daily, metrics = ic_diagnostics(values.lazy(), state.lazy(), labels.lazy(), split.discovery_start, split.discovery_end, split.validation_start, family_size, label_column=label_column, hac_max_lags=ic_lags)
        daily.write_parquet(root/"factors"/trial/"discovery_ic.parquet")
        reports[trial].update(discovery=metrics, standalone_statistical_pass=bool(metrics["bonferroni_p_value"] < .05
            and metrics["rank_ic_mean"]*(1 if specs[trial].hypothesis.expected_sign == "positive" else -1) > 0))
    # 构念成立但边际 IC 弱仍可进入联合假设检验；单因子显著性单列。
    accepted, pairs = [], []
    for trial in sorted(raw, key=lambda t: (-abs(reports[t]["discovery"]["rank_ic_hac_t"]), t)):
        for prior in accepted:
            pair = _pair_redundancy(scores[trial].filter(pl.col("date").is_between(split.discovery_start, split.discovery_end)),
                                    scores[prior].filter(pl.col("date").is_between(split.discovery_start, split.discovery_end)))
            pairs.append(dict(candidate=trial, representative=prior, **pair))
            if pair["p95_abs_spearman"] >= plan.integration.redundancy_threshold:
                reports[trial].update(status="redundant", representative=prior)
                break
        else:
            accepted.append(trial)
    write_json(root/"redundancy.json", pairs)
    options = [[t for t in accepted if reports[t]["condition_id"] == c.condition_id] for c in plan.conditions]
    combinations_list = list(product(*options)) if all(options) else []
    write_json(root/"combination_freeze.json", dict(members=combinations_list, selection_period="discovery", missing_conditions=[c.condition_id for c, xs in zip(plan.conditions, options) if not xs]))
    terminals = pl.read_parquet(plan.terminal_events_path) if plan.terminal_events_path else None
    # 分区结束即截断结算事件，验证期锁仓不得利用测试期终止信息估值。
    validation_trade = _trade_inputs(plan, market, state, days, split.validation_start, split.validation_end, split.test_start)
    combo_reports, finalists = {}, []
    for index, members in enumerate(combinations_list):
        if guard is not None:
            guard.check()
        combo_id = f"combo_{index+1:03d}"
        ladder = {q: joint_signals(scores, members, (q,)*len(members)) for q in plan.integration.selectivity_levels}
        selectivity = directional_selectivity(ladder, labels, start=split.discovery_start, end=split.discovery_end,
            boundary=split.validation_start, min_events=plan.integration.min_events_per_ticker,
            min_tickers=plan.integration.min_tickers, support_threshold=plan.integration.ticker_support_threshold, label_column=label_column)
        report = combo_reports[combo_id] = dict(members=members, selectivity=selectivity, status="selectivity_failed")
        if not selectivity["passed"]:
            continue
        tested = []
        for thresholds in plan.integration.validation_thresholds:
            signals = joint_signals(scores, members, thresholds)
            result = execute_joint(signals, validation_trade[1], validation_trade[2], validation_trade[0],
                cost_bps=plan.execution.round_trip_cost_bps, terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
            metrics = portfolio_metrics(result)
            tested.append(dict(thresholds=thresholds, metrics=metrics, filled_buys=sum(x["side"] == "buy" and x["status"] == "filled" for x in result.orders)))
        valid = [x for x in tested if x["metrics"]["calmar"] is not None and x["filled_buys"] > 0]
        report["validation_trials"] = tested
        if not valid:
            report["status"] = "no_valid_threshold"
            continue
        best = max(valid, key=lambda x: x["metrics"]["calmar"])
        report.update(status="threshold_frozen", thresholds=best["thresholds"])
        finalists.append(combo_id)
    # 先写最终阈值和保留组成，再读取任何测试期统计；测试不好也不换阈值重选。
    retained = sorted({t for cid in finalists for t in combo_reports[cid]["members"]})
    write_json(root/"strategy_freeze.json", dict(combinations={cid: combo_reports[cid] for cid in finalists}, retained_trials=retained,
        test_used_for_selection=False, plan_sha256=sha256_json(plan.model_dump(mode="json"))))
    test_trade = _trade_inputs(plan, market, state, days, split.test_start, split.test_end)
    benchmark_signals = state.filter(pl.col("valid_for_factor_compute") & pl.col("valid_for_factor_rank")).select("date", "asset", pl.lit(True).alias("trigger"))
    benchmark = execute_joint(benchmark_signals, test_trade[1], test_trade[2], test_trade[0], cost_bps=0.,
        terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
    write_json(root/"benchmark.json", dict(summary=benchmark.execution_summary, daily_returns=benchmark.daily_returns,
        unresolved_positions=benchmark.unresolved_positions, zero_recovery_daily_returns=benchmark.zero_recovery_daily_returns))
    for cid in finalists:
        report = combo_reports[cid]
        signals = joint_signals(scores, report["members"], tuple(report["thresholds"]))
        folder = root/"strategies"/cid
        folder.mkdir(parents=True)
        signals.filter(pl.col("date").is_between(split.test_start, split.test_end)).write_parquet(folder/"signals.parquet")
        outcomes = []
        for cost in (plan.execution.round_trip_cost_bps, *plan.execution.stress_cost_bps):
            result = execute_joint(signals, test_trade[1], test_trade[2], test_trade[0], cost_bps=cost,
                terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
            outcome = dict(cost_bps=cost, metrics=portfolio_metrics(result, benchmark), summary=result.execution_summary,
                           unresolved_positions=result.unresolved_positions, daily_returns=result.daily_returns,
                           zero_recovery_daily_returns=result.zero_recovery_daily_returns, orders=result.orders,
                           selections=result.selections, holdings_daily=result.holdings_daily)
            outcomes.append(outcome)
        write_json(folder/"backtest.json", outcomes)
        report.update(status="retained_combination", test=[{k: v for k, v in x.items() if k not in {"orders", "daily_returns", "zero_recovery_daily_returns", "selections", "holdings_daily"}} for x in outcomes])
    for trial, report in reports.items():
        if trial in retained:
            report["status"] = "retained_component"
            daily, metrics = ic_diagnostics(raw[trial].lazy(), state.lazy(), labels.lazy(), split.test_start, split.test_end, None, family_size, label_column=label_column, hac_max_lags=ic_lags)
            daily.write_parquet(root/"factors"/trial/"test_ic.parquet")
            report["test_ic"] = metrics
            from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
            signal = raw[trial].filter(pl.col("valid_for_factor_compute")).select(pl.col("date").alias("signal_date"),
                pl.col("asset").alias("security_id"), pl.col("raw_factor").alias("factor_value"))
            common = dict(group_count=10, round_trip_cost_bps=plan.execution.round_trip_cost_bps,
                terminal_policy=plan.execution.terminal_policy, terminal_events=terminals, allow_noncontiguous_schedule=True)
            direction = str(specs[trial].hypothesis.expected_sign)
            target = simulate_causal_extreme_portfolio(signal, test_trade[1], test_trade[2], test_trade[0], direction=direction, **common)
            other = simulate_causal_extreme_portfolio(signal, test_trade[1], test_trade[2], test_trade[0], direction="negative" if direction == "positive" else "positive", **common)
            other_rows = {r["exit_date"]: r for r in other.daily_returns}
            spread = [{"date": r["exit_date"], "Q10_Q1_net_return": r["target_long_net_return"]-other_rows[r["exit_date"]]["target_long_net_return"]} for r in target.daily_returns]
            win_rate = sum(r["Q10_Q1_net_return"] > 0 for r in spread)/len(spread)
            accounting = dict(metrics=portfolio_metrics(target, benchmark), daily_returns=target.daily_returns,
                zero_recovery_daily_returns=target.zero_recovery_daily_returns, unresolved_positions=target.unresolved_positions,
                opposite_unresolved_positions=other.unresolved_positions, benchmark_unresolved_positions=benchmark.unresolved_positions,
                win_rate=None if target.unresolved_positions or other.unresolved_positions else win_rate,
                reference_spread_win_rate=win_rate, spread=spread, orders=target.orders, selections=target.selections)
            write_json(root/"factors"/trial/"backtest.json", accounting)
            report["portfolio"] = {k:v for k,v in accounting.items() if k not in {"daily_returns", "zero_recovery_daily_returns", "spread", "orders", "selections"}}
            report["qualification"] = "FaVOR 联合策略组成；单因子显著性另列，非独立机制证明"
        elif report["status"] == "construct_passed":
            report.update(status="not_retained", reason="未进入通过方向选择性及阈值验证的完整联合假设")
        write_json(root/"trials"/f"{trial}.json", report)
        ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_COMPLETED, candidate_id=report.get("source_candidate_id", f"reserved_{trial}"),
            status=report["status"], outcome_exposed=trial in raw, artifact_refs=(str(root/"trials"/f"{trial}.json"),)))
    summary = dict(version=plan.version, run_kind=plan.run_kind, status="completed", market_id=plan.market_id,
        plan_sha256=sha256_json(plan.model_dump(mode="json")), test_consumed=split.test_consumed, sealed_oos=not split.test_consumed,
        registered=len(plan.slots), submitted=sum((root/"submissions"/s.trial_id/"receipt.json").exists() for s in plan.slots),
        construct_passed=len(raw), retained_components=len(retained), retained_combinations=len(finalists),
        diagnostic_family_size=family_size, factors=reports, combinations=combo_reports,
        scope="通过当前协议验证的候选；结构测量验证不等于经济因果证明")
    if plan.version in {"favor-gamma-v2", "favor-gamma-v3", "favor-regime-v1", "favor-exploration-v1"}:
        summary["horizon_contract"] = dict(label_column=label_column, holding_sessions=horizon,
            frequency=plan.execution.frequency, ic_hac_max_lags=ic_lags, execution_mode="fixed_horizon",
            overlapping_windows_policy="资金须实际可用，错过入场不顺延；固定20日持有并非月末目标差额执行")
    write_json(root/"summary.json", summary)
    if guard is not None:
        guard.check()
    write_json(root/"completion.json", dict(summary_sha256=file_sha(root/"summary.json"),
        strategy_freeze_sha256=file_sha(root/"strategy_freeze.json")))


def _finish_without_outcomes(plan: FavorPlan, root: Path, ledger: JsonlLedger, reports: dict, construct_count: int, *, guard=None) -> None:
    """必要条件失败即收尾，保留所有预约，不暴露标签也不将缺数记成收益失败。"""
    for trial, report in reports.items():
        if report['status'] == 'construct_passed':
            report.update(status='not_retained', reason='缺少完整条件或联合事件容量不足；未检验收益')
        write_json(root/'trials'/f'{trial}.json', report)
        ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_COMPLETED,
            candidate_id=report.get('source_candidate_id', f'reserved_{trial}'), status=report['status'],
            outcome_exposed=False, artifact_refs=(str(root/'trials'/f'{trial}.json'),)))
    write_json(root/'strategy_freeze.json', dict(combinations={}, retained_trials=[], test_used_for_selection=False,
        plan_sha256=sha256_json(plan.model_dump(mode='json'))))
    budget = validate_search_budget(plan.search_budget, [s.model_dump(exclude={'condition_id'}) for s in plan.slots])
    write_json(root/'summary.json', dict(version=plan.version, run_kind=plan.run_kind, status='completed',
        market_id=plan.market_id, plan_sha256=sha256_json(plan.model_dump(mode='json')),
        test_consumed=plan.splits.test_consumed, sealed_oos=False, return_labels_used=False,
        registered=len(plan.slots), submitted=sum((root/'submissions'/s.trial_id/'receipt.json').exists() for s in plan.slots),
        construct_passed=construct_count, retained_components=0, retained_combinations=0,
        diagnostic_family_size=budget['diagnostic_family_size'], factors=reports, combinations={},
        scope='可行性必要条件不满足；收益尚未检验，全部名额保留'))
    if guard is not None:
        guard.check()
    write_json(root/'completion.json', dict(summary_sha256=file_sha(root/'summary.json'),
        strategy_freeze_sha256=file_sha(root/'strategy_freeze.json')))


def repair_favor_terminals(failed_root: Path, recovery_root: Path, root: Path) -> Path:
    """只修正终止输入并重放同一试验；原公式、统计预算、失败账本均保留。"""
    import shutil
    failure=json.loads((failed_root/'failure.json').read_text())
    if '终止结算' not in failure.get('reason','') or (failed_root/'completion.json').exists():
        raise ValueError('本入口仅恢复终止结算合同失败且未完成的运行')
    original=load_plan(failed_root)
    terminal=recovery_root/'terminal_values.parquet'
    receipt=json.loads((recovery_root/'completion.json').read_text())
    if receipt.get('status')!='completed' or receipt['terminal_values_sha256']!=file_sha(terminal):
        raise ValueError('终止回收发布未完成或哈希不符')
    plan=original.model_copy(update=dict(terminal_events_path=str(terminal),input_sha256={**original.input_sha256,str(terminal):file_sha(terminal)}))
    verify_inputs(plan)
    events=JsonlLedger(failed_root/'ledger').verify()
    if not events or events[-1].event_type!=EventType.EVALUATION_FAILED:raise ValueError('原账本没有对应失败事件')
    root.mkdir(parents=True,exist_ok=False)
    write_json(root/'plan.json',plan.model_dump(mode='json'))
    registration=json.loads((failed_root/'registration.json').read_text())
    write_json(root/'registration.json',dict(registration,plan_sha256=sha256_json(plan.model_dump(mode='json')),
        registered_at=datetime.now(timezone.utc).isoformat(),repair_of=str(failed_root)))
    for name in ['gamma','submissions','ledger']:shutil.copytree(failed_root/name,root/name)
    from factor_miner.artifact_storage import snapshot_code
    snapshot_code(root)
    write_json(root/'repair_receipt.json',dict(failed_root=str(failed_root),original_plan_sha256=registration['plan_sha256'],
        failure_sha256=file_sha(failed_root/'failure.json'),terminal_receipt_sha256=file_sha(recovery_root/'completion.json'),
        same_candidate_identities=True,new_hypotheses=0,new_model_comparisons=0,
        scope='同一事前试验的输入合同修正重放；原提交身份继续引用原计划，原失败链完整复制并追加纠错事件，不释放或重复登记候选名额。'))
    JsonlLedger(root/'ledger').append_event(TrialEvent(event_type=EventType.CAMPAIGN_REGISTERED,status='terminal_contract_repair',
        config_hash=sha256_json(plan.model_dump(mode='json')),supersedes_event_id=events[-1].event_id,
        artifact_refs=(str(root/'repair_receipt.json'),)))
    load_plan(root)
    return root
