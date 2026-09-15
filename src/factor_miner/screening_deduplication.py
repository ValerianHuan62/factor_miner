"""固定单因子门槛通过后的相关簇代表选择。"""

from __future__ import annotations

import polars as pl


def select_cluster_representatives(
    screened: pl.DataFrame,
    clusters: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """按确认 RankIC、IC、HAC、Sharpe、换手的冻结顺序每簇留一个。"""

    required_screened = {
        "business_id", "final_status", "confirmation_rank_ic_mean",
        "confirmation_ic_mean", "discovery_rank_ic_hac_t",
        "confirmation_sharpe", "confirmation_mean_l1_turnover",
    }
    required_clusters = {"business_id", "cluster_id"}
    if missing := required_screened.difference(screened.columns):
        raise ValueError(f"单因子判定缺少字段：{sorted(missing)}")
    if missing := required_clusters.difference(clusters.columns):
        raise ValueError(f"聚类判定缺少字段：{sorted(missing)}")
    eligible = screened.filter(pl.col("final_status").is_in(["统计候选", "统计与经济候选"]))
    joined = eligible.join(
        clusters.select("business_id", "cluster_id"),
        on="business_id",
        how="left",
        validate="1:1",
    )
    if joined.filter(pl.col("cluster_id").is_null()).height:
        raise ValueError("至少一个通过候选缺少相关簇")
    ranked = joined.sort(
        [
            "cluster_id", "confirmation_rank_ic_mean", "confirmation_ic_mean",
            "discovery_rank_ic_hac_t", "confirmation_sharpe",
            "confirmation_mean_l1_turnover", "business_id",
        ],
        descending=[False, True, True, True, True, False, False],
    ).with_columns(
        pl.int_range(pl.len()).over("cluster_id").add(1).alias("cluster_rank")
    )
    representatives = ranked.filter(pl.col("cluster_rank") == 1).sort(
        ["confirmation_rank_ic_mean", "business_id"], descending=[True, False]
    )
    return representatives, ranked
