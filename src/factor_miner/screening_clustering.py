"""通过固定门槛候选的逐日截面相关聚类。"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform


def cluster_daily_factors(
    daily_panels: tuple[pl.LazyFrame, ...],
    business_ids: tuple[str, ...],
    *,
    similarity_threshold: float = 0.75,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """按 max(abs(Pearson), abs(日截面 Spearman)) 做 complete linkage。"""

    if not daily_panels or not business_ids:
        raise ValueError("日频面板和候选不能为空")
    joined = daily_panels[0]
    for panel in daily_panels[1:]:
        joined = joined.join(panel, on=["date", "asset"], how="inner", validate="1:1")
    columns = [f"fm_{business_id}" for business_id in business_ids]
    if missing := set(columns).difference(joined.collect_schema().names()):
        raise ValueError(f"日频面板缺少通过候选：{sorted(missing)}")
    if len(business_ids) == 1:
        identity = pl.DataFrame({"business_id": business_ids, business_ids[0]: [1.0]})
        mapping = pl.DataFrame({"business_id": business_ids, "cluster_id": [1]})
        return mapping, identity, identity.clone()
    data = joined.select("date", *columns).collect()
    pearson_values = data.select(columns).fill_null(0.0).to_numpy()
    pearson = np.nan_to_num(np.corrcoef(pearson_values, rowvar=False), nan=0.0)
    ranked = data.select(
        "date", *[pl.col(column).rank("average").over("date").alias(column) for column in columns]
    ).with_columns(
        [
            ((pl.col(column) - pl.col(column).mean().over("date")) / (pl.col(column).std(ddof=0).over("date") + 1e-8))
            .fill_null(0.0).alias(column)
            for column in columns
        ]
    )
    spearman_values = ranked.select(columns).to_numpy()
    spearman = np.nan_to_num(np.corrcoef(spearman_values, rowvar=False), nan=0.0)
    similarity = np.maximum(np.abs(pearson), np.abs(spearman))
    similarity = (similarity + similarity.T) / 2.0
    np.fill_diagonal(similarity, 1.0)
    distance = np.clip(1.0 - similarity, 0.0, 1.0)
    np.fill_diagonal(distance, 0.0)
    labels = fcluster(linkage(squareform(distance, checks=True), method="complete"), t=1.0 - similarity_threshold, criterion="distance")
    mapping = pl.DataFrame({"business_id": business_ids, "cluster_id": labels.tolist()}).sort("business_id")
    pearson_frame = pl.DataFrame(pearson, schema=list(business_ids)).insert_column(0, pl.Series("business_id", business_ids))
    spearman_frame = pl.DataFrame(spearman, schema=list(business_ids)).insert_column(0, pl.Series("business_id", business_ids))
    return mapping, pearson_frame, spearman_frame
