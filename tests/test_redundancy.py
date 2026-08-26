"""结构和输出冗余闸门测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.compiler import CompiledFactorPlan, compile_candidate
from factor_miner.redundancy import (
    check_output_redundancy,
    check_structural_redundancy,
)
from factor_miner.schema import CampaignSpec, FactorNode, registered_candidate
from tests.helpers import valid_campaign, valid_candidate, valid_registered_candidate


def compiled_expression(
    expression: FactorNode,
    required_fields: tuple[str, ...],
    max_lookback: int,
) -> CompiledFactorPlan:
    """构造并编译一份仅用于冗余测试的候选。"""

    candidate = valid_candidate().model_copy(
        update={
            "expression": expression,
            "required_fields": required_fields,
            "max_lookback": max_lookback,
        }
    )
    return compile_candidate(registered_candidate(candidate), set(required_fields))


def redundancy_campaign() -> CampaignSpec:
    """构造短日期区间和较低重叠门槛的 campaign。"""

    return valid_campaign().model_copy(
        update={
            "visible_start": date(2020, 1, 1),
            "visible_end": date(2021, 1, 4),
            "min_names_per_date": 3,
            "max_abs_output_correlation": 0.8,
        }
    )


def output_frame(
    column: str,
    values: tuple[float, ...],
    assets: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD"),
) -> pl.DataFrame:
    """按两年、四资产生成确定性输出面板。"""

    rows: list[dict[str, object]] = []
    for day_index in range(8):
        current = date(2020, 12, 29) + timedelta(days=day_index)
        for asset_index, asset in enumerate(assets):
            rows.append(
                {
                    "date": current,
                    "asset": asset,
                    column: values[asset_index],
                }
            )
    return pl.DataFrame(rows).sort(["date", "asset"])


class RedundancyTest(unittest.TestCase):
    """验证结构优先、输出其次的冗余策略。"""

    def test_exact_ast_and_commutative_ast_are_structurally_redundant(self) -> None:
        """相同 AST 和交换律等价 AST 都必须被识别。"""
        exact = compile_candidate(valid_registered_candidate(), {"close"})
        swapped_left = compiled_expression(
            FactorNode(
                op="add",
                args=(
                    FactorNode(op="field", field="close"),
                    FactorNode(op="field", field="volume"),
                ),
            ),
            ("close", "volume"),
            20,
        )
        swapped_right = compiled_expression(
            FactorNode(
                op="add",
                args=(
                    FactorNode(op="field", field="volume"),
                    FactorNode(op="field", field="close"),
                ),
            ),
            ("close", "volume"),
            20,
        )
        self.assertFalse(check_structural_redundancy(exact, (exact,)).passed)
        result = check_structural_redundancy(swapped_left, (swapped_right,))
        self.assertFalse(result.passed)
        self.assertEqual(result.matched_candidate_ids, (swapped_right.candidate_id,))

    def test_different_windows_and_different_ast_are_not_structural_duplicates(self) -> None:
        """不同窗口和不同 AST 即使未来输出可能相似，也不属于结构重复。"""
        five = compiled_expression(
            FactorNode(
                op="rolling_mean",
                args=(FactorNode(op="field", field="close"),),
                window=5,
            ),
            ("close",),
            5,
        )
        ten = compiled_expression(
            FactorNode(
                op="rolling_mean",
                args=(FactorNode(op="field", field="close"),),
                window=10,
            ),
            ("close",),
            10,
        )
        self.assertTrue(check_structural_redundancy(five, (ten,)).passed)

    def test_high_positive_and_negative_output_correlations_fail(self) -> None:
        """高正相关和高负相关都必须超过同一绝对相关阈值。"""
        campaign = redundancy_campaign()
        candidate = output_frame("candidate", (1.0, 2.0, 3.0, 4.0))
        positive = output_frame("reference", (1.0, 2.0, 3.0, 4.0))
        negative = output_frame("reference", (-1.0, -2.0, -3.0, -4.0))
        positive_result = check_output_redundancy(
            candidate,
            {"positive": positive},
            campaign,
            candidate_column="candidate",
        )
        negative_result = check_output_redundancy(
            candidate,
            {"negative": negative},
            campaign,
            candidate_column="candidate",
        )
        self.assertFalse(positive_result.passed)
        self.assertFalse(negative_result.passed)
        self.assertEqual(positive_result.comparisons[0].median_correlation, 1.0)
        self.assertEqual(negative_result.comparisons[0].median_correlation, -1.0)

    def test_unrelated_output_passes_and_reports_year_summaries(self) -> None:
        """无关输出应通过，并保留总体与分年度摘要。"""
        campaign = redundancy_campaign()
        candidate = output_frame("candidate", (1.0, 2.0, 3.0, 4.0))
        unrelated = output_frame("reference", (1.0, 3.0, 4.0, 2.0))
        result = check_output_redundancy(
            candidate,
            {"unrelated": unrelated},
            campaign,
            candidate_column="candidate",
        )
        comparison = result.comparisons[0]
        self.assertTrue(result.passed)
        self.assertEqual(comparison.valid_dates, 7)
        self.assertEqual(set(comparison.annual_median_correlations), {"2020", "2021"})
        self.assertEqual(comparison.min_overlap, 4)

    def test_insufficient_overlap_fails_closed(self) -> None:
        """重叠股票数不足时不能假设不相关，必须失败关闭。"""
        campaign = redundancy_campaign()
        candidate = output_frame("candidate", (1.0, 2.0, 3.0, 4.0))
        reference = output_frame("reference", (1.0, 2.0), assets=("AAA", "BBB"))
        result = check_output_redundancy(
            candidate,
            {"short": reference},
            campaign,
            candidate_column="candidate",
        )
        comparison = result.comparisons[0]
        self.assertFalse(result.passed)
        self.assertFalse(comparison.overlap_sufficient)
        self.assertEqual(comparison.valid_dates, 0)
        self.assertEqual(comparison.reason, "重叠股票数不足")


if __name__ == "__main__":
    unittest.main()
