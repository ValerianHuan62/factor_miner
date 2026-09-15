"""真实美股 Dashboard 单因子回测合同测试。"""

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from dashboard.real_backtest import (
    build_real_us_backtest,
    load_real_backtest,
    load_real_backtest_catalog,
)


class RealBacktestTest(unittest.TestCase):
    """真实回测必须使用显式开盘价与交易状态，且只输出聚合结果。"""

    def test_builds_non_overlapping_real_open_to_open_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = [date(2021, 1, 4) + timedelta(days=index) for index in range(22)]
            assets = [f"security:{index:02d}" for index in range(20)]
            market_rows = []
            state_rows = []
            factor_rows = []
            for session_index, session in enumerate(sessions):
                for asset_index, asset in enumerate(assets):
                    market_rows.append({
                        "date": session,
                        "asset": asset,
                        "open": 100.0 + session_index * (asset_index + 1) * 0.01,
                    })
                    state_rows.append({
                        "date": session,
                        "asset": asset,
                        "valid_for_factor_rank": True,
                        "valid_for_trading": True,
                        "can_open_long": True,
                        "can_close_long": True,
                    })
                    factor_rows.append({
                        "date": session,
                        "asset": asset,
                        "raw_factor": float(asset_index),
                        "valid_for_factor_compute": True,
                    })
            factor_path = root / "factor.parquet"
            market_path = root / "market.parquet"
            state_path = root / "state.parquet"
            manifest_path = root / "manifest.json"
            output_path = root / "backtest.json"
            pl.DataFrame(factor_rows).write_parquet(factor_path)
            pl.DataFrame(market_rows).write_parquet(market_path)
            pl.DataFrame(state_rows).write_parquet(state_path)
            manifest_path.write_text(json.dumps({
                "release_id": "local_test",
                "market_cutoff": sessions[-1].isoformat(),
                "adjustment_convention": "split_adjusted",
                "calendar_version": "test",
            }), encoding="utf-8")

            payload = build_real_us_backtest(
                factor_path=factor_path,
                market_path=market_path,
                state_path=state_path,
                manifest_path=manifest_path,
                output_path=output_path,
                source_candidate_id="cand_test",
                factor_id="huan001",
                factor_name="真实低波动候选",
                start_date=sessions[0],
                end_date=sessions[15],
                base_cost_bps=10.0,
                rank_ic_mean=0.02,
                rank_ic_hac_t=2.1,
            )
            loaded = load_real_backtest(output_path)

        self.assertEqual(payload, loaded)
        self.assertGreaterEqual(len(payload["daily_rows"]), 2)
        self.assertEqual(payload["mode"], "Smoke")
        self.assertIn("不是密封 OOS", payload["scope"])
        self.assertNotIn("security:00", json.dumps(payload, ensure_ascii=False))
        first = payload["daily_rows"][0]
        self.assertAlmostEqual(
            first["target_long_cost"],
            first["target_long_turnover"] * 10.0 / 20_000.0,
        )
        self.assertGreater(
            sum(row["target_long_gross_return"] for row in payload["daily_rows"]),
            sum(row["benchmark_return"] for row in payload["daily_rows"]),
        )

    def test_loader_rejects_missing_real_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text(json.dumps({
                "schema_version": "dashboard-real-backtest-v2-causal",
                "daily_rows": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "逐期聚合收益"):
                load_real_backtest(path)

    def test_catalog_hides_legacy_future_filtered_backtests(self) -> None:
        """目录中只有 v1 产物时必须明确失效，不能继续展示。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy.json").write_text(
                json.dumps({"schema_version": "dashboard-real-backtest-v1"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "旧回测已失效"):
                load_real_backtest_catalog(backtest_roots=(root,))

    def test_catalog_loads_multiple_factors_in_stable_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "schema_version": "dashboard-real-backtest-v2-causal",
                "mode": "Smoke",
                "market_id": "us_equity",
                "scope": "test",
                "evaluation": {"rank_ic_mean": 0.01, "rank_ic_hac_t": 1.0},
                "protocol": {"base_cost_bps": 10.0},
                "data_identity": {"asset_key": "security_id"},
                "window": {"start": "2021-01-01", "end": "2021-01-31"},
                "daily_rows": [
                    {
                        "entry_date": "2021-01-04", "exit_date": "2021-01-11",
                        "target_long_gross_return": 0.01, "target_long_turnover": 1.0,
                        "target_long_cost": 0.001, "target_long_net_return": 0.009,
                        "benchmark_return": 0.002,
                    },
                    {
                        "entry_date": "2021-01-11", "exit_date": "2021-01-18",
                        "target_long_gross_return": 0.02, "target_long_turnover": 0.5,
                        "target_long_cost": 0.0005, "target_long_net_return": 0.0195,
                        "benchmark_return": 0.003,
                    },
                ],
            }
            for factor_id in ("huan010", "huan002"):
                payload = {**base, "factor_id": factor_id, "factor_name": factor_id}
                (root / f"{factor_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            catalog = load_real_backtest_catalog(backtest_roots=(root,))

        self.assertEqual([item["factor_id"] for item in catalog], ["huan002", "huan010"])


if __name__ == "__main__":
    unittest.main()
