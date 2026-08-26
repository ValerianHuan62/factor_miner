"""组合评价所需因子、开盘价和主键的数据合同。"""

from __future__ import annotations

from datetime import date

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.trading_schedule import RebalanceWindow


def _data_error(message: str) -> FactorMinerError:
    """构造组合数据合同错误。"""

    return FactorMinerError(FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID, message)


def _require_columns(frame: pl.LazyFrame, required: set[str], name: str) -> None:
    """在不收集完整数据的情况下检查列合同。"""

    missing = required.difference(frame.collect_schema().names())
    if missing:
        raise _data_error(f"{name} 缺少字段：{sorted(missing)}")


def _reject_duplicate_keys(
    frame: pl.LazyFrame,
    keys: list[str],
    name: str,
) -> None:
    """以聚合探针拒绝重复主键。"""

    duplicate = (
        frame.group_by(keys)
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
        .collect()
    )
    if duplicate.height:
        raise _data_error(f"{name} 主键重复：{keys}")


def _schedule_frame(schedule: tuple[RebalanceWindow, ...]) -> pl.DataFrame:
    """把不可变窗口转换为小型确定性连接表。"""

    if not schedule:
        raise _data_error("调仓窗口不能为空")
    return pl.DataFrame(
        {
            "signal_date": [item.signal_date for item in schedule],
            "entry_date": [item.entry_date for item in schedule],
            "exit_date": [item.exit_date for item in schedule],
        }
    )


def align_open_to_open_panel(
    factor_panel: pl.LazyFrame,
    market_panel: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.LazyFrame:
    """将信号、入场开盘和退出开盘对齐成唯一 open-to-open 面板。

    因子面板合同：`signal_date`, `security_id`, `factor_value`。
    行情面板合同：`trade_date`, `security_id`, `open`。
    """

    _require_columns(
        factor_panel,
        {"signal_date", "security_id", "factor_value"},
        "factor_panel",
    )
    _require_columns(
        market_panel,
        {"trade_date", "security_id", "open"},
        "market_panel",
    )
    _reject_duplicate_keys(
        factor_panel,
        ["signal_date", "security_id"],
        "factor_panel",
    )
    _reject_duplicate_keys(
        market_panel,
        ["trade_date", "security_id"],
        "market_panel",
    )
    market = market_panel.filter(pl.col("open").is_not_null() & (pl.col("open") > 0))
    schedule_frame = _schedule_frame(schedule).lazy()
    factor = factor_panel.join(schedule_frame, on="signal_date", how="inner")
    entry_market = market.rename(
        {"trade_date": "entry_date", "open": "entry_open"}
    )
    exit_market = market.select(
        "security_id",
        pl.col("trade_date").alias("exit_price_date"),
        pl.col("open").alias("exit_open"),
    ).sort(["security_id", "exit_price_date"])
    entered = factor.join(
            entry_market,
            on=["entry_date", "security_id"],
            how="left",
    )
    if entered.filter(pl.col("entry_open").is_null()).limit(1).collect().height:
        raise _data_error("open-to-open 面板缺少入场开盘价")
    aligned = entered.sort(["security_id", "exit_date"]).join_asof(
        exit_market,
        left_on="exit_date",
        right_on="exit_price_date",
        by="security_id",
        strategy="backward",
    )
    if (
        aligned.filter(
            pl.col("exit_open").is_null()
            | (pl.col("exit_price_date") < pl.col("entry_date"))
        )
        .limit(1)
        .collect()
        .height
    ):
        raise _data_error("open-to-open 面板缺少退出日前可见估值价格")
    return aligned.with_columns(
        (pl.col("exit_open") / pl.col("entry_open") - 1.0).alias("asset_return")
    )
