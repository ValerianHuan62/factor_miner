"""结构层和实际信号层必须分别聚类。"""

from datetime import date
import unittest

from factor_miner.coverage_cluster import build_coverage_clusters
from factor_miner.coverage_schema import (
    CoverageClusterPolicy,
    CoverageFactorNode,
)
from factor_miner.coverage_signal import DailyICIdentity, SignalPatternEdge
from factor_miner.coverage_structure import StructuralEdge


def _node(factor_id: str) -> CoverageFactorNode:
    return CoverageFactorNode(
        factor_id=factor_id,
        factor_name_cn=f"{factor_id}中文因子",
        node_status="active",
        structure_source="legacy_formula_metadata",
        formula_expr=f"{factor_id}公式",
        formula_hash=f"{ord(factor_id):064x}",
        input_fields=("close",),
        operator_tags=(),
        windows=(),
        lookback_window=1,
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


def _structure_edge(a: str, b: str, strong: bool) -> StructuralEdge:
    return StructuralEdge(
        factor_a=a,
        factor_b=b,
        exact_ast_match=False,
        exact_formula_match=False,
        field_similarity=0.5,
        operator_similarity=0.5,
        window_similarity=0.5,
        category_similarity=0.5,
        structural_similarity=0.5,
        strong_structure_edge=strong,
    )


def _signal_edge(a: str, b: str, strong: bool) -> SignalPatternEdge:
    identity = DailyICIdentity(
        evaluation_policy_id="evalpol_" + "a" * 24,
        data_release_id="release-v1",
        label_id="label-v1",
        visible_start=date(2024, 1, 1),
        visible_end=date(2024, 12, 31),
    )
    return SignalPatternEdge(
        factor_a=a,
        factor_b=b,
        identity_a=identity,
        identity_b=identity,
        evaluation_policy_id=identity.evaluation_policy_id,
        data_release_id=identity.data_release_id,
        label_id=identity.label_id,
        visible_start=identity.visible_start,
        visible_end=identity.visible_end,
        raw_correlation=0.8 if strong else 0.2,
        aligned_correlation=0.8 if strong else 0.2,
        absolute_correlation=0.8 if strong else 0.2,
        overlap_dates=200,
        union_dates=200,
        missing_ratio=0.0,
        comparable=True,
        strong_signal_edge=strong,
    )


class CoverageClusterTest(unittest.TestCase):
    """一层的强边不能强迫另一层合并。"""

    def test_structure_and_signal_components_are_independent_and_stable(self) -> None:
        nodes = tuple(_node(value) for value in ("a", "b", "c", "d"))
        structural = (
            _structure_edge("a", "b", True),
            _structure_edge("b", "c", False),
            _structure_edge("c", "d", False),
        )
        signal = (
            _signal_edge("a", "b", False),
            _signal_edge("b", "c", True),
            _signal_edge("c", "d", False),
        )

        result = build_coverage_clusters(
            nodes,
            structural,
            signal,
            CoverageClusterPolicy(),
        )
        repeated = build_coverage_clusters(
            tuple(reversed(nodes)),
            tuple(reversed(structural)),
            tuple(reversed(signal)),
            CoverageClusterPolicy(),
        )

        self.assertEqual(result, repeated)
        self.assertEqual(
            sorted(result.structural_clusters.values()),
            [("a", "b"), ("c",), ("d",)],
        )
        self.assertEqual(
            sorted(result.signal_clusters.values()),
            [("a",), ("b", "c"), ("d",)],
        )
        self.assertNotEqual(
            result.factor_to_structural_cluster["b"],
            result.factor_to_signal_cluster["b"],
        )


if __name__ == "__main__":
    unittest.main()
