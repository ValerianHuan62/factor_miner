"""确定性 shadow arm 的合成生成与恢复测试。"""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.llm_orchestrator import (
    ApprovedBatchDependencies,
    prepare_approved_batch_export,
    run_approved_batch,
)
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.shadow_generators import generate_shadow_arms
from factor_miner.llm_state import CandidateSlotState
from tests.test_llm_orchestrator import (
    NOW,
    PreparedRequestProxy,
    RecordedBatchTransport,
    authorization_for,
    approved_for,
    dependencies,
    price_registry,
)


class ShadowGeneratorTest(unittest.TestCase):
    """shadow 只复用批准父候选，不读取任何结果。"""

    def _run_coverage_parent(self, root: Path):
        base = dependencies(root)
        family = base.family
        approved = approved_for(family.discovery_family_id)
        export = prepare_approved_batch_export(
            family.discovery_family_id,
            (approved,),
            base,
        )
        authorizations = {
            export.request_hashes[0]: authorization_for(
                PreparedRequestProxy(
                    request_sha256=export.request_hashes[0],
                    campaign_id=(
                        f"{family.discovery_family_id}:coverage_outcome_llm"
                    ),
                ),
                base.policy,
            )
        }
        transport = RecordedBatchTransport()
        deps = ApprovedBatchDependencies(
            artifact_root=root,
            family=family,
            policy=base.policy,
            public_payload_by_hypothesis_id=base.public_payload_by_hypothesis_id,
            authorization_by_request_hash=authorizations,
            field_registry=price_registry(),
            transport=transport,
            now=NOW,
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic"}), patch(
            "factor_miner.llm_orchestrator.platform.system",
            return_value="Linux",
        ):
            first = run_approved_batch(
                family.discovery_family_id,
                (approved,),
                deps,
            )
            authorizations[first.pending_request_hashes[0]] = authorization_for(
                PreparedRequestProxy(
                    request_sha256=first.pending_request_hashes[0],
                    campaign_id=(
                        f"{family.discovery_family_id}:coverage_outcome_llm"
                    ),
                ),
                base.policy,
            )
            run_approved_batch(
                family.discovery_family_id,
                (approved,),
                deps,
            )
        return family, approved

    def test_shadow_generates_two_matched_three_slot_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family, approved = self._run_coverage_parent(root)
            with patch(
                "factor_miner.shadow_generators.platform_system",
                return_value="Linux",
            ):
                summary = generate_shadow_arms(
                    family.discovery_family_id,
                    family,
                    root,
                    (approved,),
                    price_registry(),
                    now=NOW,
                )

            self.assertEqual(len(summary.generated_candidate_ids), 6)
            self.assertEqual(len(summary.terminal_slot_ids), 54)
            self.assertEqual(summary.failed_slot_ids, ())
            projected = LLMDiscoveryLedger(root).project_state(
                family.discovery_family_id
            )
            for slot_id in (
                "mechanical_mutation:001",
                "mechanical_mutation:002",
                "mechanical_mutation:003",
                "hypothesis_conditioned_grammar:001",
                "hypothesis_conditioned_grammar:002",
                "hypothesis_conditioned_grammar:003",
            ):
                self.assertIs(
                    projected.candidate_slot_states[slot_id],
                    CandidateSlotState.READY_FOR_REGISTRATION,
                )
            self.assertIs(
                projected.candidate_slot_states["mechanical_mutation:004"],
                CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
            )

    def test_shadow_summary_is_immutable_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family, approved = self._run_coverage_parent(root)
            with patch(
                "factor_miner.shadow_generators.platform_system",
                return_value="Linux",
            ):
                first = generate_shadow_arms(
                    family.discovery_family_id,
                    family,
                    root,
                    (approved,),
                    price_registry(),
                    now=NOW,
                )
                second = generate_shadow_arms(
                    family.discovery_family_id,
                    family,
                    root,
                    (approved,),
                    price_registry(),
                    now=NOW,
                )
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
