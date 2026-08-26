"""V0.4 因子节点历史表现必须由本次冻结每日 IC 重算。"""

from datetime import date
import unittest

import polars as pl

from factor_miner.coverage_performance import summarize_factor_performance
from factor_miner.coverage_schema import CoverageFactorNode


def _node() -> CoverageFactorNode:
    return CoverageFactorNode(
        factor_id="factor_a",
        factor_name_cn="合成因子",
        node_status="active",
        structure_source="legacy_formula_metadata",
        formula_expr="close",
        formula_hash="1" * 64,
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
        legacy_mean_ic=0.08,
        legacy_icir=0.4,
        source="synthetic.yaml",
    )


class CoveragePerformanceTest(unittest.TestCase):
    """YAML 旧摘要只能作为对账值，不能替代本次 IC 汇总。"""

    def test_recomputes_current_summary_and_preserves_legacy_difference(self) -> None:
        daily = pl.DataFrame(
            {
                "factor_id": ["factor_a"] * 4,
                "date": [
                    date(2024, 1, 2),
                    date(2024, 1, 3),
                    date(2024, 1, 4),
                    date(2024, 1, 5),
                ],
                "rank_ic": [0.1, -0.1, 0.3, None],
                "coverage": [0.9, 0.8, 1.0, 0.2],
            },
            schema_overrides={"date": pl.Date},
        )

        summary = summarize_factor_performance((_node(),), daily)[0]

        self.assertEqual(summary.valid_dates, 3)
        self.assertEqual(summary.invalid_dates, 1)
        self.assertAlmostEqual(summary.mean_rank_ic, 0.1)
        self.assertAlmostEqual(summary.std_rank_ic or 0.0, 0.2)
        self.assertAlmostEqual(summary.icir or 0.0, 0.5)
        self.assertAlmostEqual(summary.positive_rank_ic_ratio, 2 / 3)
        self.assertAlmostEqual(summary.negative_rank_ic_ratio, 1 / 3)
        self.assertAlmostEqual(summary.median_coverage, 0.9)
        self.assertAlmostEqual(summary.legacy_mean_ic_difference or 0.0, 0.02)
        self.assertEqual(summary.annual_summaries[0].year, 2024)

    def test_all_null_factor_is_retained_as_unavailable(self) -> None:
        daily = pl.DataFrame(
            {
                "factor_id": ["factor_a", "factor_a"],
                "date": [date(2024, 1, 2), date(2024, 1, 3)],
                "rank_ic": [None, None],
                "coverage": [0.0, 0.0],
            },
            schema_overrides={"date": pl.Date, "rank_ic": pl.Float64},
        )

        summary = summarize_factor_performance((_node(),), daily)[0]

        self.assertEqual(summary.status, "unavailable")
        self.assertEqual(summary.valid_dates, 0)
        self.assertIsNone(summary.mean_rank_ic)
        self.assertEqual(summary.reason, "没有有效每日 IC")


if __name__ == "__main__":
    unittest.main()
