"""轻量研究批次冻结合同测试。"""

import unittest

from pydantic import ValidationError

from factor_miner.lightweight_schema import (
    LightweightCandidateBinding,
    LightweightResearchConfig,
    build_lightweight_batch_manifest,
)
from factor_miner.llm_state import CandidateSlotState
from factor_miner.research_campaign_runner import (
    CampaignSlotEvaluation,
    LightweightCampaignEvaluationResult,
    lightweight_statistical_policy,
)


def _bindings(count: int, *, first_hash: str = "a" * 64) -> tuple[LightweightCandidateBinding, ...]:
    values = []
    for index in range(count):
        hypothesis = index // 3 + 1
        candidate = index % 3 + 1
        digest = first_hash if index == 0 else f"{index:064x}"
        values.append(
            LightweightCandidateBinding(
                slot_id=f"H{hypothesis:02d}:C{candidate:03d}",
                source_candidate_id=f"candidate_{index:03d}",
                candidate_spec_hash=digest,
            )
        )
    return tuple(values)


class LightweightSchemaTest(unittest.TestCase):
    """轻量合同必须冻结候选顺序和完整统计分母。"""

    def test_default_lightweight_batch_has_thirty_frozen_slots(self) -> None:
        config = LightweightResearchConfig()
        self.assertEqual(config.hypothesis_count, 10)
        self.assertEqual(config.candidates_per_hypothesis, 3)
        self.assertEqual(config.slot_count, 30)

    def test_manifest_identity_changes_when_candidate_changes(self) -> None:
        config = LightweightResearchConfig()
        first = build_lightweight_batch_manifest(
            config=config,
            candidate_bindings=_bindings(30, first_hash="a" * 64),
            evaluation_policy_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            coverage_graph_hash="d" * 64,
        )
        second = build_lightweight_batch_manifest(
            config=config,
            candidate_bindings=_bindings(30, first_hash="e" * 64),
            evaluation_policy_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            coverage_graph_hash="d" * 64,
        )
        self.assertEqual(first.family_size, 30)
        self.assertNotEqual(first.manifest_sha256, second.manifest_sha256)
        self.assertEqual(
            lightweight_statistical_policy(first).bonferroni_denominator,
            30,
        )

    def test_manifest_rejects_unfrozen_slot_count(self) -> None:
        with self.assertRaises(ValueError):
            build_lightweight_batch_manifest(
                config=LightweightResearchConfig(),
                candidate_bindings=_bindings(29),
                evaluation_policy_hash="b" * 64,
                memory_snapshot_hash="c" * 64,
                coverage_graph_hash="d" * 64,
            )

    def test_config_requires_positive_counts(self) -> None:
        with self.assertRaises(ValidationError):
            LightweightResearchConfig(hypothesis_count=0)

    def test_lightweight_result_uses_manifest_family_size(self) -> None:
        config = LightweightResearchConfig(
            hypothesis_count=1,
            candidates_per_hypothesis=3,
            allow_external_llm=False,
        )
        manifest = build_lightweight_batch_manifest(
            config=config,
            candidate_bindings=_bindings(3),
            evaluation_policy_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            coverage_graph_hash="d" * 64,
        )
        evaluations = tuple(
            CampaignSlotEvaluation(
                slot_id=binding.slot_id,
                slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                status="evaluated" if index < 2 else "failed",
                failure_reason=None if index < 2 else "合成失败",
            )
            for index, binding in enumerate(manifest.candidate_bindings)
        )
        result = LightweightCampaignEvaluationResult.build(
            manifest=manifest,
            slot_evaluations=evaluations,
        )
        self.assertEqual(result.registered_slot_count, 3)
        self.assertEqual(result.evaluated_slot_count, 2)
        self.assertEqual(result.failed_slot_count, 1)
        self.assertEqual(result.bonferroni_denominator, 3)

    def test_lightweight_result_rejects_missing_frozen_slot(self) -> None:
        manifest = build_lightweight_batch_manifest(
            config=LightweightResearchConfig(
                hypothesis_count=1,
                candidates_per_hypothesis=3,
            ),
            candidate_bindings=_bindings(3),
            evaluation_policy_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            coverage_graph_hash="d" * 64,
        )
        evaluations = (
            CampaignSlotEvaluation(
                slot_id=manifest.candidate_bindings[0].slot_id,
                slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                status="evaluated",
            ),
            CampaignSlotEvaluation(
                slot_id=manifest.candidate_bindings[1].slot_id,
                slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                status="failed",
                failure_reason="合成失败",
            ),
        )
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            LightweightCampaignEvaluationResult.build(
                manifest=manifest,
                slot_evaluations=evaluations,
            )


if __name__ == "__main__":
    unittest.main()
