"""V0.5 脱敏覆盖摘要与确定性 gap 选择测试。"""

from collections import Counter
from datetime import datetime, timezone
import unittest

from factor_miner.llm_brief import (
    CoverageGapCard,
    GapSelectionPolicy,
    LLMCoverageBrief,
    LLMCoverageBriefSpec,
    registered_llm_coverage_brief,
    select_coverage_gaps,
)


def gap(
    number: int,
    category: str,
    structure: str,
    signal: str,
    risk: str,
) -> CoverageGapCard:
    """构造不含真实因子身份的合成 gap。"""

    return CoverageGapCard(
        gap_id=f"G{number:03d}",
        category=category,
        subcategory=f"子类{number:03d}",
        field_aliases=("price_close",),
        operator_families=("rolling",),
        window_band="medium",
        hypothesis_axis="cross_sectional",
        structure_count_band=structure,
        signal_cluster_count_band=signal,
        failure_risk=risk,
        performance_direction_band="neutral",
        stability_band="medium",
        regime_difference_band="weak",
        canonical_gap_hash=f"{number:064x}",
    )


def gaps() -> tuple[CoverageGapCard, ...]:
    """构造覆盖排序、风险删除和动态类别配额的候选集。"""

    return (
        gap(1, "A", "1", "1", "low"),
        gap(2, "A", "0", "0", "none"),
        gap(3, "A", "0", "1", "none"),
        gap(4, "B", "2_4", "2_4", "medium"),
        gap(5, "B", "0", "1", "low"),
        gap(6, "B", "1", "0", "none"),
        gap(7, "C", "1", "1", "none"),
        gap(8, "C", "0", "2_4", "medium"),
        gap(9, "C", "2_4", "0", "low"),
        gap(10, "D", "2_4", "2_4", "medium"),
        gap(11, "D", "0", "0", "high"),
        gap(12, "D", "1", "1", "low"),
    )


class LLMBriefTest(unittest.TestCase):
    """覆盖空白选择必须稳定、分散且不追逐表现。"""

    def test_gap_selection_is_order_independent_and_recomputes_quota(self) -> None:
        forward = select_coverage_gaps(gaps(), GapSelectionPolicy(max_cards=8))
        reverse = select_coverage_gaps(
            tuple(reversed(gaps())),
            GapSelectionPolicy(max_cards=8),
        )

        self.assertEqual(forward, reverse)
        counts = Counter(card.category for card in forward)
        self.assertLessEqual(max(counts.values()), 2)
        self.assertEqual(set(counts), {"A", "B", "C", "D"})

    def test_high_failure_is_removed_before_ranking(self) -> None:
        selected = select_coverage_gaps(gaps(), GapSelectionPolicy(max_cards=10))

        self.assertNotIn("G011", {card.gap_id for card in selected})

    def test_performance_annotations_do_not_change_selection(self) -> None:
        original = select_coverage_gaps(gaps(), GapSelectionPolicy(max_cards=8))
        changed = tuple(
            item.model_copy(
                update={
                    "performance_direction_band": "strong_positive",
                    "stability_band": "strong",
                    "regime_difference_band": "strong",
                }
            )
            for item in gaps()
        )

        self.assertEqual(
            tuple(card.gap_id for card in original),
            tuple(
                card.gap_id
                for card in select_coverage_gaps(
                    changed,
                    GapSelectionPolicy(max_cards=8),
                )
            ),
        )

    def test_brief_is_content_addressed_without_source_graph_id(self) -> None:
        spec = LLMCoverageBriefSpec(
            source_coverage_graph_id="covgraph_" + "1" * 24,
            algorithm_version="brief-v1",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        brief = LLMCoverageBrief(
            brief_version="1",
            taxonomy_summary=("价格行为", "流动性"),
            coverage_gap_cards=select_coverage_gaps(
                gaps(),
                GapSelectionPolicy(max_cards=8),
            ),
            failure_pattern_cards=("定义失败偏高",),
            allowed_field_aliases=("price_close",),
            allowed_operators=("rolling_mean",),
            allowed_windows=(5, 20),
            hypothesis_budget=10,
            candidate_budget_per_hypothesis=3,
        )
        first = registered_llm_coverage_brief(spec, brief)
        second = registered_llm_coverage_brief(spec, brief)

        self.assertEqual(first, second)
        self.assertRegex(first.brief_id, r"^brief_[0-9a-f]{24}$")
        self.assertNotIn(
            spec.source_coverage_graph_id,
            first.export_payload.model_dump_json(),
        )


if __name__ == "__main__":
    unittest.main()
