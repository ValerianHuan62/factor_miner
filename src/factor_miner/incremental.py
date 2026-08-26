"""V0.2 参考因子联合正交化与增量信息评价。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from statistics import median
from typing import Mapping

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.evaluation import EvaluationMetrics, evaluate_rank_ic
from factor_miner.schema import (
    CampaignSpec,
    ExpectedSign,
    OrthogonalizationPolicySpec,
)
from factor_miner.statistics import HacInference, hac_mean_test


class DailyOrthogonalization(BaseModel):
    """单个交易日的截面正交化诊断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    total_eligible_count: int = Field(ge=0)
    complete_case_count: int = Field(ge=0)
    reference_count: int = Field(ge=1)
    reference_coverage: FiniteFloat = Field(ge=0, le=1)
    design_rank: int = Field(ge=0)
    condition_number: FiniteFloat = Field(ge=1)
    r_squared: FiniteFloat = Field(ge=0, le=1)
    residual_variance_ratio: FiniteFloat = Field(ge=0, le=1)


class OrthogonalizationSummary(BaseModel):
    """可见区间正交化的完整日期级与聚合摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis_ids: tuple[str, ...] = Field(min_length=1)
    daily: tuple[DailyOrthogonalization, ...] = Field(min_length=1)
    valid_dates: int = Field(ge=1)
    median_reference_coverage: FiniteFloat = Field(ge=0, le=1)
    median_r_squared: FiniteFloat = Field(ge=0, le=1)
    median_residual_variance_ratio: FiniteFloat = Field(ge=0, le=1)


class IncrementalInformationResult(BaseModel):
    """残差预测力及其全部增量准入条件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    orthogonalization: OrthogonalizationSummary
    residual_evaluation: EvaluationMetrics
    residual_inference: HacInference
    expected_sign: ExpectedSign
    variance_passed: bool
    direction_passed: bool
    effect_size_passed: bool
    significance_passed: bool
    passed: bool
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrthogonalizedFactor:
    """不含 outcome 的残差面板及其可序列化诊断。"""

    frame: pl.DataFrame
    summary: OrthogonalizationSummary


def orthogonalize_candidate(
    candidate_frame: pl.DataFrame,
    reference_frames: Mapping[str, pl.DataFrame],
    eligibility_frame: pl.DataFrame,
    campaign: CampaignSpec,
    policy: OrthogonalizationPolicySpec,
) -> OrthogonalizedFactor:
    """在每天股票截面上将候选排名投影到冻结参考排名空间。

    本函数不接受标签或未来收益，因此正交残差不能随 outcome 改变。
    """

    if not reference_frames:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "联合正交化的参考因子集合不能为空",
        )
    _validate_panel(candidate_frame, "raw_factor", "候选")
    _validate_panel(
        eligibility_frame,
        campaign.rank_mask_column,
        "股票池",
        require_numeric=False,
    )
    basis_ids = tuple(reference_frames)
    if len(set(basis_ids)) != len(basis_ids) or any(
        not value.strip() for value in basis_ids
    ):
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "参考因子 ID 必须非空且唯一",
        )
    for reference_id, frame in reference_frames.items():
        _validate_panel(frame, "raw_factor", reference_id)

    visible_eligibility = (
        eligibility_frame.filter(
            pl.col("date").is_between(
                campaign.visible_start,
                campaign.visible_end,
                closed="both",
            )
            & (pl.col(campaign.rank_mask_column) == True)
        )
        .select(["date", "asset"])
        .sort(["date", "asset"])
    )
    joined = visible_eligibility.join(
        candidate_frame.select(["date", "asset", "raw_factor"]).rename(
            {"raw_factor": "_candidate"}
        ),
        on=["date", "asset"],
        how="left",
        validate="1:1",
    )
    reference_columns: list[str] = []
    for index, (reference_id, frame) in enumerate(reference_frames.items()):
        column = f"_reference_{index}"
        reference_columns.append(column)
        joined = joined.join(
            frame.select(["date", "asset", "raw_factor"]).rename(
                {"raw_factor": column}
            ),
            on=["date", "asset"],
            how="left",
            validate="1:1",
        )

    dates = joined.get_column("date").unique().sort().to_list()
    if not dates:
        raise FactorMinerError(
            FailureCode.ORTHOGONALIZATION_COVERAGE_LOW,
            "可见区间没有可用于正交化的时点正确股票",
        )
    residual_frames: list[pl.DataFrame] = []
    daily: list[DailyOrthogonalization] = []
    required_names = max(
        campaign.min_names_per_date,
        len(reference_columns)
        + int(policy.include_intercept)
        + policy.min_cross_sectional_excess_names,
    )
    finite_columns = ["_candidate", *reference_columns]
    for current in dates:
        day = joined.filter(pl.col("date") == current)
        complete = day.filter(
            pl.all_horizontal(
                [
                    pl.col(column).is_finite().fill_null(False)
                    for column in finite_columns
                ]
            )
        )
        total_count = day.height
        complete_count = complete.height
        coverage = complete_count / total_count if total_count else 0.0
        if (
            coverage < policy.min_reference_coverage
            or complete_count < required_names
        ):
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_COVERAGE_LOW,
                f"{current.isoformat()}正交化完整交集不足："
                f"coverage={coverage:.6f}, names={complete_count}, "
                f"required_names={required_names}",
            )

        candidate_values = _rank_standardize(
            complete.get_column("_candidate"),
            current,
            "候选",
        )
        reference_values = np.column_stack(
            [
                _rank_standardize(
                    complete.get_column(column),
                    current,
                    basis_ids[index],
                )
                for index, column in enumerate(reference_columns)
            ]
        )
        design = (
            np.column_stack([np.ones(complete_count, dtype=float), reference_values])
            if policy.include_intercept
            else reference_values
        )
        expected_rank = design.shape[1]
        design_rank = int(np.linalg.matrix_rank(design))
        if design_rank != expected_rank:
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_RANK_DEFICIENT,
                f"{current.isoformat()}参考设计矩阵秩亏："
                f"rank={design_rank}, columns={expected_rank}",
            )
        condition_number = float(np.linalg.cond(design))
        if (
            not math.isfinite(condition_number)
            or condition_number > policy.max_condition_number
        ):
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
                f"{current.isoformat()}参考设计矩阵条件数超过冻结阈值："
                f"{condition_number}",
            )
        try:
            coefficients, _, fitted_rank, _ = np.linalg.lstsq(
                design,
                candidate_values,
                rcond=None,
            )
            residual = candidate_values - design @ coefficients
        except np.linalg.LinAlgError as error:
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
                f"{current.isoformat()}截面 OLS 失败",
            ) from error
        if int(fitted_rank) != expected_rank or not np.isfinite(residual).all():
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
                f"{current.isoformat()}截面 OLS 返回非法秩或非有限残差",
            )
        total_sum_squares = float(np.square(candidate_values).sum())
        residual_sum_squares = float(np.square(residual).sum())
        if total_sum_squares <= 0 or not math.isfinite(residual_sum_squares):
            raise FactorMinerError(
                FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
                f"{current.isoformat()}候选方差或残差平方和非法",
            )
        residual_ratio = _unit_interval(residual_sum_squares / total_sum_squares)
        r_squared = _unit_interval(1.0 - residual_ratio)
        residual_frames.append(
            complete.select(["date", "asset"]).with_columns(
                pl.Series("orthogonal_residual", residual, dtype=pl.Float64)
            )
        )
        daily.append(
            DailyOrthogonalization(
                date=current,
                total_eligible_count=total_count,
                complete_case_count=complete_count,
                reference_count=len(reference_columns),
                reference_coverage=coverage,
                design_rank=design_rank,
                condition_number=condition_number,
                r_squared=r_squared,
                residual_variance_ratio=residual_ratio,
            )
        )

    frame = pl.concat(residual_frames).sort(["date", "asset"])
    summary = OrthogonalizationSummary(
        basis_ids=basis_ids,
        daily=tuple(daily),
        valid_dates=len(daily),
        median_reference_coverage=float(
            median(item.reference_coverage for item in daily)
        ),
        median_r_squared=float(median(item.r_squared for item in daily)),
        median_residual_variance_ratio=float(
            median(item.residual_variance_ratio for item in daily)
        ),
    )
    return OrthogonalizedFactor(frame=frame, summary=summary)


def evaluate_incremental_information(
    orthogonalized: OrthogonalizedFactor,
    outcome_and_mask_frame: pl.DataFrame,
    campaign: CampaignSpec,
    expected_sign: ExpectedSign,
    policy: OrthogonalizationPolicySpec,
) -> IncrementalInformationResult:
    """使用冻结 RankIC 与 HAC/Bonferroni 口径评价正交残差。"""

    _validate_panel(
        orthogonalized.frame,
        "orthogonal_residual",
        "正交残差",
    )
    _validate_panel(
        outcome_and_mask_frame,
        campaign.label_column,
        "评价结果",
    )
    _validate_panel(
        outcome_and_mask_frame,
        campaign.rank_mask_column,
        "评价股票池",
        require_numeric=False,
    )
    evaluation_frame = (
        outcome_and_mask_frame.select(
            [
                "date",
                "asset",
                campaign.rank_mask_column,
                campaign.label_column,
            ]
        )
        .join(
            orthogonalized.frame.select(
                ["date", "asset", "orthogonal_residual"]
            ),
            on=["date", "asset"],
            how="left",
            validate="1:1",
        )
        .sort(["date", "asset"])
    )
    evaluation = evaluate_rank_ic(
        evaluation_frame,
        campaign,
        factor_column="orthogonal_residual",
    )
    if evaluation.valid_dates < campaign.min_valid_dates:
        raise FactorMinerError(
            FailureCode.INSUFFICIENT_VALID_DATES,
            "有效残差 RankIC 日期不足",
        )
    if evaluation.median_coverage < campaign.min_median_coverage:
        raise FactorMinerError(
            FailureCode.FACTOR_COVERAGE_TOO_LOW,
            "残差 RankIC 覆盖率低于冻结政策",
        )
    inference = hac_mean_test(evaluation.rank_ic_values, campaign)
    mean_rank_ic = inference.mean
    variance_passed = (
        orthogonalized.summary.median_residual_variance_ratio
        >= policy.min_median_residual_variance_ratio
    )
    direction_passed = (
        mean_rank_ic > 0
        if expected_sign is ExpectedSign.POSITIVE
        else mean_rank_ic < 0
    )
    effect_size_passed = (
        abs(mean_rank_ic) >= policy.min_abs_mean_residual_rank_ic
    )
    significance_passed = inference.bonferroni_p_value <= campaign.alpha
    failure_reasons: list[str] = []
    if not variance_passed:
        failure_reasons.append("残差独立方差低于冻结门槛")
    if not direction_passed:
        failure_reasons.append("残差 RankIC 方向与事前假设不一致")
    if not effect_size_passed:
        failure_reasons.append("残差 RankIC 效果量低于冻结门槛")
    if not significance_passed:
        failure_reasons.append("残差 RankIC 未通过 HAC 与 Bonferroni 显著性门槛")
    passed = (
        variance_passed
        and direction_passed
        and effect_size_passed
        and significance_passed
    )
    return IncrementalInformationResult(
        orthogonalization=orthogonalized.summary,
        residual_evaluation=evaluation,
        residual_inference=inference,
        expected_sign=expected_sign,
        variance_passed=variance_passed,
        direction_passed=direction_passed,
        effect_size_passed=effect_size_passed,
        significance_passed=significance_passed,
        passed=passed,
        failure_reasons=tuple(failure_reasons),
    )


def _validate_panel(
    frame: pl.DataFrame,
    value_column: str,
    name: str,
    *,
    require_numeric: bool = True,
) -> None:
    """校验日期、证券、值列和主键唯一性。"""

    missing = {"date", "asset", value_column} - set(frame.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            f"{name}面板缺少列：{sorted(missing)}",
        )
    if frame.schema.get("date") != pl.Date:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            f"{name}面板 date 必须是 Date",
        )
    if require_numeric and not frame.schema[value_column].is_numeric():
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            f"{name}面板 {value_column} 必须是数值列",
        )
    duplicates = frame.group_by(["date", "asset"]).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            f"{name}面板存在重复 date/asset 主键",
        )


def _rank_standardize(series: pl.Series, current: date, name: str) -> np.ndarray:
    """计算平均秩并使用样本标准差标准化。"""

    ranks = series.rank(method="average").cast(pl.Float64).to_numpy()
    standard_deviation = float(np.std(ranks, ddof=1))
    if not math.isfinite(standard_deviation) or standard_deviation <= 0:
        raise FactorMinerError(
            FailureCode.ORTHOGONALIZATION_RANK_DEFICIENT,
            f"{current.isoformat()}的{name}为常数或无法排名",
        )
    standardized = (ranks - float(np.mean(ranks))) / standard_deviation
    if not np.isfinite(standardized).all():
        raise FactorMinerError(
            FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
            f"{current.isoformat()}的{name}排名标准化产生非有限值",
        )
    return standardized


def _unit_interval(value: float) -> float:
    """消除浮点舍入造成的极小区间越界。"""

    if not math.isfinite(value):
        raise FactorMinerError(
            FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
            "正交化比例不是有限数字",
        )
    tolerance = 1e-12
    if value < -tolerance or value > 1.0 + tolerance:
        raise FactorMinerError(
            FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
            f"正交化比例超出 [0, 1]：{value}",
        )
    return min(1.0, max(0.0, value))
