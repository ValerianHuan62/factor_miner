"""分钟数据聚合为日级截面字段的合成测试。"""

from datetime import date, datetime, timedelta
import math
import unittest

import polars as pl

from factor_miner.intraday_features import aggregate_intraday_day
from factor_miner.intraday_schema import FROZEN_MINUTE_ROOT, IntradaySourceSpec


def _trading_minutes(day: date) -> list[datetime]:
    morning = [datetime.combine(day, datetime.min.time()).replace(hour=9, minute=31) + timedelta(minutes=index) for index in range(120)]
    afternoon = [datetime.combine(day, datetime.min.time()).replace(hour=13, minute=1) + timedelta(minutes=index) for index in range(120)]
    return morning + afternoon


def _rows(day: date, asset: str, *, count: int = 240) -> list[dict[str, object]]:
    minutes = _trading_minutes(day)[:count]
    rows: list[dict[str, object]] = []
    for index, timestamp in enumerate(minutes):
        close = 100.0 + index * 0.1
        rows.append(
            {
                "asset": asset,
                "datetime": timestamp,
                "open": 100.0 if index == 0 else close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1.0,
                "total_turnover": close,
                "num_trades": 1,
            }
        )
    return rows


class IntradayFeaturesTest(unittest.TestCase):
    """完整日产生聚合值，不完整日不能伪造信号。"""

    def test_complete_day_produces_frozen_daily_features(self) -> None:
        day = date(2026, 6, 30)
        frame = pl.DataFrame(_rows(day, "SYNTHETIC"))
        result = aggregate_intraday_day(
            frame,
            IntradaySourceSpec(root=FROZEN_MINUTE_ROOT),
        )
        row = result.row(0, named=True)
        closes = [100.0 + index * 0.1 for index in range(240)]
        expected_rv = math.sqrt(
            sum(math.log(closes[index] / closes[index - 1]) ** 2 for index in range(1, 240))
        )
        self.assertEqual(row["date"], day)
        self.assertEqual(row["completeness_status"], "分钟数据完整")
        self.assertAlmostEqual(row["intraday_open_30m_return"], closes[29] / 100.0 - 1.0)
        self.assertAlmostEqual(
            row["intraday_close_30m_amount_share"],
            sum(closes[-30:]) / sum(closes),
        )
        self.assertAlmostEqual(row["intraday_realized_volatility"], expected_rv)
        self.assertAlmostEqual(
            row["intraday_close_vwap_deviation"],
            closes[-1] / (sum(closes) / len(closes)) - 1.0,
        )

    def test_incomplete_day_keeps_features_null(self) -> None:
        frame = pl.DataFrame(_rows(date(2026, 6, 30), "SYNTHETIC", count=239))
        row = aggregate_intraday_day(
            frame,
            IntradaySourceSpec(root=FROZEN_MINUTE_ROOT),
        ).row(0, named=True)
        self.assertEqual(row["completeness_status"], "分钟数据不完整")
        for name in (
            "intraday_open_30m_return",
            "intraday_close_30m_amount_share",
            "intraday_realized_volatility",
            "intraday_close_vwap_deviation",
        ):
            self.assertIsNone(row[name])

    def test_duplicate_timestamp_marks_day_incomplete(self) -> None:
        rows = _rows(date(2026, 6, 30), "SYNTHETIC")
        rows[-1]["datetime"] = rows[-2]["datetime"]
        row = aggregate_intraday_day(
            pl.DataFrame(rows),
            IntradaySourceSpec(root=FROZEN_MINUTE_ROOT),
        ).row(0, named=True)
        self.assertEqual(row["completeness_status"], "分钟数据不完整")
        self.assertIsNone(row["intraday_realized_volatility"])


if __name__ == "__main__":
    unittest.main()
