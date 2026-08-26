"""逐因子对计算每日 RankIC 信号模式关系。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
import math

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from scipy.stats import spearmanr

from factor_miner.coverage_schema import CoverageFactorNode, CoveragePairPolicy
from factor_miner.errors import FactorMinerError, FailureCode


_DAILY_IC_COLUMNS = {
    "factor_id",
    "date",
    "rank_ic",
    "coverage",
    "evaluation_policy_id",
    "data_release_id",
    "label_id",
    "visible_start",
    "visible_end",
}


class DailyICIdentity(BaseModel):
    """一条因子 IC 序列不可混用的评价身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_policy_id: str
    data_release_id: str
    label_id: str
    visible_start: date
    visible_end: date


class SignalPatternEdge(BaseModel):
    """一个无序因子对独立计算的每日 IC 关系。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_a: str
    factor_b: str
    identity_a: DailyICIdentity
    identity_b: DailyICIdentity
    evaluation_policy_id: str | None = None
    data_release_id: str | None = None
    label_id: str | None = None
    visible_start: date | None = None
    visible_end: date | None = None
    raw_correlation: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    aligned_correlation: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    absolute_correlation: FiniteFloat | None = Field(default=None, ge=0, le=1)
    overlap_dates: int = Field(ge=0)
    union_dates: int = Field(ge=0)
    missing_ratio: FiniteFloat = Field(ge=0, le=1)
    comparable: bool
    strong_signal_edge: bool
    reason: str | None = None


PairComparator = Callable[
    [
        pl.DataFrame,
        pl.DataFrame,
        CoverageFactorNode,
        CoverageFactorNode,
        CoveragePairPolicy,
    ],
    SignalPatternEdge,
]


def compare_daily_ic_pair(
    factor_a_series: pl.DataFrame,
    factor_b_series: pl.DataFrame,
    node_a: CoverageFactorNode,
    node_b: CoverageFactorNode,
    policy: CoveragePairPolicy,
) -> SignalPatternEdge:
    """只对传入的两个因子执行日期交集、缺失和 Spearman 计算。"""

    first, identity_a = _validate_one_series(
        factor_a_series,
        node_a.factor_id,
    )
    second, identity_b = _validate_one_series(
        factor_b_series,
        node_b.factor_id,
    )
    union_dates = len(
        set(first.get_column("date").to_list())
        | set(second.get_column("date").to_list())
    )
    joined = (
        first.select(["date", "rank_ic"])
        .rename({"rank_ic": "_rank_ic_a"})
        .join(
            second.select(["date", "rank_ic"]).rename(
                {"rank_ic": "_rank_ic_b"}
            ),
            on="date",
            how="inner",
            validate="1:1",
        )
        .filter(
            pl.col("_rank_ic_a").is_finite().fill_null(False)
            & pl.col("_rank_ic_b").is_finite().fill_null(False)
        )
        .sort("date")
    )
    overlap_dates = joined.height
    missing_ratio = (
        1.0 - overlap_dates / union_dates
        if union_dates
        else 1.0
    )
    common = identity_a == identity_b
    base = {
        "factor_a": min(node_a.factor_id, node_b.factor_id),
        "factor_b": max(node_a.factor_id, node_b.factor_id),
        "identity_a": identity_a,
        "identity_b": identity_b,
        "evaluation_policy_id": (
            identity_a.evaluation_policy_id if common else None
        ),
        "data_release_id": identity_a.data_release_id if common else None,
        "label_id": identity_a.label_id if common else None,
        "visible_start": identity_a.visible_start if common else None,
        "visible_end": identity_a.visible_end if common else None,
        "overlap_dates": overlap_dates,
        "union_dates": union_dates,
        "missing_ratio": _unit_interval(missing_ratio),
    }
    if not common:
        return _not_comparable(base, "因子对评价身份不兼容")
    if overlap_dates < policy.min_overlap_dates:
        return _not_comparable(base, "因子对重叠日期少于冻结阈值")
    if missing_ratio > policy.max_missing_ratio:
        return _not_comparable(base, "因子对缺失比例超过冻结阈值")
    values_a = joined.get_column("_rank_ic_a").to_numpy()
    values_b = joined.get_column("_rank_ic_b").to_numpy()
    if len(set(values_a.tolist())) < 2 or len(set(values_b.tolist())) < 2:
        return _not_comparable(base, "因子对至少一条 IC 序列为常数")
    result = spearmanr(values_a, values_b)
    raw = float(result.statistic)
    if not math.isfinite(raw):
        return _not_comparable(base, "因子对 Spearman 相关不是有限数")
    aligned = raw * node_a.orientation_sign * node_b.orientation_sign
    raw = _correlation_bound(raw)
    aligned = _correlation_bound(aligned)
    return SignalPatternEdge(
        **base,
        raw_correlation=raw,
        aligned_correlation=aligned,
        absolute_correlation=abs(raw),
        comparable=True,
        strong_signal_edge=aligned >= policy.strong_signal_correlation,
        reason=None,
    )


def build_signal_pattern_edges(
    nodes: Sequence[CoverageFactorNode],
    daily_ic: pl.DataFrame,
    policy: CoveragePairPolicy,
    *,
    comparator: PairComparator = compare_daily_ic_pair,
) -> tuple[SignalPatternEdge, ...]:
    """按因子 ID 生成无序组合，并且每个组合只调用一次单对函数。"""

    ordered = tuple(sorted(nodes, key=lambda item: item.factor_id))
    if len({node.factor_id for node in ordered}) != len(ordered):
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "覆盖节点存在重复 factor_id",
        )
    factor_ids = set(daily_ic.get_column("factor_id").unique().to_list())
    missing_factors = {
        node.factor_id for node in ordered
    } - factor_ids
    if missing_factors:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"每日 IC 缺少因子：{sorted(missing_factors)}",
        )
    series_by_factor = {
        node.factor_id: daily_ic.filter(
            pl.col("factor_id") == node.factor_id
        )
        for node in ordered
    }
    edges: list[SignalPatternEdge] = []
    for first_index, first_node in enumerate(ordered):
        first_series = series_by_factor[first_node.factor_id]
        for second_node in ordered[first_index + 1 :]:
            second_series = series_by_factor[second_node.factor_id]
            edges.append(
                comparator(
                    first_series,
                    second_series,
                    first_node,
                    second_node,
                    policy,
                )
            )
    return tuple(edges)


def _validate_one_series(
    frame: pl.DataFrame,
    expected_factor_id: str,
) -> tuple[pl.DataFrame, DailyICIdentity]:
    """校验单因子长序列和唯一评价身份。"""

    missing = _DAILY_IC_COLUMNS - set(frame.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"每日 IC 缺少列：{sorted(missing)}",
        )
    if frame.schema.get("date") != pl.Date:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每日 IC date 必须是 Date 类型",
        )
    factor_ids = frame.get_column("factor_id").unique().to_list()
    if factor_ids != [expected_factor_id]:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"单因子 IC 序列与节点不一致：{factor_ids}",
        )
    duplicate = frame.group_by(["factor_id", "date"]).len().filter(
        pl.col("len") > 1
    )
    if duplicate.height:
        raise FactorMinerError(
            FailureCode.COVERAGE_PAIR_INVALID,
            f"{expected_factor_id} 每日 IC 存在重复 factor_id/date",
        )
    identity_columns = [
        "evaluation_policy_id",
        "data_release_id",
        "label_id",
        "visible_start",
        "visible_end",
    ]
    identities = frame.select(identity_columns).unique()
    if identities.height != 1:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"{expected_factor_id} 每日 IC 内部评价身份不唯一",
        )
    identity = DailyICIdentity.model_validate(identities.row(0, named=True))
    if identity.visible_end < identity.visible_start:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"{expected_factor_id} 每日 IC 可见区间倒置",
        )
    return frame.sort("date"), identity


def _not_comparable(
    base: dict[str, object],
    reason: str,
) -> SignalPatternEdge:
    """构造保留诊断且绝不填零的不可比较边。"""

    return SignalPatternEdge(
        **base,
        comparable=False,
        strong_signal_edge=False,
        reason=reason,
    )


def _unit_interval(value: float) -> float:
    """消除浮点边界的微小越界。"""

    return min(1.0, max(0.0, float(value)))


def _correlation_bound(value: float) -> float:
    """将有限相关系数限制到数学允许区间。"""

    return min(1.0, max(-1.0, float(value)))
