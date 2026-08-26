from pathlib import Path
from datetime import datetime, timezone
import fcntl
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import (
    EventType,
    JsonlLedger,
    TrialEvent,
    recover_interrupted_runs,
)
from factor_miner.policy import company_a_share_visible_policy
from tests.helpers import (
    valid_campaign,
    valid_registered_candidate,
    valid_research_family,
    valid_registered_reference_factor_library,
)


def pending_event(event_id: str, status: str = "started") -> TrialEvent:
    """构造一个等待账本补全序号和哈希的事件。"""

    candidate = valid_registered_candidate()
    campaign = valid_campaign()
    return TrialEvent(
        event_id=event_id,
        candidate_id=candidate.candidate_id,
        campaign_id=f"camp_test_{campaign.candidate_ids[0][5:13]}",
        run_id=f"run_{event_id}",
        event_type=EventType.RUN_STARTED,
        status=status,
        outcome_exposed=False,
    )


class LedgerTest(unittest.TestCase):
    """不可变文档和追加式哈希链账本测试。"""

    def test_documents_are_idempotent_but_cannot_change_bytes(self) -> None:
        """验证相同文档幂等，不同字节不能覆盖。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            candidate = valid_registered_candidate()
            campaign = valid_campaign()
            candidate_path = ledger.register_candidate(candidate)
            campaign_path = ledger.register_campaign(campaign)
            self.assertEqual(candidate_path, ledger.register_candidate(candidate))
            self.assertEqual(campaign_path, ledger.register_campaign(campaign))

            candidate_path.write_bytes("损坏文档".encode("utf-8"))
            with self.assertRaises(FactorMinerError) as context:
                ledger.register_candidate(candidate)
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_policy_and_family_documents_are_immutable_and_idempotent(self) -> None:
        """V0.1 policy/family 使用独立内容寻址目录且禁止覆盖。"""

        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            policy = company_a_share_visible_policy()
            family = valid_research_family()
            policy_path = ledger.register_evaluation_policy(policy)
            family_path = ledger.register_research_family(family)
            self.assertEqual(policy_path, ledger.register_evaluation_policy(policy))
            self.assertEqual(family_path, ledger.register_research_family(family))
            self.assertEqual(policy_path.parent, ledger.paths.policies_root)
            self.assertEqual(family_path.parent, ledger.paths.families_root)

            family_path.write_text("损坏", encoding="utf-8")
            with self.assertRaises(FactorMinerError) as context:
                ledger.register_research_family(family)
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_reference_library_document_is_immutable_and_content_addressed(self) -> None:
        """V0.2 参考库必须在独立目录幂等登记且禁止覆盖。"""

        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            library = valid_registered_reference_factor_library()
            path = ledger.register_reference_factor_library(library)
            self.assertEqual(path, ledger.register_reference_factor_library(library))
            self.assertEqual(path.parent, ledger.paths.reference_libraries_root)
            self.assertEqual(
                path.stem,
                library.reference_factor_library_id,
            )

            path.write_text("损坏", encoding="utf-8")
            with self.assertRaises(FactorMinerError) as context:
                ledger.register_reference_factor_library(library)
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_events_have_monotonic_sequence_and_previous_hash(self) -> None:
        """验证追加事件序号递增且 previous hash 正确。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            first = ledger.append_event(pending_event("event-1"))
            second = ledger.append_event(pending_event("event-2"))
            self.assertEqual(first.sequence, 1)
            self.assertEqual(second.sequence, 2)
            self.assertIsNone(first.previous_event_hash)
            self.assertEqual(second.previous_event_hash, first.event_hash)
            self.assertEqual(ledger.read_events(), [first, second])

    def test_fixed_event_hash_preserves_existing_ledger_bytes(self) -> None:
        """共享账本重构不得改变已经冻结的 TrialEvent 规范字节。"""

        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            stored = ledger.append_event(
                TrialEvent(
                    event_id="event-fixed",
                    created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
                    event_type=EventType.RUN_STARTED,
                    status="started",
                )
            )

            self.assertEqual(
                stored.event_hash,
                "5d3d9823d1ab8e5e3038670b8ff0110a31b365042957b0c6ae56ef0a15012164",
            )

    def test_duplicate_event_id_is_rejected(self) -> None:
        """验证 event ID 不能重复。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            ledger.append_event(pending_event("event-1"))
            with self.assertRaises(FactorMinerError) as context:
                ledger.append_event(pending_event("event-1"))
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_editing_prior_jsonl_bytes_is_detected(self) -> None:
        """验证修改既有 JSONL 字节后 verify 失败。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            ledger.append_event(pending_event("event-1"))
            ledger.append_event(pending_event("event-2"))
            content = ledger.paths.trials_path.read_bytes()
            ledger.paths.trials_path.write_bytes(content.replace(b"started", b"changed", 1))
            with self.assertRaises(FactorMinerError) as context:
                ledger.verify()
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_truncated_final_line_is_detected(self) -> None:
        """验证最后一行缺少换行或 JSON 不完整时 verify 失败。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            ledger.append_event(pending_event("event-1"))
            with ledger.paths.trials_path.open("ab") as stream:
                stream.write(b"{\"event_id\":\"truncated\"")
            with self.assertRaises(FactorMinerError) as context:
                ledger.verify()
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_second_non_blocking_writer_is_rejected(self) -> None:
        """验证第二个 writer 无法取得非阻塞锁。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            ledger.paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with ledger.paths.lock_path.open("a+b") as lock_stream:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaises(FactorMinerError) as context:
                        ledger.append_event(pending_event("event-1"))
                    self.assertEqual(
                        context.exception.code,
                        FailureCode.LEDGER_CONCURRENT_WRITER,
                    )
                finally:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def test_recovery_appends_one_interrupted_event_and_preserves_staging(self) -> None:
        """无终态运行恢复时只追加一次中断事件且不删除残留目录。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = JsonlLedger(root)
            ledger.append_event(
                TrialEvent(
                    campaign_id="camp_test",
                    run_id="run_orphan",
                    event_type=EventType.RUN_STARTED,
                    status="started",
                )
            )
            staging = root / "artifacts" / ".staging" / "run_orphan"
            staging.mkdir(parents=True)
            (staging / "partial.bin").write_bytes(b"partial")
            self.assertEqual(recover_interrupted_runs(root), ("run_orphan",))
            self.assertEqual(recover_interrupted_runs(root), ())
            self.assertEqual((staging / "partial.bin").read_bytes(), b"partial")
            self.assertEqual(ledger.read_events()[-1].event_type, EventType.INTERRUPTED)

    def test_correction_requires_superseded_event(self) -> None:
        """验证纠错事件记录 supersedes_event_id 并追加新事件。"""
        with tempfile.TemporaryDirectory() as temporary_root:
            ledger = JsonlLedger(Path(temporary_root))
            first = ledger.append_event(pending_event("event-1"))
            correction = pending_event("event-2", status="corrected").model_copy(
                update={"supersedes_event_id": first.event_id}
            )
            stored = ledger.append_event(correction)
            self.assertEqual(stored.supersedes_event_id, first.event_id)
            self.assertEqual(len(ledger.read_events()), 2)


if __name__ == "__main__":
    unittest.main()
