"""V0.4 每日 IC 必须逐因子对独立计算。"""

from datetime import date
import unittest

import polars as pl

from factor_miner.coverage_schema import CoverageFactorNode, CoveragePairPolicy
from factor_miner.coverage_signal import (
    build_signal_pattern_edges,
    compare_daily_ic_pair,
)
from factor_miner.errors import FactorMinerError


def _node(factor_id: str, orientation_sign: int) -> CoverageFactorNode:
    return CoverageFactorNode(
        factor_id=factor_id,
        factor_name_cn=f"{factor_id}中文名",
        node_status="active",
        structure_source="legacy_formula_metadata",
        formula_expr=f"{factor_id}(close)",
        formula_hash=("1" if factor_id == "a" else "2") * 64,
        input_fields=("close",),
        operator_tags=(),
        windows=(),
        lookback_window=1,
        lag_days=1,
        category="momentum",
        subcategory="price",
        description="合成测试因子",
        preprocess_method="none",
        neutralization="none",
        orientation_sign=orientation_sign,
        orientation_source="legacy_visible_mean_ic",
        legacy_mean_ic=0.1 * orientation_sign,
        source="synthetic.yaml",
    )


def _series(
    factor_id: str,
    values: list[float | None],
    *,
    policy_id: str = "evalpol_" + "a" * 24,
) -> pl.DataFrame:
    dates = [date(2024, 1, day) for day in range(2, 2 + len(values))]
    return pl.DataFrame(
        {
            "factor_id": [factor_id] * len(values),
            "date": dates,
            "rank_ic": values,
            "coverage": [0.9] * len(values),
            "evaluation_policy_id": [policy_id] * len(values),
            "data_release_id": ["release-v1"] * len(values),
            "label_id": ["label-v1"] * len(values),
            "visible_start": [date(2024, 1, 2)] * len(values),
            "visible_end": [date(2024, 1, 6)] * len(values),
        },
        schema_overrides={"date": pl.Date, "visible_start": pl.Date, "visible_end": pl.Date},
    )


class CoverageSignalTest(unittest.TestCase):
    """每个无序因子对拥有自己的日期交集和失败诊断。"""

    def test_single_pair_has_raw_and_direction_aligned_spearman(self) -> None:
        edge = compare_daily_ic_pair(
            _series("a", [1.0, 2.0, 3.0, 4.0, 5.0]),
            _series("b", [5.0, 4.0, 3.0, 2.0, 1.0]),
            _node("a", 1),
            _node("b", -1),
            CoveragePairPolicy(min_overlap_dates=5),
        )

        self.assertTrue(edge.comparable)
        self.assertEqual(edge.overlap_dates, 5)
        self.assertEqual(edge.union_dates, 5)
        self.assertEqual(edge.missing_ratio, 0.0)
        self.assertAlmostEqual(edge.raw_correlation or 0.0, -1.0)
        self.assertAlmostEqual(edge.aligned_correlation or 0.0, 1.0)
        self.assertAlmostEqual(edge.absolute_correlation or 0.0, 1.0)
        self.assertTrue(edge.strong_signal_edge)

    def test_pair_uses_its_own_missing_pattern(self) -> None:
        edge = compare_daily_ic_pair(
            _series("a", [1.0, None, 3.0, 4.0, 5.0]),
            _series("b", [1.0, 2.0, None, 4.0, 5.0]),
            _node("a", 1),
            _node("b", 1),
            CoveragePairPolicy(min_overlap_dates=3, max_missing_ratio=0.50),
        )

        self.assertTrue(edge.comparable)
        self.assertEqual(edge.overlap_dates, 3)
        self.assertEqual(edge.union_dates, 5)
        self.assertAlmostEqual(edge.missing_ratio, 0.4)

    def test_excess_missing_is_explicitly_not_comparable(self) -> None:
        edge = compare_daily_ic_pair(
            _series("a", [1.0, None, 3.0, 4.0, 5.0]),
            _series("b", [1.0, 2.0, None, 4.0, 5.0]),
            _node("a", 1),
            _node("b", 1),
            CoveragePairPolicy(min_overlap_dates=3, max_missing_ratio=0.20),
        )

        self.assertFalse(edge.comparable)
        self.assertEqual(edge.reason, "因子对缺失比例超过冻结阈值")
        self.assertIsNone(edge.raw_correlation)

    def test_identity_mismatch_is_not_filled_or_correlated(self) -> None:
        edge = compare_daily_ic_pair(
            _series("a", [1.0, 2.0, 3.0]),
            _series(
                "b",
                [1.0, 2.0, 3.0],
                policy_id="evalpol_" + "b" * 24,
            ),
            _node("a", 1),
            _node("b", 1),
            CoveragePairPolicy(min_overlap_dates=3),
        )

        self.assertFalse(edge.comparable)
        self.assertEqual(edge.reason, "因子对评价身份不兼容")
        self.assertIsNone(edge.raw_correlation)

    def test_duplicate_factor_date_is_rejected(self) -> None:
        duplicated = pl.concat(
            [_series("a", [1.0, 2.0, 3.0]), _series("a", [4.0]).head(1)]
        )

        with self.assertRaisesRegex(FactorMinerError, "重复"):
            compare_daily_ic_pair(
                duplicated,
                _series("b", [1.0, 2.0, 3.0]),
                _node("a", 1),
                _node("b", 1),
                CoveragePairPolicy(min_overlap_dates=3),
            )

    def test_builder_calls_pair_comparator_once_for_each_unordered_pair(self) -> None:
        nodes = (_node("a", 1), _node("b", -1), _node("c", 1))
        daily = pl.concat(
            [
                _series("a", [1.0, 2.0, 3.0]),
                _series("b", [3.0, 2.0, 1.0]),
                _series("c", [1.0, 3.0, 2.0]),
            ]
        )
        calls: list[tuple[str, str, int, int]] = []
        frame_ids: dict[str, set[int]] = {}

        def recording_comparator(
            first: pl.DataFrame,
            second: pl.DataFrame,
            first_node: CoverageFactorNode,
            second_node: CoverageFactorNode,
            policy: CoveragePairPolicy,
        ):
            frame_ids.setdefault(first_node.factor_id, set()).add(id(first))
            frame_ids.setdefault(second_node.factor_id, set()).add(id(second))
            calls.append(
                (
                    first.get_column("factor_id").unique().item(),
                    second.get_column("factor_id").unique().item(),
                    first.get_column("factor_id").n_unique(),
                    second.get_column("factor_id").n_unique(),
                )
            )
            return compare_daily_ic_pair(
                first,
                second,
                first_node,
                second_node,
                policy,
            )

        edges = build_signal_pattern_edges(
            nodes,
            daily,
            CoveragePairPolicy(min_overlap_dates=3),
            comparator=recording_comparator,
        )

        self.assertEqual(len(edges), 3)
        self.assertEqual(
            calls,
            [
                ("a", "b", 1, 1),
                ("a", "c", 1, 1),
                ("b", "c", 1, 1),
            ],
        )
        self.assertEqual(
            {factor_id: len(ids) for factor_id, ids in frame_ids.items()},
            {"a": 1, "b": 1, "c": 1},
        )


if __name__ == "__main__":
    unittest.main()
