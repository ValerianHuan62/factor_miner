"""周二实际交易日调度测试。"""

from datetime import date
import unittest

import polars as pl
from pydantic import ValidationError

from factor_miner.trading_schedule import (
    RebalanceWindow,
    build_tuesday_rebalance_schedule,
)


def calendar_with_tuesday_holiday() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_date": [
                date(2026, 7, 6),
                date(2026, 7, 7),
                date(2026, 7, 8),
                date(2026, 7, 13),
                date(2026, 7, 14),
                date(2026, 7, 15),
                date(2026, 7, 20),
                date(2026, 7, 21),
            ],
            "is_open": [True, False, True, True, True, True, True, True],
        }
    )


class TradingScheduleTest(unittest.TestCase):
    """调仓日和收益区间必须按实际交易日生成。"""

    def test_tuesday_holiday_rolls_to_next_open_day(self) -> None:
        schedule = build_tuesday_rebalance_schedule(
            calendar_with_tuesday_holiday(),
            date(2026, 7, 1),
            date(2026, 7, 21),
        )
        self.assertEqual(schedule[0].signal_date, date(2026, 7, 6))
        self.assertEqual(schedule[0].entry_date, date(2026, 7, 8))
        self.assertEqual(schedule[0].exit_date, date(2026, 7, 14))
        self.assertTrue(all(item.signal_date < item.entry_date < item.exit_date for item in schedule))

    def test_window_rejects_future_or_reversed_dates(self) -> None:
        with self.assertRaises(ValidationError):
            RebalanceWindow(
                signal_date=date(2026, 7, 8),
                entry_date=date(2026, 7, 7),
                exit_date=date(2026, 7, 14),
            )


if __name__ == "__main__":
    unittest.main()
