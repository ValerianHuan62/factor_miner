"""V0.4 覆盖图谱快照必须内容寻址且可发现篡改。"""

from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.coverage_cluster import CoverageClusterAssignments
from factor_miner.coverage_performance import (
    AnnualFactorPerformance,
    FactorPerformanceSummary,
)
from factor_miner.coverage_regime import RegimeFactorProfile
from factor_miner.coverage_schema import (
    CoverageClusterPolicy,
    CoverageFactorNode,
    CoverageGraphSpec,
    CoveragePairPolicy,
    CoverageStructuralPolicy,
    registered_coverage_graph_spec,
)
from factor_miner.coverage_signal import DailyICIdentity, SignalPatternEdge
from factor_miner.coverage_snapshot import (
    _write_parquet,
    coverage_catalog_sha256,
    publish_coverage_graph,
    verify_coverage_graph,
)
from factor_miner.coverage_structure import StructuralEdge
from factor_miner.errors import FactorMinerError
from factor_miner.regime_schema import RegimeAnnotation


SNAPSHOT_ID = "regsnap_" + "a" * 24


def _annotations() -> tuple[RegimeAnnotation, ...]:
    return tuple(
        RegimeAnnotation(
            regime_snapshot_id=SNAPSHOT_ID,
            canonical_state_id=state,
            economic_label=label,
            description=f"{label}的人工解释",
            author="researcher",
            created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        for state, label in (
            (0, "高波动下跌·弱宽度"),
            (1, "低波动平稳·宽度中性"),
        )
    )


def _nodes() -> tuple[CoverageFactorNode, ...]:
    return tuple(
        CoverageFactorNode(
            factor_id=factor_id,
            factor_name_cn=f"{factor_id}中文因子",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr=f"{factor_id}(close)",
            formula_hash=digit * 64,
            input_fields=("close",),
            operator_tags=("ma",),
            windows=(20,),
            lookback_window=20,
            lag_days=1,
            category="momentum",
            subcategory="price",
            description="合成因子",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.1,
            source="synthetic.yaml",
        )
        for factor_id, digit in (("factor_a", "1"), ("factor_b", "2"))
    )


def _registered(nodes: tuple[CoverageFactorNode, ...]):
    spec = CoverageGraphSpec(
        factor_catalog_sha256=coverage_catalog_sha256(nodes),
        daily_ic_manifest_sha256="2" * 64,
        regime_snapshot_id=SNAPSHOT_ID,
        regime_annotations=_annotations(),
        evaluation_policy_id="evalpol_" + "3" * 24,
        data_release_id="release-v1",
        label_id="label-v1",
        visible_start=date(2024, 1, 1),
        visible_end=date(2024, 12, 31),
        pair_policy=CoveragePairPolicy(min_overlap_dates=3),
        structural_policy=CoverageStructuralPolicy(),
        cluster_policy=CoverageClusterPolicy(),
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
    return registered_coverage_graph_spec(spec)


def _structural() -> tuple[StructuralEdge, ...]:
    return (
        StructuralEdge(
            factor_a="factor_a",
            factor_b="factor_b",
            exact_ast_match=False,
            exact_formula_match=False,
            field_similarity=1.0,
            operator_similarity=1.0,
            window_similarity=1.0,
            category_similarity=1.0,
            structural_similarity=1.0,
            strong_structure_edge=True,
        ),
    )


def _signal() -> tuple[SignalPatternEdge, ...]:
    identity = DailyICIdentity(
        evaluation_policy_id="evalpol_" + "3" * 24,
        data_release_id="release-v1",
        label_id="label-v1",
        visible_start=date(2024, 1, 1),
        visible_end=date(2024, 12, 31),
    )
    return (
        SignalPatternEdge(
            factor_a="factor_a",
            factor_b="factor_b",
            identity_a=identity,
            identity_b=identity,
            evaluation_policy_id=identity.evaluation_policy_id,
            data_release_id=identity.data_release_id,
            label_id=identity.label_id,
            visible_start=identity.visible_start,
            visible_end=identity.visible_end,
            raw_correlation=0.8,
            aligned_correlation=0.8,
            absolute_correlation=0.8,
            overlap_dates=200,
            union_dates=200,
            missing_ratio=0.0,
            comparable=True,
            strong_signal_edge=True,
        ),
    )


def _profiles() -> tuple[RegimeFactorProfile, ...]:
    return tuple(
        RegimeFactorProfile(
            factor_id=factor,
            canonical_state_id=state,
            economic_label=label,
            annotation_description=f"{label}解释",
            valid_dates=100,
            probability_mass=50.0,
            weighted_mean_rank_ic=0.02,
            weighted_std_rank_ic=0.1,
            weighted_icir=0.2,
            positive_probability_ratio=0.55,
            mean_max_probability=0.9,
            mean_normalized_entropy=0.2,
        )
        for factor in ("factor_a", "factor_b")
        for state, label in (
            (0, "高波动下跌·弱宽度"),
            (1, "低波动平稳·宽度中性"),
        )
    )


def _performance() -> tuple[FactorPerformanceSummary, ...]:
    return tuple(
        FactorPerformanceSummary(
            factor_id=factor,
            total_dates=200,
            valid_dates=200,
            invalid_dates=0,
            mean_rank_ic=0.02,
            std_rank_ic=0.1,
            icir=0.2,
            positive_rank_ic_ratio=0.55,
            negative_rank_ic_ratio=0.45,
            zero_rank_ic_ratio=0.0,
            median_coverage=0.9,
            legacy_mean_ic=0.1,
            legacy_icir=0.4,
            legacy_mean_ic_difference=-0.08,
            legacy_icir_difference=-0.2,
            annual_summaries=(
                AnnualFactorPerformance(
                    year=2024,
                    valid_dates=200,
                    mean_rank_ic=0.02,
                    std_rank_ic=0.1,
                    icir=0.2,
                    positive_rank_ic_ratio=0.55,
                    median_coverage=0.9,
                ),
            ),
        )
        for factor in ("factor_a", "factor_b")
    )


def _clusters() -> CoverageClusterAssignments:
    return CoverageClusterAssignments(
        algorithm="deterministic_connected_components",
        factor_to_structural_cluster={
            "factor_a": "struct_x",
            "factor_b": "struct_x",
        },
        factor_to_signal_cluster={
            "factor_a": "signal_x",
            "factor_b": "signal_x",
        },
        structural_clusters={"struct_x": ("factor_a", "factor_b")},
        signal_clusters={"signal_x": ("factor_a", "factor_b")},
    )


class CoverageSnapshotTest(unittest.TestCase):
    """图谱身份覆盖每个正式文件字节。"""

    def test_parquet_writer_infers_nullable_fields_from_all_records(self) -> None:
        """前百条不可比较边不得把后续相关系数误推断成 Null。"""

        identity = _signal()[0].identity_a
        unavailable = SignalPatternEdge(
            factor_a="factor_a",
            factor_b="factor_b",
            identity_a=identity,
            identity_b=identity,
            evaluation_policy_id=identity.evaluation_policy_id,
            data_release_id=identity.data_release_id,
            label_id=identity.label_id,
            visible_start=identity.visible_start,
            visible_end=identity.visible_end,
            overlap_dates=0,
            union_dates=200,
            missing_ratio=1.0,
            comparable=False,
            strong_signal_edge=False,
            reason="因子不可评价",
        )
        records = (unavailable,) * 101 + _signal()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signal_edges.parquet"

            _write_parquet(path, records)

            result = pl.read_parquet(path)
            self.assertEqual(result.schema["raw_correlation"], pl.Float64)
            self.assertEqual(result.get_column("raw_correlation")[-1], 0.8)

    def test_publish_is_idempotent_and_verify_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = _nodes()
            first = publish_coverage_graph(
                artifact_root=root,
                registered_spec=_registered(nodes),
                nodes=nodes,
                structural_edges=_structural(),
                signal_edges=_signal(),
                regime_profiles=_profiles(),
                factor_performance=_performance(),
                clusters=_clusters(),
            )
            second = publish_coverage_graph(
                artifact_root=root,
                registered_spec=_registered(nodes),
                nodes=nodes,
                structural_edges=_structural(),
                signal_edges=_signal(),
                regime_profiles=_profiles(),
                factor_performance=_performance(),
                clusters=_clusters(),
            )

            self.assertEqual(first.coverage_graph_id, second.coverage_graph_id)
            self.assertEqual(
                {path.name for path in first.snapshot_root.iterdir()},
                {
                    "build_spec.json",
                    "factor_nodes.jsonl",
                    "structural_edges.parquet",
                    "signal_pattern_edges.parquet",
                    "factor_performance.parquet",
                    "regime_profiles.parquet",
                    "cluster_assignments.json",
                    "coverage_summary.json",
                    "manifest.json",
                },
            )
            verified = verify_coverage_graph(first.snapshot_root)
            self.assertEqual(verified.coverage_graph_id, first.coverage_graph_id)

            (first.snapshot_root / "factor_nodes.jsonl").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FactorMinerError, "哈希"):
                verify_coverage_graph(first.snapshot_root)

    def test_node_change_with_matching_catalog_identity_changes_graph_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = _nodes()
            changed = (
                nodes[0].model_copy(
                    update={
                        "description": "改变后的合成因子",
                    }
                ),
                nodes[1],
            )

            first = publish_coverage_graph(
                artifact_root=root,
                registered_spec=_registered(nodes),
                nodes=nodes,
                structural_edges=_structural(),
                signal_edges=_signal(),
                regime_profiles=_profiles(),
                factor_performance=_performance(),
                clusters=_clusters(),
            )
            second = publish_coverage_graph(
                artifact_root=root,
                registered_spec=_registered(changed),
                nodes=changed,
                structural_edges=_structural(),
                signal_edges=_signal(),
                regime_profiles=_profiles(),
                factor_performance=_performance(),
                clusters=_clusters(),
            )

            self.assertNotEqual(first.coverage_graph_id, second.coverage_graph_id)


if __name__ == "__main__":
    unittest.main()
