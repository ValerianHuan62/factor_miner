"""V0.5 不可变文档与独立哈希链账本测试。"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_brief import (
    LLMCoverageBrief,
    LLMCoverageBriefSpec,
    registered_llm_coverage_brief,
    select_coverage_gaps,
    GapSelectionPolicy,
)
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_state import (
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
)
from tests.test_llm_brief import gaps
from tests.test_llm_schema import valid_family_spec


def registered_brief():
    """构造纯合成、内容寻址的脱敏 brief。"""

    return registered_llm_coverage_brief(
        LLMCoverageBriefSpec(
            source_coverage_graph_id="covgraph_" + "1" * 24,
            algorithm_version="brief-v1",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        ),
        LLMCoverageBrief(
            taxonomy_summary=("价格行为",),
            coverage_gap_cards=select_coverage_gaps(
                gaps(),
                GapSelectionPolicy(max_cards=4),
            ),
            failure_pattern_cards=("失败类型低",),
            allowed_field_aliases=("price_close",),
            allowed_operators=("rolling_mean",),
            allowed_windows=(5, 20),
        ),
    )


def family_event(family_id: str, event_id: str = "event-1") -> LLMDiscoveryEvent:
    """构造 family 进入 generating 的事件。"""

    return LLMDiscoveryEvent(
        event_id=event_id,
        discovery_family_id=family_id,
        target_kind=DiscoveryObjectKind.FAMILY,
        target_id=family_id,
        to_state=FamilyGenerationState.GENERATING,
        created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


class LLMDiscoveryLedgerTest(unittest.TestCase):
    """V0.5 必须复用既有账本强度，但使用独立事件文件。"""

    def test_documents_are_idempotent_and_cannot_be_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            brief = registered_brief()
            family_path = ledger.register_family(family)
            brief_path = ledger.register_brief(brief)

            self.assertEqual(family_path, ledger.register_family(family))
            self.assertEqual(brief_path, ledger.register_brief(brief))
            family_path.write_text("损坏", encoding="utf-8")
            with self.assertRaises(FactorMinerError):
                ledger.register_family(family)

    def test_llm_events_use_independent_verified_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LLMDiscoveryLedger(root)
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            stored = ledger.append_event(family_event(family.discovery_family_id))

            self.assertEqual(stored.sequence, 1)
            self.assertIsNotNone(stored.event_hash)
            self.assertEqual(ledger.verify(family.discovery_family_id), (stored,))
            self.assertFalse((root / "state" / "ledger" / "trials.jsonl").exists())

    def test_truncated_or_tampered_llm_chain_fails_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = LLMDiscoveryLedger(Path(directory))
            family = registered_llm_discovery_family(valid_family_spec())
            ledger.register_family(family)
            ledger.append_event(family_event(family.discovery_family_id))
            events_path = ledger.events_path(family.discovery_family_id)
            events_path.write_bytes(events_path.read_bytes().rstrip(b"\n"))

            with self.assertRaises(FactorMinerError) as context:
                ledger.project_state(family.discovery_family_id)
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)


if __name__ == "__main__":
    unittest.main()
