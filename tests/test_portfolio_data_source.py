"""open-to-open 数据连接合同测试。"""

from datetime import date
import unittest

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_data_source import align_open_to_open_panel
from factor_miner.trading_schedule import RebalanceWindow


SCHEDULE = (
    RebalanceWindow(
        signal_date=date(2026, 7, 6),
        entry_date=date(2026, 7, 7),
        exit_date=date(2026, 7, 14),
    ),
)


def factor_panel() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "signal_date": [date(2026, 7, 6), date(2026, 7, 6)],
            "security_id": ["A", "B"],
            "factor_value": [2.0, 1.0],
        }
    ).lazy()


def market_panel() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 7), date(2026, 7, 7), date(2026, 7, 14), date(2026, 7, 14)],
            "security_id": ["A", "B", "A", "B"],
            "open": [10.0, 20.0, 11.0, 21.0],
        }
    ).lazy()


class PortfolioDataSourceTest(unittest.TestCase):
    """数据缺口和重复主键必须硬失败。"""

    def test_alignment_uses_only_entry_and_exit_open(self) -> None:
        result = align_open_to_open_panel(factor_panel(), market_panel(), SCHEDULE).collect()
        self.assertEqual(result.select("security_id").to_series().to_list(), ["A", "B"])
        returns = result.select("asset_return").to_series().to_list()
        self.assertAlmostEqual(returns[0], 0.1)
        self.assertAlmostEqual(returns[1], 0.05)
        self.assertNotIn("close", result.columns)

    def test_missing_open_is_rejected(self) -> None:
        bad = market_panel().with_columns(
            pl.when(pl.col("security_id") == "A")
            .then(None)
            .otherwise(pl.col("open"))
            .alias("open")
        )
        with self.assertRaises(FactorMinerError) as context:
            align_open_to_open_panel(factor_panel(), bad, SCHEDULE)
        self.assertEqual(context.exception.code, FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID)

    def test_suspended_exit_cannot_fall_back_to_stale_open(self) -> None:
        """退出日缺价不能回退旧价，延迟退出必须交给因果状态机。"""

        market = pl.DataFrame(
            {
                "trade_date": [
                    date(2026, 7, 7), date(2026, 7, 7),
                    date(2026, 7, 13), date(2026, 7, 14),
                    date(2026, 7, 15),
                ],
                "security_id": ["A", "B", "A", "B", "A"],
                "open": [10.0, 20.0, 9.0, 21.0, 100.0],
            }
        ).lazy()

        with self.assertRaisesRegex(FactorMinerError, "禁止回退"):
            align_open_to_open_panel(factor_panel(), market, SCHEDULE)

    def test_duplicate_security_date_key_is_rejected(self) -> None:
        duplicate = pl.concat([market_panel().collect(), market_panel().collect()]).lazy()
        with self.assertRaises(FactorMinerError) as context:
            align_open_to_open_panel(factor_panel(), duplicate, SCHEDULE)
        self.assertEqual(context.exception.code, FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID)


if __name__ == "__main__":
    unittest.main()
