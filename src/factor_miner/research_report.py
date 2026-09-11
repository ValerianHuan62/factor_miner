"""从冻结候选完成构念审计、发现确认、因果持仓和 Dashboard 报告。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
import math
import hashlib
from pathlib import Path
from statistics import mean, stdev
from itertools import combinations

import polars as pl

from factor_miner.compiler import attach_market_sessions, build_polars_expr, compile_candidate
from factor_miner.market_context import attach_market_context
from factor_miner.dispersion_context import attach_dispersion_context
from factor_miner.vix_context import attach_vix_context, attach_vvix_context, attach_ovx_context
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.ic_diagnostics import _hac_t
from factor_miner.schema import TrustedCandidateFactorSpec, registered_trusted_candidate, CampaignSpec, campaign_id
from factor_miner.ledger import JsonlLedger, TrialEvent, EventType
from factor_miner.trading_schedule import RebalanceWindow
from factor_miner.report_data_quality import audit_price_continuity
from factor_miner.simple_strategy import select_strategy_factors, composite_rank_panel


def write_json(path: Path, payload: object) -> None:
    """不可覆盖地保存本次运行记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)


def performance(rows: list[dict[str, object]]) -> dict[str, float]:
    """计算已明确估值情景的净值指标，不将其冒充未确定的真实回收。"""
    values = [float(x["target_long_net_return"]) for x in rows]
    excess = [v - float(x["benchmark_return"]) for v, x in zip(values, rows)]
    elapsed = (date.fromisoformat(str(rows[-1]["exit_date"])) - date.fromisoformat(str(rows[0]["entry_date"]))).days
    scale = math.sqrt(len(values) * 365.25 / elapsed)
    wealth = peak = 1.0
    drawdown = 0.0
    for value in values:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = max(drawdown, 1 - wealth / peak)
    return {"period_return": wealth - 1, "annualized_return": wealth ** (365.25 / elapsed) - 1,
        "max_drawdown": drawdown, "sharpe": mean(values) / stdev(values) * scale if stdev(values) else 0.0,
        "information_ratio": mean(excess) / stdev(excess) * scale if stdev(excess) else 0.0}


def ic_diagnostics(factor: pl.LazyFrame, state: pl.LazyFrame, labels: pl.LazyFrame, start: date, end: date, boundary: date | None, family_size: int | None = 30, *, label_column: str = "label_o2o_5d", hac_max_lags: int = 5) -> tuple[pl.DataFrame, dict[str, object]]:
    """计算条件标签上的 IC，使用真实标签退出事件 purge。"""
    if not isinstance(hac_max_lags,int) or hac_max_lags<0:
        raise ValueError("HAC滞后必须是非负整数")
    if family_size is not None and (type(family_size) is not int or family_size < 1):
        raise ValueError("多重检验族必须保留全部登记名额")
    joined = factor.join(state.select("date", "asset", "valid_for_factor_rank"), on=["date", "asset"]).join(labels, on=["date", "asset"]).filter(pl.col("date").is_between(start, end))
    if boundary:
        joined = joined.filter(pl.col("label_exit_date") < boundary)
    eligible = pl.col("valid_for_factor_compute") & pl.col("valid_for_factor_rank") & pl.col("raw_factor").is_finite() & pl.col(label_column).is_finite()
    counts = joined.group_by("date").agg(pl.col("valid_for_factor_rank").sum().alias("universe"), eligible.sum().alias("usable"))
    daily = joined.filter(eligible).group_by("date").agg(pl.len().alias("names"), pl.corr("raw_factor", label_column).alias("ic"), pl.corr("raw_factor", label_column, method="spearman").alias("rank_ic")).filter((pl.col("names") >= 20) & pl.col("ic").is_finite() & pl.col("rank_ic").is_finite()).sort("date").collect()
    if daily.height < 60:
        raise ValueError("有效截面日期不足 60")
    ic, rank = daily["ic"].to_list(), daily["rank_ic"].to_list()
    rank_t = _hac_t(rank, hac_max_lags)
    coverage = counts.filter(pl.col("universe") > 0).select((pl.col("usable") / pl.col("universe")).median()).collect().item()
    return daily, {"ic_mean": mean(ic), "rank_ic_mean": mean(rank), "ic_std": stdev(ic), "rank_ic_std": stdev(rank),
        "ic_ir": mean(ic) / stdev(ic) if stdev(ic) else 0.0, "rank_ic_ir": mean(rank) / stdev(rank) if stdev(rank) else 0.0,
        "ic_hac_t": _hac_t(ic, hac_max_lags), "rank_ic_hac_t": rank_t, "valid_dates": len(ic), "median_coverage": coverage,
        "raw_p_value": math.erfc(abs(rank_t) / math.sqrt(2)), "bonferroni_p_value": None if family_size is None else min(1, family_size * math.erfc(abs(rank_t) / math.sqrt(2))), "hac_max_lags": hac_max_lags}


def report_schedule(days: list[date], start: date, end: date, frequency: str, *, holding_sessions: int = 5) -> tuple[RebalanceWindow, ...]:
    """按完整市场日历取日、周末或月末信号；固定持有长度独立于采样频率。"""
    if type(holding_sessions) is not int or holding_sessions < 1:
        raise ValueError("持有期必须为正整数市场交易日")
    if not days or days != sorted(set(days)):
        raise ValueError("交易日历必须非空、唯一且有序")
    if frequency == "weekly_last_session":
        last = {}
        for day in days:
            last[day.isocalendar()[:2]] = day
        signals = [day for day in last.values() if start <= day <= end]
    elif frequency == "every_5_sessions":
        signals = [day for day in days if start <= day <= end][::5]
    elif frequency == "daily":
        signals = [day for day in days if start <= day <= end]
    elif frequency == "monthly_last_session":
        last = {(day.year, day.month): day for day in days}
        signals = [day for day in last.values() if start <= day <= end]
    else:
        raise ValueError("未知调仓频率")
    positions = {day: i for i, day in enumerate(days)}
    offset = holding_sessions + 1
    if not signals or any(positions[day] + offset >= len(days) for day in signals):
        raise ValueError("交易日历不足以覆盖冻结持有期")
    return tuple(RebalanceWindow(signal_date=day, entry_date=days[positions[day]+1], exit_date=days[positions[day]+offset]) for day in signals)


def report_direction(policy: str, hypothesis_direction: str, rank_ic: float) -> str:
    """既有假设复核按原方向执行，不把发现期反号悄悄变成新策略。"""
    if hypothesis_direction not in {"positive", "negative"}:
        raise ValueError("假设方向非法")
    if policy == "original_hypothesis":
        return hypothesis_direction
    if policy == "discovery":
        return "positive" if rank_ic >= 0 else "negative"
    raise ValueError("未知方向冻结口径")


def run_report(config_path: Path) -> Path:
    """正式入口失败时保留本次登记和中断事件，禁止丢弃已花费名额。"""
    config = json.loads(config_path.read_text())
    root = Path(config["output_root"])
    if root.exists():
        raise ValueError("输出目录已存在，不能重复启动或改写原运行")
    # 原子争用输出根；未取得所有权的进程不能给另一运行追加中断事件。
    root.mkdir(parents=True, exist_ok=False)
    try:
        return _run_report(config_path)
    except Exception:
        if (root / "protocol.json").exists():
            JsonlLedger(root / "ledger").append_event(TrialEvent(event_type=EventType.INTERRUPTED,
                run_id=config["run_id"], status="研究失败或中断，冻结名额和全部已有产物保留"))
        raise


def _run_report(config_path: Path) -> Path:
    """只读版本化数据，通过统一 CLI 完成用户批准的报告模式。"""
    config = json.loads(config_path.read_text())
    family_size = config.get("family_size", len(config["candidates"]))
    if not isinstance(family_size, int) or family_size < len(config["candidates"]):
        raise ValueError("试验预算不足，不能排除失败或重复候选")
    correlation_threshold = float(config.get("max_abs_output_correlation", .8))
    if not 0 < correlation_threshold <= 1:
        raise ValueError("冗余阈值必须在 (0,1] 内")
    root = Path(config["output_root"])
    write_json(root / "protocol.json", config)
    from factor_miner.artifact_storage import snapshot_code
    code_hashes = snapshot_code(root, directory='code_snapshot', write_identity=False)
    write_json(root / "code_identity.json", {"files": code_hashes, "polars_version": pl.__version__})
    ledger = JsonlLedger(root / "ledger")
    candidates = []
    for item in config["candidates"]:
        spec = TrustedCandidateFactorSpec.model_validate_json(Path(item["spec_path"]).read_text())
        candidate = registered_trusted_candidate(spec)
        if candidate.candidate_id != item["candidate_id"]:
            raise ValueError("冻结候选身份不一致")
        ledger.register_candidate(candidate)
        candidates.append((item, spec))
    campaign = CampaignSpec(visible_start=config["discovery_start"], visible_end=config["discovery_end"],
        next_split_start=config["confirmation_start"], candidate_ids=tuple(item["candidate_id"] for item, _ in candidates),
        max_hypotheses=family_size, alpha=.05, label_column="label_o2o_5d", rank_mask_column="valid_for_factor_rank",
        hac_max_lags=5, min_valid_dates=60, min_names_per_date=20, min_median_coverage=.8,
        max_abs_output_correlation=correlation_threshold)
    ledger.register_campaign(campaign)
    ledger.append_event(TrialEvent(event_type=EventType.RUN_STARTED, campaign_id=campaign_id(campaign), run_id=config["run_id"],
        config_hash=hashlib.sha256(config_path.read_bytes()).hexdigest(), status="全部候选与预算已登记，尚未读取标签"))
    dataset = Path(config["dataset_root"])
    manifest = json.loads((dataset / "manifest.json").read_text())
    if manifest.get("canonical_release_id") != config["canonical_release_id"]:
        raise ValueError("发布版本不一致")
    for name, expected_hash in config.get("input_sha256", {}).items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"冻结输入内容发生变化：{Path(name).name}")
    market, state = (pl.scan_parquet(dataset / f"{x}.parquet") for x in ("market", "state"))
    allowed_fields = {"open", "high", "low", "close", "volume"}
    if config.get("market_context_path") or config.get("market_context_contract_path"):
        context_path = Path(config["market_context_path"])
        contract_path = Path(config["market_context_contract_path"])
        if any(str(p) not in config.get("input_sha256", {}) for p in [context_path,contract_path]):
            raise ValueError("市场上下文必须在读取结果前绑定冻结输入")
        market = attach_market_context(market,dataset/'calendar.parquet',context_path,contract_path)
        allowed_fields.add("market_return")
    if config.get("vix_context_path") or config.get("vix_context_contract_path"):
        context_path = Path(config["vix_context_path"])
        contract_path = Path(config["vix_context_contract_path"])
        source_path = Path(json.loads(contract_path.read_text())["source_snapshot_path"])
        if any(str(p) not in config.get("input_sha256", {}) for p in [context_path,contract_path,source_path]):
            raise ValueError("VIX上下文与公开来源必须在结果前绑定冻结输入")
        market = attach_vix_context(market,dataset/'calendar.parquet',context_path,contract_path)
        allowed_fields.add("vix_change")
    if config.get("vvix_context_path") or config.get("vvix_context_contract_path"):
        context_path = Path(config["vvix_context_path"])
        contract_path = Path(config["vvix_context_contract_path"])
        source_path = Path(json.loads(contract_path.read_text())["source_snapshot_path"])
        if any(str(p) not in config.get("input_sha256", {}) for p in [context_path,contract_path,source_path]):
            raise ValueError("VVIX上下文与公开来源必须在结果前绑定冻结输入")
        market = attach_vvix_context(market,dataset/'calendar.parquet',context_path,contract_path)
        allowed_fields.add("vvix_change")
    if config.get("ovx_context_path") or config.get("ovx_context_contract_path"):
        context_path = Path(config["ovx_context_path"])
        contract_path = Path(config["ovx_context_contract_path"])
        source_path = Path(json.loads(contract_path.read_text())["source_snapshot_path"])
        if any(str(p) not in config.get("input_sha256", {}) for p in [context_path,contract_path,source_path]):
            raise ValueError("OVX上下文与公开来源必须在结果前绑定冻结输入")
        market = attach_ovx_context(market,dataset/'calendar.parquet',context_path,contract_path)
        allowed_fields.add("ovx_change")
    if config.get("dispersion_context_path") or config.get("dispersion_context_contract_path"):
        context_path = Path(config["dispersion_context_path"])
        contract_path = Path(config["dispersion_context_contract_path"])
        contract = json.loads(contract_path.read_text())
        bound = [context_path, contract_path, *(Path(p) for p in contract["source_sha256"])]
        if any(str(p) not in config.get("input_sha256", {}) for p in bound):
            raise ValueError("分散程度上下文及全部来源必须在结果前冻结")
        if contract["market_path"] != str(dataset/'market.parquet') or contract["state_path"] != str(dataset/'state.parquet') or contract["market_context_path"] != config.get("market_context_path"):
            raise ValueError("分散程度必须使用本批冻结的股票与市场输入")
        market = attach_dispersion_context(market, dataset/'calendar.parquet', context_path, contract_path)
        allowed_fields.add("dispersion_change")
    quality = audit_price_continuity(market, state, manifest.get("reviewed_data_policy", {}).get("accepted_jumps", []))
    write_json(root / "data_quality_audit.json", quality)
    if quality["status"] == "review_required":
        raise ValueError("仍有可用样本包含未裁决价格断点，停止本次研究")
    days = pl.read_parquet(dataset / "calendar.parquet")["date"].to_list()
    calendar_market = None
    discovery_start, discovery_end, confirmation_start, confirmation_end = (date.fromisoformat(config[k]) for k in ("discovery_start", "discovery_end", "confirmation_start", "confirmation_end"))
    weekly_dates = None
    if config.get("ic_frequency") == "weekly_last_session":
        weekly_dates = [w.signal_date for w in report_schedule(days, discovery_start, confirmation_end, "weekly_last_session")]
    def evaluation_panel(path: Path) -> pl.LazyFrame:
        """原始层保持日频；统计层严格按冻结信号日取样。"""
        panel = pl.scan_parquet(path)
        return panel.filter(pl.col("date").is_in(weekly_dates)) if weekly_dates is not None else panel
    reports = []
    # 先对全部原始公式执行构念审计，再打开收益标签。
    for number, (item, spec) in enumerate(candidates, 1):
        print(f"原始计算与构念检验 {number}/{len(candidates)}：{item['design']}", flush=True)
        plan = compile_candidate(registered_trusted_candidate(spec), allowed_fields)
        factor_market = market
        if {"calendar_delay", "calendar_delta"}.intersection(plan.expression_metadata["operator_signature"]):
            if calendar_market is None:
                calendar_market = attach_market_sessions(market, pl.read_parquet(dataset / "calendar.parquet"))
            factor_market = calendar_market
        condition = ObservableCondition.model_validate(item["observable_condition"])
        audit = validate_construct(plan.expression, condition)
        folder = root / "candidates" / item["candidate_id"]
        write_json(folder / "construct_validation.json", audit)
        write_json(folder / "execution_plan.json", plan.model_dump())
        raw = factor_market.sort("date", "asset").with_columns(build_polars_expr(spec.expression).alias("raw_factor"), (pl.col("date").cum_count().over("asset") > 119).alias("warmup")).join(state.select("date", "asset", "valid_for_factor_compute"), on=["date", "asset"]).select("date", "asset", "raw_factor", (pl.col("warmup") & pl.col("valid_for_factor_compute")).alias("valid_for_factor_compute"))
        raw.sink_parquet(folder / "raw_factor.parquet")
        # 只用发现期当期市场状态形成分组统计，不读取收益标签或确认期。
        profiles = market.filter(pl.col("date") <= discovery_end).sort("date", "asset").with_columns(
            (pl.col("close") / pl.col("open") - 1).alias("intraday_return"),
            ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("relative_range"),
            ((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low"))).alias("close_position"),
            (pl.col("volume") / pl.col("volume").rolling_mean(20).over("asset")).alias("relative_volume"),
        ).join(pl.scan_parquet(folder / "raw_factor.parquet").filter(pl.col("valid_for_factor_compute") & pl.col("raw_factor").is_finite()).select("date", "asset", "raw_factor"), on=["date", "asset"]).join(state.select("date", "asset", "valid_for_factor_rank"), on=["date", "asset"]).filter((pl.col("date") >= discovery_start) & pl.col("valid_for_factor_rank")).with_columns(
            ((pl.col("raw_factor").rank(method="ordinal").over("date") - 1) * 5 / pl.len().over("date")).floor().cast(pl.Int64).alias("factor_bin")
        )
        fields = ("intraday_return", "relative_range", "close_position", "relative_volume")
        profile = profiles.group_by("factor_bin").agg(pl.len().alias("sample_count"), *(pl.col(field).filter(pl.col(field).is_finite()).median().alias(field + "_median") for field in fields)).sort("factor_bin").collect()
        audit["training_condition_profile"] = profile.to_dicts()
        write_json(folder / "training_condition_profile.json", audit["training_condition_profile"])
        reports.append({**item, "construct_validation": audit, "folder": str(folder), "hypothesis": spec.hypothesis.model_dump(mode="json"), "expression": spec.expression.model_dump(mode="json")})
    labels = pl.scan_parquet(dataset / "label.parquet")
    for number, report in enumerate(reports, 1):
        print(f"发现期 {number}/{len(reports)}", flush=True)
        folder = Path(report["folder"])
        daily, metrics = ic_diagnostics(evaluation_panel(folder / "raw_factor.parquet"), state, labels, discovery_start, discovery_end, confirmation_start, family_size)
        daily.write_parquet(folder / "discovery_ic.parquet")
        report["discovery"] = metrics
        report["discovered_direction"] = "positive" if metrics["rank_ic_mean"] >= 0 else "negative"
        report["direction"] = report_direction(config.get("direction_policy", "discovery"), report["hypothesis_direction"], metrics["rank_ic_mean"])
    write_json(root / "direction_freeze.json", [{"candidate_id": x["candidate_id"], "direction": x["direction"], "discovery": x["discovery"]} for x in reports])
    print("完整发现期输出冗余检查", flush=True)
    wide = state.filter(pl.col("date").is_between(discovery_start, discovery_end) & pl.col("valid_for_factor_rank")).select("date", "asset")
    for number, report in enumerate(reports):
        raw = pl.scan_parquet(Path(report["folder"]) / "raw_factor.parquet").filter(pl.col("valid_for_factor_compute")).select("date", "asset", pl.when(pl.col("raw_factor").is_finite()).then(pl.col("raw_factor")).alias(f"f{number}"))
        wide = wide.join(raw, on=["date", "asset"], how="left")
    pairs = list(combinations(range(len(reports)), 2))
    correlations = wide.group_by("date").agg(*(pl.corr(f"f{i}", f"f{j}", method="spearman").abs().alias(f"{i}_{j}") for i, j in pairs)).collect()
    edges = []
    redundant = set()
    for i, j in pairs:
        series = correlations[f"{i}_{j}"].drop_nulls().filter(correlations[f"{i}_{j}"].drop_nulls().is_finite())
        p95 = series.quantile(.95) if len(series) else None
        if p95 is not None and p95 >= correlation_threshold:
            edges.append({"first_slot": i + 1, "second_slot": j + 1, "p95_abs_spearman": p95})
            if i not in redundant:
                redundant.add(j)
    write_json(root / "redundancy.json", {"threshold": correlation_threshold, "pair_count": len(pairs), "edges": edges, "representative_rule": "按原始槽位顺序保留，既有因子均继续展示"})
    if config.get("legacy_report_root"):
        from factor_miner.report_redundancy import check_legacy
        legacy_rejected = check_legacy(reports, Path(config["legacy_report_root"]), state,
            discovery_start, discovery_end, float(config["legacy_max_abs_output_correlation"]), root)
        redundant.update(legacy_rejected)
    strategy_factors = select_strategy_factors(reports, redundant) if config.get("simple_strategy") else []
    if config.get("simple_strategy"):
        write_json(root / "strategy_freeze.json", {"protocol": config["simple_strategy"],
            "selected_factors": [{"factor_id": item["factor_id"], "direction": item["direction"], "discovery": item["discovery"]} for item in strategy_factors],
            "confirmation_outcomes_used_for_selection": False,
            "status": "frozen" if strategy_factors else "no_eligible_factor_no_forced_strategy"})
    del wide, correlations
    schedule = report_schedule(days, confirmation_start, confirmation_end, config.get("rebalance_frequency", "every_5_sessions"))
    signal_days = [window.signal_date for window in schedule]
    trade_market = market.filter(pl.col("date") >= signal_days[0]).select(pl.col("date").alias("trade_date"), pl.col("asset").alias("security_id"), "open").collect()
    trade_state = state.filter(pl.col("date") >= signal_days[0]).select(pl.col("date").alias("trade_date"), pl.col("asset").alias("security_id"), "valid_for_factor_rank", "can_open_long", "can_close_long").collect()
    terminals = pl.read_parquet(config["terminal_events_path"])
    benchmark_signal = state.filter(pl.col("date").is_in(signal_days) & pl.col("valid_for_factor_compute")).select(pl.col("date").alias("signal_date"), pl.col("asset").alias("security_id"), pl.lit(1.0).alias("factor_value")).collect()
    print("计算合格股票池等权基准及其未确定持仓", flush=True)
    benchmark = simulate_causal_extreme_portfolio(benchmark_signal, trade_market, trade_state, schedule, group_count=1, terminal_policy="report_unresolved", terminal_events=terminals, retain_daily_holdings=False, allow_noncontiguous_schedule=config.get("rebalance_frequency") == "weekly_last_session")
    benchmark_map = {x["exit_date"]: x for x in benchmark.daily_returns}
    benchmark_stress_map = {x["exit_date"]: x for x in benchmark.zero_recovery_daily_returns}
    write_json(root / "benchmark.json", {"summary": benchmark.execution_summary, "unresolved_positions": benchmark.unresolved_positions})
    def attach(rows: tuple[dict[str, object], ...], benchmark_rows: dict[date, dict[str, object]]) -> list[dict[str, object]]:
        """将已退出转现金的基准合法延长到共同报告末日。"""
        output = []
        for row in rows:
            match = benchmark_rows.get(row["exit_date"])
            if match is None and row["exit_date"] <= max(benchmark_rows):
                raise ValueError("基准缺少中间交易日")
            output.append({**row, "entry_date": str(row["entry_date"]), "exit_date": str(row["exit_date"]), "benchmark_return": float(match["target_long_net_return"]) if match else 0.0})
        return output
    for number, report in enumerate(reports):
        print(f"确认与持仓回测 {number+1}/{len(reports)}：{report['factor_id']}", flush=True)
        folder = Path(report["folder"])
        raw = pl.scan_parquet(folder / "raw_factor.parquet")
        daily, metrics = ic_diagnostics(evaluation_panel(folder / "raw_factor.parquet"), state, labels, confirmation_start, confirmation_end, None, family_size)
        daily.write_parquet(folder / "confirmation_ic.parquet")
        signal = raw.filter(pl.col("date").is_in(signal_days) & pl.col("valid_for_factor_compute")).select(pl.col("date").alias("signal_date"), pl.col("asset").alias("security_id"), pl.col("raw_factor").alias("factor_value")).collect()
        result = simulate_causal_extreme_portfolio(signal, trade_market, trade_state, schedule, direction=report["direction"], group_count=10, round_trip_cost_bps=10, terminal_policy="report_unresolved", terminal_events=terminals, retain_daily_holdings=False, allow_noncontiguous_schedule=config.get("rebalance_frequency") == "weekly_last_session")
        opposite = simulate_causal_extreme_portfolio(signal, trade_market, trade_state, schedule, direction="negative" if report["direction"] == "positive" else "positive", group_count=10, round_trip_cost_bps=10, terminal_policy="report_unresolved", terminal_events=terminals, retain_daily_holdings=False, allow_noncontiguous_schedule=config.get("rebalance_frequency") == "weekly_last_session")
        normal, stress = attach(result.daily_returns, benchmark_map), attach(result.zero_recovery_daily_returns, benchmark_stress_map)
        other = {x["exit_date"]: x for x in opposite.daily_returns}
        spread = [float(x["target_long_net_return"]) - float(other[x["exit_date"]]["target_long_net_return"]) for x in result.daily_returns if x["exit_date"] in other]
        report["evaluation"] = metrics
        report["inference"] = {"family_size": family_size, "redundant": number in redundant,
            "statistical_direction_confirmed": report["discovery"]["bonferroni_p_value"] < .05 and metrics["bonferroni_p_value"] < .05 and metrics["rank_ic_mean"] * (1 if report["direction"] == "positive" else -1) > 0,
            "reference_mark_spread_win_rate": sum(x > 0 for x in spread) / len(spread), "actual_spread_win_rate": None if result.unresolved_positions or opposite.unresolved_positions else sum(x > 0 for x in spread) / len(spread)}
        pl.DataFrame(result.orders).write_parquet(folder / "orders.parquet")
        payload = {"schema_version": "dashboard-real-backtest-v3-accounting", "mode": "Consumed confirmation diagnostics", "market_id": "us_equity",
            "factor_id": report["factor_id"], "source_candidate_id": report["candidate_id"], "factor_name": report["design"], "scope": "已使用确认期重算；未知持仓不伪造回收价值",
            "evaluation": metrics, "inference": report["inference"], "direction": report["direction"], "construct_validation": report["construct_validation"],
            "window": {"start": str(confirmation_start), "end": str(confirmation_end)}, "data_identity": {"canonical_release_id": config["canonical_release_id"], "dataset_root": str(dataset)},
            "protocol": {"base_cost_bps": 10, "holding_period": 5, "terminal_policy": "report_unresolved", "direction_policy": config.get("direction_policy", "discovery"), "rebalance_frequency": config.get("rebalance_frequency", "every_5_sessions"), "ic_frequency": config.get("ic_frequency", "daily"), "execution": "T日冻结，T+1买入；买不到留现金，卖不掉逐日重试", "price_basis": "split_adjusted_open", "dividend_treatment": "价格收益，不含现金分红；已有终止收益按合同结算", "benchmark": "canonical计算合格池等权，同样处理未确定持仓", "valuation": "参考曲线沿用最后可用开盘价；不代表真实可回收价值，压力情景在末日零回收"},
            "daily_rows": normal, "zero_recovery_daily_rows": stress, "reference_metrics": performance(normal), "zero_recovery_metrics": performance(stress),
            "execution_audit": {"target": result.execution_summary, "benchmark": benchmark.execution_summary, "orders_path": str(folder / "orders.parquet")},
            "unresolved_positions": result.unresolved_positions, "terminal_settlements": [x for x in result.orders if x["side"] == "terminal"],
            "benchmark_unresolved_positions": benchmark.unresolved_positions,
            "generated_at": datetime.now(timezone.utc).isoformat()}
        write_json(root / "dashboard_backtests" / f"{report['factor_id']}.json", payload)
        write_json(folder / "report.json", report)
        ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_COMPLETED, candidate_id=report["candidate_id"],
            campaign_id=campaign_id(campaign), run_id=config["run_id"], outcome_exposed=True,
            status="诊断已完成；是否入选须检查方向、构念及全部筛选闸门", artifact_refs=(str(folder / "report.json"),)))
    if strategy_factors:
        print("简单策略：冻结发现期候选，等权排名组合与多头回测", flush=True)
        strategy_folder = root / "simple_strategy"
        strategy_folder.mkdir()
        composite = composite_rank_panel(strategy_factors, state)
        composite.sink_parquet(strategy_folder / "composite_scores.parquet")
        composite = pl.scan_parquet(strategy_folder / "composite_scores.parquet")
        strategy_ic, strategy_metrics = ic_diagnostics(evaluation_panel(strategy_folder / "composite_scores.parquet"), state, labels, confirmation_start, confirmation_end, None, family_size)
        strategy_ic.write_parquet(strategy_folder / "confirmation_ic.parquet")
        signal = composite.filter(pl.col("date").is_in(signal_days)).select(pl.col("date").alias("signal_date"), pl.col("asset").alias("security_id"), pl.col("raw_factor").alias("factor_value")).collect()
        strategy_result = simulate_causal_extreme_portfolio(signal, trade_market, trade_state, schedule,
            direction="positive", group_count=10, round_trip_cost_bps=10, terminal_policy="report_unresolved", terminal_events=terminals, retain_daily_holdings=False, allow_noncontiguous_schedule=config.get("rebalance_frequency") == "weekly_last_session")
        normal, stress = attach(strategy_result.daily_returns, benchmark_map), attach(strategy_result.zero_recovery_daily_returns, benchmark_stress_map)
        pl.DataFrame(strategy_result.orders).write_parquet(strategy_folder / "orders.parquet")
        pl.DataFrame(strategy_result.selections).write_parquet(strategy_folder / "selections.parquet")
        strategy_id = config.get("strategy_id", "strategy_equal_rank_q10")
        strategy_payload = {**payload, "factor_id": strategy_id, "source_candidate_id": None,
            "factor_name": "简单策略：冻结因子等权打分，前10%等权多头", "direction": "positive",
            "scope": "策略工程验证；选因子仅使用发现期，确认历史已经消费",
            "evaluation": strategy_metrics, "inference": {"statistical_claim": "单个预先冻结组合的工程验证，不作为新增显著性通过候选", "selected_factors": [item["factor_id"] for item in strategy_factors]},
            "construct_validation": {"status": "组合的构成因子基础检验符合", "condition": {"observation": "对冻结因子的有向截面排名等权平均；不是新的经济机制证明"}, "checks": [], "training_condition_profile": []},
            "daily_rows": normal, "zero_recovery_daily_rows": stress, "reference_metrics": performance(normal), "zero_recovery_metrics": performance(stress),
            "execution_audit": {"target": strategy_result.execution_summary, "benchmark": benchmark.execution_summary, "orders_path": str(strategy_folder / "orders.parquet")},
            "unresolved_positions": strategy_result.unresolved_positions, "terminal_settlements": [item for item in strategy_result.orders if item["side"] == "terminal"],
            "strategy_contract": config["simple_strategy"]}
        write_json(root / "dashboard_backtests" / f"{strategy_id}.json", strategy_payload)
        write_json(strategy_folder / "report.json", strategy_payload)
    write_json(root / "run_manifest.json", {"status": "completed_with_explicit_valuation_uncertainty", "run_id": config["run_id"], "candidate_count": len(reports), "reports": reports, "sealed_oos": False, "protocol": str(root / "protocol.json")})
    ledger.append_event(TrialEvent(event_type=EventType.RUN_COMPLETED, campaign_id=campaign_id(campaign), run_id=config["run_id"], status="报告完成，保留全部名额"))
    return root
