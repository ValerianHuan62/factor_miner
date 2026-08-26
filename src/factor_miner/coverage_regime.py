"""使用下一交易日可得的 filtered probability 描述因子状态表现。"""

from __future__ import annotations

from collections.abc import Sequence
import math
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.coverage_schema import (
    CoverageFactorNode,
    CoverageRegimePolicy,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_schema import RegimeAnnotation


class RegimeFactorProfile(BaseModel):
    """一个因子在一个中文命名状态下的概率加权 IC 摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_id: str
    status: Literal["ok", "unavailable"] = "ok"
    reason: str | None = None
    canonical_state_id: int = Field(ge=0)
    economic_label: str
    annotation_description: str
    valid_dates: int = Field(ge=0)
    probability_mass: FiniteFloat = Field(ge=0)
    weighted_mean_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    weighted_std_rank_ic: FiniteFloat | None = Field(default=None, ge=0)
    weighted_icir: FiniteFloat | None = None
    positive_probability_ratio: FiniteFloat | None = Field(default=None, ge=0, le=1)
    mean_max_probability: FiniteFloat | None = Field(default=None, ge=0, le=1)
    mean_normalized_entropy: FiniteFloat | None = Field(default=None, ge=0, le=1)


def build_regime_profiles(
    nodes: Sequence[CoverageFactorNode],
    daily_ic: pl.DataFrame,
    filtered_regimes: pl.DataFrame,
    regime_snapshot_id: str,
    annotations: Sequence[RegimeAnnotation],
    policy: CoverageRegimePolicy,
) -> tuple[RegimeFactorProfile, ...]:
    """按 earliest_use_date 对齐并生成每因子、每状态画像。"""

    annotation_by_state = _validate_annotations(
        regime_snapshot_id,
        annotations,
    )
    state_count = len(annotation_by_state)
    regimes = _validate_filtered_regimes(
        filtered_regimes,
        state_count,
        policy,
    )
    required_ic = {"factor_id", "date", "rank_ic", "coverage"}
    missing_ic = required_ic - set(daily_ic.columns)
    if missing_ic or daily_ic.schema.get("date") != pl.Date:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"状态画像每日 IC 合同非法，缺列：{sorted(missing_ic)}",
        )
    earliest_min = regimes.get_column("earliest_use_date").min()
    earliest_max = regimes.get_column("earliest_use_date").max()
    profiles: list[RegimeFactorProfile] = []
    for node in sorted(nodes, key=lambda item: item.factor_id):
        factor_ic = daily_ic.filter(
            (pl.col("factor_id") == node.factor_id)
            & pl.col("rank_ic").is_finite().fill_null(False)
            & pl.col("date").is_between(
                earliest_min,
                earliest_max,
                closed="both",
            )
        )
        if factor_ic.is_empty():
            profiles.extend(
                _unavailable_profiles(
                    node,
                    annotation_by_state,
                    "没有有效每日 IC",
                )
            )
            continue
        joined = factor_ic.join(
            regimes,
            left_on="date",
            right_on="earliest_use_date",
            how="left",
            validate="m:1",
        )
        missing_dates = joined.filter(pl.col("status").is_null())
        if missing_dates.height:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                f"{node.factor_id} 的状态日历在快照区间存在缺口",
            )
        unavailable = joined.filter(pl.col("status") != "ok")
        if unavailable.height:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                f"{node.factor_id} 对齐到 unavailable 状态日期",
            )
        rows = joined.select(
            [
                "rank_ic",
                "canonical_state_probabilities",
                "max_probability",
                "normalized_entropy",
            ]
        ).to_dicts()
        for state_id, annotation in sorted(annotation_by_state.items()):
            profiles.append(
                _one_profile(
                    node,
                    state_id,
                    annotation,
                    rows,
                    policy,
                )
            )
    return tuple(profiles)


def _validate_annotations(
    regime_snapshot_id: str,
    annotations: Sequence[RegimeAnnotation],
) -> dict[int, RegimeAnnotation]:
    """状态必须有且只有一条属于本快照的人工中文注释。"""

    if any(
        annotation.regime_snapshot_id != regime_snapshot_id
        for annotation in annotations
    ):
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "状态人工注释引用了错误快照",
        )
    by_state = {
        annotation.canonical_state_id: annotation
        for annotation in annotations
    }
    if len(by_state) != len(annotations) or not by_state:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每个状态必须有唯一人工中文注释",
        )
    return by_state


def _validate_filtered_regimes(
    frame: pl.DataFrame,
    state_count: int,
    policy: CoverageRegimePolicy,
) -> pl.DataFrame:
    """核对日表字段、唯一日期、概率维度与概率和。"""

    if any("smoothed" in column for column in frame.columns):
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "覆盖图谱禁止读取 smoothed 状态概率",
        )
    required = {
        "observation_date",
        "earliest_use_date",
        "status",
        "canonical_state_probabilities",
        "max_probability",
        "normalized_entropy",
    }
    missing = required - set(frame.columns)
    if (
        missing
        or frame.schema.get("observation_date") != pl.Date
        or frame.schema.get("earliest_use_date") != pl.Date
    ):
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"filtered 状态日表合同非法，缺列：{sorted(missing)}",
        )
    duplicate = frame.group_by("earliest_use_date").len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "filtered 状态日表 earliest_use_date 重复",
        )
    for row in frame.filter(pl.col("status") == "ok").select(
        ["canonical_state_probabilities"]
    ).to_dicts():
        probabilities = row["canonical_state_probabilities"]
        if probabilities is None or len(probabilities) != state_count:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                "filtered 状态概率维度与人工中文注释不一致",
            )
        values = [float(value) for value in probabilities]
        if (
            any(not math.isfinite(value) or value < 0 or value > 1 for value in values)
            or not math.isclose(
                sum(values),
                1.0,
                rel_tol=0.0,
                abs_tol=policy.probability_tolerance,
            )
        ):
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                "filtered 状态概率非法",
            )
    return frame.sort("earliest_use_date")


def _one_profile(
    node: CoverageFactorNode,
    state_id: int,
    annotation: RegimeAnnotation,
    rows: Sequence[dict[str, object]],
    policy: CoverageRegimePolicy,
) -> RegimeFactorProfile:
    """计算一个因子和一个状态的概率加权统计。"""

    weighted: list[tuple[float, float, float, float]] = []
    for row in rows:
        probabilities = row["canonical_state_probabilities"]
        if not isinstance(probabilities, list):
            probabilities = list(probabilities)  # type: ignore[arg-type]
        weight = float(probabilities[state_id])
        if weight <= 0:
            continue
        rank_ic = float(row["rank_ic"]) * node.orientation_sign
        weighted.append(
            (
                weight,
                rank_ic,
                float(row["max_probability"]),
                float(row["normalized_entropy"]),
            )
        )
    mass = sum(item[0] for item in weighted)
    if (
        len(weighted) < policy.min_valid_dates_per_state
        or mass < policy.min_probability_mass
    ):
        return RegimeFactorProfile(
            factor_id=node.factor_id,
            status="unavailable",
            reason="状态概率质量不足",
            canonical_state_id=state_id,
            economic_label=annotation.economic_label,
            annotation_description=annotation.description,
            valid_dates=len(weighted),
            probability_mass=mass,
        )
    mean_ic = sum(weight * value for weight, value, _, _ in weighted) / mass
    variance = (
        sum(weight * (value - mean_ic) ** 2 for weight, value, _, _ in weighted)
        / mass
    )
    std_ic = math.sqrt(max(0.0, variance))
    positive_ratio = (
        sum(weight for weight, value, _, _ in weighted if value > 0) / mass
    )
    mean_max_probability = (
        sum(weight * maximum for weight, _, maximum, _ in weighted) / mass
    )
    mean_entropy = (
        sum(weight * entropy for weight, _, _, entropy in weighted) / mass
    )
    return RegimeFactorProfile(
        factor_id=node.factor_id,
        canonical_state_id=state_id,
        economic_label=annotation.economic_label,
        annotation_description=annotation.description,
        valid_dates=len(weighted),
        probability_mass=mass,
        weighted_mean_rank_ic=mean_ic,
        weighted_std_rank_ic=std_ic,
        weighted_icir=mean_ic / std_ic if std_ic > 0 else None,
        positive_probability_ratio=positive_ratio,
        mean_max_probability=mean_max_probability,
        mean_normalized_entropy=mean_entropy,
    )


def _unavailable_profiles(
    node: CoverageFactorNode,
    annotations: dict[int, RegimeAnnotation],
    reason: str,
) -> tuple[RegimeFactorProfile, ...]:
    """为没有可计算 IC 的节点保留每个中文状态占位画像。"""

    return tuple(
        RegimeFactorProfile(
            factor_id=node.factor_id,
            status="unavailable",
            reason=reason,
            canonical_state_id=state_id,
            economic_label=annotation.economic_label,
            annotation_description=annotation.description,
            valid_dates=0,
            probability_mass=0.0,
        )
        for state_id, annotation in sorted(annotations.items())
    )
