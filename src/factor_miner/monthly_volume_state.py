"""自然月成交原始观察及后续截面状态；只做无收益的 FaVOR 前置预检。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
import json

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.favor_validation import require_panel
from factor_miner.schema import HypothesisSpec


RAW_FIELDS = ("capitalization_volume_raw", "capitalization_price_raw", "close")


class MonthlyVolumeGamma(BaseModel):
    """固定观察拓扑；不接受任意公式、排名算子或窗口搜索。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["monthly-volume-state-gamma-v1"] = "monthly-volume-state-gamma-v1"
    volume_basis: Literal["raw_volume_times_raw_price_over_split_adjusted_close"] = "raw_volume_times_raw_price_over_split_adjusted_close"
    baseline_months: Literal[12] = 12
    baseline_last_lag: Literal[7] = 7
    min_active_sessions: Literal[10] = 10
    min_raw_price: Literal[1.0] = 1.0
    month_completion: Literal["all_calendar_sessions_valid"] = "all_calendar_sessions_valid"
    baseline_weighting: Literal["equal_month"] = "equal_month"
    rank_policy: Literal["average_rank_minus_half_over_n"] = "average_rank_minus_half_over_n"
    cap_months: Literal[5] = 5
    missing_policy: Literal["unknown_until_reset_or_saturation"] = "unknown_until_reset_or_saturation"
    availability: Literal["after_month_last_session_close"] = "after_month_last_session_close"

    @property
    def identity(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class MonthlyVolumePreflightPlan(BaseModel):
    """仅预检观察能力；不是可提交因子或读取收益的研究许可证。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["favor-monthly-state-preflight-v1"]
    hypothesis: HypothesisSpec
    gamma: MonthlyVolumeGamma
    dataset_root: str
    data_contract_path: str
    source_protocol_path: str
    input_sha256: dict[str, str]
    discovery_start: date
    discovery_end: date
    selectivity_levels: tuple[Literal[.5, .7, .9], ...] = (.5, .7, .9)
    min_events_per_ticker: Literal[5] = 5
    min_tickers: Literal[20] = 20
    support_threshold: Literal[.5] = .5
    min_signal_coverage: Literal[.8] = .8
    historical_attempt_checkpoint: int = Field(ge=0)
    diagnostic_family_floor: int = Field(ge=1)
    output_token_limit: None = None
    preflight_target: Literal["high_atv_and_first_extreme_month"] = "high_atv_and_first_extreme_month"

    @model_validator(mode="after")
    def ordered(self):
        if self.discovery_start >= self.discovery_end or self.selectivity_levels != (.5, .7, .9):
            raise ValueError("月度状态预检日期或三档水平不合法")
        return self


def _month_id() -> pl.Expr:
    return pl.col("date").dt.year() * 12 + pl.col("date").dt.month() - 1


def raw_monthly_volume(market: pl.DataFrame, state: pl.DataFrame,
                       calendar: pl.DataFrame, gamma: MonthlyVolumeGamma,
                       *, through: date) -> pl.DataFrame:
    """先按完整自然月聚合；输出未排名的 ATV 和月末事前股票池资格。"""
    require_panel(market, {"date", "asset", *RAW_FIELDS}, "月度成交行情")
    require_panel(state, {"date", "asset", "valid_for_factor_compute", "valid_for_factor_rank"}, "月度成交状态")
    for key in ("valid_for_factor_compute", "valid_for_factor_rank"):
        if state.schema[key] != pl.Boolean or state[key].null_count():
            raise ValueError("月度成交 mask 必须为非空布尔值")
    days = calendar["date"].to_list()
    if not days or calendar.schema["date"] != pl.Date or days != sorted(set(days)) or None in days:
        raise ValueError("月度成交日历为空、乱序或重复")
    # 使用完整的已冻结交易日历判定月末，绝不把截断日伪装成月末。
    months = calendar.with_columns(_month_id().alias("month_id")).group_by("month_id").agg(
        pl.col("date").max().alias("date"), pl.len().alias("calendar_sessions"))
    # 日历末端可能被发布截断；只有看见后一个自然月的日历，才能确认前月已完整。
    months = months.filter((pl.col("date") <= through) & (pl.col("month_id") < months["month_id"].max()))
    m = market.filter(pl.col("date") <= through)
    s = state.filter(pl.col("date") <= through)
    if not set(m["date"].to_list()).issubset(days) or not set(s["date"].to_list()).issubset(days):
        raise ValueError("月度观察日期不属于冻结交易日历")
    m = m.join(s.select("date", "asset", "valid_for_factor_compute"), on=["date", "asset"], how="left", validate="1:1")
    if m["valid_for_factor_compute"].null_count():
        raise ValueError("月度行情缺少对应状态")
    v, p, c = (pl.col(x) for x in RAW_FIELDS)
    good = pl.col("valid_for_factor_compute") & v.is_finite() & (v >= 0) & p.is_finite() & (p > 0) & c.is_finite() & (c > 0)
    m = m.with_columns(_month_id().alias("month_id"),
        pl.when(good).then(v * p / c).alias("adjusted_volume"),
        (good & (v > 0)).fill_null(False).alias("active"))
    agg = m.group_by("asset", "month_id").agg(pl.len().alias("observed_sessions"),
        pl.col("adjusted_volume").is_finite().sum().alias("valid_sessions"),
        pl.col("adjusted_volume").mean().alias("mean_volume"), pl.col("active").sum().alias("active_sessions"))
    # 所有有状态的证券月份都留下，缺行情的月份不能从时间轴上消失。
    skeleton = s.with_columns(_month_id().alias("month_id")).select("asset", "month_id").unique().join(months, on="month_id")
    raw = skeleton.join(agg, on=["asset", "month_id"], how="left", validate="1:1")
    raw = raw.with_columns(pl.when((pl.col("observed_sessions") == pl.col("calendar_sessions")) &
        (pl.col("valid_sessions") == pl.col("calendar_sessions")) &
        (pl.col("active_sessions") >= gamma.min_active_sessions)).then(pl.col("mean_volume")).alias("monthly_volume"))
    raw = raw.sort("asset", "month_id").with_columns(
        pl.col("monthly_volume").rolling_mean(12, min_samples=12).over("asset").alias("_baseline"),
        (pl.col("month_id") - pl.col("month_id").shift(11).over("asset")).alias("_span"))
    history = raw.select("asset", (pl.col("month_id") + 7).alias("month_id"),
        pl.when(pl.col("_span") == 11).then(pl.col("_baseline")).alias("baseline_volume"))
    raw = raw.drop("_baseline", "_span").join(history, on=["asset", "month_id"], how="left", validate="1:1")
    raw = raw.with_columns(pl.when((pl.col("monthly_volume") > 0) & (pl.col("baseline_volume") > 0)).then(
        (pl.col("monthly_volume") / pl.col("baseline_volume")).log()).alias("atv"))
    last = s.join(months.select("date"), on="date").select("date", "asset", "valid_for_factor_rank")
    last = last.join(market.select("date", "asset", "capitalization_price_raw"), on=["date", "asset"], how="left", validate="1:1")
    if last.filter(pl.col("valid_for_factor_rank") & ~pl.col("capitalization_price_raw").is_finite().fill_null(False)).height:
        raise ValueError("可排名月末缺少原始股价，不能猜测一美元股票池资格")
    last = last.with_columns((pl.col("valid_for_factor_rank") & (pl.col("capitalization_price_raw") >= gamma.min_raw_price)).alias("universe_member"))
    raw = raw.join(last.select("date", "asset", "universe_member"), on=["date", "asset"], how="left", validate="1:1")
    # 缺少月末状态的证券仍保留未知，不进入当月股票池，也不冒充非极端状态。
    return raw.select("date", "asset", "month_id", "calendar_sessions", "observed_sessions", "valid_sessions",
        "active_sessions", "monthly_volume", "baseline_volume", "atv", "universe_member").sort("asset", "date")


def monthly_volume_states(raw: pl.DataFrame, gamma: MonthlyVolumeGamma) -> pl.DataFrame:
    """在原始观察之后排名；缺月或未知历史导致左端不确定，直到重置或达到上限。"""
    require_panel(raw, {"date", "asset", "month_id", "atv", "universe_member"}, "月度原始观察")
    usable = raw.filter(pl.col("universe_member") & pl.col("atv").is_finite())
    ranked = usable.select("date", "asset", ((pl.col("atv").rank("average").over("date") - .5) / pl.len().over("date")).alias("percentile"))
    frame = raw.select("date", "asset", "month_id", "universe_member").join(ranked, on=["date", "asset"], how="left", validate="1:1")
    frame = frame.with_columns(pl.when(pl.col("percentile").is_null()).then(None)
        .when(pl.col("percentile") >= .9).then(1).when(pl.col("percentile") <= .1).then(-1).otherwise(0).cast(pl.Int8).alias("extreme"))
    frame = frame.sort("asset", "month_id").with_columns(pl.col("extreme").shift(1).over("asset").alias("_previous"),
        (pl.col("month_id") - pl.col("month_id").shift(1).over("asset") == 1).fill_null(False).alias("_adjacent"))
    frame = frame.with_columns((pl.col("_adjacent") & pl.col("_previous").is_not_null() &
        pl.col("extreme").is_not_null() & (pl.col("_previous") != pl.col("extreme"))).fill_null(False).alias("_known_start"),
        (~pl.col("_adjacent") | pl.col("_previous").is_null() | pl.col("extreme").is_null() |
         (pl.col("_previous") != pl.col("extreme"))).fill_null(True).cast(pl.Int64).alias("_new_run"))
    frame = frame.with_columns(pl.col("_new_run").cum_sum().over("asset").alias("_run"))
    frame = frame.with_columns(pl.col("date").cum_count().over("asset", "_run").alias("observed_run_months"),
        pl.col("_known_start").first().over("asset", "_run").alias("known_start"))
    frame = frame.with_columns(pl.when(pl.col("extreme").is_null()).then(None)
        .when(pl.col("extreme") == 0).then(0)
        .when(pl.col("known_start") | (pl.col("observed_run_months") >= gamma.cap_months))
        .then(pl.col("extreme") * pl.col("observed_run_months").cast(pl.Int64).clip(1, gamma.cap_months))
        .otherwise(None).cast(pl.Int8).alias("patv"))
    return frame.drop("_previous", "_adjacent", "_known_start", "_new_run", "_run")


def monthly_state_feasibility(raw: pl.DataFrame, states: pl.DataFrame, plan: MonthlyVolumePreflightPlan) -> dict:
    """统计每月一次的乐观容量；整个事前股票池作为分母，不能只数触发股票。"""
    universe = raw.filter(pl.col("date").is_between(plan.discovery_start, plan.discovery_end) & pl.col("universe_member"))
    joined = universe.join(states.select("date", "asset", "patv", "percentile"), on=["date", "asset"], how="left", validate="1:1")
    coverage = joined.group_by("date").agg(pl.len().alias("universe"), pl.col("atv").is_finite().sum().alias("raw_valid"),
        pl.col("patv").is_not_null().sum().alias("state_valid")).sort("date")
    raw_coverage = coverage.select((pl.col("raw_valid") / pl.col("universe")).median()).item() if coverage.height else None
    state_coverage = coverage.select((pl.col("state_valid") / pl.col("universe")).median()).item() if coverage.height else None
    n = universe["asset"].n_unique()
    eligible_sets, levels, event_sets = [], [], []
    for q in plan.selectivity_levels:
        # PATV=1 是事前冻结的早期高成交状态，仅检查其容量，不挑最好的持续月份。
        events = joined.filter((pl.col("patv") == 1) & (pl.col("percentile") >= q))
        counts = events.group_by("asset").len()
        sufficient = set(counts.filter(pl.col("len") >= plan.min_events_per_ticker)["asset"].to_list())
        eligible_sets.append(sufficient)
        event_sets.append(set(events.select("date", "asset").iter_rows()))
        levels.append(dict(level=q, events=events.height, triggered_tickers=events["asset"].n_unique(), sufficient_tickers=len(sufficient)))
    sufficient = len(set.intersection(*eligible_sets))
    support = sufficient / n if n else 0.
    # 同一触发集合无法满足现有方向选择性所需的严格改善；这是结构限制，不读收益也可判断。
    identical_ladder_by_definition = all(events == event_sets[0] for events in event_sets[1:])
    capacity = n >= plan.min_tickers and support >= plan.support_threshold
    coverage_passed = (raw_coverage is not None and state_coverage is not None and
        min(raw_coverage, state_coverage) >= plan.min_signal_coverage)
    return dict(coverage_by_month=coverage.to_dicts(), raw_coverage=raw_coverage, state_coverage=state_coverage,
        universe_tickers=n, sufficient_tickers=sufficient, support_upper_bound=support,
        required_support=plan.support_threshold, ladder=levels, event_capacity_passed=capacity,
        identical_ladder_by_definition=identical_ladder_by_definition,
        coverage_passed=coverage_passed, feasible=coverage_passed and capacity and not identical_ladder_by_definition, return_labels_used=False,
        reason="早期极端成交状态已经限定最高十分组，三档触发集合相同，不能满足现有严格方向改善；另行报告覆盖与事件容量，不放松门槛。")


def preflight_monthly_volume(payload: dict, output_path: Path, *, with_data: bool) -> dict:
    """接入 preflight-favor；计划与 Γ 先落盘，失败记录保留，绝不打开标签。"""
    from factor_miner.favor_workflow import file_sha
    from factor_miner.favor_schema import FavorDataRelease
    from factor_miner.research_report import write_json
    from factor_miner.artifact_storage import snapshot_code
    from factor_miner.monthly_volume_synthetic import inspect_monthly_volume_contract

    plan = MonthlyVolumePreflightPlan.model_validate(payload)
    if output_path.exists():
        raise FileExistsError(output_path)
    run = output_path.with_suffix(".monthly_state")
    run.mkdir(parents=True, exist_ok=False)
    write_json(run / "plan.json", plan.model_dump(mode="json"))
    write_json(run / "gamma.json", {**plan.gamma.model_dump(mode="json"), "gamma_sha256": plan.gamma.identity,
        "hypothesis_sha256": sha256_json(plan.hypothesis.model_dump(mode="json"))})
    snapshot_code(run)
    try:
        synthetic = inspect_monthly_volume_contract(plan.gamma)
        report = dict(version=plan.version, plan_sha256=sha256_json(plan.model_dump(mode="json")),
            gamma_sha256=plan.gamma.identity, synthetic=synthetic, passed=synthetic["passed"],
            return_labels_used=False, real_market_data_used=False, registered_candidates=0,
            mechanism_status="mechanism_unverified", needs_review=False)
        if with_data and report["passed"]:
            dataset = Path(plan.dataset_root)
            paths = {k: dataset / f"{k}.parquet" for k in ("market", "state", "calendar")}
            paths.update(manifest=dataset / "manifest.json", release=Path(plan.data_contract_path), protocol=Path(plan.source_protocol_path))
            for name, path in paths.items():
                if plan.input_sha256.get(str(path)) != file_sha(path):
                    raise ValueError(f"月度预检输入未冻结或变化：{name}")
            release = FavorDataRelease.model_validate_json(paths["release"].read_text())
            manifest = json.loads(paths["manifest"].read_text()); protocol = json.loads(paths["protocol"].read_text())
            if (release.market_id != "us_equity" or release.adjustment != "split-adjusted price-only" or
                    release.as_of_date < plan.discovery_end or manifest["release_id"] != release.release_id or
                    manifest["as_of_date"] != release.as_of_date.isoformat() or protocol["release_id"] != release.release_id or
                    protocol["price_volume_basis"] != "unadjusted_same_day"):
                raise ValueError("发布身份、拆股价格或同日原始量价口径不符合月度观察合同")
            for name in ("market", "state", "calendar"):
                if manifest["files"][f"{name}.parquet"]["sha256"] != plan.input_sha256[str(paths[name])]:
                    raise ValueError("发布清单与冻结输入哈希冲突")
            if (release.calendar_version != plan.input_sha256[str(paths['calendar'])] or
                    release.state_version != plan.input_sha256[str(paths['state'])]):
                raise ValueError("发布合同的日历或状态版本与冻结文件不符")
            for name in ('market', 'state'):
                actual_end = pl.scan_parquet(paths[name]).select(pl.col('date').max()).collect().item()
                if actual_end != release.as_of_date:
                    raise ValueError("实际发布截止日与合同不符")
            market = pl.scan_parquet(paths["market"]).select("date", "asset", *RAW_FIELDS).filter(pl.col("date") <= plan.discovery_end).collect()
            state = pl.scan_parquet(paths["state"]).select("date", "asset", "valid_for_factor_compute", "valid_for_factor_rank").filter(pl.col("date") <= plan.discovery_end).collect()
            calendar = pl.read_parquet(paths["calendar"], columns=["date"])
            raw = raw_monthly_volume(market, state, calendar, plan.gamma, through=plan.discovery_end)
            states = monthly_volume_states(raw, plan.gamma)
            raw.write_parquet(run / "raw_monthly.parquet")
            states.write_parquet(run / "monthly_states.parquet")
            feasibility = monthly_state_feasibility(raw, states, plan)
            report.update(data_feasibility=feasibility, real_market_data_used=True, needs_review=not feasibility['feasible'])
            report["artifacts"] = {p.name: {"sha256": file_sha(p), "bytes": p.stat().st_size} for p in (run / "raw_monthly.parquet", run / "monthly_states.parquet")}
        report["checked_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output_path, report)
    except Exception as error:
        write_json(run / "failure.json", dict(error_type=type(error).__name__, reason=str(error), return_labels_used=False))
        raise
    return dict(passed=report["passed"], needs_review=report["needs_review"], output_path=str(output_path),
        plan_sha256=report["plan_sha256"], measurements=2, return_labels_used=False)
