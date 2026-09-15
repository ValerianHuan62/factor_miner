"""十组、多空、换手和成本测试。"""

from datetime import date
import unittest

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_evaluation import (
    PortfolioBacktestResult,
    run_target_long_backtest,
)
from factor_miner.portfolio_schema import PortfolioEvaluationPolicy
from factor_miner.trading_schedule import RebalanceWindow


SCHEDULE = (
    RebalanceWindow(signal_date=date(2026, 7, 6), entry_date=date(2026, 7, 7), exit_date=date(2026, 7, 14)),
    RebalanceWindow(signal_date=date(2026, 7, 13), entry_date=date(2026, 7, 14), exit_date=date(2026, 7, 21)),
)


SESSIONS = (
    date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 13),
    date(2026, 7, 14), date(2026, 7, 21),
)


def inputs(count: int = 10) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """构造信号、逐日开盘、状态和逐日基准。"""

    assets = [f"S{number:02d}" for number in range(1, count + 1)]
    factor = pl.DataFrame([
        {
            "signal_date": window.signal_date,
            "security_id": asset,
            "factor_value": float(number if index == 0 else count - number + 1),
        }
        for index, window in enumerate(SCHEDULE)
        for number, asset in enumerate(assets, 1)
    ])
    market = pl.DataFrame([
        {
            "trade_date": session,
            "security_id": asset,
            "open": 100.0 + day_index * number,
        }
        for day_index, session in enumerate(SESSIONS)
        for number, asset in enumerate(assets, 1)
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
    benchmark = pl.DataFrame({
        "entry_date": SESSIONS[:-1],
        "exit_date": SESSIONS[1:],
        "benchmark_return": [0.0] * (len(SESSIONS) - 1),
    })
    return factor, market, state, benchmark


def run_backtest(count: int = 10, *, direction: str = "positive") -> PortfolioBacktestResult:
    """按新的因果接口执行测试回测。"""

    factor, market, state, benchmark = inputs(count)
    return run_target_long_backtest(
        factor, market, state, benchmark, SCHEDULE, PortfolioEvaluationPolicy(),
        direction=direction,  # type: ignore[arg-type]
    )


class PortfolioEvaluationTest(unittest.TestCase):
    """组合收益必须遵守冻结的分组和成本定义。"""

    def test_only_target_long_and_benchmark_are_published(self) -> None:
        """目标多头日序列和持仓不能泄露十组或多空组合。"""

        result = run_backtest(direction="negative")
        first = result.daily_returns[0]
        self.assertIn("target_long_gross_return", first)
        self.assertIn("target_long_net_return", first)
        self.assertIn("benchmark_return", first)
        self.assertNotIn("Q10_Q1_net_return", first)
        self.assertNotIn("Q9_Q2_net_return", first)
        self.assertFalse(any(key.startswith("Q") for key in first))
        self.assertAlmostEqual(first["target_long_turnover"], 1.0 / 1.0007)
        self.assertAlmostEqual(first["target_long_cost"], 0.0007 / 1.0007)
        first_weights = [row for row in result.weights if row["entry_date"] == date(2026, 7, 7)]
        grouped = (
            pl.DataFrame(first_weights)
            .group_by("portfolio")
            .agg(pl.col("weight").sum())
            .sort("portfolio")
        )
        self.assertEqual(grouped.height, 1)
        self.assertEqual(grouped["portfolio"].to_list(), ["target_long"])
        self.assertTrue(all(abs(value - 1.0) < 1e-12 for value in grouped["weight"].to_list()))

    def test_positive_selects_highest_and_negative_selects_lowest_decile(self) -> None:
        """方向选错时，目标多头的首期毛收益会落在错误的截面极端。"""

        positive = run_backtest(direction="positive")
        negative = run_backtest(direction="negative")
        positive_assets = [row["security_id"] for row in positive.weights if row["signal_date"] == SCHEDULE[0].signal_date]
        negative_assets = [row["security_id"] for row in negative.weights if row["signal_date"] == SCHEDULE[0].signal_date]
        self.assertEqual(positive_assets, ["S10"])
        self.assertEqual(negative_assets, ["S01"])

    def test_governance_win_rate_uses_internal_extreme_spread(self) -> None:
        """胜率必须来自内部逐期极端组差，而不是目标多头汇总指标。"""

        result = run_backtest(direction="positive")
        diagnostic = result.governance.extreme_spread_daily[0]
        self.assertAlmostEqual(result.governance.win_rate, 0.5)
        self.assertTrue(isinstance(diagnostic.extreme_spread_net_return, float))

    def test_build_rejects_q_group_or_unknown_public_daily_fields(self) -> None:
        """旧分组收益或未登记字段不能绕过评价器进入公开 artifact。"""

        result = run_backtest()
        polluted = dict(result.daily_returns[0])
        polluted["Q10_Q1_net_return"] = 0.01
        with self.assertRaises(FactorMinerError) as context:
            PortfolioBacktestResult.build(
                policy_id=result.policy_id,
                direction=result.direction,
                daily_returns=(polluted, *result.daily_returns[1:]),
                weights=result.weights,
                governance=result.governance,
            )
        self.assertEqual(context.exception.code, FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID)

    def test_build_rejects_non_target_or_unknown_weight_fields(self) -> None:
        """非目标多头标签和未登记持仓字段不能进入公开 artifact。"""

        result = run_backtest()
        q_group = dict(result.weights[0])
        q_group["portfolio"] = "Q1"
        with self.assertRaises(FactorMinerError):
            PortfolioBacktestResult.build(
                policy_id=result.policy_id,
                direction=result.direction,
                daily_returns=result.daily_returns,
                weights=(q_group, *result.weights[1:]),
                governance=result.governance,
            )
        unknown = dict(result.weights[0])
        unknown["asset_return"] = 0.01
        with self.assertRaises(FactorMinerError):
            PortfolioBacktestResult.build(
                policy_id=result.policy_id,
                direction=result.direction,
                daily_returns=result.daily_returns,
                weights=(unknown, *result.weights[1:]),
                governance=result.governance,
            )

    def test_insufficient_names_fail_without_empty_groups(self) -> None:
        with self.assertRaises(FactorMinerError) as context:
            run_backtest(9)
        self.assertEqual(context.exception.code, FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID)


if __name__ == "__main__":
    unittest.main()
