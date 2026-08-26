"""把完整交易日分钟路径确定性聚合为日级截面字段。"""

from __future__ import annotations

from datetime import time

import polars as pl

from factor_miner.intraday_schema import IntradaySourceSpec


_INPUT_COLUMNS = {
    "asset",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_turnover",
    "num_trades",
}

_FEATURE_COLUMNS = (
    "intraday_open_30m_return",
    "intraday_close_30m_amount_share",
    "intraday_realized_volatility",
    "intraday_close_vwap_deviation",
)


def aggregate_intraday_day(
    frame: pl.DataFrame,
    spec: IntradaySourceSpec,
) -> pl.DataFrame:
    """聚合分钟路径；不完整、重复或越界交易日的信号保持 NULL。"""

    missing = _INPUT_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError(f"分钟聚合输入缺少字段：{sorted(missing)}")
    if frame.schema.get("datetime") != pl.Datetime:
        raise ValueError("分钟聚合 datetime 必须为 Polars Datetime")
    if frame.height == 0:
        raise ValueError("分钟聚合输入不能为空")

    ordered = (
        frame.sort(["asset", "datetime"])
        .with_columns(
            pl.col("datetime").dt.date().alias("date"),
            pl.col("datetime").dt.time().alias("minute_time"),
        )
        .with_columns(
            (
                pl.col("minute_time").is_between(
                    time(9, 31), time(11, 30), closed="both"
                )
                | pl.col("minute_time").is_between(
                    time(13, 1), time(15, 0), closed="both"
                )
            ).alias("valid_session_minute"),
            (pl.col("close") / pl.col("close").shift(1).over(["asset", "date"]))
            .log()
            .alias("minute_log_return"),
        )
    )
    grouped = ordered.group_by(["date", "asset"], maintain_order=True).agg(
        pl.len().alias("minute_count"),
        pl.col("datetime").n_unique().alias("unique_minute_count"),
        pl.col("valid_session_minute").all().alias("all_minutes_in_session"),
        pl.col("minute_time").first().alias("first_minute"),
        pl.col("minute_time").last().alias("last_minute"),
        pl.col("open").first().alias("day_first_open"),
        pl.col("close")
        .filter(pl.col("minute_time") <= time(10, 0))
        .last()
        .alias("open_30m_close"),
        pl.col("total_turnover")
        .filter(pl.col("minute_time") >= time(14, 31))
        .sum()
        .alias("close_30m_turnover"),
        pl.col("total_turnover").sum().alias("day_turnover"),
        pl.col("volume").sum().alias("day_volume"),
        pl.col("close").last().alias("day_last_close"),
        pl.col("minute_log_return").pow(2).sum().sqrt().alias("realized_volatility"),
    )
    complete = (
        (pl.col("minute_count") == spec.expected_minutes_per_complete_day)
        & (pl.col("unique_minute_count") == spec.expected_minutes_per_complete_day)
        & pl.col("all_minutes_in_session")
        & (pl.col("first_minute") == time(9, 31))
        & (pl.col("last_minute") == time(15, 0))
    )
    result = grouped.with_columns(
        complete.alias("is_complete"),
        pl.when(complete)
        .then(pl.lit("分钟数据完整"))
        .otherwise(pl.lit("分钟数据不完整"))
        .alias("completeness_status"),
    ).with_columns(
        pl.when(pl.col("is_complete") & (pl.col("day_first_open") != 0))
        .then(pl.col("open_30m_close") / pl.col("day_first_open") - 1.0)
        .otherwise(None)
        .alias("intraday_open_30m_return"),
        pl.when(pl.col("is_complete") & (pl.col("day_turnover") != 0))
        .then(pl.col("close_30m_turnover") / pl.col("day_turnover"))
        .otherwise(None)
        .alias("intraday_close_30m_amount_share"),
        pl.when(pl.col("is_complete"))
        .then(pl.col("realized_volatility"))
        .otherwise(None)
        .alias("intraday_realized_volatility"),
        pl.when(
            pl.col("is_complete")
            & (pl.col("day_volume") != 0)
            & (pl.col("day_turnover") != 0)
        )
        .then(
            pl.col("day_last_close")
            / (pl.col("day_turnover") / pl.col("day_volume"))
            - 1.0
        )
        .otherwise(None)
        .alias("intraday_close_vwap_deviation"),
    )
    return result.select(
        "date",
        "asset",
        "completeness_status",
        *_FEATURE_COLUMNS,
    ).sort(["date", "asset"])
