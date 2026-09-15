"""跨市场 Dashboard 配置与只读预检测试。"""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from dashboard.market_profiles import dashboard_dsn, load_market_profiles, preflight_market


class MarketProfileTest(unittest.TestCase):
    """市场适配必须显式映射字段与状态，不能猜测交易语义。"""

    def test_postgres_dsn_is_physical_per_market(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FM_DASHBOARD_DSN": "postgresql://a-share-legacy",
                "FM_DASHBOARD_DSN_A_SHARE": "postgresql://a-share",
                "FM_DASHBOARD_DSN_US_EQUITY": "postgresql://us-equity",
            },
            clear=False,
        ):
            self.assertEqual(dashboard_dsn("a_share"), "postgresql://a-share")
            self.assertEqual(dashboard_dsn("us_equity"), "postgresql://us-equity")

    def test_us_database_never_falls_back_to_legacy_a_share_dsn(self) -> None:
        with patch.dict(
            "os.environ",
            {"FM_DASHBOARD_DSN": "postgresql://a-share-legacy", "FM_DASHBOARD_DSN_US_EQUITY": ""},
            clear=False,
        ):
            self.assertEqual(dashboard_dsn("us_equity"), "")

    def test_local_profile_preflight_reads_schema_and_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pl.DataFrame(
                {
                    "session": [date(2026, 1, 2), date(2026, 1, 5)],
                    "sid": ["A", "A"],
                    "px_open": [10.0, 10.5],
                    "px_high": [11.0, 11.0],
                    "px_low": [9.5, 10.0],
                    "px_close": [10.5, 10.8],
                    "shares": [100, 120],
                }
            ).write_parquet(root / "market.parquet")
            pl.DataFrame(
                {
                    "session": [date(2026, 1, 2), date(2026, 1, 5)],
                    "sid": ["A", "A"],
                    "valid_for_factor_compute": [True, True],
                    "valid_for_factor_rank": [True, True],
                    "valid_for_trading": [True, False],
                }
            ).write_parquet(root / "state.parquet")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            config = root / "profiles.json"
            config.write_text(
                json.dumps(
                    {
                        "markets": [
                            {
                                "market_id": "test_market",
                                "display_name": "测试市场",
                                "adapter": "test_v1",
                                "data_root": str(root),
                                "market_path": "market.parquet",
                                "state_path": "state.parquet",
                                "manifest_path": "manifest.json",
                                "artifact_root": "artifacts/test_market",
                                "date_column": "session",
                                "asset_column": "sid",
                                "market_columns": {
                                    "open": "px_open",
                                    "high": "px_high",
                                    "low": "px_low",
                                    "close": "px_close",
                                    "volume": "shares",
                                },
                                "state_columns": [
                                    "valid_for_factor_compute",
                                    "valid_for_factor_rank",
                                    "valid_for_trading",
                                ],
                                "adjustment_convention": "测试复权",
                                "calendar_version": "test-calendar-v1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profile = load_market_profiles(config)[0]
            result = preflight_market(profile)

        self.assertTrue(result.ready)
        self.assertEqual(profile.backtest_roots, ())
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.start_date, "2026-01-02")
        self.assertEqual(result.end_date, "2026-01-05")

    def test_preflight_rejects_missing_state_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pl.DataFrame(
                {
                    "date": [date(2026, 1, 2)],
                    "asset": ["A"],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1],
                }
            ).write_parquet(root / "market.parquet")
            pl.DataFrame({"date": [date(2026, 1, 2)], "asset": ["A"]}).write_parquet(root / "state.parquet")
            config = root / "profiles.json"
            config.write_text(
                json.dumps(
                    {
                        "markets": [
                            {
                                "market_id": "broken",
                                "data_root": str(root),
                                "market_path": "market.parquet",
                                "state_path": "state.parquet",
                                "artifact_root": "artifacts/broken",
                                "date_column": "date",
                                "asset_column": "asset",
                                "market_columns": {name: name for name in ("open", "high", "low", "close", "volume")},
                                "state_columns": ["valid_for_trading"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = preflight_market(load_market_profiles(config)[0])

        self.assertFalse(result.ready)
        self.assertIn("valid_for_trading", result.failures[0])

    def test_profile_preflight_accepts_csv(self) -> None:
        """市场配置与通用 CLI 一样允许 CSV，不把 Parquet 变成硬依赖。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market = pl.DataFrame(
                {
                    "date": [date(2026, 1, 2)],
                    "asset": ["A"],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1],
                }
            )
            market.write_csv(root / "market.csv")
            market.select("date", "asset").with_columns(
                pl.lit(True).alias("valid_for_trading")
            ).write_csv(root / "state.csv")
            config = root / "profiles.json"
            config.write_text(
                json.dumps(
                    {
                        "markets": [
                            {
                                "market_id": "csv_market",
                                "data_root": str(root),
                                "market_path": "market.csv",
                                "state_path": "state.csv",
                                "artifact_root": "artifacts/csv_market",
                                "date_column": "date",
                                "asset_column": "asset",
                                "market_columns": {name: name for name in ("open", "high", "low", "close", "volume")},
                                "state_columns": ["valid_for_trading"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = preflight_market(load_market_profiles(config)[0])

        self.assertTrue(result.ready)
        self.assertEqual(result.rows, 1)


if __name__ == "__main__":
    unittest.main()
