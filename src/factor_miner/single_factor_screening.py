"""固定方向的周频单因子统计与经济门槛。"""

from __future__ import annotations

from datetime import date
import math
from statistics import mean, median, stdev
from typing import Sequence

import polars as pl

from factor_miner.rolling_ridge import RollingRidgePolicy, top_n_metrics
from factor_miner.statistics import FrozenStatisticalPolicy, hac_mean_test_frozen


def _correlations(frame: pl.DataFrame, signal: str) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for day in frame.partition_by("date", maintain_order=True):
        complete = day.drop_nulls([signal, "label_raw"])
        if complete.height < 50:
            continue
        pearson = complete.select(pl.corr(signal, "label_raw", method="pearson")).item()
        rank_ic = complete.select(pl.corr(signal, "label_raw", method="spearman")).item()
        if all(value is not None and math.isfinite(float(value)) for value in (pearson, rank_ic)):
            rows.append({"date": day.item(0, "date"), "ic": float(pearson), "rank_ic": float(rank_ic), "coverage": complete.height / day.height})
    return pl.DataFrame(rows, schema={"date": pl.Date, "ic": pl.Float64, "rank_ic": pl.Float64, "coverage": pl.Float64})


def _summary(
    panel: pl.DataFrame,
    signal: str,
    *,
    family_size: int,
    cost: float,
) -> dict[str, object]:
    correlations = _correlations(panel, signal)
    ic = correlations.get_column("ic").to_list()
    rank_ic = correlations.get_column("rank_ic").to_list()
    if len(rank_ic) < 2:
        raise ValueError("单因子有效周数不足")
    inference = hac_mean_test_frozen(
        rank_ic,
        FrozenStatisticalPolicy(
            policy_id="factor-screening-protocol-v1",
            alpha=0.05,
            hac_max_lags=5,
            min_valid_dates=1,
            bonferroni_denominator=family_size,
            multiplicity_policy="bonferroni_over_frozen_family_budget",
        ),
    )
    predictions = panel.select("date", "code", "label_raw", pl.col(signal).alias("prediction"))
    weekly, portfolio = top_n_metrics(
        predictions,
        RollingRidgePolicy(cost_per_l1_turnover=cost),
    )
    annual_rank = correlations.with_columns(pl.col("date").dt.year().alias("year")).group_by("year").agg(pl.col("rank_ic").mean())
    annual_returns = weekly.with_columns(pl.col("date").dt.year().alias("year")).group_by("year").agg(
        ((pl.col("net_return") + 1.0).product() - 1.0).alias("net_return")
    )
    deciles = (
        panel.drop_nulls([signal, "label_raw"])
        .with_columns(
            (((pl.col(signal).rank("average").over("date") - 1.0) * 10.0 / pl.len().over("date")).floor() + 1.0)
            .clip(1, 10).alias("decile")
        )
        .group_by("decile").agg(pl.col("label_raw").mean().alias("return"))
    )
    monotonicity = deciles.select(pl.corr("decile", "return", method="spearman")).item() if deciles.height >= 5 else None
    positive_annual = [max(0.0, float(value)) for value in annual_returns.get_column("net_return").to_list()]
    profit_concentration = max(positive_annual) / sum(positive_annual) if sum(positive_annual) > 0 else 1.0
    return {
        "valid_weeks": len(rank_ic),
        "median_coverage": median(correlations.get_column("coverage").to_list()),
        "ic_mean": mean(ic),
        "rank_ic_mean": mean(rank_ic),
        "ic_std": stdev(ic),
        "rank_ic_std": stdev(rank_ic),
        "ic_ir": mean(ic) / stdev(ic) if stdev(ic) else None,
        "rank_ic_ir": mean(rank_ic) / stdev(rank_ic) if stdev(rank_ic) else None,
        "rank_ic_hac_t": inference.t_value,
        "rank_ic_hac_p": inference.raw_p_value,
        "rank_ic_bonferroni_p": inference.bonferroni_p_value,
        "positive_years_rank_ic": int(annual_rank.get_column("rank_ic").gt(0).sum()),
        "positive_years_return": int(annual_returns.get_column("net_return").gt(0).sum()),
        "decile_monotonicity": float(monotonicity) if monotonicity is not None else None,
        "profit_concentration": profit_concentration,
        **portfolio,
    }


def screen_single_factors(
    panel: pl.DataFrame,
    business_ids: Sequence[str],
    *,
    family_size: int = 367,
    discovery_start: date = date(2021, 1, 1),
    discovery_end: date = date(2023, 12, 31),
    confirmation_start: date = date(2024, 1, 1),
    confirmation_end: date = date(2026, 6, 30),
) -> pl.DataFrame:
    """应用固定发现期与确认期硬门槛，不按目标数量补位。"""

    rows: list[dict[str, object]] = []
    for business_id in business_ids:
        columns = [f"fm_{business_id}__mean5", f"fm_{business_id}__last"]
        if any(column not in panel.columns for column in columns):
            rows.append({"business_id": business_id, "final_status": "D级拒绝", "failure_reasons": "周频面板缺少因子列"})
            continue
        signal = f"_signal_{business_id}"
        factor_panel = panel.select("date", "code", "label_raw", ((pl.col(columns[0]) + pl.col(columns[1])) / 2).alias(signal))
        develop_panel = factor_panel.filter(pl.col("date").is_between(discovery_start, discovery_end))
        confirm_panel = factor_panel.filter(pl.col("date").is_between(confirmation_start, confirmation_end))
        if _correlations(develop_panel, signal).height < 2:
            rows.append({"business_id": business_id, "final_status": "D级拒绝", "failure_reasons": "发现期:有效周数不足"})
            continue
        if _correlations(confirm_panel, signal).height < 2:
            rows.append({"business_id": business_id, "final_status": "D级拒绝", "failure_reasons": "确认期:有效周数不足"})
            continue
        develop = _summary(develop_panel, signal, family_size=family_size, cost=0.0014)
        confirm = _summary(confirm_panel, signal, family_size=family_size, cost=0.0014)
        confirm_double = _summary(confirm_panel, signal, family_size=family_size, cost=0.0028)
        discovery_gates = {
            "coverage": float(develop["median_coverage"]) >= 0.85,
            "coverage_weeks": int(develop["valid_weeks"]) >= 130,
            "ic_mean": float(develop["ic_mean"]) >= 0.010,
            "rank_ic_mean": float(develop["rank_ic_mean"]) >= 0.015,
            "ic_ir": develop["ic_ir"] is not None and float(develop["ic_ir"]) >= 0.08,
            "rank_ic_ir": develop["rank_ic_ir"] is not None and float(develop["rank_ic_ir"]) >= 0.10,
            "bonferroni": float(develop["rank_ic_bonferroni_p"]) <= 0.05,
            "positive_years": int(develop["positive_years_rank_ic"]) >= 2,
            "ic_rankic_same_direction": float(develop["ic_mean"]) > 0 and float(develop["rank_ic_mean"]) > 0,
        }
        discovery_rank = float(develop["rank_ic_mean"])
        retention = float(confirm["rank_ic_mean"]) / discovery_rank if discovery_rank > 0 else -math.inf
        confirmation_statistical_gates = {
            "coverage": float(confirm["median_coverage"]) >= 0.85,
            "ic_mean": float(confirm["ic_mean"]) > 0,
            "rank_ic_mean": float(confirm["rank_ic_mean"]) >= 0.010,
            "rank_ic_retention": retention >= 0.5,
            "positive_years_rank_ic": int(confirm["positive_years_rank_ic"]) >= 2,
        }
        economic_gates = {
            "net_annual_return": float(confirm["annualized_return"]) > 0,
            "double_cost_annual_return": float(confirm_double["annualized_return"]) > 0,
            "net_sharpe": float(confirm["sharpe"]) >= 0.5,
            "positive_years_return": int(confirm["positive_years_return"]) >= 2,
            "decile_monotonicity": confirm["decile_monotonicity"] is not None and float(confirm["decile_monotonicity"]) >= 0.60,
            "turnover": float(confirm["mean_l1_turnover"]) <= 1.50,
            "profit_concentration": float(confirm["profit_concentration"]) <= 0.60,
        }
        statistical_failures = [f"发现期:{key}" for key, passed in discovery_gates.items() if not passed]
        statistical_failures.extend(
            f"确认期:{key}" for key, passed in confirmation_statistical_gates.items() if not passed
        )
        economic_warnings = [f"经济警告:{key}" for key, passed in economic_gates.items() if not passed]
        failures = [*statistical_failures, *economic_warnings]
        if statistical_failures:
            final_status = "D级拒绝"
        elif economic_warnings:
            final_status = "统计候选"
        else:
            final_status = "统计与经济候选"
        rows.append(
            {
                "business_id": business_id,
                "final_status": final_status,
                "failure_reasons": "；".join(failures),
                "statistical_failure_reasons": "；".join(statistical_failures),
                "economic_warning_reasons": "；".join(economic_warnings),
                **{f"discovery_{key}": value for key, value in develop.items()},
                **{f"confirmation_{key}": value for key, value in confirm.items()},
                "confirmation_double_cost_annualized_return": confirm_double["annualized_return"],
                "rank_ic_retention": retention,
            }
        )
    return pl.DataFrame(rows).sort("business_id")
