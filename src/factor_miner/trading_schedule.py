"""周二调仓和 open-to-open 窗口的确定性交易日对齐。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from pydantic import BaseModel, ConfigDict, model_validator

from factor_miner.errors import FactorMinerError, FailureCode


class RebalanceWindow(BaseModel):
    """一个完整的信号、入场和退出窗口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_date: date
    entry_date: date
    exit_date: date

    @model_validator(mode="after")
    def validate_order(self) -> RebalanceWindow:
        """信号必须早于入场，入场必须早于退出。"""

        if not self.signal_date < self.entry_date < self.exit_date:
            raise ValueError("RebalanceWindow 日期顺序必须为 signal < entry < exit")
        return self


def _schedule_error(message: str) -> FactorMinerError:
    """构造交易日历合同错误。"""

    return FactorMinerError(FailureCode.CALENDAR_CONTRACT_INVALID, message)


def build_month_end_target_schedule(
    days: list[date], signal_start: date, signal_end: date, liquidation_date: date,
) -> tuple[RebalanceWindow, ...]:
    """月末收盘形成信号，次一市场日交易差额；最终清仓日必须事前明确。"""
    if not days or days != sorted(set(days)) or any(type(d) is not date for d in days):
        raise _schedule_error("市场日历必须为非空、有序且唯一的日期")
    if signal_start > signal_end or liquidation_date not in days:
        raise _schedule_error("信号区间或最终清仓日不合法")
    last = {(d.year, d.month): d for d in days}
    signals = [d for d in last.values() if signal_start <= d <= signal_end]
    positions = {d: i for i, d in enumerate(days)}
    if not signals or any(positions[d]+1 >= len(days) for d in signals):
        raise _schedule_error("没有完整月末信号或缺少下一市场日")
    entries = [days[positions[d]+1] for d in signals]
    if liquidation_date <= entries[-1]:
        raise _schedule_error("最终清仓日必须晚于最后一次入场")
    exits = [*entries[1:], liquidation_date]
    return tuple(RebalanceWindow(signal_date=s, entry_date=e, exit_date=x)
                 for s, e, x in zip(signals, entries, exits, strict=True))


def _calendar_dates(calendar: pl.DataFrame) -> tuple[date, ...]:
    """读取带 open 标志的真实交易日日历。"""

    required = {"trade_date", "is_open"}
    missing = required.difference(calendar.columns)
    if missing:
        raise _schedule_error("交易日日历缺少 trade_date 或 is_open")
    if calendar.select(pl.col("trade_date").is_duplicated().any()).item():
        raise _schedule_error("交易日日历 trade_date 重复")
    if calendar.schema["trade_date"] != pl.Date:
        raise _schedule_error("交易日日历 trade_date 必须为 Date")
    if calendar.schema["is_open"] != pl.Boolean:
        raise _schedule_error("交易日日历 is_open 必须为 Boolean")
    open_dates = (
        calendar.filter(pl.col("is_open"))
        .select("trade_date")
        .to_series()
        .to_list()
    )
    dates = tuple(sorted(open_dates))
    if len(dates) < 3:
        raise _schedule_error("交易日日历有效交易日不足")
    return dates


def build_tuesday_rebalance_schedule(
    calendar: pl.DataFrame,
    visible_start: date,
    visible_end: date,
) -> tuple[RebalanceWindow, ...]:
    """按实际日历生成周二调仓窗口，周二休市则顺延到下一开放日。

    `visible_start` 和 `visible_end` 约束入场日；为避免截断 open-to-open
    收益，退出日也必须落在可见区间内。
    """

    if visible_start >= visible_end:
        raise _schedule_error("可见区间必须正向")
    open_dates = _calendar_dates(calendar)
    open_set = set(open_dates)
    entries: list[date] = []
    days_until_tuesday = (1 - visible_start.weekday()) % 7
    tuesday = visible_start + timedelta(days=days_until_tuesday)
    while tuesday <= visible_end:
        if tuesday in open_set:
            entry = tuesday
        else:
            next_tuesday = tuesday + timedelta(days=7)
            same_week_open = [
                item
                for item in open_dates
                if tuesday < item < next_tuesday
            ]
            if not same_week_open:
                tuesday += timedelta(days=7)
                continue
            entry = same_week_open[0]
        if visible_start <= entry <= visible_end:
            entries.append(entry)
        tuesday += timedelta(days=7)

    entries = sorted(set(entries))
    windows: list[RebalanceWindow] = []
    for index in range(len(entries) - 1):
        entry = entries[index]
        exit_date = entries[index + 1]
        prior = [item for item in open_dates if item < entry]
        if not prior:
            continue
        windows.append(
            RebalanceWindow(
                signal_date=prior[-1],
                entry_date=entry,
                exit_date=exit_date,
            )
        )
    return tuple(windows)
