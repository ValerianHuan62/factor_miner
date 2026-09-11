"""报告级价格连续性警报：留证并限制结论，不事后删除持仓或猜测价格。"""

from __future__ import annotations

import polars as pl


def audit_price_continuity(market: pl.LazyFrame, state: pl.LazyFrame | None = None,
                           accepted_jumps: list[dict[str, object]] | None = None) -> dict[str, object]:
    """检测相邻可见报价超过十倍的变化；长时间无报价也保留供身份核查。"""
    frames = []
    for field in ("open", "close"):
        if field not in market.collect_schema():
            continue
        frames.append(market.filter(pl.col(field).is_finite() & (pl.col(field) > 0)).sort("asset", "date").with_columns(
            pl.col(field).shift(1).over("asset").alias("previous_price"),
            pl.col("date").shift(1).over("asset").alias("previous_date"),
        ).with_columns((pl.col(field) / pl.col("previous_price")).alias("ratio"))
            .filter(pl.col("ratio").is_finite() & ((pl.col("ratio") > 5) | (pl.col("ratio") < .2)))
            .select("asset", "date", "previous_date", "previous_price", pl.col(field).alias("price"), "ratio", pl.lit(field).alias("price_field")))
    jumps = pl.concat(frames).sort("ratio", descending=True)
    if state is not None:
        flags = ("valid_for_factor_compute", "valid_for_factor_rank", "can_open_long", "can_close_long", "valid_for_o2o_label")
        jumps = jumps.join(state.select("date", "asset", pl.any_horizontal(*(pl.col(x) for x in flags)).alias("usable")), on=["date", "asset"], how="left", validate="m:1")
    else:
        jumps = jumps.with_columns(pl.lit(True).alias("usable"))
    evidence = jumps.collect().to_dicts()
    decisions = {(str(item["security_id"]), str(item["session_date"])): item for item in accepted_jumps or []}
    for row in evidence:
        decision = decisions.get((str(row["asset"]), str(row["date"])))
        expected = decision.get(str(row["price_field"]) + "_ratio") if decision else None
        row["reviewed_real_jump"] = expected is not None and abs(float(row["ratio"]) / float(expected) - 1) <= .005
    unresolved = sum(row["usable"] is not False and not row["reviewed_real_jump"] for row in evidence)
    return {
        "version": "price-continuity-l2-reviewed-v2",
        "status": "review_required" if unresolved else "no_unresolved_eligible_discontinuity",
        "anomaly_count": len(evidence), "eligible_unreviewed_count": unresolved,
        "anomalies": evidence,
        "rule": "开盘与收盘相邻正报价比值大于5或小于0.2；联用L2资格及具体事件和比例的已核实暴跌裁决，不按收益好坏删样本",
        "conclusion": "存在未解释价格断点时，仅供记账诊断，收益和 IC 均不得作为已通过数据门禁的投资结论。未删行、未猜测复权比例。",
    }
