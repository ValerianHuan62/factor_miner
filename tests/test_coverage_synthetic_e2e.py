"""V0.4 从 YAML、每日 IC 和状态快照到图谱的合成端到端。"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.coverage_catalog import load_legacy_factor_catalog
from factor_miner.coverage_schema import (
    CoverageClusterPolicy,
    CoverageGraphSpec,
    CoveragePairPolicy,
    CoverageRegimePolicy,
    CoverageStructuralPolicy,
    registered_coverage_graph_spec,
)
from factor_miner.coverage_snapshot import (
    coverage_catalog_sha256,
    verify_coverage_graph,
)
from factor_miner.coverage_workflow import build_coverage_graph
from factor_miner.regime_schema import RegimeAnnotation
from factor_miner.regime_snapshot import publish_regime_snapshot
from tests.test_coverage_catalog import YAML_SAMPLE
from tests.test_regime_snapshot import input_provenance, research_fixture


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class CoverageSyntheticE2ETest(unittest.TestCase):
    """Mac 合成测试不接触公司真实数据或真实 IC。"""

    def test_builds_named_dual_layer_graph_from_long_daily_ic(self) -> None:
        features, _, report, deployment = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regime = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            filtered = pl.read_parquet(
                regime.snapshot_root / "filtered_regimes.parquet"
            ).filter(pl.col("status") == "ok")
            use_dates = filtered.get_column("earliest_use_date").head(12).to_list()
            catalog_path = root / "factors.yaml"
            catalog_path.write_text(YAML_SAMPLE, encoding="utf-8")
            nodes = load_legacy_factor_catalog(catalog_path)
            daily_path = root / "daily_ic.parquet"
            daily = pl.DataFrame(
                {
                    "factor_id": [
                        factor_id
                        for factor_id in ("alpha_003_n10_v1", "alpha_007_n50_v1")
                        for _ in use_dates
                    ],
                    "date": use_dates * 2,
                    "rank_ic": [
                        float(index + 1) / 100
                        for index in range(len(use_dates))
                    ]
                    + [
                        -float(index + 1) / 100
                        for index in range(len(use_dates))
                    ],
                    "coverage": [0.9] * (2 * len(use_dates)),
                    "evaluation_policy_id": ["evalpol_" + "3" * 24]
                    * (2 * len(use_dates)),
                    "data_release_id": ["synthetic-release-v1"]
                    * (2 * len(use_dates)),
                    "label_id": ["label-v1"] * (2 * len(use_dates)),
                    "visible_start": [min(use_dates)] * (2 * len(use_dates)),
                    "visible_end": [max(use_dates)] * (2 * len(use_dates)),
                },
                schema_overrides={
                    "date": pl.Date,
                    "visible_start": pl.Date,
                    "visible_end": pl.Date,
                },
            )
            daily.write_parquet(daily_path)
            annotations = tuple(
                RegimeAnnotation(
                    regime_snapshot_id=regime.regime_snapshot_id,
                    canonical_state_id=state,
                    economic_label=label,
                    description=f"{label}的合成人工解释",
                    author="researcher",
                    created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
                )
                for state, label in (
                    (0, "高波动下跌·弱宽度"),
                    (1, "低波动平稳·宽度中性"),
                )
            )
            spec = CoverageGraphSpec(
                factor_catalog_sha256=coverage_catalog_sha256(nodes),
                daily_ic_manifest_sha256=_sha256(daily_path),
                regime_snapshot_id=regime.regime_snapshot_id,
                regime_annotations=annotations,
                evaluation_policy_id="evalpol_" + "3" * 24,
                data_release_id="synthetic-release-v1",
                label_id="label-v1",
                visible_start=min(use_dates),
                visible_end=max(use_dates),
                pair_policy=CoveragePairPolicy(
                    min_overlap_dates=3,
                    max_missing_ratio=0.2,
                ),
                structural_policy=CoverageStructuralPolicy(),
                cluster_policy=CoverageClusterPolicy(),
                regime_policy=CoverageRegimePolicy(
                    min_valid_dates_per_state=1,
                    min_probability_mass=0.01,
                ),
                algorithm_version="coverage-v0.4.0",
                random_seed=0,
                runtime_provenance={
                    "code_commit": "4" * 40,
                    "config_hash": "5" * 64,
                    "uv_lock_sha256": "6" * 64,
                    "release_manifest_sha256": "7" * 64,
                },
                created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            )

            result = build_coverage_graph(
                catalog_path=catalog_path,
                daily_ic_path=daily_path,
                regime_snapshot_root=regime.snapshot_root,
                artifact_root=root,
                registered_spec=registered_coverage_graph_spec(spec),
            )

            verified = verify_coverage_graph(result.snapshot_root)
            self.assertEqual(verified.coverage_graph_id, result.coverage_graph_id)
            signal = pl.read_parquet(
                result.snapshot_root / "signal_pattern_edges.parquet"
            )
            self.assertEqual(signal.height, 1)
            self.assertTrue(signal.row(0, named=True)["comparable"])
            self.assertAlmostEqual(
                signal.row(0, named=True)["aligned_correlation"],
                1.0,
            )
            profiles = pl.read_parquet(
                result.snapshot_root / "regime_profiles.parquet"
            )
            self.assertEqual(
                set(profiles.get_column("economic_label").to_list()),
                {"高波动下跌·弱宽度", "低波动平稳·宽度中性"},
            )
            performance = pl.read_parquet(
                result.snapshot_root / "factor_performance.parquet"
            )
            self.assertEqual(performance.height, 2)
            summary = json.loads(
                (result.snapshot_root / "coverage_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["factor_count"], 2)
            self.assertEqual(summary["pair_count"], 1)


if __name__ == "__main__":
    unittest.main()
