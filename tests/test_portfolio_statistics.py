"""实际交易日日历年化和组合指标测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_statistics import calculate_portfolio_metrics


def calendar() -> pl.DataFrame:
    days = [date(2026, 1, 2) + timedelta(days=index) for index in range(10)]
    return pl.DataFrame({"trade_date": days, "is_open": [True] * len(days)})


def returns() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "entry_date": [date(2026, 1, 2), date(2026, 1, 6)],
            "exit_date": [date(2026, 1, 6), date(2026, 1, 10)],
            "target_long_net_return": [0.10, -0.05],
            "target_long_gross_return": [0.11, -0.04],
            "benchmark_return": [0.02, 0.01],
        }
    )


def benchmark() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "entry_date": [date(2026, 1, 2), date(2026, 1, 6)],
            "exit_date": [date(2026, 1, 6), date(2026, 1, 10)],
            "benchmark_return": [0.02, 0.01],
        }
    )


class PortfolioStatisticsTest(unittest.TestCase):
    """指标必须使用净收益和真实日历。"""

    def test_metrics_use_actual_calendar_not_fixed_252(self) -> None:
        result = calculate_portfolio_metrics(
            returns(),
            benchmark(),
            calendar(),
            calendar_version="synthetic-calendar-v1",
            calendar_sha256="a" * 64,
        )
        self.assertEqual(
            set(result.series),
            {"target_long_gross_return", "target_long_net_return", "CSI300"},
        )
        metrics = result.series["target_long_net_return"]
        self.assertAlmostEqual(metrics.period_return, 0.045)
        self.assertNotEqual(metrics.annualization_factor, 252)
        self.assertGreater(metrics.sharpe, 0)
        self.assertAlmostEqual(metrics.max_drawdown, 0.05)
        self.assertAlmostEqual(metrics.excess_return, (1.10 / 1.02) * (0.95 / 1.01) - 1.0)
        self.assertEqual(result.series["CSI300"].excess_return, 0.0)

    def test_group_or_long_short_returns_cannot_enter_metrics(self) -> None:
        """公开指标输入出现旧分组收益时必须硬失败，而不是静默投影。"""

        with self.assertRaises(FactorMinerError) as context:
            calculate_portfolio_metrics(
                returns().with_columns(pl.lit(0.01).alias("Q10_Q1_net_return")),
                benchmark(),
                calendar(),
                calendar_version="synthetic-calendar-v1",
                calendar_sha256="a" * 64,
            )
        self.assertEqual(context.exception.code, FailureCode.INSUFFICIENT_VALID_DATES)

    def test_missing_benchmark_date_and_empty_series_fail_closed(self) -> None:
        with self.assertRaises(FactorMinerError) as context:
            calculate_portfolio_metrics(
                returns(),
                benchmark().filter(pl.col("exit_date") != date(2026, 1, 10)),
                calendar(),
                calendar_version="synthetic-calendar-v1",
                calendar_sha256="a" * 64,
            )
        self.assertEqual(context.exception.code, FailureCode.INSUFFICIENT_VALID_DATES)
        with self.assertRaises(FactorMinerError):
            calculate_portfolio_metrics(
                returns().clear(),
                benchmark(),
                calendar(),
                calendar_version="synthetic-calendar-v1",
                calendar_sha256="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
