"""固定候选池的方向感知筛选、成本压力与相关簇去重。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
from statistics import mean
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json


class CandidateScreeningPolicy(BaseModel):
    """在读取逐候选结果前冻结的筛选与稳健性政策。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["candidate-screening-v1", "candidate-screening-v2"] = "candidate-screening-v1"
    cutoff_at: datetime
    target_min: int = Field(default=20, ge=1)
    target_max: int = Field(default=30, ge=1)
    minimum_oriented_rank_ic: float = Field(default=0.01, ge=0.0, le=1.0)
    minimum_positive_year_fraction: float = Field(default=2.0 / 3.0, ge=0.0, le=1.0)
    minimum_positive_horizon_fraction: float = Field(default=0.6, ge=0.0, le=1.0)
    require_positive_confirmation_information_ratio: bool = True
    require_positive_confirmation_sharpe: bool = True
    require_positive_recent_rank_ic: bool = True
    require_positive_recent_information_ratio: bool = True
    stress_cost_bps: tuple[Literal[28], Literal[42]] = (28, 42)
    require_positive_28bp_information_ratio: bool = True
    require_positive_42bp_sharpe: bool = True
    cluster_abs_active_return_correlation: float = Field(default=0.80, ge=0.0, le=1.0)
    correlation_series: Literal[
        "target_long_active_return",
        "extreme_spread_net_return",
    ] = "target_long_active_return"
    minimum_correlation_observations: int = Field(default=40, ge=3)
    maximum_selected_per_cluster: int = Field(default=1, ge=1)
    regime_is_descriptive_only: Literal[True] = True

    @field_validator("cutoff_at")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        """筛选截止时点必须带时区并规范为 UTC。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("筛选截止时点必须带时区")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_target_range(self) -> CandidateScreeningPolicy:
        """目标下限不能超过目标上限。"""

        if self.target_min > self.target_max:
            raise ValueError("目标数量下限不得超过上限")
        if self.version == "candidate-screening-v1" and self.correlation_series != "target_long_active_return":
            raise ValueError("v1 必须使用目标多头主动收益相关性")
        if self.version == "candidate-screening-v2" and self.correlation_series != "extreme_spread_net_return":
            raise ValueError("v2 必须使用冻结极端组差净收益相关性")
        return self

    @property
    def policy_sha256(self) -> str:
        """返回冻结政策的规范内容哈希。"""

        return sha256_json(self.model_dump(mode="json"))


class CandidateScreeningInput(BaseModel):
    """从一个已发布候选产物提取的筛选输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    business_id: str = Field(pattern=r"^huan[0-9]{3,}$")
    source_run_id: str = Field(pattern=r"^run_[0-9a-f]{24}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formula_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hypothesis: str = Field(min_length=1)
    selected_direction: Literal["positive", "negative"]
    hypothesis_relation: Literal["supported", "reversed"]
    confirmation_passed: bool
    confirmation_rank_ic_mean: float
    confirmation_rank_ic_hac_t: float
    confirmation_ic_mean: float
    confirmation_ic_hac_t: float
    confirmation_information_ratio: float
    confirmation_sharpe: float
    confirmation_max_drawdown: float = Field(ge=0.0)
    confirmation_annualized_return: float
    annual_rank_ic: dict[str, float]
    horizon_rank_ic: dict[int, float]
    recent_rank_ic_mean: float
    recent_information_ratio: float
    recent_sharpe: float
    mean_turnover: float = Field(ge=0.0)
    annualization_factor: float = Field(default=52.0, gt=0.0)
    portfolio_rows: tuple[dict[str, object], ...] = Field(min_length=2)
    governance_spread_returns: dict[str, float] = Field(default_factory=dict)
    barra_status: str
    barra_common_contribution_share: float | None = Field(default=None, ge=0.0, le=1.0)
    barra_factor_risk_share: float | None = Field(default=None, ge=0.0, le=1.0)
    barra_max_abs_style_exposure: float | None = Field(default=None, ge=0.0)
    barra_max_abs_industry_exposure: float | None = Field(default=None, ge=0.0)
    regime_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    regime_oriented_rank_ic: dict[str, float] = Field(default_factory=dict)


class PortfolioStressMetrics(BaseModel):
    """按更高双边成本重算的目标多头组合指标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_bps: int = Field(gt=0)
    observations: int = Field(ge=2)
    annualized_return: float
    sharpe: float
    information_ratio: float
    max_drawdown: float = Field(ge=0.0)


class CandidateScreeningRecord(BaseModel):
    """一个候选的筛选判定和完整可见诊断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    business_id: str
    hypothesis: str
    source_run_id: str
    source_manifest_sha256: str
    spec_sha256: str
    formula_sha256: str
    status: Literal["核心候选", "观察候选", "相关簇备选", "未入选"]
    selection_rank: int | None = Field(default=None, ge=1)
    correlation_cluster: int | None = Field(default=None, ge=1)
    failure_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    selected_direction: Literal["positive", "negative"]
    hypothesis_relation: Literal["supported", "reversed"]
    oriented_rank_ic_mean: float
    oriented_rank_ic_hac_t: float
    oriented_ic_mean: float
    oriented_ic_hac_t: float
    positive_year_fraction: float = Field(ge=0.0, le=1.0)
    positive_horizon_fraction: float = Field(ge=0.0, le=1.0)
    confirmation_information_ratio: float
    confirmation_sharpe: float
    confirmation_max_drawdown: float
    confirmation_annualized_return: float
    recent_oriented_rank_ic_mean: float
    recent_information_ratio: float
    recent_sharpe: float
    mean_turnover: float
    stress_metrics: dict[str, PortfolioStressMetrics]
    barra_status: str
    barra_common_contribution_share: float | None = None
    barra_factor_risk_share: float | None = None
    barra_max_abs_style_exposure: float | None = None
    barra_max_abs_industry_exposure: float | None = None
    regime_coverage: float | None = None
    regime_oriented_rank_ic: dict[str, float] = Field(default_factory=dict)


class UnavailableCandidateRecord(BaseModel):
    """稳定编号存在但缺少可核验完整发布产物的候选。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    business_id: str
    failure_reason: str


class CandidateScreeningReport(BaseModel):
    """固定候选全集的筛选与稳健性结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["candidate-screening-report-v1"] = "candidate-screening-report-v1"
    policy: CandidateScreeningPolicy
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int = Field(ge=1)
    auditable_candidate_count: int = Field(ge=0)
    unavailable_candidate_count: int = Field(ge=0)
    input_complete: bool
    statistical_eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    target_satisfied: bool
    candidates: tuple[CandidateScreeningRecord, ...]
    unavailable_candidates: tuple[UnavailableCandidateRecord, ...] = ()
    selected: tuple[CandidateScreeningRecord, ...]
    source_manifest_sha256s: tuple[str, ...]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _compound(values: list[float]) -> float:
    """计算收益序列复合收益并拒绝无效值。"""

    wealth = 1.0
    for value in values:
        if not math.isfinite(value) or value <= -1.0:
            raise ValueError("组合收益包含非有限值或小于等于 -100% 的值")
        wealth *= 1.0 + value
    return wealth - 1.0


def _sample_std(values: list[float]) -> float:
    """计算样本标准差。"""

    if len(values) < 2:
        raise ValueError("组合收益至少需要两个观测")
    center = mean(values)
    return math.sqrt(sum((item - center) ** 2 for item in values) / (len(values) - 1))


def _max_drawdown(values: list[float]) -> float:
    """返回财富曲线最大回撤的正数幅度。"""

    wealth = 1.0
    peak = wealth
    result = 0.0
    for value in values:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        result = max(result, 1.0 - wealth / peak)
    return result


def stressed_portfolio_metrics(
    rows: tuple[dict[str, object], ...],
    *,
    cost_bps: int,
    annualization_factor: float,
) -> PortfolioStressMetrics:
    """从冻结毛收益和换手重算更高成本下的组合表现。"""

    if cost_bps <= 0 or not math.isfinite(annualization_factor) or annualization_factor <= 0:
        raise ValueError("成本和年化系数必须为有限正数")
    returns: list[float] = []
    benchmark: list[float] = []
    for row in rows:
        gross = row.get("target_long_gross_return")
        turnover = row.get("target_long_turnover")
        benchmark_return = row.get("benchmark_return")
        if not all(isinstance(item, int | float) for item in (gross, turnover, benchmark_return)):
            raise ValueError("组合压力测试缺少毛收益、换手或基准收益")
        net = float(gross) - float(turnover) * cost_bps / 10_000.0
        if float(benchmark_return) <= -1.0:
            raise ValueError("基准收益不能小于等于 -100%")
        returns.append(net)
        benchmark.append(float(benchmark_return))
    excess = [
        (1.0 + value) / (1.0 + base) - 1.0
        for value, base in zip(returns, benchmark, strict=True)
    ]
    return_std = _sample_std(returns)
    excess_std = _sample_std(excess)
    scale = math.sqrt(annualization_factor)
    total_return = _compound(returns)
    return PortfolioStressMetrics(
        cost_bps=cost_bps,
        observations=len(returns),
        annualized_return=(1.0 + total_return) ** (annualization_factor / len(returns)) - 1.0,
        sharpe=0.0 if return_std == 0.0 else mean(returns) / return_std * scale,
        information_ratio=0.0 if excess_std == 0.0 else mean(excess) / excess_std * scale,
        max_drawdown=_max_drawdown(returns),
    )


def _oriented(value: float, direction: Literal["positive", "negative"]) -> float:
    """按发现期冻结方向校正指标符号。"""

    return value if direction == "positive" else -value


def _positive_fraction(
    values: Mapping[object, float],
    direction: Literal["positive", "negative"],
) -> float:
    """计算方向校正后为正的年份或期限占比。"""

    if not values:
        return 0.0
    return sum(_oriented(float(value), direction) > 0.0 for value in values.values()) / len(values)


def _selection_returns(
    candidate: CandidateScreeningInput,
    policy: CandidateScreeningPolicy,
) -> dict[str, float]:
    """按冻结版本提取目标多头主动收益或极端组差净收益。"""

    if policy.correlation_series == "extreme_spread_net_return":
        if len(candidate.governance_spread_returns) < policy.minimum_correlation_observations:
            raise ValueError("候选缺少足够的冻结极端组差净收益相关性输入")
        return dict(candidate.governance_spread_returns)

    result: dict[str, float] = {}
    for row in candidate.portfolio_rows:
        exit_date = row.get("exit_date")
        value = row.get("target_long_net_return")
        benchmark = row.get("benchmark_return")
        if not isinstance(exit_date, str) or not isinstance(value, int | float) or not isinstance(benchmark, int | float):
            raise ValueError("组合相关性输入缺少 exit_date、净收益或基准收益")
        if float(benchmark) <= -1.0:
            raise ValueError("基准收益不能小于等于 -100%")
        if exit_date in result:
            raise ValueError("组合相关性输入 exit_date 重复")
        result[exit_date] = (1.0 + float(value)) / (1.0 + float(benchmark)) - 1.0
    return result


def _correlation(
    left: Mapping[str, float],
    right: Mapping[str, float],
    *,
    minimum_observations: int,
) -> float | None:
    """按共同退出日期计算主动收益 Pearson 相关。"""

    common = sorted(set(left).intersection(right))
    if len(common) < minimum_observations:
        return None
    left_values = [left[item] for item in common]
    right_values = [right[item] for item in common]
    left_center = mean(left_values)
    right_center = mean(right_values)
    numerator = sum(
        (a - left_center) * (b - right_center)
        for a, b in zip(left_values, right_values, strict=True)
    )
    denominator = math.sqrt(
        sum((item - left_center) ** 2 for item in left_values)
        * sum((item - right_center) ** 2 for item in right_values)
    )
    return 0.0 if denominator == 0.0 else numerator / denominator


def _evaluate_candidate(
    candidate: CandidateScreeningInput,
    policy: CandidateScreeningPolicy,
) -> CandidateScreeningRecord:
    """应用冻结硬门槛并保存所有失败原因。"""

    direction = candidate.selected_direction
    oriented_rank_ic = _oriented(candidate.confirmation_rank_ic_mean, direction)
    oriented_rank_t = _oriented(candidate.confirmation_rank_ic_hac_t, direction)
    positive_year_fraction = _positive_fraction(candidate.annual_rank_ic, direction)
    positive_horizon_fraction = _positive_fraction(candidate.horizon_rank_ic, direction)
    recent_oriented_rank_ic = _oriented(candidate.recent_rank_ic_mean, direction)
    stress = {
        str(cost): stressed_portfolio_metrics(
            candidate.portfolio_rows,
            cost_bps=cost,
            annualization_factor=candidate.annualization_factor,
        )
        for cost in policy.stress_cost_bps
    }
    failures: list[str] = []
    warnings: list[str] = []
    if not candidate.confirmation_passed:
        failures.append("确认期统计闸门未通过")
    if oriented_rank_ic < policy.minimum_oriented_rank_ic:
        failures.append("确认期方向校正 RankIC 未达到冻结下限")
    if positive_year_fraction < policy.minimum_positive_year_fraction:
        failures.append("确认期年度方向稳定性不足")
    if positive_horizon_fraction < policy.minimum_positive_horizon_fraction:
        failures.append("多期限方向稳定性不足")
    if policy.require_positive_confirmation_information_ratio and candidate.confirmation_information_ratio <= 0.0:
        failures.append("确认期信息比率不为正")
    if policy.require_positive_confirmation_sharpe and candidate.confirmation_sharpe <= 0.0:
        failures.append("确认期净 Sharpe 不为正")
    if policy.require_positive_recent_rank_ic and recent_oriented_rank_ic <= 0.0:
        failures.append("最近期方向校正 RankIC 不为正")
    if policy.require_positive_recent_information_ratio and candidate.recent_information_ratio <= 0.0:
        failures.append("最近期信息比率不为正")
    if policy.require_positive_28bp_information_ratio and stress["28"].information_ratio <= 0.0:
        failures.append("28bp 成本压力下信息比率不为正")
    if policy.require_positive_42bp_sharpe and stress["42"].sharpe <= 0.0:
        failures.append("42bp 成本压力下净 Sharpe 不为正")
    if candidate.hypothesis_relation == "reversed":
        warnings.append("发现方向与事前假设相反，经济机制不得视为通过")
    if candidate.barra_status != "available":
        warnings.append("Barra 归因不可用，不把风险暴露缺失解释为零")
    if candidate.regime_coverage is not None and candidate.regime_coverage < 0.8:
        warnings.append("市场状态覆盖不足八成，仅作描述性诊断")
    return CandidateScreeningRecord(
        candidate_id=candidate.candidate_id,
        business_id=candidate.business_id,
        hypothesis=candidate.hypothesis,
        source_run_id=candidate.source_run_id,
        source_manifest_sha256=candidate.source_manifest_sha256,
        spec_sha256=candidate.spec_sha256,
        formula_sha256=candidate.formula_sha256,
        status="未入选",
        failure_reasons=tuple(failures),
        warnings=tuple(warnings),
        selected_direction=direction,
        hypothesis_relation=candidate.hypothesis_relation,
        oriented_rank_ic_mean=oriented_rank_ic,
        oriented_rank_ic_hac_t=oriented_rank_t,
        oriented_ic_mean=_oriented(candidate.confirmation_ic_mean, direction),
        oriented_ic_hac_t=_oriented(candidate.confirmation_ic_hac_t, direction),
        positive_year_fraction=positive_year_fraction,
        positive_horizon_fraction=positive_horizon_fraction,
        confirmation_information_ratio=candidate.confirmation_information_ratio,
        confirmation_sharpe=candidate.confirmation_sharpe,
        confirmation_max_drawdown=candidate.confirmation_max_drawdown,
        confirmation_annualized_return=candidate.confirmation_annualized_return,
        recent_oriented_rank_ic_mean=recent_oriented_rank_ic,
        recent_information_ratio=candidate.recent_information_ratio,
        recent_sharpe=candidate.recent_sharpe,
        mean_turnover=candidate.mean_turnover,
        stress_metrics=stress,
        barra_status=candidate.barra_status,
        barra_common_contribution_share=candidate.barra_common_contribution_share,
        barra_factor_risk_share=candidate.barra_factor_risk_share,
        barra_max_abs_style_exposure=candidate.barra_max_abs_style_exposure,
        barra_max_abs_industry_exposure=candidate.barra_max_abs_industry_exposure,
        regime_coverage=candidate.regime_coverage,
        regime_oriented_rank_ic={
            key: _oriented(value, direction)
            for key, value in candidate.regime_oriented_rank_ic.items()
        },
    )


def _ranking_key(record: CandidateScreeningRecord) -> tuple[float, ...]:
    """按冻结字典序排序，避免随意加权总分。"""

    barra_share = record.barra_common_contribution_share
    return (
        -record.oriented_rank_ic_hac_t,
        -record.confirmation_information_ratio,
        -record.oriented_rank_ic_mean,
        -record.stress_metrics["28"].information_ratio,
        -record.confirmation_sharpe,
        record.confirmation_max_drawdown,
        record.mean_turnover,
        barra_share if barra_share is not None else 2.0,
    )


def _representative_clusters(
    candidates: tuple[CandidateScreeningInput, ...],
    eligible_order: tuple[str, ...],
    policy: CandidateScreeningPolicy,
) -> dict[str, int]:
    """按冻结排名代表做确定性聚类，避免单链接链式吞并。"""

    by_id = {item.candidate_id: item for item in candidates}
    returns = {
        candidate_id: _selection_returns(by_id[candidate_id], policy)
        for candidate_id in eligible_order
    }
    result: dict[str, int] = {}
    representatives: list[str] = []
    for candidate_id in eligible_order:
        matches: list[tuple[float, int]] = []
        for index, representative in enumerate(representatives, start=1):
            value = _correlation(
                returns[candidate_id],
                returns[representative],
                minimum_observations=policy.minimum_correlation_observations,
            )
            if value is not None and abs(value) >= policy.cluster_abs_active_return_correlation:
                matches.append((abs(value), index))
        if matches:
            result[candidate_id] = max(matches, key=lambda item: (item[0], -item[1]))[1]
        else:
            representatives.append(candidate_id)
            result[candidate_id] = len(representatives)
    return result


def screen_candidates(
    candidates: tuple[CandidateScreeningInput, ...],
    policy: CandidateScreeningPolicy,
    *,
    unavailable_candidates: tuple[UnavailableCandidateRecord, ...] = (),
) -> CandidateScreeningReport:
    """执行硬门槛、字典序排序和主动收益相关簇去重。"""

    if not candidates and not unavailable_candidates:
        raise ValueError("候选筛选输入不能为空")
    candidate_ids = [item.candidate_id for item in candidates]
    business_ids = [item.business_id for item in candidates]
    if len(set(candidate_ids)) != len(candidate_ids) or len(set(business_ids)) != len(business_ids):
        raise ValueError("候选 ID 或业务编号重复")
    evaluated = [_evaluate_candidate(item, policy) for item in candidates]
    eligible = sorted(
        (item for item in evaluated if not item.failure_reasons),
        key=_ranking_key,
    )
    clusters = _representative_clusters(
        candidates,
        tuple(item.candidate_id for item in eligible),
        policy,
    )
    by_cluster: dict[int, list[CandidateScreeningRecord]] = defaultdict(list)
    for item in eligible:
        by_cluster[clusters[item.candidate_id]].append(item)
    representatives = sorted(
        (items[0] for items in by_cluster.values()),
        key=_ranking_key,
    )[: policy.target_max]
    selected_ids = {item.candidate_id for item in representatives}
    selected_records: list[CandidateScreeningRecord] = []
    for rank, item in enumerate(representatives, start=1):
        selected_records.append(
            item.model_copy(
                update={
                    "status": "核心候选",
                    "selection_rank": rank,
                    "correlation_cluster": clusters[item.candidate_id],
                }
            )
        )
    if len(selected_records) < policy.target_min:
        fillers = [item for item in eligible if item.candidate_id not in selected_ids]
        for item in fillers[: policy.target_min - len(selected_records)]:
            selected_ids.add(item.candidate_id)
            selected_records.append(
                item.model_copy(
                    update={
                        "status": "观察候选",
                        "selection_rank": len(selected_records) + 1,
                        "correlation_cluster": clusters[item.candidate_id],
                        "warnings": item.warnings + ("为满足目标下限保留的相关簇次优候选",),
                    }
                )
            )
    selected_by_id = {item.candidate_id: item for item in selected_records}
    final_records: list[CandidateScreeningRecord] = []
    for item in evaluated:
        if item.candidate_id in selected_by_id:
            final_records.append(selected_by_id[item.candidate_id])
        elif item.candidate_id in clusters:
            final_records.append(
                item.model_copy(
                    update={
                        "status": "相关簇备选",
                        "correlation_cluster": clusters[item.candidate_id],
                    }
                )
            )
        else:
            final_records.append(item)
    final_records.sort(key=lambda item: item.business_id)
    selected_records.sort(key=lambda item: item.selection_rank or 10**9)
    manifests = tuple(sorted({item.source_manifest_sha256 for item in candidates}))
    payload = {
        "version": "candidate-screening-report-v1",
        "policy_sha256": policy.policy_sha256,
        "candidate_count": len(candidates) + len(unavailable_candidates),
        "auditable_candidate_count": len(candidates),
        "unavailable_candidate_count": len(unavailable_candidates),
        "input_complete": not unavailable_candidates,
        "statistical_eligible_count": len(eligible),
        "selected_count": len(selected_records),
        "target_satisfied": policy.target_min <= len(selected_records) <= policy.target_max,
        "candidates": [item.model_dump(mode="json") for item in final_records],
        "unavailable_candidates": [
            item.model_dump(mode="json") for item in unavailable_candidates
        ],
        "selected": [item.model_dump(mode="json") for item in selected_records],
        "source_manifest_sha256s": manifests,
    }
    return CandidateScreeningReport(
        policy=policy,
        report_sha256=sha256_json(payload),
        **payload,
    )
