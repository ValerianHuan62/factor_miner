"""冻结可见区间内的逐日截面 Spearman RankIC 评价。"""

from __future__ import annotations

from datetime import date
import math
from statistics import mean, median, stdev

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import CampaignSpec


class DailyRankIC(BaseModel):
    """单个可见日期的 RankIC、覆盖率和无效原因。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    rank_ic: FiniteFloat | None = None
    eligible_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    factor_coverage: FiniteFloat = Field(ge=0, le=1)
    label_coverage: FiniteFloat = Field(ge=0, le=1)
    coverage: FiniteFloat = Field(ge=0, le=1)
    reason: str | None = None


class AnnualEvaluationSummary(BaseModel):
    """单个自然年度的有效 RankIC 诊断摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    year: str
    valid_dates: int = Field(ge=0)
    mean_rank_ic: FiniteFloat | None = None
    std_rank_ic: FiniteFloat | None = None
    icir: FiniteFloat | None = None
    positive_rank_ic_ratio: FiniteFloat = Field(ge=0, le=1)
    negative_rank_ic_ratio: FiniteFloat = Field(ge=0, le=1)
    zero_rank_ic_ratio: FiniteFloat = Field(ge=0, le=1)
    median_coverage: FiniteFloat = Field(ge=0, le=1)


class EvaluationMetrics(BaseModel):
    """可见 RankIC 的完整日期级评价摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    daily: tuple[DailyRankIC, ...]
    total_dates: int = Field(ge=0)
    valid_dates: int = Field(ge=0)
    invalid_dates: int = Field(ge=0)
    median_coverage: FiniteFloat = Field(ge=0, le=1)
    mean_rank_ic: FiniteFloat | None = None
    std_rank_ic: FiniteFloat | None = None
    icir: FiniteFloat | None = None
    positive_rank_ic_ratio: FiniteFloat = Field(default=0.0, ge=0, le=1)
    negative_rank_ic_ratio: FiniteFloat = Field(default=0.0, ge=0, le=1)
    zero_rank_ic_ratio: FiniteFloat = Field(default=0.0, ge=0, le=1)
    rank_ic_quantiles: dict[str, FiniteFloat] = Field(default_factory=dict)
    annual_summaries: dict[str, AnnualEvaluationSummary] = Field(default_factory=dict)
    eligible_count_min: int = Field(default=0, ge=0)
    eligible_count_median: FiniteFloat = Field(default=0.0, ge=0)
    eligible_count_max: int = Field(default=0, ge=0)
    median_factor_coverage: FiniteFloat = Field(default=0.0, ge=0, le=1)
    median_label_coverage: FiniteFloat = Field(default=0.0, ge=0, le=1)

    @property
    def rank_ic_values(self) -> tuple[float, ...]:
        """返回按日期顺序排列的有效 RankIC 序列。"""

        return tuple(item.rank_ic for item in self.daily if item.rank_ic is not None)


def evaluate_rank_ic(
    frame: pl.DataFrame | pl.LazyFrame,
    campaign: CampaignSpec,
    factor_column: str = "raw_factor",
) -> EvaluationMetrics:
    """按冻结 campaign 计算每日截面 Spearman RankIC。

    参数：
        frame: 包含 date、asset、原始因子、rank mask 和 label 的面板。
        campaign: 结果产生前冻结的可见区间、mask、label 和最小股票数。
        factor_column: 原始因子列名，默认是 `raw_factor`。

    返回：
        包含全部输入日期有效/无效状态和覆盖率的 `EvaluationMetrics`。

    异常：
        输入缺少列、日期类型不符或因子列为空时抛出领域错误。
    """

    data = frame.collect() if isinstance(frame, pl.LazyFrame) else frame
    required = {
        "date",
        "asset",
        factor_column,
        campaign.rank_mask_column,
        campaign.label_column,
    }
    missing = required - set(data.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"RankIC 输入缺少列：{sorted(missing)}",
        )
    if data.schema.get("date") != pl.Date:
        raise FactorMinerError(FailureCode.FIELD_MISSING, "RankIC 输入 date 必须是 Date 类型")

    visible = data.filter(
        pl.col("date").is_between(
            campaign.visible_start,
            campaign.visible_end,
            closed="both",
        )
    ).sort(["date", "asset"])
    dates = visible.get_column("date").unique().sort().to_list()
    daily = tuple(
        _daily_rank_ic(visible.filter(pl.col("date") == current), current, campaign, factor_column)
        for current in dates
    )
    coverages = [item.coverage for item in daily]
    valid_dates = sum(item.rank_ic is not None for item in daily)
    diagnostics = _rank_ic_diagnostics(daily)
    return EvaluationMetrics(
        daily=daily,
        total_dates=len(daily),
        valid_dates=valid_dates,
        invalid_dates=len(daily) - valid_dates,
        median_coverage=float(median(coverages)) if coverages else 0.0,
        **diagnostics,
    )


def _daily_rank_ic(
    day: pl.DataFrame,
    current: date,
    campaign: CampaignSpec,
    factor_column: str,
) -> DailyRankIC:
    """计算一个日期的有效样本、覆盖率和 Spearman 值。"""

    eligible = day.filter(
        (pl.col(campaign.rank_mask_column) == True)
        & (pl.col(factor_column).is_finite() == True)
        & (pl.col(campaign.label_column).is_finite() == True)
    )
    total_count = day.height
    eligible_count = eligible.height
    factor_count = day.select(pl.col(factor_column).is_finite().fill_null(False).sum()).item()
    label_count = day.select(
        pl.col(campaign.label_column).is_finite().fill_null(False).sum()
    ).item()
    factor_coverage = factor_count / total_count if total_count else 0.0
    label_coverage = label_count / total_count if total_count else 0.0
    coverage = eligible_count / total_count if total_count else 0.0
    base = {
        "date": current,
        "eligible_count": eligible_count,
        "total_count": total_count,
        "factor_coverage": factor_coverage,
        "label_coverage": label_coverage,
        "coverage": coverage,
    }
    if eligible_count < campaign.min_names_per_date:
        return DailyRankIC(**base, reason="有效股票数不足")

    factor_ranks = eligible.get_column(factor_column).rank(method="average").cast(pl.Float64)
    label_ranks = eligible.get_column(campaign.label_column).rank(method="average").cast(pl.Float64)
    factor_mean = factor_ranks.mean()
    label_mean = label_ranks.mean()
    if factor_mean is None or label_mean is None:
        return DailyRankIC(**base, reason="常数输入")
    factor_centered = factor_ranks - factor_mean
    label_centered = label_ranks - label_mean
    numerator = float((factor_centered * label_centered).sum())
    denominator = math.sqrt(
        float((factor_centered * factor_centered).sum())
        * float((label_centered * label_centered).sum())
    )
    if denominator <= 0 or not math.isfinite(denominator):
        return DailyRankIC(**base, reason="常数输入")
    value = numerator / denominator
    if not math.isfinite(value):
        return DailyRankIC(**base, reason="常数输入")
    return DailyRankIC(**base, rank_ic=value)


def _rank_ic_diagnostics(daily: tuple[DailyRankIC, ...]) -> dict[str, object]:
    """从每日结果计算冻结评价协议之外的描述性诊断。"""

    values = [item.rank_ic for item in daily if item.rank_ic is not None]
    eligible_counts = [item.eligible_count for item in daily]
    factor_coverages = [item.factor_coverage for item in daily]
    label_coverages = [item.label_coverage for item in daily]
    diagnostics: dict[str, object] = {
        "eligible_count_min": min(eligible_counts) if eligible_counts else 0,
        "eligible_count_median": float(median(eligible_counts)) if eligible_counts else 0.0,
        "eligible_count_max": max(eligible_counts) if eligible_counts else 0,
        "median_factor_coverage": float(median(factor_coverages))
        if factor_coverages
        else 0.0,
        "median_label_coverage": float(median(label_coverages))
        if label_coverages
        else 0.0,
        "mean_rank_ic": float(mean(values)) if values else None,
        "std_rank_ic": float(stdev(values)) if len(values) >= 2 else None,
        "icir": _icir(values),
        "positive_rank_ic_ratio": _ratio(values, lambda value: value > 0),
        "negative_rank_ic_ratio": _ratio(values, lambda value: value < 0),
        "zero_rank_ic_ratio": _ratio(values, lambda value: value == 0),
        "rank_ic_quantiles": _rank_ic_quantiles(values),
        "annual_summaries": _annual_summaries(daily),
    }
    return diagnostics


def _icir(values: list[float]) -> float | None:
    """用有效每日 RankIC 的样本标准差计算 ICIR。"""

    if len(values) < 2:
        return None
    deviation = stdev(values)
    if deviation == 0:
        return None
    return float(mean(values) / deviation)


def _ratio(values: list[float], predicate: object) -> float:
    """计算有效 RankIC 满足方向条件的日期比例。"""

    if not values:
        return 0.0
    return float(sum(predicate(value) for value in values) / len(values))


def _rank_ic_quantiles(values: list[float]) -> dict[str, float]:
    """用确定性的线性插值计算五个 RankIC 分位数。"""

    if not values:
        return {}
    ordered = sorted(values)
    return {
        name: _linear_quantile(ordered, probability)
        for name, probability in (
            ("q05", 0.05),
            ("q25", 0.25),
            ("q50", 0.50),
            ("q75", 0.75),
            ("q95", 0.95),
        )
    }


def _linear_quantile(ordered: list[float], probability: float) -> float:
    """按 type-7 位置规则计算一个线性插值分位数。"""

    if len(ordered) == 1:
        return float(ordered[0])
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + weight * (ordered[upper] - ordered[lower]))


def _annual_summaries(
    daily: tuple[DailyRankIC, ...],
) -> dict[str, AnnualEvaluationSummary]:
    """按自然年汇总有效 RankIC，不把无效日期伪装成零。"""

    grouped: dict[str, list[DailyRankIC]] = {}
    for item in daily:
        if item.rank_ic is not None:
            grouped.setdefault(str(item.date.year), []).append(item)
    result: dict[str, AnnualEvaluationSummary] = {}
    for year, items in grouped.items():
        values = [item.rank_ic for item in items if item.rank_ic is not None]
        result[year] = AnnualEvaluationSummary(
            year=year,
            valid_dates=len(values),
            mean_rank_ic=float(mean(values)),
            std_rank_ic=float(stdev(values)) if len(values) >= 2 else None,
            icir=_icir(values),
            positive_rank_ic_ratio=_ratio(values, lambda value: value > 0),
            negative_rank_ic_ratio=_ratio(values, lambda value: value < 0),
            zero_rank_ic_ratio=_ratio(values, lambda value: value == 0),
            median_coverage=float(median(item.coverage for item in items)),
        )
    return result
