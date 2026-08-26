"""V0.4 数学形式层的独立关系权重测试。"""

import unittest

from factor_miner.coverage_schema import (
    CoverageFactorNode,
    CoverageStructuralPolicy,
)
from factor_miner.coverage_structure import compare_factor_structure


def _node(
    factor_id: str,
    *,
    fields: tuple[str, ...],
    operators: tuple[str, ...],
    windows: tuple[int, ...],
    category: str,
    subcategory: str,
    formula_hash: str,
) -> CoverageFactorNode:
    return CoverageFactorNode(
        factor_id=factor_id,
        factor_name_cn=f"{factor_id}中文因子",
        node_status="active",
        structure_source="legacy_formula_metadata",
        formula_expr=f"{factor_id}公式",
        formula_hash=formula_hash,
        input_fields=fields,
        operator_tags=operators,
        windows=windows,
        lookback_window=max(windows, default=0),
        lag_days=1,
        category=category,
        subcategory=subcategory,
        description="合成结构因子",
        preprocess_method="none",
        neutralization="none",
        orientation_sign=1,
        orientation_source="legacy_visible_mean_ic",
        legacy_mean_ic=0.1,
        source="synthetic.yaml",
    )


class CoverageStructureTest(unittest.TestCase):
    """结构总分不能掩盖精确重复和各分量差异。"""

    def test_structure_components_are_preserved_separately(self) -> None:
        first = _node(
            "a",
            fields=("close", "volume"),
            operators=("ma", "rank_ts"),
            windows=(20,),
            category="momentum",
            subcategory="price",
            formula_hash="1" * 64,
        )
        second = _node(
            "b",
            fields=("amount", "close"),
            operators=("corr", "rank_ts"),
            windows=(40,),
            category="momentum",
            subcategory="volume_price",
            formula_hash="2" * 64,
        )

        edge = compare_factor_structure(
            first,
            second,
            CoverageStructuralPolicy(window_scale=20.0),
        )

        self.assertFalse(edge.exact_formula_match)
        self.assertFalse(edge.exact_ast_match)
        self.assertAlmostEqual(edge.field_similarity, 1 / 3)
        self.assertAlmostEqual(edge.operator_similarity, 1 / 3)
        self.assertAlmostEqual(edge.window_similarity, 0.5)
        self.assertEqual(edge.category_similarity, 0.5)
        self.assertAlmostEqual(edge.structural_similarity, 0.39166666666666666)

    def test_exact_formula_is_always_a_strong_structure_edge(self) -> None:
        first = _node(
            "a",
            fields=("close",),
            operators=("ma",),
            windows=(20,),
            category="momentum",
            subcategory="price",
            formula_hash="1" * 64,
        )
        second = first.model_copy(
            update={"factor_id": "b", "factor_name_cn": "b中文因子"}
        )

        edge = compare_factor_structure(
            first,
            second,
            CoverageStructuralPolicy(strong_structure_similarity=0.99),
        )

        self.assertTrue(edge.exact_formula_match)
        self.assertTrue(edge.strong_structure_edge)


if __name__ == "__main__":
    unittest.main()
