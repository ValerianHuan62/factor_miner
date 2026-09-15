"""因果订单状态机的未来信息与不可成交测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.errors import FactorMinerError
from factor_miner.trading_schedule import RebalanceWindow


SESSIONS = tuple(date(2026, 1, 5) + timedelta(days=index) for index in range(5))
SCHEDULE = (
    RebalanceWindow(signal_date=SESSIONS[0], entry_date=SESSIONS[1], exit_date=SESSIONS[3]),
)


def inputs() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    assets = ("A", "B", "C", "D")
    factor = pl.DataFrame({
        "signal_date": [SESSIONS[0]] * 4,
        "security_id": list(assets),
        "factor_value": [4.0, 3.0, 2.0, 1.0],
    })
    market = pl.DataFrame([
        {"trade_date": session, "security_id": asset, "open": 100.0 + index}
        for index, session in enumerate(SESSIONS)
        for asset in assets
    ])
    state = pl.DataFrame([
        {
            "trade_date": session,
            "security_id": asset,
            "valid_for_factor_rank": True,
            "can_open_long": True,
            "can_close_long": True,
        }
        for session in SESSIONS
        for asset in assets
    ])
    return factor, market, state


class CausalBacktestTest(unittest.TestCase):
    """选股只看 T 日，未来成交失败只能改变现金与退出路径。"""

    def test_overlapping_windows_do_not_reuse_locked_money_or_retry_entries(self):
        factor, market, state = inputs()
        factor = pl.concat([factor, factor.with_columns(pl.lit(SESSIONS[1]).alias('signal_date'))])
        overlap = SCHEDULE + (RebalanceWindow(signal_date=SESSIONS[1], entry_date=SESSIONS[2], exit_date=SESSIONS[4]),)
        with self.assertRaisesRegex(FactorMinerError, '首尾连接'):
            simulate_causal_extreme_portfolio(factor, market, state, overlap, group_count=4)
        result = simulate_causal_extreme_portfolio(factor, market, state, overlap, group_count=4, allow_noncontiguous_schedule=True)
        buys = [x for x in result.orders if x['side']=='buy']
        self.assertEqual([x['status'] for x in buys], ['filled','rejected_final'])
        self.assertEqual(buys[1]['reason'], 'insufficient_cash_locked')
        self.assertEqual(buys[1]['actual_date'],SESSIONS[2])
        self.assertTrue(all(x['actual_date'] != SESSIONS[3] for x in buys))
        self.assertAlmostEqual(result.execution_summary['final_cash'],103/101)

    def test_future_execution_state_cannot_change_frozen_selection(self) -> None:
        factor, market, state = inputs()
        baseline = simulate_causal_extreme_portfolio(factor, market, state, SCHEDULE, group_count=2)
        changed = state.with_columns(
            pl.when((pl.col("trade_date") == SESSIONS[1]) & (pl.col("security_id") == "A"))
            .then(False).otherwise(pl.col("can_open_long")).alias("can_open_long")
        )
        result = simulate_causal_extreme_portfolio(factor, market, changed, SCHEDULE, group_count=2)
        self.assertEqual(baseline.selections, result.selections)
        rejected = [row for row in result.orders if row["side"] == "buy" and row["status"] == "rejected_final"]
        self.assertEqual([row["security_id"] for row in rejected], ["A"])
        self.assertFalse(any(row["security_id"] == "C" and row["side"] == "buy" for row in result.orders))

    def test_unfilled_exit_retries_next_session(self) -> None:
        factor, market, state = inputs()
        delayed = state.with_columns(
            pl.when((pl.col("trade_date") == SESSIONS[3]) & (pl.col("security_id") == "A"))
            .then(False).otherwise(pl.col("can_close_long")).alias("can_close_long")
        )
        result = simulate_causal_extreme_portfolio(factor, market, delayed, SCHEDULE, group_count=4)
        sells = [row for row in result.orders if row["side"] == "sell"]
        self.assertEqual([row["status"] for row in sells], ["rejected_retry", "filled"])
        self.assertEqual(sells[-1]["actual_date"], SESSIONS[4])

    def test_unresolved_terminal_position_fails_closed(self) -> None:
        factor, market, state = inputs()
        blocked = state.with_columns(
            pl.when((pl.col("trade_date") >= SESSIONS[3]) & (pl.col("security_id") == "A"))
            .then(False).otherwise(pl.col("can_close_long")).alias("can_close_long")
        )
        with self.assertRaisesRegex(FactorMinerError, "fail closed"):
            simulate_causal_extreme_portfolio(factor, market, blocked, SCHEDULE, group_count=4)

    def test_report_unresolved_does_not_sell_and_stress_writes_down_only_at_end(self) -> None:
        factor, market, state = inputs()
        blocked = state.with_columns(pl.when((pl.col("trade_date") >= SESSIONS[3]) & (pl.col("security_id") == "A")).then(False).otherwise(pl.col("can_close_long")).alias("can_close_long"))
        result = simulate_causal_extreme_portfolio(factor, market, blocked, SCHEDULE, group_count=4, terminal_policy="report_unresolved")
        self.assertEqual(result.execution_summary["valuation_status"], "unresolved")
        self.assertEqual(len(result.unresolved_positions), 1)
        self.assertIsNone(result.unresolved_positions[0]["terminal_value"])
        self.assertEqual(result.daily_returns[:-1], result.zero_recovery_daily_returns[:-1])
        self.assertAlmostEqual(result.zero_recovery_daily_returns[-1]["target_long_net_return"], -1.0)
        self.assertFalse(any(x["status"] == "filled" and x["side"] == "sell" for x in result.orders))

    def test_terminal_recovery_is_paid_without_discarding_selected_stock(self) -> None:
        factor, market, state = inputs()
        events = pl.DataFrame({"security_id": ["A"], "event_date": [SESSIONS[2]], "available_at": [SESSIONS[2]], "terminal_value": [20.2], "source": ["verified_fixture"]})
        result = simulate_causal_extreme_portfolio(factor, market, state, SCHEDULE, group_count=4, terminal_policy="report_unresolved", terminal_events=events)
        self.assertEqual(result.selections[0]["security_id"], "A")
        self.assertAlmostEqual(result.execution_summary["final_cash"], .2)
        self.assertEqual(result.execution_summary["terminal_settlement_count"], 1)
        settled = next(x for x in result.orders if x["side"] == "terminal")
        self.assertEqual(settled["actual_date"], SESSIONS[3])
        self.assertEqual(result.unresolved_positions, ())


if __name__ == "__main__":
    unittest.main()
