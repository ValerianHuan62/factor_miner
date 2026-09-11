"""只用发现期筛选候选，冻结等权排名组合；不按确认收益调参。"""

from __future__ import annotations

from pathlib import Path
import polars as pl


def select_strategy_factors(reports: list[dict], redundant: set[int]) -> list[dict]:
    """参数为已登记发现期报告与冗余槽，返回最多三个不同假设的合格候选。"""
    eligible = [report for index, report in enumerate(reports)
        if index not in redundant
        and report["construct_validation"]["status"] == "基础检验符合"
        and report["direction"] == report["hypothesis_direction"]
        and report["discovery"]["bonferroni_p_value"] < .05
        and abs(report["discovery"]["rank_ic_mean"]) >= .01
        and report["discovery"]["median_coverage"] >= .8]
    ordered = sorted(eligible, key=lambda report: (-abs(report["discovery"]["rank_ic_hac_t"]), report["slot"]))
    selected: list[dict] = []
    for report in ordered:
        if report["hypothesis_id"] not in {item["hypothesis_id"] for item in selected}:
            selected.append(report)
        if len(selected) == 3:
            break
    return selected


def composite_rank_panel(selected: list[dict], state: pl.LazyFrame) -> pl.LazyFrame:
    """参数为冻结候选与信号日状态，返回完整交集内的等权截面百分位排名。"""
    if not selected:
        raise ValueError("没有达到发现期门槛的候选，不强行构造策略")
    panel = state.filter(pl.col("valid_for_factor_rank")).select("date", "asset")
    names = []
    for index, report in enumerate(selected):
        name = f"score_{index}"
        names.append(name)
        oriented = pl.scan_parquet(Path(report["folder"]) / "raw_factor.parquet").filter(
            pl.col("valid_for_factor_compute") & pl.col("raw_factor").is_finite()
        ).select("date", "asset", (pl.col("raw_factor") * (1 if report["direction"] == "positive" else -1)).alias(name))
        panel = panel.join(oriented, on=["date", "asset"], how="inner", validate="1:1")
    panel = panel.with_columns(*((pl.col(name).rank(method="average").over("date") / pl.len().over("date")).alias(name) for name in names))
    return panel.select("date", "asset", pl.mean_horizontal(*names).alias("raw_factor"), pl.lit(True).alias("valid_for_factor_compute"))
