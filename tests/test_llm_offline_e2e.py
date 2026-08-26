"""V0.5 纯合成 120 槽登记、重放与封存端到端测试。"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_seal import (
    build_generation_seal,
    verify_generation_seal,
)
from factor_miner.llm_state import (
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    expected_candidate_slot_ids,
)
from tests.test_llm_schema import valid_family_spec
from tests.test_llm_seal import seal_inputs


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def append_complete_generation(ledger, family, *, omit_last: bool = False):
    """追加 family generating 与全部候选明确终态事件。"""

    ledger.append_event(
        LLMDiscoveryEvent(
            event_id="event-family-generating",
            discovery_family_id=family.discovery_family_id,
            target_kind=DiscoveryObjectKind.FAMILY,
            target_id=family.discovery_family_id,
            to_state=FamilyGenerationState.GENERATING,
            created_at=NOW,
        )
    )
    slot_ids = expected_candidate_slot_ids(family.spec)
    if omit_last:
        slot_ids = slot_ids[:-1]
    for index, slot_id in enumerate(slot_ids, start=1):
        ledger.append_event(
            LLMDiscoveryEvent(
                event_id=f"event-slot-{index:03d}",
                discovery_family_id=family.discovery_family_id,
                target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                target_id=slot_id,
                to_state=(
                    CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
                ),
                created_at=NOW,
            )
        )


class LLMOfflineE2ETest(unittest.TestCase):
    """完整离线流程必须可重放，且不完整 family 不能 seal。"""

    def test_complete_120_slot_family_replays_and_seals_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            append_complete_generation(ledger, family)
            first = ledger.project_state(family.discovery_family_id)
            second = ledger.project_state(family.discovery_family_id)
            object_hashes = {
                slot_id: f"{index:064x}"
                for index, slot_id in enumerate(
                    first.candidate_slot_states,
                    start=1,
                )
            }
            seal = build_generation_seal(
                family,
                first,
                slot_object_hashes=object_hashes,
                evaluation_dependency_intervals=(),
                **seal_inputs(),
            )
            path = ledger.register_generation_seal(seal)
            ledger.append_event(
                LLMDiscoveryEvent(
                    event_id="event-family-generation-sealed",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.FAMILY,
                    target_id=family.discovery_family_id,
                    to_state=FamilyGenerationState.GENERATION_SEALED,
                    created_at=NOW,
                )
            )
            sealed_state = ledger.project_state(family.discovery_family_id)
            verify_generation_seal(family, sealed_state, seal)

            self.assertEqual(first, second)
            self.assertTrue(path.is_file())
            self.assertEqual(
                ledger.load_generation_seal(family.discovery_family_id),
                seal,
            )

    def test_one_unfinished_slot_blocks_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            append_complete_generation(ledger, family, omit_last=True)
            state = ledger.project_state(family.discovery_family_id)
            hashes = {
                slot_id: f"{index:064x}"
                for index, slot_id in enumerate(
                    state.candidate_slot_states,
                    start=1,
                )
            }

            with self.assertRaises(FactorMinerError) as context:
                build_generation_seal(
                    family,
                    state,
                    slot_object_hashes=hashes,
                    evaluation_dependency_intervals=(),
                    **seal_inputs(),
                )
            self.assertEqual(
                context.exception.code,
                FailureCode.LLM_FAMILY_NOT_SEALED,
            )


if __name__ == "__main__":
    unittest.main()
