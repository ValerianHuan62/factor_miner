"""固定交易日日历标签与跨分区事件区间清除。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode


@dataclass(frozen=True, slots=True)
class LabelEventPurgeAudit:
    """记录一次标签事件区间 purge 的样本变化。"""

    next_split_start: date
    rows_before: int
    rows_after: int
    removed_rows: int
    removed_signal_dates: int


def _label_error(message: str) -> FactorMinerError:
    """构造标签数据合同错误。"""

    return FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, message)


def build_fixed_session_o2o_labels(
    market_panel: pl.DataFrame | pl.LazyFrame,
    *,
    entry_offset_sessions: int = 1,
    holding_sessions: int = 5,
    calendar: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """按统一市场交易日历构造固定入场至指定期限退出的开盘收益。

    参数：
        market_panel: 包含 date、asset、open 的市场面板。
        entry_offset_sessions: 信号日到入场日的交易日偏移。
        holding_sessions: 入场到计划退出之间的交易日数。
    返回值：
        与行情主键完全一致的标签表，包含入场日、退出日和原始 O2O 标签。
    """

    if entry_offset_sessions < 1 or holding_sessions < 1:
        raise _label_error("标签 entry_offset_sessions 和 holding_sessions 必须为正整数")
    market = market_panel.collect() if isinstance(market_panel, pl.LazyFrame) else market_panel
    required = {"date", "asset", "open"}
    missing = required.difference(market.columns)
    if missing:
        raise _label_error(f"标签行情缺少字段：{sorted(missing)}")
    if market.schema.get("date") != pl.Date:
        raise _label_error("标签行情 date 必须是 Date 类型")
    if market.select(pl.struct(["date", "asset"]).is_duplicated().any()).item():
        raise _label_error("标签行情 date/asset 主键重复")

    if calendar is None:
        sessions = market.get_column("date").unique().sort().to_list()
    else:
        if calendar.schema.get('date') != pl.Date or calendar['date'].null_count() or calendar.is_empty():
            raise _label_error('显式日历为空或日期类型错误')
        sessions=calendar['date'].to_list()
        if sessions!=sorted(set(sessions)) or set(market['date'])-set(sessions):
            raise _label_error('显式日历重复、乱序或不覆盖行情')
    label_column=f'label_o2o_{holding_sessions}d'
    exit_offset = entry_offset_sessions + holding_sessions
    schedule_rows: list[dict[str, date | None]] = []
    for index, signal_date in enumerate(sessions):
        entry_index = index + entry_offset_sessions
        exit_index = index + exit_offset
        schedule_rows.append(
            {
                "date": signal_date,
                "label_entry_date": sessions[entry_index] if entry_index < len(sessions) else None,
                "label_exit_date": sessions[exit_index] if exit_index < len(sessions) else None,
            }
        )
    schedule = pl.DataFrame(
        schedule_rows,
        schema={
            "date": pl.Date,
            "label_entry_date": pl.Date,
            "label_exit_date": pl.Date,
        },
    )
    prices = market.select("date", "asset", pl.col("open").cast(pl.Float64))
    entry_prices = prices.rename(
        {"date": "label_entry_date", "open": "label_entry_open"}
    )
    exit_prices = prices.rename(
        {"date": "label_exit_date", "open": "label_exit_open"}
    )
    labeled = (
        market.select("date", "asset")
        .join(schedule, on="date", how="left", validate="m:1")
        .join(entry_prices, on=["label_entry_date", "asset"], how="left", validate="m:1")
        .join(exit_prices, on=["label_exit_date", "asset"], how="left", validate="m:1")
        .with_columns(
            pl.when(
                pl.col("label_entry_open").is_finite()
                & pl.col("label_exit_open").is_finite()
                & (pl.col("label_entry_open") > 0)
                & (pl.col("label_exit_open") > 0)
            )
            .then(pl.col("label_exit_open") / pl.col("label_entry_open") - 1.0)
            .otherwise(None)
            .alias(label_column)
        )
        .select(
            "date",
            "asset",
            "label_entry_date",
            "label_exit_date",
            label_column,
        )
        .sort(["date", "asset"])
    )
    if labeled.height != market.height:
        raise _label_error("固定日历标签行数与行情不一致")
    return labeled


def purge_label_event_overlap(
    panel: pl.DataFrame | pl.LazyFrame,
    *,
    next_split_start: date,
    signal_date_column: str = "date",
    label_exit_date_column: str = "label_exit_date",
) -> tuple[pl.DataFrame, LabelEventPurgeAudit]:
    """删除标签退出事件进入下一分区的监督样本。"""

    data = panel.collect() if isinstance(panel, pl.LazyFrame) else panel
    required = {signal_date_column, label_exit_date_column}
    missing = required.difference(data.columns)
    if missing:
        raise _label_error(f"事件区间 purge 缺少字段：{sorted(missing)}")
    if data.schema.get(signal_date_column) != pl.Date or data.schema.get(label_exit_date_column) != pl.Date:
        raise _label_error("事件区间 purge 的信号日和标签退出日必须是 Date 类型")
    before = data.height
    removed = data.filter(
        pl.col(label_exit_date_column).is_null()
        | (pl.col(label_exit_date_column) >= next_split_start)
    )
    kept = data.filter(
        pl.col(label_exit_date_column).is_not_null()
        & (pl.col(label_exit_date_column) < next_split_start)
    )
    return kept, LabelEventPurgeAudit(
        next_split_start=next_split_start,
        rows_before=before,
        rows_after=kept.height,
        removed_rows=removed.height,
        removed_signal_dates=removed.get_column(signal_date_column).n_unique(),
    )
