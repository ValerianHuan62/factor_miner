"""服务器侧 outcome 标签与 V0.4 每日 IC 长表构建。"""

from __future__ import annotations

from collections.abc import Sequence
import math

import polars as pl
from scipy.stats import spearmanr

from factor_miner.coverage_schema import CoverageFactorNode
from factor_miner.coverage_signal import DailyICIdentity
from factor_miner.errors import FactorMinerError, FailureCode


def build_visible_o2o_5d_labels(adjusted_bars: pl.DataFrame) -> pl.DataFrame:
    """在明确的 outcome 层计算 open(t+6)/open(t+1)-1。

    本函数有意读取未来开盘价，因此只能用于可见标签派生，绝不能进入因子
    计算、市场状态推断或交易时点之前的工具层。
    """

    required = {"date", "asset", "adj_open"}
    missing = required - set(adjusted_bars.columns)
    if missing or adjusted_bars.schema.get("date") != pl.Date:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"可见标签行情合同非法，缺列：{sorted(missing)}",
        )
    _reject_duplicate_keys(adjusted_bars, "可见标签行情")
    ordered = adjusted_bars.sort(["asset", "date"])
    return (
        ordered.with_columns(
            (
                pl.col("adj_open").shift(-6).over("asset")
                / pl.col("adj_open").shift(-1).over("asset")
                - 1.0
            ).alias("label_o2o_5d")
        )
        .with_columns(
            pl.when(pl.col("label_o2o_5d").is_finite())
            .then(pl.col("label_o2o_5d"))
            .otherwise(None)
            .alias("label_o2o_5d")
        )
        .select(["date", "asset", "label_o2o_5d"])
        .sort(["date", "asset"])
    )


def build_daily_ic_long(
    raw_factor_wide: pl.DataFrame,
    state: pl.DataFrame,
    labels: pl.DataFrame,
    nodes: Sequence[CoverageFactorNode],
    identity: DailyICIdentity,
    *,
    min_names_per_date: int,
) -> pl.DataFrame:
    """逐因子、逐日期计算截面 Spearman 并返回长表。"""

    if min_names_per_date < 2:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每日 IC 最小股票数不能小于 2",
        )
    for frame, name, required in (
        (raw_factor_wide, "原始因子", {"date", "asset"}),
        (state, "股票状态", {"date", "asset", "valid_for_factor_rank"}),
        (labels, "可见标签", {"date", "asset", "label_o2o_5d"}),
    ):
        missing = required - set(frame.columns)
        if missing or frame.schema.get("date") != pl.Date:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                f"{name}合同非法，缺列：{sorted(missing)}",
            )
        _reject_duplicate_keys(frame, name)
    factor_ids = tuple(sorted(node.factor_id for node in nodes))
    missing_factors = set(factor_ids) - set(raw_factor_wide.columns)
    if missing_factors:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"原始因子宽表缺少列：{sorted(missing_factors)}",
        )
    eligible = (
        state.filter(pl.col("valid_for_factor_rank") == True)
        .select(["date", "asset"])
        .filter(
            pl.col("date").is_between(
                identity.visible_start,
                identity.visible_end,
                closed="both",
            )
        )
    )
    totals = {
        row["date"]: row["len"]
        for row in eligible.group_by("date").len().to_dicts()
    }
    outcome = eligible.join(
        labels.select(["date", "asset", "label_o2o_5d"]),
        on=["date", "asset"],
        how="left",
        validate="1:1",
    ).join(
        raw_factor_wide.select(["date", "asset", *factor_ids]),
        on=["date", "asset"],
        how="left",
        validate="1:1",
    )
    rows: list[dict[str, object]] = []
    for node in sorted(nodes, key=lambda item: item.factor_id):
        factor_panel = outcome.select(
            ["date", "asset", "label_o2o_5d", node.factor_id]
        ).sort(["date", "asset"])
        by_date = factor_panel.partition_by(
            "date",
            as_dict=True,
            maintain_order=True,
        )
        for current in sorted(totals):
            day = by_date[(current,)]
            complete = day.filter(
                pl.col(node.factor_id).is_finite().fill_null(False)
                & pl.col("label_o2o_5d").is_finite().fill_null(False)
            )
            value: float | None = None
            if (
                complete.height >= min_names_per_date
                and complete.get_column(node.factor_id).n_unique() >= 2
                and complete.get_column("label_o2o_5d").n_unique() >= 2
            ):
                statistic = float(
                    spearmanr(
                        complete.get_column(node.factor_id).to_numpy(),
                        complete.get_column("label_o2o_5d").to_numpy(),
                    ).statistic
                )
                if math.isfinite(statistic):
                    value = min(1.0, max(-1.0, statistic))
            rows.append(
                {
                    "factor_id": node.factor_id,
                    "date": current,
                    "rank_ic": value,
                    "coverage": complete.height / totals[current],
                    "evaluation_policy_id": identity.evaluation_policy_id,
                    "data_release_id": identity.data_release_id,
                    "label_id": identity.label_id,
                    "visible_start": identity.visible_start,
                    "visible_end": identity.visible_end,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "factor_id": pl.String,
            "date": pl.Date,
            "rank_ic": pl.Float64,
            "coverage": pl.Float64,
            "evaluation_policy_id": pl.String,
            "data_release_id": pl.String,
            "label_id": pl.String,
            "visible_start": pl.Date,
            "visible_end": pl.Date,
        },
    ).sort(["factor_id", "date"])


def _reject_duplicate_keys(frame: pl.DataFrame, name: str) -> None:
    """所有面板必须拥有唯一 date/asset 主键。"""

    duplicate = frame.group_by(["date", "asset"]).len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"{name}存在重复 date/asset",
        )
