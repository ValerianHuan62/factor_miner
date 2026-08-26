"""冻结多期限 IC/RankIC、HAC t 值和可视化诊断。"""

from __future__ import annotations

from datetime import date
import math
from statistics import mean, stdev
from typing import Iterable

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import EvaluationPolicySpec


FROZEN_HORIZONS = (1, 3, 5, 10, 20)


class DailyIC(BaseModel):
    """一个日期和期限的 IC/RankIC。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    horizon: int = Field(gt=0)
    ic: float
    rank_ic: float
    eligible_count: int = Field(ge=0)


class ICDecayPoint(BaseModel):
    """一个标签期限的衰减汇总。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon: int = Field(gt=0)
    ic_mean: float
    rank_ic_mean: float
    valid_dates: int = Field(ge=0)


class ICAutocorrelationPoint(BaseModel):
    """IC 序列的固定滞后自相关。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lag: int = Field(gt=0)
    ic_autocorrelation: float
    rank_ic_autocorrelation: float


class ICDiagnostics(BaseModel):
    """Dashboard 和可见评价共用的 IC 诊断结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_horizon: int = 5
    horizons: tuple[int, ...] = FROZEN_HORIZONS
    daily: tuple[DailyIC, ...] = Field(min_length=1)
    ic_sequence: tuple[float, ...] = Field(min_length=1)
    rank_ic_sequence: tuple[float, ...] = Field(min_length=1)
    ic_mean: float
    rank_ic_mean: float
    ic_std: float
    rank_ic_std: float
    ic_ir: float
    rank_ic_ir: float
    ir: float
    p_ic_lt_neg_002: float = Field(ge=0, le=1)
    p_ic_gt_pos_002: float = Field(ge=0, le=1)
    ic_hac_t: float
    rank_ic_hac_t: float
    ic_distribution: dict[str, float]
    rank_ic_distribution: dict[str, float]
    decay: tuple[ICDecayPoint, ...] = Field(min_length=len(FROZEN_HORIZONS))
    autocorrelation: tuple[ICAutocorrelationPoint, ...]
    annual_summary: dict[str, dict[str, float | int]]


def _ic_error(message: str) -> FactorMinerError:
    """构造 IC 诊断输入错误。"""

    return FactorMinerError(FailureCode.STAT_FAMILY_NOT_FROZEN, message)


def _pearson(left: list[float], right: list[float]) -> float:
    """计算一组截面的 Pearson 相关。"""

    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    denominator = math.sqrt(left_var * right_var)
    if denominator == 0:
        raise _ic_error("IC 截面存在常数输入")
    return numerator / denominator


def _rank(values: list[float]) -> list[float]:
    """以 average tie method 计算截面排名。"""

    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[indexed[position][0]] = rank
        cursor = end
    return ranks


def _sample_std(values: list[float]) -> float:
    """返回样本标准差；常数 IC 的 IR 定义为 0。"""

    return stdev(values) if len(values) >= 2 else 0.0


def _ir(values: list[float]) -> float:
    """计算均值除以样本标准差的 IC IR。"""

    deviation = _sample_std(values)
    return 0.0 if deviation == 0.0 else mean(values) / deviation


def _hac_t(values: list[float], max_lags: int) -> float:
    """用冻结 Newey-West/HAC 长度计算均值 t 统计量。"""

    if not values:
        raise _ic_error("HAC 输入为空")
    center = mean(values)
    count = len(values)
    gamma_zero = sum((value - center) ** 2 for value in values) / count
    variance = gamma_zero
    for lag in range(1, min(max_lags, count - 1) + 1):
        covariance = sum(
            (values[index] - center) * (values[index - lag] - center)
            for index in range(lag, count)
        ) / count
        variance += 2.0 * (1.0 - lag / (max_lags + 1.0)) * covariance
    variance_of_mean = max(variance / count, 0.0)
    return 0.0 if variance_of_mean == 0.0 else center / math.sqrt(variance_of_mean)


def _autocorrelation(values: list[float], lag: int) -> float:
    """计算固定滞后的样本相关。"""

    if len(values) <= lag + 1:
        return 0.0
    left = values[lag:]
    right = values[:-lag]
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right, strict=True)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return 0.0 if denominator == 0.0 else numerator / denominator


def _quantiles(values: list[float]) -> dict[str, float]:
    """生成 IC 分布图所需的固定分位点。"""

    ordered = sorted(values)
    if not ordered:
        raise _ic_error("IC 分布输入为空")
    result: dict[str, float] = {}
    for name, probability in (("q05", 0.05), ("q25", 0.25), ("q50", 0.5), ("q75", 0.75), ("q95", 0.95)):
        position = probability * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        weight = position - lower
        result[name] = ordered[lower] + (ordered[upper] - ordered[lower]) * weight
    return result


def _annual_summary(rows: Iterable[DailyIC]) -> dict[str, dict[str, float | int]]:
    """按自然年度输出描述性 IC 摘要。"""

    grouped: dict[str, list[DailyIC]] = {}
    for row in rows:
        grouped.setdefault(str(row.date.year), []).append(row)
    return {
        year: {
            "valid_dates": len(items),
            "ic_mean": mean(item.ic for item in items),
            "rank_ic_mean": mean(item.rank_ic for item in items),
        }
        for year, items in sorted(grouped.items())
    }


def evaluate_ic_horizons(
    factor_panel: pl.LazyFrame,
    horizons: tuple[int, ...],
    policy: EvaluationPolicySpec,
) -> ICDiagnostics:
    """评价冻结的 1/3/5/10/20 日 IC 与 RankIC。

    面板字段为 `date`、`asset`、`factor_value`、冻结 rank mask，以及
    `forward_return_{h}`。这些 forward return 必须由服务器侧 point-in-time
    标签生产，不由本函数向后填充或使用未来字段补缺。
    """

    if horizons != FROZEN_HORIZONS:
        raise _ic_error("IC 期限必须严格冻结为 (1, 3, 5, 10, 20)")
    if policy.label_horizon_sessions != 5:
        raise _ic_error("主 IC 期限必须与冻结的 5 日标签一致")
    data = factor_panel.collect()
    required = {"date", "asset", "factor_value", policy.rank_mask_column}
    required.update(f"forward_return_{horizon}" for horizon in horizons)
    missing = required.difference(data.columns)
    if missing:
        raise _ic_error(f"IC 输入缺少字段：{sorted(missing)}")
    if data.schema.get("date") != pl.Date:
        raise _ic_error("IC 输入 date 必须为 Date")
    if data.select(pl.struct(["date", "asset"]).is_duplicated().any()).item():
        raise _ic_error("IC 输入 date/asset 主键重复")
    rows_by_horizon: dict[int, list[DailyIC]] = {}
    for horizon in horizons:
        horizon_rows: list[DailyIC] = []
        return_column = f"forward_return_{horizon}"
        eligible = data.filter(
            (pl.col(policy.rank_mask_column) == True)
            & pl.col("factor_value").is_finite()
            & pl.col(return_column).is_finite()
        )
        for day in eligible.partition_by("date", maintain_order=True):
            current = day.get_column("date")[0]
            if day.height < policy.min_names_per_date:
                continue
            factor_values = day["factor_value"].to_list()
            return_values = day[return_column].to_list()
            ic = _pearson(factor_values, return_values)
            rank_ic = _pearson(_rank(factor_values), _rank(return_values))
            horizon_rows.append(
                DailyIC(
                    date=current,
                    horizon=horizon,
                    ic=ic,
                    rank_ic=rank_ic,
                    eligible_count=day.height,
                )
            )
        if len(horizon_rows) < policy.min_valid_dates:
            raise _ic_error(f"期限 {horizon} 的有效日期不足")
        rows_by_horizon[horizon] = horizon_rows

    primary = rows_by_horizon[5]
    ic_values = [item.ic for item in primary]
    rank_values = [item.rank_ic for item in primary]
    decay = tuple(
        ICDecayPoint(
            horizon=horizon,
            ic_mean=mean(item.ic for item in rows_by_horizon[horizon]),
            rank_ic_mean=mean(item.rank_ic for item in rows_by_horizon[horizon]),
            valid_dates=len(rows_by_horizon[horizon]),
        )
        for horizon in horizons
    )
    autocorrelation = tuple(
        ICAutocorrelationPoint(
            lag=lag,
            ic_autocorrelation=_autocorrelation(ic_values, lag),
            rank_ic_autocorrelation=_autocorrelation(rank_values, lag),
        )
        for lag in range(1, min(5, len(primary) - 1) + 1)
    )
    return ICDiagnostics(
        daily=tuple(primary),
        ic_sequence=tuple(ic_values),
        rank_ic_sequence=tuple(rank_values),
        ic_mean=mean(ic_values),
        rank_ic_mean=mean(rank_values),
        ic_std=_sample_std(ic_values),
        rank_ic_std=_sample_std(rank_values),
        ic_ir=_ir(ic_values),
        rank_ic_ir=_ir(rank_values),
        ir=_ir(rank_values),
        p_ic_lt_neg_002=sum(value < -0.02 for value in ic_values) / len(ic_values),
        p_ic_gt_pos_002=sum(value > 0.02 for value in ic_values) / len(ic_values),
        ic_hac_t=_hac_t(ic_values, policy.hac_max_lags),
        rank_ic_hac_t=_hac_t(rank_values, policy.hac_max_lags),
        ic_distribution=_quantiles(ic_values),
        rank_ic_distribution=_quantiles(rank_values),
        decay=decay,
        autocorrelation=autocorrelation,
        annual_summary=_annual_summary(primary),
    )
