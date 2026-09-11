"""既有 A 股候选的独立事后联合贡献研究入口。"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.joint_contribution import (
    block_indices, choose_representatives, coalition_design, coalition_key,
    correlation_summary, paired_delta, fit_rolling_models, predict_from_audit, shapley_values, sharpe,
)
from factor_miner.research_report import write_json, report_schedule, performance


class JointStudyConfig(BaseModel):
    """所有选择规则事前冻结；不提供正式确认或新因子入口。"""
    model_config = ConfigDict(extra='forbid')
    version: str = 'joint-contribution-diagnostic-v1'
    collection: Path
    replay_from: Path | None = None
    expected_candidates: int = Field(ge=2)
    discovery_start: date
    discovery_end: date
    evaluation_start: date
    evaluation_end: date
    forbidden_start: date
    train_signals: int = Field(default=104, ge=12)
    retrain_signals: int = Field(default=13, ge=1)
    alpha: float = Field(default=1000., gt=0, allow_inf_nan=False)
    min_names: int = Field(default=20, ge=10)
    min_dates: int = Field(default=60, ge=2)
    min_common_coverage: float = Field(default=.5, gt=0, le=1)
    redundancy_threshold: float = Field(default=.85, gt=0, le=1)
    permutations: int = Field(default=16, ge=2)
    exact_shapley: bool = False
    seed: int = 20260911
    bootstrap_repetitions: int = Field(default=200, ge=20)
    bootstrap_block_days: int = Field(default=20, ge=1)
    max_models: int = Field(default=180, ge=4)
    max_seconds: int = Field(default=7200, ge=1)
    max_artifact_bytes: int = Field(default=4294967296, ge=1)
    historical_budget_status: str = 'unrecoverable'
    outcomes_previously_seen: bool = True
    research_question: str = Field(min_length=10)

    @model_validator(mode='after')
    def validate_scope(self):
        if self.version != 'joint-contribution-diagnostic-v1':
            raise ValueError('未知联合诊断版本')
        if not self.discovery_start < self.discovery_end < self.evaluation_start < self.evaluation_end < self.forbidden_start:
            raise ValueError('发现、评价和禁止读取日期边界不合法')
        if self.historical_budget_status != 'unrecoverable' or not self.outcomes_previously_seen:
            raise ValueError('本入口仅支持明确事后诊断，不恢复正式统计资格')
        return self


def digest(path: Path) -> str:
    """流式内容身份，不将大文件加载到内存。"""
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def candidate_inputs(collection: Path, expected: int) -> tuple[list[dict], dict[str, str], Path]:
    """只使用已发布研究候选的身份；保留原结论及原始方向。"""
    from factor_miner.favor_cost_review import load_cost_review
    from factor_miner.ledger import JsonlLedger
    from factor_miner.compiler import compile_candidate
    from factor_miner.schema import TrustedCandidateFactorSpec
    manifest_path = collection / 'collection.json'
    manifest = read_json(manifest_path)
    if manifest['market_id'] != 'a_share':
        raise ValueError('本版只接收 A 股研究候选集合')
    files = {str(manifest_path.resolve()): digest(manifest_path)}
    candidates, releases = [], set()
    for item in manifest['reviews']:
        root = Path(item['root'])
        for name in ('summary', 'publication'):
            path = root / (name + '.json')
            if digest(path) != item[name + '_sha256']:
                raise ValueError('候选集合发布身份变化')
        summary = load_cost_review(root)
        publication = read_json(root/'publication.json')
        for name in ('summary', 'publication', 'protocol', 'completion'):
            files[str((root/(name+'.json')).resolve())] = digest(root/(name+'.json'))
        for row in summary['results']:
            if not row['admission']['eligible']:
                continue
            source = Path(row['source_root'])
            plan = read_json(source/'plan.json')
            original = read_json(source/'summary.json')['factors'][row['trial_id']]
            if original['source_candidate_id'] != row['source_candidate_id']:
                raise ValueError('研究候选与源报告身份不一致')
            slot = next(x for x in plan['slots'] if x['trial_id'] == row['trial_id'])
            condition = next(x for x in plan['conditions'] if x['condition_id'] == slot['condition_id'])
            if row['original_direction'] != condition['expected_return_sign']:
                raise ValueError('原始方向与事前条件不一致')
            raw = source/'factors'/row['trial_id']/'raw_factor.parquet'
            recorded_raw = Path(original['raw_path'])
            if recorded_raw.is_absolute():
                consistent_raw = recorded_raw.resolve() == raw.resolve()
            else:
                consistent_raw = str(raw).endswith('/' + str(recorded_raw))
            if not consistent_raw:
                raise ValueError('源报告原始矩阵路径与候选目录不一致')
            spec = source/'submissions'/row['trial_id']/'spec.json'
            compiled = compile_candidate(TrustedCandidateFactorSpec.model_validate(read_json(spec)), plan['allowed_fields'])
            if compiled.candidate_id != row['source_candidate_id']:
                raise ValueError('Spec 编译身份与候选不符')
            JsonlLedger(source/'ledger').verify()
            source_files = [source/'plan.json', source/'summary.json', source/'completion.json', spec, raw]
            source_files += sorted((source/'ledger').glob('*.jsonl'))
            source_files += [root/row['trial_id']/'report.json']
            for path in source_files:
                files[str(path.resolve())] = digest(path)
            for path, identity in plan['input_sha256'].items():
                if path in files and files[path] != identity:
                    raise ValueError('候选数据发布版本不一致')
                files[path] = identity
            releases.add(plan['dataset_root'])
            candidates.append(dict(factor_id=publication['aliases'][row['trial_id']],
                source_candidate_id=row['source_candidate_id'], trial_id=row['trial_id'],
                name=row['name'], original_direction=row['original_direction'], group=slot['mechanism_id'],
                ast_hash=compiled.ast_hash,
                source_root=str(source), raw_path=str(raw), spec_path=str(spec),
                original_statistical_pass=row.get('standalone_statistical_pass'),
                qualification='research_candidate_pending_confirmation', mechanism_status='mechanism_unverified'))
    if len(candidates) != expected or len(releases) != 1:
        raise ValueError('候选数或共同数据发布不符合预期')
    for key in ('factor_id', 'source_candidate_id'):
        if len({c[key] for c in candidates}) != len(candidates):
            raise ValueError('候选身份重复')
    return sorted(candidates, key=lambda c: c['factor_id']), files, Path(next(iter(releases)))


def verify_files(files: dict[str, str]):
    for path, identity in files.items():
        if digest(Path(path)) != identity:
            raise ValueError('冻结输入身份变化：' + path)


def register_joint_study(config_path: Path, output: Path) -> Path:
    """先登记候选、预算、代码和输入身份，不读取新的收益或相关性。"""
    cfg = JointStudyConfig.model_validate(read_json(config_path))
    candidates, inputs, dataset = candidate_inputs(cfg.collection, cfg.expected_candidates)
    required_inputs = [dataset/(name+'.parquet') for name in ('market', 'state', 'calendar', 'label', 'terminal_values')]
    required_inputs += [dataset/'release.json']
    if any(str(p.resolve()) not in inputs for p in required_inputs):
        raise ValueError('共同数据发布输入清单不完整')
    verify_files(inputs)
    release = read_json(dataset/'release.json')
    for name in ('release_id', 'adjustment', 'calendar_version', 'state_version', 'as_of_date', 'state_as_of_date'):
        if not release.get(name):
            raise ValueError('数据发布身份不完整：' + name)
    if release['as_of_date'] != release['state_as_of_date']:
        raise ValueError('行情与状态截止日不一致')
    if date.fromisoformat(release['as_of_date']) < cfg.evaluation_end:
        raise ValueError('数据发布没有覆盖评价末日')
    groups = {}
    for c in candidates:
        groups.setdefault(c['group'], []).append(c['factor_id'])
    models, paths = coalition_design(groups, permutations=cfg.permutations, seed=cfg.seed,
                                    exact=cfg.exact_shapley, max_models=cfg.max_models)
    replay = None
    if cfg.replay_from is not None:
        source = cfg.replay_from.resolve()
        old = read_json(source/'protocol.json')
        if not ((source/'completion.json').exists() or (source/'failure.json').exists()):
            raise ValueError('只能复用已经结束的运行')
        previous = dict(old['config']); current = cfg.model_dump(mode='json')
        previous.pop('replay_from', None); current.pop('replay_from', None)
        if previous != current or old['candidates'] != candidates or old['models'] != models:
            raise ValueError('重放不能改变研究参数、名单、资源上限或模型清单')
        for name in ('joint_contribution.py', 'research_incremental.py', 'causal_backtest.py', 'research_report.py'):
            if old['code_sha256'][name] != digest(Path(__file__).parent/name):
                raise ValueError('复用结果的模型或执行实现已改变')
        if old['input_sha256'] != inputs:
            raise ValueError('复用结果的数据或候选身份改变')
        cached = {}
        old_paths = read_json(source/'model_paths.json')
        for model, folder in old_paths.items():
            path = source/'execution'/folder
            primary = model in (coalition_key(()), coalition_key(old['groups']), 'equal_rank', 'reduced')
            costs = (0, 14, 20, 40) if primary else (14,)
            if all((path/str(cost)/'metrics.json').is_file() for cost in costs):
                cached[model] = str(path)
                for p in path.rglob('*'):
                    if p.is_file(): inputs[str(p.resolve())] = digest(p)
        for name in ('protocol.json', 'model_paths.json', 'fit_audit.json', 'model_freeze.json'):
            p = source/name; inputs[str(p.resolve())] = digest(p)
        replay = dict(source=str(source), models=cached, inherited_completed_models=len(set(cached.values())),
                      interpretation='仅补完原冻结清单，旧消耗及中断保留，不新增统计试验或重置历史族')
    out = output.resolve()
    if any(out == Path(p).resolve() or out in Path(p).resolve().parents for p in inputs):
        raise ValueError('产物目录不得包含输入文件')
    out.mkdir(parents=True, exist_ok=False)
    code = {}
    for path in sorted(Path(__file__).parent.glob('*.py')):
        code[path.name] = digest(path)
        target = out/'code'/path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
    protocol = dict(config=cfg.model_dump(mode='json'), registered_at=datetime.now(timezone.utc).isoformat(),
        candidates=candidates, groups=groups, models=models, permutations=paths,
        dataset=str(dataset), release=release, input_sha256=inputs, code_sha256=code,
        replay=replay,
        git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        primary_comparison='相关性代表精简 Ridge 与全部候选 Ridge 的万14多头参考净 Sharpe 差',
        value_function='每个非空子集重训 Ridge；空集为相同信号日共同股票池等权多头',
        selection_rule='发现期中位绝对 Spearman >= 阈值且同机制，按覆盖及稳定编号选代表',
        fixed_costs=[0, 14, 20, 40], shapley_cost_bps=14, historical_family_size=None,
        formal_pass=None, sealed_oos=False, new_candidates=0, test_returns_read=False,
        limits='分块区间仅条件于冻结预测，不消除历史筛选、模型拟合或多重比较偏差')
    write_json(out/'protocol.json', protocol)
    structures = {}
    for c in candidates:
        if 'ast_hash' in c:
            structures.setdefault(c['ast_hash'], []).append(c['factor_id'])
    write_json(out/'structural_relations.json', dict(by_ast_hash=structures,
        interpretation='结构重复保留在本轮诊断；不晋级独立信号或改写原冗余结论'))
    write_json(out/'registration.json', dict(protocol_sha256=digest(out/'protocol.json'), status='registered'))
    return out


def common_panel(candidates, state: pl.DataFrame, dates: list[date], cfg: JointStudyConfig):
    """按信号日掩码和有限原始测量构造共同截面，完全不读取标签。"""
    eligible = state.filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_rank')).select('date', 'asset')
    if eligible.select(pl.struct('date', 'asset').is_duplicated().any()).item():
        raise ValueError('状态主键重复')
    panel = eligible
    quality = []
    total = eligible.height
    for c in candidates:
        raw = pl.scan_parquet(c['raw_path']).filter(pl.col('date').is_in(dates)).select(
            'date', 'asset', 'raw_factor', 'valid_for_factor_compute').collect()
        if raw.select(pl.struct('date', 'asset').is_duplicated().any()).item():
            raise ValueError('因子主键重复')
        valid = raw.filter(pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite())
        valid = valid.join(eligible, on=['date', 'asset'], how='inner', validate='1:1')
        quality.append({**c, 'coverage': valid.height / total if total else 0})
        panel = panel.join(valid.select('date', 'asset', pl.col('raw_factor').alias(c['factor_id'])),
                           on=['date', 'asset'], how='inner', validate='1:1')
    coverage = eligible.group_by('date').len().rename({'len': 'eligible'}).join(
        panel.group_by('date').len().rename({'len': 'complete'}), on='date', how='left').with_columns(
        (pl.col('complete').fill_null(0)/pl.col('eligible')).alias('coverage')).sort('date')
    if set(dates) != set(coverage['date'].to_list()) or coverage.filter(
            (pl.col('coverage') < cfg.min_common_coverage) | (pl.col('complete').fill_null(0) < cfg.min_names)).height:
        raise ValueError('共同截面覆盖不足；不能按模型更换股票池')
    return panel.sort('date', 'asset'), quality, coverage


def load_labels(panel: pl.DataFrame, dataset: Path, days: list[date], cfg: JointStudyConfig) -> pl.DataFrame:
    """固定 T+1/T+6 日历核对；标签仅附加到既定预测股票池。"""
    labels = pl.scan_parquet(dataset/'label.parquet').filter(
        pl.col('date').is_in(panel['date'].unique().to_list()) & (pl.col('label_exit_date') < cfg.forbidden_start)
    ).select('date', 'asset', 'label_entry_date', 'label_exit_date', 'label_o2o_5d').collect()
    positions = {d: i for i, d in enumerate(days)}
    for d, entry, end in labels.select('date', 'label_entry_date', 'label_exit_date').unique().iter_rows():
        if entry != days[positions[d]+1] or end != days[positions[d]+6]:
            raise ValueError('标签未遵守固定市场日历 T+1/T+6')
    data = panel.join(labels, on=['date', 'asset'], how='left', validate='1:1')
    return data.with_columns(pl.when(pl.col('label_o2o_5d').is_finite()).then(pl.col('label_o2o_5d')).alias('_target')).with_columns(
        ((pl.col('_target')-pl.col('_target').mean().over('date')) / pl.col('_target').std(ddof=0).over('date')).alias('target_z'))


def evaluate_prediction(prediction, market, state, schedule, terminals, cost, no_signal=False):
    """复用因果成交引擎；不以未来可成交性改变已冻结选股。"""
    from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
    panel = prediction.select(pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'),
                              pl.col('score').alias('factor_value'))
    return simulate_causal_extreme_portfolio(panel, market, state, schedule,
        group_count=1 if no_signal else 10, round_trip_cost_bps=cost,
        terminal_policy='report_unresolved', terminal_events=terminals,
        retain_daily_holdings=False, allow_noncontiguous_schedule=True)


def save_execution(root, result):
    """订单、选择与未确定持仓保留，减少每个子集的重复日持仓矩阵。"""
    root.mkdir(parents=True, exist_ok=False)
    for name in ('daily_returns', 'selections', 'orders', 'unresolved_positions', 'zero_recovery_daily_returns'):
        rows = getattr(result, name)
        pl.DataFrame(rows).write_parquet(root/(name+'.parquet'))
    rows = [{**r, 'benchmark_return': 0.} for r in result.daily_returns]
    zero = [{**r, 'benchmark_return': 0.} for r in result.zero_recovery_daily_returns]
    metrics = dict(reference=performance(rows), actual=None if result.unresolved_positions else performance(rows),
                   zero_recovery=performance(zero) if zero else performance(rows),
                   unresolved_positions=len(result.unresolved_positions), execution=result.execution_summary)
    for scenario, data in [('reference', rows), ('actual', rows), ('zero_recovery', zero or rows)]:
        if metrics[scenario] is not None:
            metrics[scenario]['sharpe'] = sharpe(np.array([r['target_long_net_return'] for r in data]))
    write_json(root/'metrics.json', metrics)
    return metrics


def run_joint_study(root: Path) -> Path:
    """登记后单写入执行一轮，失败留痕；不改输入和生产投影。"""
    root = root.resolve()
    if digest(root/'protocol.json') != read_json(root/'registration.json')['protocol_sha256']:
        raise ValueError('已登记协议发生变化')
    protocol = read_json(root/'protocol.json')
    cfg = JointStudyConfig.model_validate(protocol['config'])
    verify_files(protocol['input_sha256'])
    for name, identity in protocol['code_sha256'].items():
        if digest(Path(__file__).parent/name) != identity or digest(root/'code'/name) != identity:
            raise ValueError('研究代码变化；须使用原快照或登记新版本')
    write_json(root/'started.json', dict(started_at=datetime.now(timezone.utc).isoformat()))
    started = time.monotonic()

    def budget(stage):
        size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        if (time.monotonic()-started >= cfg.max_seconds or size >= cfg.max_artifact_bytes
                or shutil.disk_usage(root).free < 256 * 1024**2):
            raise ValueError('达到冻结时间或产物预算：' + stage)
        print(stage, flush=True)

    try:
        dataset = Path(protocol['dataset'])
        days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
        if days != sorted(set(days)):
            raise ValueError('交易日历重复或未排序')
        state = pl.scan_parquet(dataset/'state.parquet').filter(
            pl.col('date').is_between(cfg.discovery_start, cfg.evaluation_end)).select(
            'date', 'asset', 'valid_for_factor_rank', 'can_open_long', 'can_close_long').collect()
        if state.select(pl.struct('date', 'asset').is_duplicated().any()).item():
            raise ValueError('状态主键重复')
        for flag in ('valid_for_factor_rank', 'can_open_long', 'can_close_long'):
            if state.schema[flag] != pl.Boolean or state[flag].null_count():
                raise ValueError('状态掩码必须为非空布尔值')
        discovery_dates = [d for d in days if cfg.discovery_start <= d <= cfg.discovery_end]
        budget('读取发现期共同截面，不读取新收益')
        discovery, quality, coverage = common_panel(protocol['candidates'], state, discovery_dates, cfg)
        coverage.write_parquet(root/'discovery_coverage.parquet')
        write_json(root/'preflight.json', dict(candidates=quality, return_labels_read=False,
                    dates=len(discovery_dates), common_rows=discovery.height))
        pairs = correlation_summary(discovery, [c['factor_id'] for c in quality], cfg.min_names, cfg.min_dates)
        pl.DataFrame(pairs).write_parquet(root/'correlations.parquet')
        relations = choose_representatives(quality, pairs, cfg.redundancy_threshold)
        write_json(root/'representatives.json', relations)
        representatives = [r['factor_id'] for r in relations if r['role'] == '研究代表']
        del discovery
        models = protocol['models']
        models['reduced'] = representatives
        if len(models)+1 > cfg.max_models:
            raise ValueError('精简对照超过模型预算')
        write_json(root/'model_freeze.json', dict(models=models, selection_uses_returns=False))
        # 在整个发现和评价范围锚定五交易日步长，最后一笔必须在评价结束前退出。
        all_schedule = report_schedule(days, cfg.discovery_start, cfg.evaluation_end, 'every_5_sessions')
        all_schedule = tuple(w for w in all_schedule if w.exit_date <= cfg.evaluation_end)
        signal_dates = [w.signal_date for w in all_schedule]
        budget('读取五交易日信号网格，冻结共同股票池')
        panel, _, signal_coverage = common_panel(protocol['candidates'], state, signal_dates, cfg)
        signal_coverage.write_parquet(root/'signal_coverage.parquet')
        features = [c['factor_id'] for c in quality]
        signs = {c['factor_id']: 1 if c['original_direction'] == 'positive' else -1 for c in quality}
        panel = panel.with_columns([
            (((pl.col(f).rank(method='average').over('date')-.5)/pl.len().over('date')-.5)*signs[f]).alias(f)
            for f in features])
        panel = load_labels(panel, dataset, days, cfg)
        panel.write_parquet(root/'model_panel.parquet')
        budget('按标签退出事件 purge，拟合有限子集')
        unique_models, aliases, by_features = {}, {}, {}
        for model, members in models.items():
            key = tuple(sorted(members))
            canonical = by_features.setdefault(key, model)
            aliases[model] = canonical
            unique_models[canonical] = list(key)
        fit_audit = fit_rolling_models(panel, unique_models, evaluation_start=cfg.evaluation_start,
            train_signals=cfg.train_signals, retrain_signals=cfg.retrain_signals, alpha=cfg.alpha, min_names=cfg.min_names)
        write_json(root/'fit_audit.json', fit_audit)
        if protocol.get('replay'):
            old_audit = read_json(Path(protocol['replay']['source'])/'fit_audit.json')
            new_audit = json.loads(json.dumps(fit_audit, default=str))
            if len(old_audit) != len(new_audit):
                raise ValueError('重放拟合次数变化')
            for old, new in zip(old_audit, new_audit):
                for field in old:
                    if field in ('weights', 'intercept'):
                        if not np.allclose(old[field], new[field], rtol=1e-12, atol=1e-12):
                            raise ValueError('重放模型权重变化')
                    elif old[field] != new[field]:
                        raise ValueError('重放拟合审计变化')
        evaluation_panel = panel.filter(pl.col('date') >= cfg.evaluation_start).select('date', 'asset', *features)
        canonical_paths = {model: f'{i:03}' for i, model in enumerate([*unique_models, 'equal_rank'])}
        canonical_by_path = {value: key for key, value in canonical_paths.items()}
        mapping = {model: canonical_paths[canonical] for model, canonical in aliases.items()}
        mapping['equal_rank'] = canonical_paths['equal_rank']
        write_json(root/'model_aliases.json', aliases)
        write_json(root/'model_paths.json', mapping)
        del panel
        schedule = tuple(w for w in all_schedule if w.signal_date >= cfg.evaluation_start)
        first, last = schedule[0].signal_date, schedule[-1].exit_date
        market = pl.scan_parquet(dataset/'market.parquet').filter(pl.col('date').is_between(first, last)).select(
            pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'), 'open').collect()
        trade_state = state.filter(pl.col('date').is_between(first, last)).rename({'date': 'trade_date', 'asset': 'security_id'})
        terminals = pl.scan_parquet(dataset/'terminal_values.parquet').filter(
            (pl.col('event_date') <= last) & (pl.col('available_at') <= last)).collect()
        full = coalition_key(protocol['groups'])
        primary = [coalition_key(()), full, 'equal_rank', 'reduced']
        order = list(dict.fromkeys(primary + list(mapping)))
        returns, metrics, dates, executed = {}, {}, None, {}
        for i, model in enumerate(order):
            if mapping[model] in executed:
                previous = executed[mapping[model]]
                metrics[model], returns[model] = metrics[previous], returns[previous]
                continue
            budget(f'万14因果成交 {i+1}/{len(order)}：{model}')
            cached = (protocol.get('replay') or {}).get('models', {}).get(model)
            if cached:
                target = root/'execution'/mapping[model]
                target.parent.mkdir(exist_ok=True)
                target.symlink_to(Path(cached), target_is_directory=True)
                metrics[model] = read_json(target/'14/metrics.json')
                saved = pl.read_parquet(target/'14/daily_returns.parquet')
                current_dates = saved['exit_date'].to_list()
                if dates is not None and current_dates != dates:
                    raise ValueError('复用收益日期不一致')
                dates = current_dates
                returns[model] = saved['target_long_net_return'].to_numpy()
                executed[mapping[model]] = model
                continue
            canonical = canonical_by_path[mapping[model]]
            if canonical == 'equal_rank':
                prediction = evaluation_panel.select('date', 'asset', pl.mean_horizontal(features).alias('score'))
            else:
                prediction = predict_from_audit(evaluation_panel, canonical, unique_models[canonical], fit_audit, cfg.evaluation_start)
            result = evaluate_prediction(prediction, market, trade_state, schedule, terminals, 14, no_signal=model == coalition_key(()))
            metrics[model] = save_execution(root/'execution'/mapping[model]/'14', result)
            executed[mapping[model]] = model
            current_dates = [r['exit_date'] for r in result.daily_returns]
            if dates is not None and current_dates != dates:
                raise ValueError('子集收益日期不一致')
            dates = current_dates
            returns[model] = np.array([r['target_long_net_return'] for r in result.daily_returns], dtype=float)
            if mapping[model] in {mapping[m] for m in primary}:
                for cost in (0, 20, 40):
                    budget(f'固定对照成本压力：{model} / {cost}bps')
                    stress = evaluate_prediction(prediction, market, trade_state, schedule, terminals, cost, no_signal=model == coalition_key(()))
                    save_execution(root/'execution'/mapping[model]/str(cost), stress)
            del result
        budget('计算配对日期分块区间与组 Shapley')
        indices = block_indices(len(dates), cfg.bootstrap_block_days, cfg.bootstrap_repetitions, cfg.seed)
        deletions = []
        for c in quality:
            model = 'drop:' + c['factor_id']
            deletions.append(dict(kind='factor', member=c['factor_id'], group=c['group'],
                                 **paired_delta(returns[full], returns[model], indices)))
        for group in protocol['groups']:
            model = coalition_key([g for g in protocol['groups'] if g != group])
            deletions.append(dict(kind='group', member=group, group=group,
                                 **paired_delta(returns[full], returns[model], indices)))
        years = np.array([d.year for d in dates])
        for row in deletions:
            without = 'drop:'+row['member'] if row['kind'] == 'factor' else coalition_key([g for g in protocol['groups'] if g != row['group']])
            row['yearly_delta_sharpe'] = json.dumps({str(y): sharpe(returns[full][years == y])-sharpe(returns[without][years == y]) for y in sorted(set(years))})
            row['actual_delta_sharpe'] = row['delta_sharpe'] if metrics[full]['actual'] is not None and metrics[without]['actual'] is not None else None
        pl.DataFrame(deletions).write_parquet(root/'deletion_contributions.parquet')
        group_keys = sorted(protocol['groups'])
        values = {m: sharpe(v) for m, v in returns.items() if m.startswith('groups:')}
        attribution = shapley_values(values, group_keys, protocol['permutations'])
        samples = {g: [] for g in group_keys}
        for ix in indices:
            vals = {m: sharpe(returns[m][ix]) for m in values}
            for row in shapley_values(vals, group_keys, protocol['permutations']):
                samples[row['group']].append(row['contribution'])
        for row in attribution:
            x = samples[row['group']]
            row.update(conditional_ci_low=float(np.quantile(x, .025)), conditional_ci_high=float(np.quantile(x, .975)))
        pl.DataFrame(attribution).write_parquet(root/'group_shapley.parquet')
        comparison = paired_delta(returns['reduced'], returns[full], indices)
        summary = dict(version=cfg.version, status='completed_diagnostic', candidates=len(quality),
            groups=len(group_keys), representatives=len(representatives), models=len(set(mapping.values())), logical_comparisons=len(mapping),
            formal_pass=None, historical_family_size=None, sealed_oos=False, test_returns_read=False,
            evaluation_first=str(dates[0]), evaluation_last=str(dates[-1]), reference_metrics=metrics,
            reduction_comparison=comparison, group_shapley=attribution,
            group_details_expanded=False, expansion_reason='首轮预算仅包含全池单因子删除与组层归因；不按已见收益追加子集',
            actual_contribution_available=all(m['actual'] is not None for m in metrics.values()),
            auxiliary_information_ratio_benchmark='zero_return；不是相对市场指数的超额信息比',
            replay=protocol.get('replay'),
            uncertainty_scope=protocol['limits'])
        write_json(root/'summary.json', summary)
        render_report(root, summary, relations, deletions, attribution, mapping, primary)
        verify_files(protocol['input_sha256'])
        write_json(root/'completion.json', dict(summary_sha256=digest(root/'summary.json'),
            protocol_sha256=digest(root/'protocol.json'), elapsed_seconds=time.monotonic()-started,
            artifacts={str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file() and 'code' not in p.relative_to(root).parts}))
    except Exception as error:
        write_json(root/'failure.json', dict(reason=str(error), elapsed_seconds=time.monotonic()-started,
            formal_pass=None, outputs_retained=True))
        raise
    return root


def render_report(root, summary, relations, deletions, attribution, mapping, primary):
    """独立中文报告只展示研究结果，不写生产 Dashboard。"""
    import html
    labels = {primary[0]: '无信号等权股票池', primary[1]: '全部候选 Ridge', 'equal_rank': '原方向等权排名', 'reduced': '相关性代表 Ridge'}
    lines = ['# A 股联合贡献诊断结果', '',
        f"{summary['candidates']} 个候选，{summary['groups']} 个机制组，相关性精简为 {summary['representatives']} 个研究代表。", '',
        '本轮为已消费历史上的事后诊断；不恢复单因子统计资格，不证明密封样本外效果。全部数值为最后报价参考估值情景，实际未知终值单列。', '',
        '| 对照 | 成本bps | 参考年化 | 参考Sharpe | 参考回撤 | 未确定持仓 |', '|---|---:|---:|---:|---:|---:|']
    for model in primary:
        for cost in (0, 14, 20, 40):
            m = read_json(root/'execution'/mapping[model]/str(cost)/'metrics.json')
            r = m['reference']
            lines.append(f"|{labels[model]}|{cost}|{r['annualized_return']:.2%}|{r['sharpe']:.3f}|{r['max_drawdown']:.2%}|{m['unresolved_positions']}|")
    d = summary['reduction_comparison']
    lines += ['', f"精简减全部的参考 ΔSharpe = {d['delta_sharpe']:.3f}，固定预测日期分块95%区间 [{d['conditional_ci_low']:.3f}, {d['conditional_ci_high']:.3f}]。", '',
        '该区间未计入历史候选筛选与模型估计误差，不用于正式显著性判断。排列标准误另列；它只描述 Shapley 随机近似误差。', '',
        '## 机制组贡献', '', '| 机制组 | 重训子集Shapley | 排列标准误 | 日期分块95%区间 |', '|---|---:|---:|---|']
    for r in sorted(attribution, key=lambda r: -r['contribution']):
        lines.append(f"|{r['group']}|{r['contribution']:.3f}|{r['permutation_se']:.3f}|[{r['conditional_ci_low']:.3f}, {r['conditional_ci_high']:.3f}]|")
    lines += ['', '## 单因子删除与替代关系', '', '正 ΔSharpe 表示从全部组合删掉它后参考表现下降。不能把多个接近零的因子同时删除；同类替补不会改变原资格。', '',
              '| 因子 | 角色 | 代表 | 删除损失 | 日期分块95%区间 |', '|---|---|---|---:|---|']
    by_id = {d['member']: d for d in deletions if d['kind'] == 'factor'}
    for r in relations:
        d = by_id[r['factor_id']]
        lines.append(f"|{r['factor_id']}|{r['role']}|{r['representative']}|{d['delta_sharpe']:.3f}|[{d['conditional_ci_low']:.3f}, {d['conditional_ci_high']:.3f}]|")
    lines += ['', '## 方法边界', '',
        '普通收益预测 Ridge 不等于 Ridge-SDF。本版归因对每个子集重新训练，称重训子集 Shapley，不声称完整复现 SPPC。空集使用相同共同股票池等权多头，贡献加总为全部组合减该基准的 Sharpe 差。', '',
        '精简名单在读取本轮收益前由发现期相关性及覆盖决定；所有比较使用相同完整信号截面。弱单因子 IC 未被用作新的准入闸门。', '',
        '不自动删因子、不修改原报告、不写生产数据库、不自动合并 main。']
    (root/'研究结果.md').write_text('\n'.join(lines)+'\n')
    import plotly.graph_objects as go
    fig = go.Figure()
    for model in primary:
        rows = pl.read_parquet(root/'execution'/mapping[model]/'14/daily_returns.parquet')
        fig.add_scatter(x=rows['exit_date'].to_list(), y=np.cumprod(1+rows['target_long_net_return'].to_numpy()), name=labels[model])
    fig.update_layout(title='双边万14 · 最后报价参考净值', template='plotly_white', height=480)
    table = '<br>'.join(html.escape(line) for line in lines)
    (root/'研究结果.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>A股联合贡献诊断</title><style>body{max-width:1250px;margin:40px auto;font:16px/1.7 system-ui;padding:20px}pre{white-space:pre-wrap}</style><h1>A股联合贡献诊断</h1>'+fig.to_html(full_html=False, include_plotlyjs=True)+'<pre>'+table+'</pre></html>')
