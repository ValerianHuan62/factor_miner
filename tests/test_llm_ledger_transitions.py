"""事件追加前预检和污染 family 隔离测试。"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_state import (
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
)
from tests.test_llm_schema import valid_family_spec


class LedgerTransitionTest(unittest.TestCase):
    """非法状态不能污染事件文件。"""

    def test_invalid_transition_is_rejected_before_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            ledger.append_event(
                LLMDiscoveryEvent(
                    event_id="event-generating",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.FAMILY,
                    target_id=family.discovery_family_id,
                    to_state=FamilyGenerationState.GENERATING,
                    created_at=datetime.now(timezone.utc),
                )
            )
            events_path = ledger.events_path(family.discovery_family_id)
            before = events_path.read_bytes()
            with self.assertRaises(FactorMinerError) as context:
                ledger.append_event(
                    LLMDiscoveryEvent(
                        event_id="event-illegal",
                        discovery_family_id=family.discovery_family_id,
                        target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                        target_id="coverage_outcome_llm:001",
                        to_state=CandidateSlotState.READY_FOR_REGISTRATION,
                        created_at=datetime.now(timezone.utc),
                    )
                )
            self.assertEqual(
                context.exception.code,
                FailureCode.LLM_STATE_TRANSITION_INVALID,
            )
            self.assertEqual(events_path.read_bytes(), before)

    def test_invalid_existing_state_is_quarantined_without_repair_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LLMDiscoveryLedger(root)
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            # 通过底层哈希链构造“字节合法、状态非法”的旧审计样本。
            ledger._store(family.discovery_family_id).append(  # noqa: SLF001
                LLMDiscoveryEvent(
                    event_id="event-invalid-history",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                    target_id="coverage_outcome_llm:001",
                    to_state=CandidateSlotState.READY_FOR_REGISTRATION,
                    created_at=datetime.now(timezone.utc),
                )
            )
            events_path = ledger.events_path(family.discovery_family_id)
            before = events_path.read_bytes()
            with self.assertRaises(FactorMinerError) as context:
                ledger.append_event(
                    LLMDiscoveryEvent(
                        event_id="event-repair",
                        discovery_family_id=family.discovery_family_id,
                        target_kind=DiscoveryObjectKind.FAMILY,
                        target_id=family.discovery_family_id,
                        to_state=FamilyGenerationState.GENERATING,
                        created_at=datetime.now(timezone.utc),
                    )
                )
            self.assertEqual(context.exception.code, FailureCode.AUDIT_QUARANTINED)
            self.assertEqual(events_path.read_bytes(), before)

    def test_direct_artifact_writes_also_reject_quarantined_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            arm = family.spec.arm_specs[0]
            with self.assertRaises(FactorMinerError) as context:
                ledger.register_arm_spec(
                    "llmfamily_1bae19965638a6ac9620e0b0",
                    arm,
                )
            self.assertEqual(context.exception.code, FailureCode.AUDIT_QUARANTINED)


if __name__ == "__main__":
    unittest.main()
