"""金融语义覆盖矩阵与固定稀疏配额测试。"""

from __future__ import annotations

import unittest

from factor_miner.semantic_coverage import (
    SemanticPlanTags,
    build_semantic_coverage,
    build_semantic_quota,
)


def plan(
    event_tag: str,
    context_tag: str,
    *,
    quality_tags: tuple[str, ...] = (),
    direction_tag: str = "continuation",
    output_tag: str = "continuous_score",
) -> SemanticPlanTags:
    return SemanticPlanTags(
        event_tag=event_tag,
        context_tag=context_tag,
        quality_tags=quality_tags,
        direction_tag=direction_tag,
        output_tag=output_tag,
    )


class SemanticCoverageTest(unittest.TestCase):
    """覆盖矩阵只计假设语义，不把三个候选重复放大。"""

    def test_tags_are_complete_when_present_and_qualities_are_canonical(self) -> None:
        value = plan(
            "breakout",
            "recent_extreme",
            quality_tags=("volume_confirmation", "multi_horizon_consistency"),
        )
        self.assertEqual(
            value.quality_tags,
            ("multi_horizon_consistency", "volume_confirmation"),
        )
        with self.assertRaises(ValueError):
            plan(
                "breakout",
                "recent_extreme",
                quality_tags=("volume_confirmation", "volume_confirmation"),
            )

    def test_heatmap_counts_event_context_and_reports_exact_duplicates(self) -> None:
        first = plan("breakout", "recent_extreme")
        second = plan("breakout", "recent_extreme")
        third = plan("volume_change", "volume_regime", output_tag="event_strength")

        coverage = build_semantic_coverage((first, second, third, None))

        self.assertEqual(coverage.tagged_hypothesis_count, 3)
        self.assertEqual(coverage.untagged_hypothesis_count, 1)
        self.assertEqual(
            coverage.event_context_counts["breakout"]["recent_extreme"],
            2,
        )
        self.assertEqual(len(coverage.duplicate_regions), 1)
        self.assertEqual(coverage.duplicate_regions[0].count, 2)

    def test_quota_is_fixed_six_uncovered_three_sparse_one_free(self) -> None:
        coverage = build_semantic_coverage(
            (
                plan("breakout", "recent_extreme"),
                plan("breakout", "recent_extreme"),
                plan("volume_change", "volume_regime"),
            )
        )

        quota = build_semantic_quota(coverage)

        self.assertEqual(len(quota.slots), 10)
        self.assertEqual(
            tuple(item.quota_kind for item in quota.slots),
            ("uncovered",) * 6 + ("sparse",) * 3 + ("free",),
        )
        targets = tuple(
            (item.event_tag, item.context_tag)
            for item in quota.slots
            if item.quota_kind != "free"
        )
        self.assertEqual(len(targets), len(set(targets)))
        self.assertIsNone(quota.slots[-1].event_tag)
        self.assertIsNone(quota.slots[-1].context_tag)


if __name__ == "__main__":
    unittest.main()
