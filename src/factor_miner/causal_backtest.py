"""跨市场日频开盘价的因果订单与持仓状态机。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import math
from typing import Literal

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.trading_schedule import RebalanceWindow


@dataclass(frozen=True, slots=True)
class CausalExecutionResult:
    """保存冻结选择、订单、持仓和逐交易日收益审计。"""

    daily_returns: tuple[dict[str, object], ...]
    selections: tuple[dict[str, object], ...]
    orders: tuple[dict[str, object], ...]
    holdings_daily: tuple[dict[str, object], ...]
    execution_summary: dict[str, object]
    unresolved_positions: tuple[dict[str, object], ...] = ()
    zero_recovery_daily_returns: tuple[dict[str, object], ...] = ()


@dataclass(slots=True)
class _PositionLot:
    """一笔实际成交且尚未退出的持仓批次。"""

    security_id: str
    signal_date: date
    entry_date: date
    scheduled_exit_date: date
    shares: float
    last_price: float
    target_weight: float
    last_price_date: date | None = None


def _execution_error(message: str) -> FactorMinerError:
    """构造因果回测合同错误。"""

    return FactorMinerError(FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID, message)


def _collect(frame: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    """统一收集 Polars 输入。"""

    return frame.collect() if isinstance(frame, pl.LazyFrame) else frame


def _require_unique(
    frame: pl.DataFrame,
    *,
    required: set[str],
    keys: list[str],
    name: str,
) -> None:
    """检查字段、主键和空主键。"""

    missing = required.difference(frame.columns)
    if missing:
        raise _execution_error(f"{name} 缺少字段：{sorted(missing)}")
    if frame.select(pl.any_horizontal(*(pl.col(key).is_null() for key in keys))).to_series().any():
        raise _execution_error(f"{name} 主键不能包含空值")
    if frame.select(pl.struct(keys).is_duplicated().any()).item():
        raise _execution_error(f"{name} 主键重复：{keys}")


def _prepare_selections(
    factor_panel: pl.DataFrame,
    state_panel: pl.DataFrame,
    schedule: tuple[RebalanceWindow, ...],
    *,
    direction: Literal["positive", "negative"],
    group_count: int,
    allow_empty_signals: bool = False,
) -> tuple[dict[str, object], ...]:
    """仅使用 T 日因子与排名资格冻结极端组列表和目标权重。"""

    schedule_frame = pl.DataFrame(
        {
            "signal_date": [item.signal_date for item in schedule],
            "entry_date": [item.entry_date for item in schedule],
            "scheduled_exit_date": [item.exit_date for item in schedule],
        }
    )
    signal_state = state_panel.select(
        pl.col("trade_date").alias("signal_date"),
        "security_id",
        "valid_for_factor_rank",
    )
    candidates = (
        factor_panel.join(schedule_frame, on="signal_date", how="inner", validate="m:1")
        .join(signal_state, on=["signal_date", "security_id"], how="left", validate="1:1")
    )
    if candidates.filter(pl.col("valid_for_factor_rank").is_null()).height:
        raise _execution_error("T 日候选缺少 valid_for_factor_rank 状态")
    multiplier = 1.0 if direction == "positive" else -1.0
    ranked = (
        candidates.filter(
            pl.col("valid_for_factor_rank")
            & pl.col("factor_value").is_finite()
        )
        .with_columns((pl.col("factor_value") * multiplier).alias("_oriented_factor"))
        .sort(
            ["signal_date", "_oriented_factor", "security_id"],
            descending=[False, True, False],
        )
        .with_columns(
            pl.col("_oriented_factor")
            .rank(method="ordinal", descending=True)
            .over("signal_date")
            .alias("rank"),
            pl.len().over("signal_date").alias("eligible_count"),
        )
        .with_columns(
            (pl.col("eligible_count") / group_count)
            .ceil()
            .cast(pl.Int64)
            .alias("target_count")
        )
    )
    missing_dates = set(item.signal_date for item in schedule).difference(
        ranked.get_column("signal_date").unique().to_list()
    )
    if missing_dates and not allow_empty_signals:
        raise _execution_error(f"信号日没有可排名证券：{sorted(missing_dates)[:5]}")
    if ranked.filter(pl.col("eligible_count") < group_count).height:
        raise _execution_error("某个信号日可排名证券少于分组数")
    selected = (
        ranked.filter(pl.col("rank") <= pl.col("target_count"))
        .with_columns((1.0 / pl.col("target_count")).alias("target_weight"))
        .select(
            "signal_date",
            "entry_date",
            "scheduled_exit_date",
            "security_id",
            "factor_value",
            "rank",
            "eligible_count",
            "target_weight",
        )
        .sort(["signal_date", "rank", "security_id"])
    )
    return tuple(selected.to_dicts())


def simulate_causal_extreme_portfolio(
    factor_panel: pl.DataFrame | pl.LazyFrame,
    market_panel: pl.DataFrame | pl.LazyFrame,
    state_panel: pl.DataFrame | pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    *,
    direction: Literal["positive", "negative"] = "positive",
    group_count: int = 10,
    round_trip_cost_bps: float = 0.0,
    terminal_policy: Literal["fail_closed", "report_unresolved"] = "fail_closed",
    terminal_events: pl.DataFrame | None = None,
    retain_daily_holdings: bool = True,
    allow_noncontiguous_schedule: bool = False,
    allow_empty_signals: bool = False,
    execution_mode: Literal["fixed_horizon", "weekly_target", "target_difference"] = "fixed_horizon",
) -> CausalExecutionResult:
    """先冻结排名，再按 T+1 买入和计划退出后逐日重试卖出。

    参数：
        factor_panel: T 日因子值，字段为 signal_date、security_id、factor_value。
        market_panel: 日频可执行开盘价，字段为 trade_date、security_id、open。
        state_panel: 日频排名和成交状态，包含 valid_for_factor_rank、can_open_long、can_close_long。
        schedule: 冻结的信号、入场和计划退出窗口。
        direction: 因子高值或低值方向。
        group_count: 截面分组数；1 表示选择全部信号日合格证券。
        round_trip_cost_bps: 完整买卖双边成本基点。
        terminal_policy: 数据终点无法退出时，硬失败或显式保留未确定持仓。
        allow_noncontiguous_schedule: 显式允许节假日窗口重叠或间隔；资金仍须实际可用，不延后买入。
        allow_empty_signals: 联合条件策略允许无触发日持现金，原极端组默认仍拒绝空截面。
        execution_mode: target_difference 按冻结日程交易目标差额；weekly_target 保留旧版身份与相同算法。
    返回值：
        可审计的逐日组合结果。
    """

    if direction not in {"positive", "negative"}:
        raise _execution_error("因子方向只能是 positive 或 negative")
    if group_count < 1 or round_trip_cost_bps < 0:
        raise _execution_error("group_count 必须为正整数且成本不能为负")
    if terminal_policy not in {"fail_closed", "report_unresolved"}:
        raise _execution_error("未知终端规则")
    if execution_mode not in {"fixed_horizon", "weekly_target", "target_difference"}:
        raise _execution_error("未知持仓更新规则")
    target_difference = execution_mode in {"weekly_target", "target_difference"}
    settlements: dict[str, list[dict[str, object]]] = {}
    if terminal_events is not None:
        _require_unique(terminal_events, required={"security_id", "event_date", "available_at", "terminal_value", "source"}, keys=["security_id", "event_date"], name="终止结算")
        for event in terminal_events.iter_rows(named=True):
            value = event["terminal_value"]
            if value is None or not math.isfinite(float(value)) or float(value) < 0:
                raise _execution_error("终止价值必须为已核实的非负有限值")
            if not isinstance(event["event_date"], date) or not isinstance(event["available_at"], date):
                raise _execution_error("终止事件必须包含事件日和可得日")
            settlements.setdefault(str(event["security_id"]), []).append(event)
    if not schedule:
        raise _execution_error("因果回测调仓窗口不能为空")
    if any(not w.signal_date < w.entry_date < w.exit_date for w in schedule):
        raise _execution_error("窗口必须先信号、后入场、再退出")
    if any(schedule[i].signal_date <= schedule[i-1].signal_date or schedule[i].entry_date <= schedule[i-1].entry_date or schedule[i].exit_date <= schedule[i-1].exit_date for i in range(1, len(schedule))):
        raise _execution_error("调仓窗口必须严格按时间递增且不能重复")
    if not allow_noncontiguous_schedule and any(schedule[index].entry_date != schedule[index - 1].exit_date for index in range(1, len(schedule))):
        raise _execution_error("因果回测要求相邻窗口首尾连接")

    factor = _collect(factor_panel)
    market = _collect(market_panel)
    state = _collect(state_panel)
    _require_unique(
        factor,
        required={"signal_date", "security_id", "factor_value"},
        keys=["signal_date", "security_id"],
        name="因子面板",
    )
    _require_unique(
        market,
        required={"trade_date", "security_id", "open"},
        keys=["trade_date", "security_id"],
        name="行情面板",
    )
    _require_unique(
        state,
        required={
            "trade_date",
            "security_id",
            "valid_for_factor_rank",
            "can_open_long",
            "can_close_long",
        },
        keys=["trade_date", "security_id"],
        name="状态面板",
    )
    for column in ("valid_for_factor_rank", "can_open_long", "can_close_long"):
        if state.schema.get(column) != pl.Boolean:
            raise _execution_error(f"状态字段 {column} 必须是 Boolean 类型")
    if state.filter(
        pl.any_horizontal(
            pl.col("valid_for_factor_rank").is_null(),
            pl.col("can_open_long").is_null(),
            pl.col("can_close_long").is_null(),
        )
    ).height:
        raise _execution_error("排名与成交状态不能包含空值")

    selections = _prepare_selections(
        factor,
        state,
        schedule,
        direction=direction,
        group_count=group_count,
        allow_empty_signals=allow_empty_signals,
    )
    entries_by_date: dict[date, list[dict[str, object]]] = {}
    for selection in selections:
        entries_by_date.setdefault(selection["entry_date"], []).append(selection)  # type: ignore[arg-type]
    next_decision = {w.entry_date: schedule[i+1].entry_date if i+1 < len(schedule)
                     else w.exit_date for i, w in enumerate(schedule)}

    sessions = tuple(market.get_column("trade_date").unique().sort().to_list())
    session_set = set(sessions)
    for window in schedule:
        if {window.signal_date, window.entry_date, window.exit_date}.difference(session_set):
            raise _execution_error("调仓窗口端点不在统一行情交易日历")
    price_map = {
        (row["trade_date"], str(row["security_id"])): float(row["open"])
        for row in market.select("trade_date", "security_id", "open").iter_rows(named=True)
        if row["open"] is not None and math.isfinite(float(row["open"])) and float(row["open"]) > 0
    }
    open_state = {
        (row["trade_date"], str(row["security_id"])): bool(row["can_open_long"])
        for row in state.select("trade_date", "security_id", "can_open_long").iter_rows(named=True)
    }
    close_state = {
        (row["trade_date"], str(row["security_id"])): bool(row["can_close_long"])
        for row in state.select("trade_date", "security_id", "can_close_long").iter_rows(named=True)
    }
    first_signal = schedule[0].signal_date
    last_scheduled_exit = schedule[-1].exit_date
    start_index = sessions.index(first_signal)
    one_side_cost_rate = round_trip_cost_bps / 20_000.0
    cash = 1.0
    positions: list[_PositionLot] = []
    order_rows: list[dict[str, object]] = []
    holding_rows: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    previous_nav = 1.0
    previous_date = first_signal
    terminal_settlement_count = 0

    for current_date in sessions[start_index:]:
        for lot in positions:
            mark = price_map.get((current_date, lot.security_id))
            if mark is not None and close_state.get((current_date, lot.security_id)) is True:
                lot.last_price = mark
                lot.last_price_date = current_date

        transaction_cost = 0.0
        turnover_notional = 0.0
        retained: list[_PositionLot] = []
        unsettled: list[_PositionLot] = []
        for lot in positions:
            # 日级可得日按收盘后解释，下一交易日才把结算现金用于开盘订单。
            applicable = [event for event in settlements.get(lot.security_id, []) if lot.entry_date <= event["event_date"] < current_date and event["available_at"] < current_date]
            if applicable:
                event = min(applicable, key=lambda item: item["event_date"])
                recovery = lot.shares * float(event["terminal_value"])
                cash += recovery
                terminal_settlement_count += 1
                order_rows.append({"signal_date": lot.signal_date, "scheduled_date": event["event_date"], "actual_date": current_date, "security_id": lot.security_id, "side": "terminal", "status": "settled", "reason": event["source"], "target_weight": lot.target_weight, "price": event["terminal_value"], "shares": lot.shares, "gross_notional": recovery, "transaction_cost": 0.0})
                continue
            unsettled.append(lot)
        positions = unsettled
        target_shares: dict[str, float] = {}
        if target_difference and current_date in next_decision:
            # 股票和权重已在信号日冻结；仅订单数量使用实际入场开盘价。
            target_nav = cash + sum(lot.shares * lot.last_price for lot in positions)
            for order in entries_by_date.get(current_date, []):
                sid = str(order["security_id"])
                price = price_map.get((current_date, sid))
                target_shares[sid] = (float(order["target_weight"]) * target_nav / price
                    if price is not None else sum(lot.shares for lot in positions if lot.security_id == sid))
            remaining = dict(target_shares)
            revised: list[_PositionLot] = []
            for lot in positions:
                keep = min(lot.shares, remaining.get(lot.security_id, 0.0))
                remaining[lot.security_id] = max(0.0, remaining.get(lot.security_id, 0.0) - keep)
                if keep > 1e-15:
                    revised.append(replace(lot, shares=keep, scheduled_exit_date=next_decision[current_date]))
                if lot.shares - keep > 1e-15:
                    revised.append(replace(lot, shares=lot.shares-keep, scheduled_exit_date=current_date))
            positions = revised
        for lot in positions:
            if current_date < lot.scheduled_exit_date:
                retained.append(lot)
                continue
            key = (current_date, lot.security_id)
            can_close = close_state.get(key)
            exit_open = price_map.get(key)
            if can_close is not True or exit_open is None:
                retained.append(lot)
                order_rows.append(
                    {
                        "signal_date": lot.signal_date,
                        "scheduled_date": lot.scheduled_exit_date,
                        "actual_date": current_date,
                        "security_id": lot.security_id,
                        "side": "sell",
                        "status": "rejected_retry",
                        "reason": "missing_state" if can_close is None else ("can_close_long_false" if not can_close else "invalid_open"),
                        "target_weight": lot.target_weight,
                        "price": exit_open,
                        "shares": lot.shares,
                        "gross_notional": 0.0,
                        "transaction_cost": 0.0,
                    }
                )
                continue
            gross_notional = lot.shares * exit_open
            cost = gross_notional * one_side_cost_rate
            cash += gross_notional - cost
            transaction_cost += cost
            turnover_notional += gross_notional
            order_rows.append(
                {
                    "signal_date": lot.signal_date,
                    "scheduled_date": lot.scheduled_exit_date,
                    "actual_date": current_date,
                    "security_id": lot.security_id,
                    "side": "sell",
                    "status": "filled",
                    "reason": "",
                    "target_weight": lot.target_weight,
                    "price": exit_open,
                    "shares": lot.shares,
                    "gross_notional": gross_notional,
                    "transaction_cost": cost,
                }
            )
        positions = retained

        entry_orders = entries_by_date.get(current_date, [])
        equity_before_buys = cash + sum(lot.shares * lot.last_price for lot in positions)
        buy_requests: list[tuple[dict[str, object], float, float]] = []
        for order in entry_orders:
            security_id = str(order["security_id"])
            key = (current_date, security_id)
            can_open = open_state.get(key)
            entry_open = price_map.get(key)
            if can_open is None:
                if terminal_policy == "fail_closed":
                    raise _execution_error(f"入场订单缺少状态：date={current_date} security_id={security_id}")
                # 已冻结的订单缺少成交依据时不成交，保留原因，不能换入其他股票。
                can_open = False
            if not can_open or entry_open is None:
                order_rows.append(
                    {
                        "signal_date": order["signal_date"],
                        "scheduled_date": current_date,
                        "actual_date": current_date,
                        "security_id": security_id,
                        "side": "buy",
                        "status": "rejected_final",
                        "reason": "can_open_long_false" if not can_open else "invalid_open",
                        "target_weight": order["target_weight"],
                        "price": entry_open,
                        "shares": 0.0,
                        "gross_notional": 0.0,
                        "transaction_cost": 0.0,
                    }
                )
                continue
            if target_difference:
                held = sum(lot.shares for lot in positions if lot.security_id == security_id)
                requested = max(0.0, target_shares[security_id] - held) * entry_open
                if requested <= 1e-15:
                    continue
            else:
                requested = float(order["target_weight"]) * equity_before_buys
            buy_requests.append((order, entry_open, requested))
        required_cash = sum(
            notional * (1.0 + one_side_cost_rate)
            for _, _, notional in buy_requests
        )
        cash_scale = min(1.0, cash / required_cash) if required_cash > 0 else 1.0
        for order, entry_open, requested_notional in buy_requests:
            gross_notional = requested_notional * cash_scale
            if gross_notional <= 1e-15:
                order_rows.append(
                    {
                        "signal_date": order["signal_date"],
                        "scheduled_date": current_date,
                        "actual_date": current_date,
                        "security_id": order["security_id"],
                        "side": "buy",
                        "status": "rejected_final",
                        "reason": "insufficient_cash_locked",
                        "target_weight": order["target_weight"],
                        "price": entry_open,
                        "shares": 0.0,
                        "gross_notional": 0.0,
                        "transaction_cost": 0.0,
                    }
                )
                continue
            shares = gross_notional / entry_open
            cost = gross_notional * one_side_cost_rate
            cash -= gross_notional + cost
            transaction_cost += cost
            turnover_notional += gross_notional
            positions.append(
                _PositionLot(
                    security_id=str(order["security_id"]),
                    signal_date=order["signal_date"],  # type: ignore[arg-type]
                    entry_date=current_date,
                    scheduled_exit_date=(next_decision[current_date] if target_difference
                                         else order["scheduled_exit_date"]),  # type: ignore[arg-type]
                    shares=shares,
                    last_price=entry_open,
                    target_weight=float(order["target_weight"]),
                    last_price_date=current_date,
                )
            )
            order_rows.append(
                {
                    "signal_date": order["signal_date"],
                    "scheduled_date": current_date,
                    "actual_date": current_date,
                    "security_id": order["security_id"],
                    "side": "buy",
                    "status": "filled",
                    "reason": "",
                    "target_weight": order["target_weight"],
                    "price": entry_open,
                    "shares": shares,
                    "gross_notional": gross_notional,
                    "transaction_cost": cost,
                }
            )

        current_nav = cash + sum(lot.shares * lot.last_price for lot in positions)
        if cash < -1e-12 or not math.isfinite(current_nav) or current_nav < 0:
            raise _execution_error("现金或净值违反无杠杆有限值约束")
        if current_date > first_signal:
            daily_rows.append(
                {
                    "entry_date": previous_date,
                    "exit_date": current_date,
                    "target_long_gross_return": (current_nav + transaction_cost) / previous_nav - 1.0 if previous_nav else 0.0,
                    "target_long_turnover": turnover_notional / previous_nav if previous_nav else 0.0,
                    "target_long_cost": transaction_cost / previous_nav if previous_nav else 0.0,
                    "target_long_net_return": current_nav / previous_nav - 1.0 if previous_nav else 0.0,
                    **({"cost_turnover_sides": 2} if terminal_policy == "report_unresolved" else {}),
                }
            )
        for lot in positions if retain_daily_holdings else ():
            holding_rows.append(
                {
                    "date": current_date,
                    "security_id": lot.security_id,
                    "signal_date": lot.signal_date,
                    "entry_date": lot.entry_date,
                    "scheduled_exit_date": lot.scheduled_exit_date,
                    "shares": lot.shares,
                    "mark_open": lot.last_price,
                    "market_value": lot.shares * lot.last_price,
                    "target_weight": lot.target_weight,
                    "exit_delayed": current_date >= lot.scheduled_exit_date,
                }
            )
        previous_nav = current_nav
        previous_date = current_date
        has_future_entries = any(entry_date > current_date for entry_date in entries_by_date)
        if terminal_policy == "fail_closed" and current_date >= last_scheduled_exit and not positions and not has_future_entries:
            break

    if positions and terminal_policy == "fail_closed":
        unresolved = sorted({lot.security_id for lot in positions})
        raise _execution_error(
            "回测终点仍有无法合法退出的持仓，已 fail closed："
            + ",".join(unresolved[:20])
        )
    if len(daily_rows) < 2:
        raise _execution_error("因果回测逐日收益不足两个观测")
    unfilled = sum(
        row["side"] == "buy" and row["status"] != "filled" for row in order_rows
    )
    delayed_attempts = sum(
        row["side"] == "sell" and row["status"] == "rejected_retry" for row in order_rows
    )
    unresolved_rows = tuple({
        "security_id": lot.security_id, "entry_date": lot.entry_date,
        "scheduled_exit_date": lot.scheduled_exit_date, "shares": lot.shares,
        "last_observed_price": lot.last_price, "reference_mark_value": lot.shares * lot.last_price,
        "last_observed_date": lot.last_price_date,
        "terminal_value": None, "status": "价值未确定", "reason": "未能合法退出且无已可得的终止结算",
    } for lot in positions)
    unresolved_mark = sum(lot.shares * lot.last_price for lot in positions)
    stress = [dict(row) for row in daily_rows]
    if unresolved_mark:
        previous_reference_nav = 1.0
        for row in daily_rows[:-1]:
            previous_reference_nav *= 1.0 + float(row["target_long_net_return"])
        for field in ("target_long_net_return", "target_long_gross_return"):
            stress[-1][field] = max(-1.0, float(stress[-1][field]) - unresolved_mark / previous_reference_nav)
    return_summary = {
        "valuation_status": "unresolved" if positions else "resolved",
        "final_cash": cash, "final_reference_nav": previous_nav,
        "unresolved_reference_value": unresolved_mark,
        "unresolved_position_count": len(positions), "unresolved_security_count": len({lot.security_id for lot in positions}),
        "zero_recovery_final_nav": cash,
        "terminal_settlement_count": terminal_settlement_count,
        "reference_valuation_rule": "未退出持仓按最后观察价展示参考估值，不代表实际可回收金额",
        "zero_recovery_rule": "仅在报告末日将未退出持仓回收额设为零；不改变历史选股、成交或可用现金",
    }
    return CausalExecutionResult(
        daily_returns=tuple(daily_rows),
        selections=selections,
        orders=tuple(order_rows),
        holdings_daily=tuple(holding_rows),
        unresolved_positions=unresolved_rows,
        zero_recovery_daily_returns=tuple(stress),
        execution_summary={
            "execution_contract": ("causal_target_difference_v1" if execution_mode == "target_difference" else
                                   "causal_weekly_target_v1" if execution_mode == "weekly_target" else
                                   "causal_daily_open_v2" if terminal_policy == "report_unresolved" else "causal_daily_open_v1"),
            "execution_mode": execution_mode,
            **return_summary,
            "execution_granularity": "daily_ohlc",
            "selection_before_future_fills": True,
            "unfilled_buy_policy": "cash_no_replacement_no_retry",
            "delayed_exit_policy": "retry_each_market_session",
            "locked_position_cash_policy": "locked_capital_not_reused",
            "allow_noncontiguous_schedule": allow_noncontiguous_schedule,
            "terminal_policy": terminal_policy,
            "round_trip_cost_bps": round_trip_cost_bps,
            "selection_count": len(selections),
            "unfilled_buy_count": unfilled,
            "delayed_exit_attempt_count": delayed_attempts,
        },
    )
