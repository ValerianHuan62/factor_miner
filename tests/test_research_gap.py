"""任务二 coverage gap report 的 synthetic graph 边界测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import polars as pl

from factor_miner.canonical import canonical_json_bytes, sha256_json
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
    CoverageGraphManifest,
    _write_parquet,
    coverage_catalog_sha256,
    publish_coverage_graph,
)
from factor_miner.coverage_structure import StructuralEdge
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_brief import GapSelectionPolicy
from factor_miner.research_evolution_schema import (
    CoverageGapCard,
    DesignDiversityPolicy,
    ResearchMemorySnapshot,
)
from factor_miner.research_gap import (
    _sanitize_field_alias,
    _sanitize_operator_family,
    build_coverage_gap_report,
    build_sanitized_gap_brief,
    load_verified_coverage_graph,
)
from factor_miner.regime_schema import RegimeAnnotation

SNAPSHOT_ID = "regsnap_" + "a" * 24
NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _annotations() -> tuple[RegimeAnnotation, ...]:
    return (
        RegimeAnnotation(
            regime_snapshot_id=SNAPSHOT_ID,
            canonical_state_id=0,
            economic_label="高波动下跌·弱宽度",
            description="合成状态一",
            author="researcher",
            created_at=NOW,
        ),
        RegimeAnnotation(
            regime_snapshot_id=SNAPSHOT_ID,
            canonical_state_id=1,
            economic_label="低波动平稳·宽度中性",
            description="合成状态二",
            author="researcher",
            created_at=NOW,
        ),
    )


def _nodes_dense() -> tuple[CoverageFactorNode, ...]:
    return (
        CoverageFactorNode(
            factor_id="factor_a",
            factor_name_cn="收盘价滚动均值",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr="ma(close, 5)",
            formula_hash="1" * 64,
            input_fields=("close",),
            operator_tags=("ma",),
            windows=(5,),
            lookback_window=5,
            lag_days=1,
            category="momentum",
            subcategory="price",
            description="synthetic a",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.02,
            legacy_icir=0.10,
            source="synthetic.yaml",
        ),
        CoverageFactorNode(
            factor_id="factor_b",
            factor_name_cn="成交量滚动均值",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr="ma(volume, 20)",
            formula_hash="2" * 64,
            input_fields=("volume",),
            operator_tags=("ma",),
            windows=(20,),
            lookback_window=20,
            lag_days=1,
            category="liquidity",
            subcategory="volume",
            description="synthetic b",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.03,
            legacy_icir=0.11,
            source="synthetic.yaml",
        ),
        CoverageFactorNode(
            factor_id="factor_c",
            factor_name_cn="收盘价截面排名",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr="rank(close)",
            formula_hash="3" * 64,
            input_fields=("close",),
            operator_tags=("rank",),
            windows=(5,),
            lookback_window=5,
            lag_days=1,
            category="cross_sectional",
            subcategory="rank",
            description="synthetic c",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.01,
            legacy_icir=0.08,
            source="synthetic.yaml",
        ),
        CoverageFactorNode(
            factor_id="factor_d",
            factor_name_cn="价量交互相关",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr="corr(close, amount, 60)",
            formula_hash="4" * 64,
            input_fields=("amount", "close"),
            operator_tags=("corr",),
            windows=(60,),
            lookback_window=60,
            lag_days=1,
            category="interaction",
            subcategory="price_volume",
            description="synthetic d",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.04,
            legacy_icir=0.12,
            source="synthetic.yaml",
        ),
    )


def _nodes_empty() -> tuple[CoverageFactorNode, ...]:
    return (
        CoverageFactorNode(
            factor_id="factor_only",
            factor_name_cn="单一收盘价滚动均值",
            node_status="active",
            structure_source="legacy_formula_metadata",
            formula_expr="ma(close, 5)",
            formula_hash="9" * 64,
            input_fields=("close",),
            operator_tags=("ma",),
            windows=(5,),
            lookback_window=5,
            lag_days=1,
            category="momentum",
            subcategory="price",
            description="synthetic only",
            preprocess_method="none",
            neutralization="none",
            orientation_sign=1,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=0.02,
            legacy_icir=0.10,
            source="synthetic.yaml",
        ),
    )


def _registered(nodes: tuple[CoverageFactorNode, ...]):
    spec = CoverageGraphSpec(
        factor_catalog_sha256=coverage_catalog_sha256(nodes),
        daily_ic_manifest_sha256="f" * 64,
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
        created_at=NOW,
    )
    return registered_coverage_graph_spec(spec)


def _structural_dense() -> tuple[StructuralEdge, ...]:
    return (
        StructuralEdge(factor_a="factor_a", factor_b="factor_b", exact_ast_match=False, exact_formula_match=False, field_similarity=0.0, operator_similarity=1.0, window_similarity=0.5, category_similarity=0.0, structural_similarity=0.40, strong_structure_edge=False),
        StructuralEdge(factor_a="factor_a", factor_b="factor_c", exact_ast_match=False, exact_formula_match=False, field_similarity=1.0, operator_similarity=0.0, window_similarity=1.0, category_similarity=0.0, structural_similarity=0.55, strong_structure_edge=False),
        StructuralEdge(factor_a="factor_a", factor_b="factor_d", exact_ast_match=False, exact_formula_match=False, field_similarity=0.5, operator_similarity=0.0, window_similarity=0.10, category_similarity=0.0, structural_similarity=0.20, strong_structure_edge=False),
        StructuralEdge(factor_a="factor_b", factor_b="factor_c", exact_ast_match=False, exact_formula_match=False, field_similarity=0.0, operator_similarity=0.0, window_similarity=0.5, category_similarity=0.0, structural_similarity=0.10, strong_structure_edge=False),
        StructuralEdge(factor_a="factor_b", factor_b="factor_d", exact_ast_match=False, exact_formula_match=False, field_similarity=0.0, operator_similarity=0.0, window_similarity=0.20, category_similarity=0.0, structural_similarity=0.05, strong_structure_edge=False),
        StructuralEdge(factor_a="factor_c", factor_b="factor_d", exact_ast_match=False, exact_formula_match=False, field_similarity=0.5, operator_similarity=0.0, window_similarity=0.10, category_similarity=0.0, structural_similarity=0.20, strong_structure_edge=False),
    )


def _signal_dense() -> tuple[SignalPatternEdge, ...]:
    identity = DailyICIdentity(
        evaluation_policy_id="evalpol_" + "3" * 24,
        data_release_id="release-v1",
        label_id="label-v1",
        visible_start=date(2024, 1, 1),
        visible_end=date(2024, 12, 31),
    )
    return (
        SignalPatternEdge(factor_a="factor_a", factor_b="factor_b", identity_a=identity, identity_b=identity, evaluation_policy_id=identity.evaluation_policy_id, data_release_id=identity.data_release_id, label_id=identity.label_id, visible_start=identity.visible_start, visible_end=identity.visible_end, raw_correlation=0.2, aligned_correlation=0.2, absolute_correlation=0.2, overlap_dates=200, union_dates=200, missing_ratio=0.0, comparable=True, strong_signal_edge=False, reason=None),
        SignalPatternEdge(factor_a="factor_a", factor_b="factor_c", identity_a=identity, identity_b=identity, evaluation_policy_id=identity.evaluation_policy_id, data_release_id=identity.data_release_id, label_id=identity.label_id, visible_start=identity.visible_start, visible_end=identity.visible_end, raw_correlation=0.8, aligned_correlation=0.8, absolute_correlation=0.8, overlap_dates=200, union_dates=200, missing_ratio=0.0, comparable=True, strong_signal_edge=True, reason=None),
        SignalPatternEdge(factor_a="factor_a", factor_b="factor_d", identity_a=identity, identity_b=identity, overlap_dates=2, union_dates=200, missing_ratio=0.99, comparable=False, strong_signal_edge=False, reason="因子对重叠日期少于冻结阈值"),
        SignalPatternEdge(factor_a="factor_b", factor_b="factor_c", identity_a=identity, identity_b=identity, overlap_dates=1, union_dates=200, missing_ratio=0.995, comparable=False, strong_signal_edge=False, reason="因子对重叠日期少于冻结阈值"),
        SignalPatternEdge(factor_a="factor_b", factor_b="factor_d", identity_a=identity, identity_b=identity, evaluation_policy_id=identity.evaluation_policy_id, data_release_id=identity.data_release_id, label_id=identity.label_id, visible_start=identity.visible_start, visible_end=identity.visible_end, raw_correlation=0.1, aligned_correlation=0.1, absolute_correlation=0.1, overlap_dates=180, union_dates=200, missing_ratio=0.10, comparable=True, strong_signal_edge=False, reason=None),
        SignalPatternEdge(factor_a="factor_c", factor_b="factor_d", identity_a=identity, identity_b=identity, evaluation_policy_id=identity.evaluation_policy_id, data_release_id=identity.data_release_id, label_id=identity.label_id, visible_start=identity.visible_start, visible_end=identity.visible_end, raw_correlation=0.3, aligned_correlation=0.3, absolute_correlation=0.3, overlap_dates=190, union_dates=200, missing_ratio=0.05, comparable=True, strong_signal_edge=False, reason=None),
    )


def _performance_dense() -> tuple[FactorPerformanceSummary, ...]:
    return (
        FactorPerformanceSummary(
            factor_id="factor_a",
            total_dates=200,
            valid_dates=180,
            invalid_dates=20,
            mean_rank_ic=0.02,
            std_rank_ic=0.10,
            icir=0.20,
            positive_rank_ic_ratio=0.55,
            negative_rank_ic_ratio=0.45,
            zero_rank_ic_ratio=0.0,
            median_coverage=0.45,
            annual_summaries=(AnnualFactorPerformance(year=2024, valid_dates=180, mean_rank_ic=0.02, std_rank_ic=0.10, icir=0.20, positive_rank_ic_ratio=0.55, median_coverage=0.45),),
        ),
        FactorPerformanceSummary(
            factor_id="factor_b",
            total_dates=200,
            valid_dates=200,
            invalid_dates=0,
            mean_rank_ic=0.03,
            std_rank_ic=0.10,
            icir=0.30,
            positive_rank_ic_ratio=0.60,
            negative_rank_ic_ratio=0.40,
            zero_rank_ic_ratio=0.0,
            median_coverage=0.90,
            annual_summaries=(AnnualFactorPerformance(year=2024, valid_dates=200, mean_rank_ic=0.03, std_rank_ic=0.10, icir=0.30, positive_rank_ic_ratio=0.60, median_coverage=0.90),),
        ),
        FactorPerformanceSummary(
            factor_id="factor_c",
            status="unavailable",
            reason="没有有效每日 IC",
            total_dates=200,
            valid_dates=0,
            invalid_dates=200,
            median_coverage=None,
        ),
        FactorPerformanceSummary(
            factor_id="factor_d",
            total_dates=200,
            valid_dates=200,
            invalid_dates=0,
            mean_rank_ic=0.04,
            std_rank_ic=0.10,
            icir=0.40,
            positive_rank_ic_ratio=0.65,
            negative_rank_ic_ratio=0.35,
            zero_rank_ic_ratio=0.0,
            median_coverage=0.85,
            annual_summaries=(AnnualFactorPerformance(year=2024, valid_dates=200, mean_rank_ic=0.04, std_rank_ic=0.10, icir=0.40, positive_rank_ic_ratio=0.65, median_coverage=0.85),),
        ),
    )


def _profiles_dense() -> tuple[RegimeFactorProfile, ...]:
    return (
        RegimeFactorProfile(factor_id="factor_a", status="ok", reason=None, canonical_state_id=0, economic_label="高波动下跌·弱宽度", annotation_description="合成状态一", valid_dates=120, probability_mass=65.0, weighted_mean_rank_ic=0.11, weighted_std_rank_ic=0.12, weighted_icir=0.91, positive_probability_ratio=0.70, mean_max_probability=0.30, mean_normalized_entropy=0.25),
        RegimeFactorProfile(factor_id="factor_a", status="unavailable", reason="状态概率质量不足", canonical_state_id=1, economic_label="低波动平稳·宽度中性", annotation_description="合成状态二", valid_dates=10, probability_mass=8.0),
        RegimeFactorProfile(factor_id="factor_b", status="ok", reason=None, canonical_state_id=0, economic_label="高波动下跌·弱宽度", annotation_description="合成状态一", valid_dates=120, probability_mass=70.0, weighted_mean_rank_ic=0.09, weighted_std_rank_ic=0.10, weighted_icir=0.90, positive_probability_ratio=0.68, mean_max_probability=0.32, mean_normalized_entropy=0.22),
        RegimeFactorProfile(factor_id="factor_b", status="unavailable", reason="状态概率质量不足", canonical_state_id=1, economic_label="低波动平稳·宽度中性", annotation_description="合成状态二", valid_dates=12, probability_mass=9.0),
        RegimeFactorProfile(factor_id="factor_c", status="ok", reason=None, canonical_state_id=0, economic_label="高波动下跌·弱宽度", annotation_description="合成状态一", valid_dates=120, probability_mass=72.0, weighted_mean_rank_ic=0.08, weighted_std_rank_ic=0.10, weighted_icir=0.80, positive_probability_ratio=0.66, mean_max_probability=0.29, mean_normalized_entropy=0.21),
        RegimeFactorProfile(factor_id="factor_c", status="unavailable", reason="状态概率质量不足", canonical_state_id=1, economic_label="低波动平稳·宽度中性", annotation_description="合成状态二", valid_dates=9, probability_mass=7.0),
        RegimeFactorProfile(factor_id="factor_d", status="ok", reason=None, canonical_state_id=0, economic_label="高波动下跌·弱宽度", annotation_description="合成状态一", valid_dates=120, probability_mass=75.0, weighted_mean_rank_ic=0.07, weighted_std_rank_ic=0.09, weighted_icir=0.78, positive_probability_ratio=0.65, mean_max_probability=0.28, mean_normalized_entropy=0.20),
        RegimeFactorProfile(factor_id="factor_d", status="ok", reason=None, canonical_state_id=1, economic_label="低波动平稳·宽度中性", annotation_description="合成状态二", valid_dates=115, probability_mass=68.0, weighted_mean_rank_ic=0.06, weighted_std_rank_ic=0.08, weighted_icir=0.75, positive_probability_ratio=0.64, mean_max_probability=0.55, mean_normalized_entropy=0.65),
    )


def _clusters_dense() -> CoverageClusterAssignments:
    return CoverageClusterAssignments(
        algorithm="deterministic_connected_components",
        factor_to_structural_cluster={
            "factor_a": "struct_a",
            "factor_b": "struct_b",
            "factor_c": "struct_c",
            "factor_d": "struct_d",
        },
        factor_to_signal_cluster={
            "factor_a": "signal_ab",
            "factor_b": "signal_ab",
            "factor_c": "signal_cd",
            "factor_d": "signal_cd",
        },
        structural_clusters={
            "struct_a": ("factor_a",),
            "struct_b": ("factor_b",),
            "struct_c": ("factor_c",),
            "struct_d": ("factor_d",),
        },
        signal_clusters={
            "signal_ab": ("factor_a", "factor_b"),
            "signal_cd": ("factor_c", "factor_d"),
        },
    )


def _structural_empty() -> tuple[StructuralEdge, ...]:
    return ()


def _signal_empty() -> tuple[SignalPatternEdge, ...]:
    return ()


def _performance_empty() -> tuple[FactorPerformanceSummary, ...]:
    return (
        FactorPerformanceSummary(
            factor_id="factor_only",
            total_dates=200,
            valid_dates=200,
            invalid_dates=0,
            mean_rank_ic=0.02,
            std_rank_ic=0.10,
            icir=0.20,
            positive_rank_ic_ratio=0.55,
            negative_rank_ic_ratio=0.45,
            zero_rank_ic_ratio=0.0,
            median_coverage=0.90,
            annual_summaries=(AnnualFactorPerformance(year=2024, valid_dates=200, mean_rank_ic=0.02, std_rank_ic=0.10, icir=0.20, positive_rank_ic_ratio=0.55, median_coverage=0.90),),
        ),
    )


def _profiles_empty() -> tuple[RegimeFactorProfile, ...]:
    return (
        RegimeFactorProfile(factor_id="factor_only", status="ok", reason=None, canonical_state_id=0, economic_label="高波动下跌·弱宽度", annotation_description="合成状态一", valid_dates=120, probability_mass=70.0, weighted_mean_rank_ic=0.09, weighted_std_rank_ic=0.10, weighted_icir=0.90, positive_probability_ratio=0.68, mean_max_probability=0.32, mean_normalized_entropy=0.22),
        RegimeFactorProfile(factor_id="factor_only", status="ok", reason=None, canonical_state_id=1, economic_label="低波动平稳·宽度中性", annotation_description="合成状态二", valid_dates=120, probability_mass=72.0, weighted_mean_rank_ic=0.08, weighted_std_rank_ic=0.10, weighted_icir=0.80, positive_probability_ratio=0.66, mean_max_probability=0.30, mean_normalized_entropy=0.21),
    )


def _clusters_empty() -> CoverageClusterAssignments:
    return CoverageClusterAssignments(
        algorithm="deterministic_connected_components",
        factor_to_structural_cluster={"factor_only": "struct_only"},
        factor_to_signal_cluster={"factor_only": "signal_only"},
        structural_clusters={"struct_only": ("factor_only",)},
        signal_clusters={"signal_only": ("factor_only",)},
    )


def _publish_graph(root: Path, *, dense: bool) -> Path:
    if dense:
        result = publish_coverage_graph(
            artifact_root=root,
            registered_spec=_registered(_nodes_dense()),
            nodes=_nodes_dense(),
            structural_edges=_structural_dense(),
            signal_edges=_signal_dense(),
            factor_performance=_performance_dense(),
            regime_profiles=_profiles_dense(),
            clusters=_clusters_dense(),
        )
    else:
        result = publish_coverage_graph(
            artifact_root=root,
            registered_spec=_registered(_nodes_empty()),
            nodes=_nodes_empty(),
            structural_edges=_structural_empty(),
            signal_edges=_signal_empty(),
            factor_performance=_performance_empty(),
            regime_profiles=_profiles_empty(),
            clusters=_clusters_empty(),
        )
    return result.snapshot_root


def _memory_snapshot(graph_root: Path) -> ResearchMemorySnapshot:
    manifest = CoverageGraphManifest.model_validate_json((graph_root / "manifest.json").read_bytes())
    manifest_hash = sha256_json(manifest.model_dump(mode="json"))
    return ResearchMemorySnapshot(
        memory_snapshot_id="memorysnap_" + "1" * 24,
        created_at=NOW,
        cutoff_at=NOW,
        source_family_ids=("llmfamily_" + "2" * 24,),
        source_seal_ids=("seal_" + "3" * 24,),
        source_run_ids=("run_" + "4" * 24,),
        entry_ids=(),
        entry_hashes=(),
        exclusion_rules=("polluted_family",),
        entry_count=0,
        coverage_graph_id=manifest.coverage_graph_id,
        coverage_graph_manifest_hash=manifest_hash,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rewrite_manifest(graph_root: Path) -> Path:
    manifest = CoverageGraphManifest.model_validate_json((graph_root / "manifest.json").read_bytes())
    file_names = tuple(sorted(name for name in manifest.files))
    new_hashes = {name: _sha256_file(graph_root / name) for name in file_names}
    payload = manifest.model_dump(mode="json")
    payload["files"] = new_hashes
    payload.pop("coverage_graph_id")
    new_id = f"covgraph_{sha256_json(payload)[:24]}"
    rewritten = CoverageGraphManifest(coverage_graph_id=new_id, **payload)
    (graph_root / "manifest.json").write_bytes(canonical_json_bytes(rewritten.model_dump(mode="json")) + b"\n")
    target = graph_root.parent / new_id
    graph_root.rename(target)
    return target


def _duplicate_factor_performance_key(graph_root: Path) -> Path:
    frame = pl.read_parquet(graph_root / "factor_performance.parquet")
    duplicated = pl.DataFrame([frame.row(0, named=True), frame.row(0, named=True), *frame.to_dicts()[2:]], infer_schema_length=None)
    duplicated.write_parquet(graph_root / "factor_performance.parquet", compression="zstd", statistics=True)
    return _rewrite_manifest(graph_root)


def _mutate_regime_profile_state(graph_root: Path) -> Path:
    frame = pl.read_parquet(graph_root / "regime_profiles.parquet")
    rows = frame.to_dicts()
    rows[0]["canonical_state_id"] = 9
    mutated = pl.DataFrame(rows, infer_schema_length=None)
    mutated.write_parquet(graph_root / "regime_profiles.parquet", compression="zstd", statistics=True)
    return _rewrite_manifest(graph_root)


class ResearchGapTests(unittest.TestCase):
    """只允许 synthetic graph，并在输入边界层做硬失败。"""

    def test_legacy_cross_sectional_rank_operator_is_sanitized(self) -> None:
        """旧 coverage graph 的 rank_cs 标签必须映射到公开 rank 家族。"""

        for operator_tag, expected_family in (
            ("rank_cs", "rank"),
            ("rank_ts", "rolling"),
            ("sign", "arithmetic"),
        ):
            with self.subTest(operator_tag=operator_tag):
                self.assertEqual(_sanitize_operator_family(operator_tag), expected_family)

    def test_legacy_market_fields_are_sanitized_to_public_aliases(self) -> None:
        """旧行情字段必须映射到公开字段族，不把内部列名外发。"""

        expected = {
            "prev_close": "price_close",
            "free_circulation": "market_cap",
            "size_free_circulation": "market_cap",
            "money": "amount",
        }
        for field_name, expected_alias in expected.items():
            with self.subTest(field_name=field_name):
                self.assertEqual(_sanitize_field_alias(field_name), expected_alias)

    def test_load_verified_graph_rejects_missing_file_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)

            missing = graph_root / "coverage_summary.json"
            missing.unlink()
            with self.assertRaises(FactorMinerError) as caught:
                load_verified_coverage_graph(graph_root)
            self.assertIs(caught.exception.code, FailureCode.LEDGER_CORRUPT)

        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            (graph_root / "factor_nodes.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(FactorMinerError) as caught:
                load_verified_coverage_graph(graph_root)
            self.assertIs(caught.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_load_verified_graph_rejects_duplicate_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _publish_graph(Path(directory), dense=True)
            working = Path(directory) / "duplicate"
            shutil.copytree(source, working)
            rewritten = _duplicate_factor_performance_key(working)
            with self.assertRaises(FactorMinerError) as caught:
                load_verified_coverage_graph(rewritten)
            self.assertIs(caught.exception.code, FailureCode.LEDGER_CORRUPT)
            self.assertIn("factor_performance", caught.exception.message)

    def test_load_verified_graph_rejects_regime_profile_inconsistency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _publish_graph(Path(directory), dense=True)
            working = Path(directory) / "regime"
            shutil.copytree(source, working)
            rewritten = _mutate_regime_profile_state(working)
            with self.assertRaises(FactorMinerError) as caught:
                load_verified_coverage_graph(rewritten)
            self.assertIs(caught.exception.code, FailureCode.LEDGER_CORRUPT)
            self.assertIn("regime_profiles", caught.exception.message)

    def test_build_gap_report_returns_three_categories_and_deterministic_sort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            graph = load_verified_coverage_graph(graph_root)
            memory = _memory_snapshot(graph_root)

            first = build_coverage_gap_report(graph, memory, policy=GapSelectionPolicy(max_cards=10))
            second = build_coverage_gap_report(graph, memory, policy=GapSelectionPolicy(max_cards=10))
            truncated = build_coverage_gap_report(graph, memory, policy=GapSelectionPolicy(max_cards=3))

            self.assertEqual(first, second)
            self.assertEqual(
                {card.gap_category.value for card in first.gap_cards},
                {"structural", "market_regime", "data_availability"},
            )
            self.assertLessEqual(len(first.gap_cards), 10)
            self.assertEqual(
                tuple((card.gap_category.value, card.card_sha256, card.gap_id) for card in first.gap_cards),
                tuple(sorted((card.gap_category.value, card.card_sha256, card.gap_id) for card in first.gap_cards)),
            )
            self.assertGreaterEqual(first.truncated_count, 0)
            self.assertTrue(first.truncation_reason)
            self.assertGreater(truncated.truncated_count, 0)
            self.assertIn("稳定截断", truncated.truncation_reason)
            self.assertEqual(len(truncated.gap_cards), 3)
            self.assertEqual(
                {card.gap_category.value for card in truncated.gap_cards},
                {"structural", "market_regime", "data_availability"},
            )

    def test_build_gap_report_rejects_budget_below_three_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            graph = load_verified_coverage_graph(graph_root)

            with self.assertRaises(FactorMinerError) as caught:
                build_coverage_gap_report(
                    graph,
                    _memory_snapshot(graph_root),
                    policy=GapSelectionPolicy(max_cards=2),
                )
            self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
            self.assertIn("预算不足", caught.exception.message)

    def test_build_gap_report_rejects_missing_gap_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            graph = load_verified_coverage_graph(graph_root)
            stable_profiles = tuple(
                profile.model_copy(
                    update={
                        "status": "ok",
                        "reason": None,
                        "valid_dates": max(profile.valid_dates, 120),
                        "probability_mass": max(profile.probability_mass, 70.0),
                        "mean_normalized_entropy": 0.20,
                    }
                )
                for profile in graph.regime_profiles
            )
            no_market_gap_graph = replace(
                graph,
                regime_profiles=stable_profiles,
            )

            with self.assertRaises(FactorMinerError) as caught:
                build_coverage_gap_report(
                    no_market_gap_graph,
                    _memory_snapshot(graph_root),
                    policy=GapSelectionPolicy(max_cards=10),
                )
            self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
            self.assertIn("market_regime", caught.exception.message)

    def test_build_sanitized_gap_brief_does_not_emit_history_numbers_and_rejects_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            graph = load_verified_coverage_graph(graph_root)
            report = build_coverage_gap_report(
                graph,
                _memory_snapshot(graph_root),
                policy=GapSelectionPolicy(max_cards=10),
            )

            brief = build_sanitized_gap_brief(
                report,
                field_registry_hash="9" * 64,
                design_policy=DesignDiversityPolicy(),
            )
            serialized = json.dumps(brief, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("rank_ic", serialized)
            self.assertNotIn("0.02", serialized)
            self.assertNotIn("2024-01-01", serialized)
            self.assertNotIn("truncated_count", serialized)
            self.assertNotIn("truncation_reason", serialized)

            unsafe = report.model_copy(
                update={
                    "gap_cards": (
                        CoverageGapCard(
                            gap_id="/data/secret",
                            gap_category=report.gap_cards[0].gap_category,
                            sanitized_labels=report.gap_cards[0].sanitized_labels,
                            allowed_field_aliases=report.gap_cards[0].allowed_field_aliases,
                            allowed_operator_families=report.gap_cards[0].allowed_operator_families,
                            temporal_window_bins=report.gap_cards[0].temporal_window_bins,
                            structure_cluster_count_band=report.gap_cards[0].structure_cluster_count_band,
                            signal_cluster_count_band=report.gap_cards[0].signal_cluster_count_band,
                            missing_or_failure_risk=report.gap_cards[0].missing_or_failure_risk,
                        ),
                    ),
                }
            )
            with self.assertRaises(FactorMinerError) as caught:
                build_sanitized_gap_brief(
                    unsafe,
                    field_registry_hash="9" * 64,
                    design_policy=DesignDiversityPolicy(),
                )
            self.assertIs(caught.exception.code, FailureCode.LLM_PRIVACY_VIOLATION)

    def test_build_gap_report_rejects_empty_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=False)
            graph = load_verified_coverage_graph(graph_root)
            with self.assertRaises(FactorMinerError) as caught:
                build_coverage_gap_report(
                    graph,
                    _memory_snapshot(graph_root),
                    policy=GapSelectionPolicy(max_cards=10),
                )
            self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)

    def test_build_gap_report_rejects_polluted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = _publish_graph(Path(directory), dense=True)
            graph = load_verified_coverage_graph(graph_root)
            valid = _memory_snapshot(graph_root)
            polluted = ResearchMemorySnapshot.model_construct(
                **{
                    **valid.model_dump(),
                    "source_family_ids": ("llmfamily_1bae19965638a6ac9620e0b0",),
                    "snapshot_sha256": valid.snapshot_sha256,
                }
            )
            with self.assertRaises(FactorMinerError) as caught:
                build_coverage_gap_report(
                    graph,
                    polluted,
                    policy=GapSelectionPolicy(max_cards=10),
                )
            self.assertIs(caught.exception.code, FailureCode.POLLUTED_FAMILY_REJECTED)


if __name__ == "__main__":
    unittest.main()
