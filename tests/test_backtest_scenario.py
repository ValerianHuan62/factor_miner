"""Dashboard 回测情景分析测试。"""

from datetime import date
import unittest

from dashboard.backtest_scenario import (
    calculate_scenario_metrics,
    infer_published_cost_bps,
    nav_rows,
    scenario_rows,
)


ROWS = [
    {
        "entry_date": "2026-01-02",
        "exit_date": "2026-01-09",
        "target_long_gross_return": 0.02,
        "target_long_turnover": 0.5,
        "target_long_cost": 0.0007,
        "target_long_net_return": 0.0193,
        "benchmark_return": 0.01,
    },
    {
        "entry_date": "2026-01-09",
        "exit_date": "2026-01-16",
        "target_long_gross_return": -0.01,
        "target_long_turnover": 1.0,
        "target_long_cost": 0.0014,
        "target_long_net_return": -0.0114,
        "benchmark_return": -0.005,
    },
    {
        "entry_date": "2026-01-16",
        "exit_date": "2026-01-23",
        "target_long_gross_return": 0.03,
        "target_long_turnover": 0.25,
        "target_long_cost": 0.00035,
        "target_long_net_return": 0.02965,
        "benchmark_return": 0.015,
    },
]


class BacktestScenarioTest(unittest.TestCase):
    """交互参数只能重算情景结果，不能改写正式逐期记录。"""

    def test_infers_frozen_cost_rate(self) -> None:
        self.assertAlmostEqual(infer_published_cost_bps(ROWS) or 0.0, 14.0)

    def test_recalculates_cost_and_filters_exit_date(self) -> None:
        result = scenario_rows(
            ROWS,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 23),
            cost_bps=10.0,
            slippage_bps=5.0,
        )

        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0]["scenario_cost"], 0.0015)
        self.assertAlmostEqual(result[0]["scenario_net_return"], -0.0115)
        self.assertEqual(ROWS[1]["target_long_net_return"], -0.0114)

    def test_builds_nav_drawdown_and_metrics(self) -> None:
        result = scenario_rows(
            ROWS,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            cost_bps=14.0,
            slippage_bps=0.0,
        )
        path = nav_rows(result)
        metrics = calculate_scenario_metrics(result)

        self.assertAlmostEqual(path[0]["scenario_nav"], 1.0193)
        self.assertLess(path[1]["drawdown"], 0.0)
        self.assertEqual(metrics.observations, 3)
        self.assertGreater(metrics.annualized_return, 0.0)
        self.assertGreater(metrics.max_drawdown, 0.0)
        self.assertAlmostEqual(
            metrics.calmar_ratio or 0.0,
            metrics.annualized_return / metrics.max_drawdown,
        )

    def test_rejects_missing_turnover_instead_of_assuming_zero(self) -> None:
        broken = [dict(ROWS[0]), dict(ROWS[1])]
        broken[0].pop("target_long_turnover")
        with self.assertRaisesRegex(ValueError, "target_long_turnover"):
            scenario_rows(
                broken,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
                cost_bps=14.0,
                slippage_bps=0.0,
            )


if __name__ == "__main__":
    unittest.main()
