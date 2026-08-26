"""候选因子的结构重复和输出相关性冗余闸门。"""

from __future__ import annotations

from datetime import date
import math
from statistics import median
from typing import Mapping, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.compiler import CompiledFactorPlan
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import CampaignSpec


class StructuralRedundancy(BaseModel):
    """规范化 AST 和结构签名的重复检查结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    passed: bool
    matched_candidate_ids: tuple[str, ...]
    signature_matched_candidate_ids: tuple[str, ...]
    candidate_ast_hash: str


class OutputCorrelationSummary(BaseModel):
    """候选与一个 reference 的总体、年度和重叠摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_name: str
    valid_dates: int = Field(ge=0)
    invalid_dates: int = Field(ge=0)
    median_correlation: FiniteFloat | None = None
    abs_correlation_p95: FiniteFloat | None = None
    annual_median_correlations: dict[str, FiniteFloat]
    median_overlap: FiniteFloat | None = None
    min_overlap: int = Field(ge=0)
    overlap_sufficient: bool
    redundant: bool
    reason: str | None = None


class OutputRedundancy(BaseModel):
    """候选与冻结 reference pool 的输出冗余检查结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str | None = None
    candidate_column: str
    passed: bool
    comparisons: tuple[OutputCorrelationSummary, ...]
    failure_code: FailureCode | None = None


def check_structural_redundancy(
    candidate: CompiledFactorPlan,
    references: Sequence[CompiledFactorPlan],
) -> StructuralRedundancy:
    """比较候选与冻结 reference pool 的规范化 AST 和结构签名。

    参数：
        candidate: 当前按 campaign 顺序待检查的编译计划。
        references: 冻结 reference pool 与排在当前候选之前的候选计划。

    返回：
        带有精确 AST 重复和结构签名重复明细的结果。
    """

    matched = tuple(
        plan.candidate_id
        for plan in references
        if plan.ast_hash == candidate.ast_hash
    )
    candidate_signature = _plan_signature(candidate)
    signature_matched = tuple(
        plan.candidate_id
        for plan in references
        if _plan_signature(plan) == candidate_signature
    )
    return StructuralRedundancy(
        candidate_id=candidate.candidate_id,
        passed=not matched and not signature_matched,
        matched_candidate_ids=matched,
        signature_matched_candidate_ids=signature_matched,
        candidate_ast_hash=candidate.ast_hash,
    )


def check_output_redundancy(
    candidate_frame: pl.DataFrame,
    reference_frames: Mapping[str, pl.DataFrame],
    campaign: CampaignSpec,
    *,
    candidate_column: str = "raw_factor",
    candidate_id: str | None = None,
) -> OutputRedundancy:
    """在冻结可见区间逐日计算候选与 reference 的截面 Spearman。

    参数：
        candidate_frame: 包含 date、asset 和候选输出列的面板。
        reference_frames: 按冻结名称排列的 reference 输出面板。
        campaign: 提供可见区间、最小重叠数和冗余阈值的冻结协议。
        candidate_column: 候选输出列名。
        candidate_id: 可选的候选 ID，用于审计结果。

    返回：
        包含总体中位数、绝对相关 95 分位数、分年度摘要和重叠数量的结果。

    异常：
        输入缺列、主键重复或日期类型错误时抛出领域错误。
    """

    _validate_output_frame(candidate_frame, candidate_column, "候选")
    comparisons: list[OutputCorrelationSummary] = []
    for reference_name, reference_frame in reference_frames.items():
        reference_columns = [
            column for column in reference_frame.columns if column not in {"date", "asset"}
        ]
        if len(reference_columns) != 1:
            raise FactorMinerError(
                FailureCode.FIELD_MISSING,
                f"{reference_name}输出必须恰好包含一个数值列",
            )
        _validate_output_frame(reference_frame, reference_columns[0], reference_name)
        comparisons.append(
            _compare_one_reference(
                candidate_frame,
                reference_frame,
                reference_name,
                candidate_column,
                campaign,
            )
        )
    passed = all(
        comparison.overlap_sufficient and not comparison.redundant
        for comparison in comparisons
    )
    failure_code = None if passed else FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
    return OutputRedundancy(
        candidate_id=candidate_id,
        candidate_column=candidate_column,
        passed=passed,
        comparisons=tuple(comparisons),
        failure_code=failure_code,
    )


def _plan_signature(plan: CompiledFactorPlan) -> tuple[object, ...]:
    """提取字段、窗口、算子和字段签名。"""

    metadata = plan.expression_metadata
    return (
        plan.required_fields,
        plan.required_lookback,
        tuple(metadata.get("operator_signature", ())),
        tuple(metadata.get("field_signature", ())),
    )


def _validate_output_frame(frame: pl.DataFrame, column: str, name: str) -> None:
    """验证输出面板字段、日期类型和 date/asset 唯一性。"""

    missing = {"date", "asset", column} - set(frame.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"{name}输出缺少列：{sorted(missing)}",
        )
    if frame.schema.get("date") != pl.Date:
        raise FactorMinerError(FailureCode.FIELD_MISSING, f"{name}输出 date 必须是 Date 类型")
    duplicate = frame.group_by(["date", "asset"]).len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise FactorMinerError(
            FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
            f"{name}输出存在重复 date/asset 主键",
        )


def _compare_one_reference(
    candidate_frame: pl.DataFrame,
    reference_frame: pl.DataFrame,
    reference_name: str,
    candidate_column: str,
    campaign: CampaignSpec,
) -> OutputCorrelationSummary:
    """计算一个 reference 的每日、总体和年度相关摘要。"""

    reference_column = next(
        column
        for column in reference_frame.columns
        if column not in {"date", "asset"}
    )
    candidate = candidate_frame.select(["date", "asset", candidate_column]).rename(
        {candidate_column: "_candidate"}
    )
    reference = reference_frame.select(["date", "asset", reference_column]).rename(
        {reference_column: "_reference"}
    )
    joined = (
        candidate.join(reference, on=["date", "asset"], how="inner", validate="1:1")
        .filter(
            pl.col("date").is_between(
                campaign.visible_start,
                campaign.visible_end,
                closed="both",
            )
        )
        .sort(["date", "asset"])
    )
    dates = joined.get_column("date").unique().sort().to_list()
    correlations: list[tuple[date, float]] = []
    overlap_counts: list[int] = []
    invalid_dates = 0
    for current in dates:
        day = joined.filter(pl.col("date") == current)
        eligible = day.filter(
            (pl.col("_candidate").is_finite() == True)
            & (pl.col("_reference").is_finite() == True)
        )
        overlap_counts.append(eligible.height)
        if eligible.height < campaign.min_names_per_date:
            invalid_dates += 1
            continue
        correlation = _spearman(
            eligible.get_column("_candidate"),
            eligible.get_column("_reference"),
        )
        if correlation is None:
            invalid_dates += 1
            continue
        correlations.append((current, correlation))
    if not correlations:
        return OutputCorrelationSummary(
            reference_name=reference_name,
            valid_dates=0,
            invalid_dates=invalid_dates,
            annual_median_correlations={},
            median_overlap=float(median(overlap_counts)) if overlap_counts else None,
            min_overlap=min(overlap_counts) if overlap_counts else 0,
            overlap_sufficient=False,
            redundant=False,
            reason="重叠股票数不足",
        )
    values = [value for _, value in correlations]
    annual_values: dict[str, list[float]] = {}
    for current, value in correlations:
        annual_values.setdefault(str(current.year), []).append(value)
    annual_medians = {
        year: float(median(year_values))
        for year, year_values in sorted(annual_values.items())
    }
    median_correlation = float(median(values))
    abs_p95 = _percentile_95([abs(value) for value in values])
    summary_abs_values = [abs(median_correlation), abs_p95, *map(abs, annual_medians.values())]
    redundant = any(value > campaign.max_abs_output_correlation for value in summary_abs_values)
    return OutputCorrelationSummary(
        reference_name=reference_name,
        valid_dates=len(correlations),
        invalid_dates=invalid_dates,
        median_correlation=median_correlation,
        abs_correlation_p95=abs_p95,
        annual_median_correlations=annual_medians,
        median_overlap=float(median(overlap_counts)) if overlap_counts else None,
        min_overlap=min(overlap_counts) if overlap_counts else 0,
        overlap_sufficient=True,
        redundant=redundant,
    )


def _spearman(left: pl.Series, right: pl.Series) -> float | None:
    """计算两列有限值的截面 Spearman，常数输入返回 None。"""

    left_rank = left.rank(method="average").cast(pl.Float64)
    right_rank = right.rank(method="average").cast(pl.Float64)
    left_mean = left_rank.mean()
    right_mean = right_rank.mean()
    if left_mean is None or right_mean is None:
        return None
    left_centered = left_rank - left_mean
    right_centered = right_rank - right_mean
    denominator = math.sqrt(
        float((left_centered * left_centered).sum())
        * float((right_centered * right_centered).sum())
    )
    if denominator <= 0 or not math.isfinite(denominator):
        return None
    value = float((left_centered * right_centered).sum()) / denominator
    return value if math.isfinite(value) else None


def _percentile_95(values: Sequence[float]) -> float:
    """使用线性插值计算确定性的绝对相关 95 分位数。"""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("分位数输入不能为空")
    position = (len(ordered) - 1) * 0.95
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)
