"""从本次冻结每日 IC 重算因子历史表现摘要。"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import mean, median, stdev

import polars as pl
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.coverage_schema import CoverageFactorNode
from factor_miner.errors import FactorMinerError, FailureCode


class AnnualFactorPerformance(BaseModel):
    """单个自然年度的因子 IC 摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    year: int
    valid_dates: int = Field(ge=1)
    mean_rank_ic: FiniteFloat = Field(ge=-1, le=1)
    std_rank_ic: FiniteFloat | None = Field(default=None, ge=0)
    icir: FiniteFloat | None = None
    positive_rank_ic_ratio: FiniteFloat = Field(ge=0, le=1)
    median_coverage: FiniteFloat = Field(ge=0, le=1)


class FactorPerformanceSummary(BaseModel):
    """图谱节点在统一评价身份下的当前历史表现。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_id: str
    status: Literal["ok", "unavailable"] = "ok"
    reason: str | None = None
    total_dates: int = Field(ge=1)
    valid_dates: int = Field(ge=0)
    invalid_dates: int = Field(ge=0)
    mean_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    std_rank_ic: FiniteFloat | None = Field(default=None, ge=0)
    icir: FiniteFloat | None = None
    positive_rank_ic_ratio: FiniteFloat | None = Field(default=None, ge=0, le=1)
    negative_rank_ic_ratio: FiniteFloat | None = Field(default=None, ge=0, le=1)
    zero_rank_ic_ratio: FiniteFloat | None = Field(default=None, ge=0, le=1)
    median_coverage: FiniteFloat | None = Field(default=None, ge=0, le=1)
    legacy_mean_ic: FiniteFloat | None = None
    legacy_icir: FiniteFloat | None = None
    legacy_mean_ic_difference: FiniteFloat | None = None
    legacy_icir_difference: FiniteFloat | None = None
    annual_summaries: tuple[AnnualFactorPerformance, ...] = ()


def summarize_factor_performance(
    nodes: Sequence[CoverageFactorNode],
    daily_ic: pl.DataFrame,
) -> tuple[FactorPerformanceSummary, ...]:
    """逐因子汇总当前冻结 IC，不采信 YAML 中旧的结果摘要。"""

    required = {"factor_id", "date", "rank_ic", "coverage"}
    missing = required - set(daily_ic.columns)
    if missing or daily_ic.schema.get("date") != pl.Date:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"因子表现每日 IC 合同非法，缺列：{sorted(missing)}",
        )
    duplicate = daily_ic.group_by(["factor_id", "date"]).len().filter(
        pl.col("len") > 1
    )
    if duplicate.height:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "因子表现每日 IC 存在重复 factor_id/date",
        )
    summaries: list[FactorPerformanceSummary] = []
    for node in sorted(nodes, key=lambda item: item.factor_id):
        frame = daily_ic.filter(pl.col("factor_id") == node.factor_id).sort("date")
        if frame.is_empty():
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                f"因子表现缺少 {node.factor_id}",
            )
        valid = frame.filter(
            pl.col("rank_ic").is_finite().fill_null(False)
            & pl.col("coverage").is_finite().fill_null(False)
        )
        if valid.is_empty():
            summaries.append(
                FactorPerformanceSummary(
                    factor_id=node.factor_id,
                    status="unavailable",
                    reason="没有有效每日 IC",
                    total_dates=frame.height,
                    valid_dates=0,
                    invalid_dates=frame.height,
                    legacy_mean_ic=node.legacy_mean_ic,
                    legacy_icir=node.legacy_icir,
                )
            )
            continue
        values = [float(value) for value in valid.get_column("rank_ic").to_list()]
        coverages = [
            float(value) for value in valid.get_column("coverage").to_list()
        ]
        current_mean = float(mean(values))
        current_std = float(stdev(values)) if len(values) >= 2 else None
        current_icir = (
            current_mean / current_std
            if current_std is not None and current_std > 0
            else None
        )
        annual = tuple(
            _annual_summary(year, group)
            for year, group in valid.with_columns(
                pl.col("date").dt.year().alias("_year")
            ).partition_by("_year", as_dict=True, maintain_order=True).items()
        )
        summaries.append(
            FactorPerformanceSummary(
                factor_id=node.factor_id,
                total_dates=frame.height,
                valid_dates=valid.height,
                invalid_dates=frame.height - valid.height,
                mean_rank_ic=current_mean,
                std_rank_ic=current_std,
                icir=current_icir,
                positive_rank_ic_ratio=_ratio(values, lambda value: value > 0),
                negative_rank_ic_ratio=_ratio(values, lambda value: value < 0),
                zero_rank_ic_ratio=_ratio(values, lambda value: value == 0),
                median_coverage=float(median(coverages)),
                legacy_mean_ic=node.legacy_mean_ic,
                legacy_icir=node.legacy_icir,
                legacy_mean_ic_difference=(
                    current_mean - node.legacy_mean_ic
                    if node.legacy_mean_ic is not None
                    else None
                ),
                legacy_icir_difference=(
                    current_icir - node.legacy_icir
                    if current_icir is not None and node.legacy_icir is not None
                    else None
                ),
                annual_summaries=annual,
            )
        )
    return tuple(summaries)


def _annual_summary(
    year_key: tuple[int, ...],
    frame: pl.DataFrame,
) -> AnnualFactorPerformance:
    """计算一个自然年度的有效 IC 摘要。"""

    values = [float(value) for value in frame.get_column("rank_ic").to_list()]
    coverages = [float(value) for value in frame.get_column("coverage").to_list()]
    current_mean = float(mean(values))
    current_std = float(stdev(values)) if len(values) >= 2 else None
    return AnnualFactorPerformance(
        year=int(year_key[0]),
        valid_dates=len(values),
        mean_rank_ic=current_mean,
        std_rank_ic=current_std,
        icir=(
            current_mean / current_std
            if current_std is not None and current_std > 0
            else None
        ),
        positive_rank_ic_ratio=_ratio(values, lambda value: value > 0),
        median_coverage=float(median(coverages)),
    )


def _ratio(values: Sequence[float], predicate) -> float:
    """返回满足条件的观测比例。"""

    return sum(bool(predicate(value)) for value in values) / len(values)
