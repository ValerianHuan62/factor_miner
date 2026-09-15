"""open-to-open 面板与便携滚动 Ridge 的合成测试。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

import numpy as np
import polars as pl

from factor_miner.rolling_ridge import (
    RollingRidgePolicy,
    rolling_ridge_predictions,
    top_n_metrics,
)
from factor_miner.quantlake_screening import build_quantlake_screening_batches
from factor_miner.screening_panel import merge_feature_columns, rebuild_open_label_panel
from factor_miner.screening_deduplication import select_cluster_representatives
from factor_miner.screening_clustering import cluster_daily_factors
from factor_miner.single_factor_screening import screen_single_factors


class ScreeningPipelineTest(unittest.TestCase):
    def test_quantlake_panel_freezes_discovery_direction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            labels = []
            start = date(2021, 1, 4)
            for week in range(3):
                friday = start + timedelta(days=week * 7 + 4)
                for day in range(5):
                    current = start + timedelta(days=week * 7 + day)
                    for asset in range(60):
                        rows.append({"date": current, "code": f"A{asset:03d}", "factor_x": float(asset)})
                for asset in range(60):
                    labels.append({"date": friday, "code": f"A{asset:03d}", "label_raw": -float(asset) / 1000.0})
            pl.DataFrame(rows).write_parquet(root / "factors.parquet")
            pl.DataFrame(labels).write_parquet(root / "labels.parquet")
            result = build_quantlake_screening_batches(
                factor_paths=(root / "factors.parquet",),
                label_panel=root / "labels.parquet",
                output_root=root / "output",
                batch_size=1,
            )
            catalog = pl.read_csv(root / "output" / "因子方向目录.csv", encoding="utf8-lossy")
            panel = pl.read_parquet(root / "output" / "weekly_factor_features_001_open.parquet")
            self.assertEqual(result["factor_count"], 1)
            self.assertEqual(catalog.item(0, "selected_direction"), "negative")
            self.assertGreater(
                panel.select(pl.corr("fm_factor_x__last", "label_raw", method="spearman")).item(),
                0.99,
            )

    def test_rebuild_uses_first_and_sixth_future_market_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(8)]
            pl.DataFrame(
                {"date": [dates[0]], "code": ["A"], "old_feature": [1.0], "label_raw": [99.0], "target_z": [99.0]}
            ).write_parquet(root / "features.parquet")
            pl.DataFrame(
                {"date": dates, "code": ["A"] * 8, "adj_open": [float(index + 1) for index in range(8)]}
            ).write_parquet(root / "market.parquet")
            pl.DataFrame(
                {"trade_date": dates, "code": ["A"] * 8, "valid_for_trading": [True] * 8}
            ).write_parquet(root / "state.parquet")
            metadata = rebuild_open_label_panel(
                feature_panel=root / "features.parquet",
                market_paths=(root / "market.parquet",),
                state_path=root / "state.parquet",
                output_path=root / "open.parquet",
            )
            row = pl.read_parquet(root / "open.parquet").to_dicts()[0]
            self.assertAlmostEqual(row["label_raw"], 7.0 / 2.0 - 1.0)
            self.assertEqual(row["entry_date"], dates[1])
            self.assertEqual(row["exit_date"], dates[6])
            self.assertEqual(metadata["version"], "screening-open-panel-v2")

    def test_rebuild_does_not_extend_horizon_around_suspension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(9)]
            pl.DataFrame({"date": [dates[0]], "code": ["A"], "feature": [1.0]}).write_parquet(
                root / "features.parquet"
            )
            pl.DataFrame(
                {"date": dates, "code": ["A"] * len(dates), "adj_open": [float(index + 1) for index in range(len(dates))]}
            ).write_parquet(root / "market.parquet")
            pl.DataFrame(
                {
                    "trade_date": dates,
                    "code": ["A"] * len(dates),
                    "valid_for_trading": [True, False, True, True, True, True, True, True, True],
                }
            ).write_parquet(root / "state.parquet")
            rebuild_open_label_panel(
                feature_panel=root / "features.parquet",
                market_paths=(root / "market.parquet",),
                state_path=root / "state.parquet",
                output_path=root / "open.parquet",
            )
            row = pl.read_parquet(root / "open.parquet").to_dicts()[0]
            self.assertEqual(row["entry_date"], dates[1])
            self.assertEqual(row["exit_date"], dates[6])
            self.assertIsNone(row["label_raw"])

    def test_merge_features_only_adds_explicit_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = {"date": [date(2026, 1, 2)], "code": ["A"]}
            pl.DataFrame({**key, "label_raw": [0.01]}).write_parquet(root / "panel.parquet")
            pl.DataFrame({**key, "fin_value__last": [1.0], "unrelated": [2.0]}).write_parquet(root / "source.parquet")
            metadata = merge_feature_columns(
                panel_path=root / "panel.parquet",
                source_path=root / "source.parquet",
                prefixes=("fin_",),
                output_path=root / "merged.parquet",
            )
            self.assertEqual(pl.read_parquet(root / "merged.parquet").columns, ["date", "code", "label_raw", "fin_value__last"])
            self.assertEqual(metadata["feature_count"], 1)

    def test_rolling_ridge_is_walk_forward_and_builds_top_n_returns(self) -> None:
        rows = []
        start = date(2025, 1, 3)
        for week in range(12):
            current = start + timedelta(days=7 * week)
            for asset in range(30):
                feature = float(asset - 15) / 15.0
                rows.append(
                    {
                        "date": current,
                        "code": f"A{asset:02d}",
                        "factor__mean5": feature,
                        "factor__last": feature,
                        "target_z": feature,
                        "label_raw": feature / 100.0,
                    }
                )
        policy = RollingRidgePolicy(
            train_weeks=6,
            validation_weeks=2,
            retrain_weeks=2,
            alphas=(0.1, 1.0),
            top_n=5,
        )
        predictions = rolling_ridge_predictions(
            pl.DataFrame(rows),
            ["factor__mean5", "factor__last"],
            policy,
        )
        weekly, metrics = top_n_metrics(predictions, policy)
        self.assertEqual(predictions.get_column("date").n_unique(), 5)
        self.assertEqual(weekly.height, 5)
        self.assertGreater(float(metrics["sharpe"]), 0.0)

    def test_single_factor_fixed_gates_select_stable_signal(self) -> None:
        rows = []
        generator = np.random.default_rng(7)
        current = date(2021, 1, 8)
        week = 0
        while current <= date(2026, 6, 26):
            for asset in range(100):
                signal = (asset - 49.5) / 50.0
                noise = float(generator.normal(0.0, 0.12))
                rows.append(
                    {
                        "date": current,
                        "code": f"A{asset:03d}",
                        "label_raw": signal / 50.0 + noise / 20.0,
                        "fm_huan001__mean5": signal,
                        "fm_huan001__last": signal + noise,
                    }
                )
            current += timedelta(days=7)
            week += 1
        result = screen_single_factors(pl.DataFrame(rows), ["huan001"], family_size=1)
        self.assertEqual(result.item(0, "final_status"), "统计与经济候选")
        self.assertEqual(result.item(0, "failure_reasons"), "")

    def test_single_factor_insufficient_weeks_is_candidate_level_rejection(self) -> None:
        panel = pl.DataFrame(
            {
                "date": [date(2021, 1, 8)] * 60,
                "code": [f"A{asset:03d}" for asset in range(60)],
                "label_raw": [float(asset) for asset in range(60)],
                "fm_sparse__mean5": [float(asset) for asset in range(60)],
                "fm_sparse__last": [float(asset) for asset in range(60)],
            }
        )
        result = screen_single_factors(panel, ["sparse"], family_size=216)
        self.assertEqual(result.item(0, "final_status"), "D级拒绝")
        self.assertEqual(result.item(0, "failure_reasons"), "发现期:有效周数不足")

    def test_economic_failure_warns_without_deleting_statistical_candidate(self) -> None:
        rows = []
        generator = np.random.default_rng(19)
        current = date(2021, 1, 8)
        week = 0
        while current <= date(2026, 6, 26):
            direction = -1.0 if week % 2 else 1.0
            for asset in range(100):
                signal = direction * (asset - 49.5) / 50.0
                rows.append(
                    {
                        "date": current,
                        "code": f"A{asset:03d}",
                        "label_raw": signal / 50.0 + float(generator.normal(0.0, 0.004)),
                        "fm_turnover__mean5": signal,
                        "fm_turnover__last": signal,
                    }
                )
            current += timedelta(days=7)
            week += 1
        result = screen_single_factors(pl.DataFrame(rows), ["turnover"], family_size=1)
        self.assertEqual(result.item(0, "final_status"), "统计候选")
        self.assertIn("经济警告:turnover", result.item(0, "economic_warning_reasons"))
        self.assertEqual(result.item(0, "statistical_failure_reasons"), "")

    def test_deduplication_keeps_best_confirmation_rank_ic_per_cluster(self) -> None:
        screened = pl.DataFrame(
            {
                "business_id": ["huan001", "huan002", "huan003"],
                "final_status": ["统计与经济候选"] * 3,
                "confirmation_rank_ic_mean": [0.03, 0.02, 0.01],
                "confirmation_ic_mean": [0.02, 0.03, 0.01],
                "discovery_rank_ic_hac_t": [4.0, 5.0, 3.0],
                "confirmation_sharpe": [1.0, 1.2, 0.8],
                "confirmation_mean_l1_turnover": [0.8, 0.7, 0.5],
            }
        )
        clusters = pl.DataFrame(
            {"business_id": ["huan001", "huan002", "huan003"], "cluster_id": [1, 1, 2]}
        )
        representatives, ranked = select_cluster_representatives(screened, clusters)
        self.assertEqual(representatives.get_column("business_id").to_list(), ["huan001", "huan003"])
        self.assertEqual(ranked.filter(pl.col("business_id") == "huan002").item(0, "cluster_rank"), 2)

    def test_daily_factor_clustering_uses_absolute_similarity(self) -> None:
        panel = pl.DataFrame(
            {
                "date": [date(2025, 1, 1)] * 4 + [date(2025, 1, 2)] * 4,
                "asset": ["A", "B", "C", "D"] * 2,
                "fm_huan001": [1.0, 2.0, 3.0, 4.0, 2.0, 4.0, 6.0, 8.0],
                "fm_huan002": [-1.0, -2.0, -3.0, -4.0, -2.0, -4.0, -6.0, -8.0],
                "fm_huan003": [1.0, -1.0, -1.0, 1.0, -1.0, 1.0, 1.0, -1.0],
            }
        )
        mapping, _, _ = cluster_daily_factors((panel.lazy(),), ("huan001", "huan002", "huan003"))
        clusters = dict(mapping.select("business_id", "cluster_id").iter_rows())
        self.assertEqual(clusters["huan001"], clusters["huan002"])
        self.assertNotEqual(clusters["huan001"], clusters["huan003"])

    def test_daily_factor_clustering_accepts_single_survivor(self) -> None:
        panel = pl.DataFrame(
            {"date": [date(2025, 1, 1)], "asset": ["A"], "fm_only": [1.0]}
        )
        mapping, pearson, spearman = cluster_daily_factors((panel.lazy(),), ("only",))
        self.assertEqual(mapping.to_dicts(), [{"business_id": "only", "cluster_id": 1}])
        self.assertEqual(pearson.item(0, "only"), 1.0)
        self.assertEqual(spearman.item(0, "only"), 1.0)


if __name__ == "__main__":
    unittest.main()
