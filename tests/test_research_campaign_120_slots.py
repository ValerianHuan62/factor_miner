"""正式研究族 120 槽登记、shadow 收口和中断恢复测试。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_orchestrator import (
    ApprovedBatchDependencies,
    prepare_approved_batch_export,
)
from factor_miner.llm_state import CANDIDATE_TERMINALS
from factor_miner.research_campaign_runner import run_approved_campaign_generation
from tests.test_llm_orchestrator import (
    NOW,
    PreparedRequestProxy,
    RecordedBatchTransport,
    approved_for,
    authorization_for,
    dependencies,
    price_registry,
)


class ResearchCampaign120SlotTest(unittest.TestCase):
    """中断恢复只能复用原请求，最终四条 arm 必须都有明确终态。"""

    def test_resume_reaches_exactly_120_terminal_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = dependencies(root)
            approved = approved_for(base.family.discovery_family_id)
            export = prepare_approved_batch_export(
                base.family.discovery_family_id,
                (approved,),
                base,
            )
            authorization_map = {
                export.request_hashes[0]: authorization_for(
                    PreparedRequestProxy(
                        request_sha256=export.request_hashes[0],
                        campaign_id=(
                            f"{base.family.discovery_family_id}:coverage_outcome_llm"
                        ),
                    ),
                    base.policy,
                )
            }
            dependencies_with_transport = ApprovedBatchDependencies(
                artifact_root=root,
                family=base.family,
                policy=base.policy,
                public_payload_by_hypothesis_id=base.public_payload_by_hypothesis_id,
                authorization_by_request_hash=authorization_map,
                field_registry=price_registry(),
                transport=RecordedBatchTransport(),
                now=NOW,
            )
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic"}), patch(
                "factor_miner.llm_orchestrator.platform.system",
                return_value="Linux",
            ), patch(
                "factor_miner.shadow_generators.platform_system",
                return_value="Linux",
            ):
                first = run_approved_campaign_generation(
                    base.family.discovery_family_id,
                    base.family,
                    dependencies_with_transport,
                    (approved,),
                )
                self.assertEqual(first.status, "awaiting_authorization")
                request_path = next(
                    (
                        root
                        / "state"
                        / "llm_approved_batches"
                        / base.family.discovery_family_id
                        / "requests"
                        / "semantic_lint"
                    ).glob("*.json")
                )
                request_hash = json.loads(request_path.read_text())["request_sha256"]
                authorization_map[request_hash] = authorization_for(
                    PreparedRequestProxy(
                        request_sha256=request_hash,
                        campaign_id=(
                            f"{base.family.discovery_family_id}:coverage_outcome_llm"
                        ),
                    ),
                    base.policy,
                )
                second = run_approved_campaign_generation(
                    base.family.discovery_family_id,
                    base.family,
                    dependencies_with_transport,
                    (approved,),
                )

            state = LLMDiscoveryLedger(root).project_state(
                base.family.discovery_family_id
            )
            self.assertEqual(second.status, "completed")
            self.assertEqual(len(state.candidate_slot_states), 120)
            self.assertTrue(
                all(value in CANDIDATE_TERMINALS for value in state.candidate_slot_states.values())
            )
            self.assertEqual(len(tuple(state.applied_event_ids)), len(set(state.applied_event_ids)))


if __name__ == "__main__":
    unittest.main()
