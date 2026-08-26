"""自主研究命令收件箱和中文状态测试。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from factor_miner.autonomous_schema import (
    AutonomousResearchState,
    AutonomousStage,
    ResearchCommand,
    ResearchStartAuthorization,
)
from factor_miner.research_control import ResearchControlStore


NOW = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)


def state(
    run_id: str,
    *,
    stage: AutonomousStage = AutonomousStage.CONTEXT_PREPARING,
    sequence: int = 0,
    last_error: str | None = None,
) -> AutonomousResearchState:
    """构造不含研究结果的合成状态。"""

    return AutonomousResearchState.build(
        run_id=run_id,
        sequence=sequence,
        stage=stage,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=sequence),
        command_ids=("researchcmd_" + "a" * 24,),
        stage_refs={"启动命令": "a" * 64},
        last_error=last_error,
    )


class ResearchControlTest(unittest.TestCase):
    """Dashboard 只能写幂等命令，Worker 独占运行状态。"""

    def test_duplicate_start_command_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchControlStore(Path(directory))
            command = ResearchCommand.start(
                requested_by="research_owner",
                requested_at=NOW,
            )
            first = store.submit(command)
            second = store.submit(command)
            self.assertEqual(first, second)
            self.assertEqual(store.pending_commands(), (command,))

    def test_second_active_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchControlStore(Path(directory))
            store.publish_state(state("autrun_" + "1" * 24))
            with self.assertRaisesRegex(ValueError, "已有活动研究批次"):
                store.submit(
                    ResearchCommand.start(
                        requested_by="research_owner",
                        requested_at=NOW,
                    )
                )

    def test_processed_command_leaves_pending_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchControlStore(Path(directory))
            command = ResearchCommand.start(
                requested_by="research_owner",
                requested_at=NOW,
            )
            store.submit(command)
            store.mark_processed(command.command_id, processed_at=NOW)
            self.assertEqual(store.pending_commands(), ())

    def test_state_round_trip_and_sequence_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchControlStore(Path(directory))
            run_id = "autrun_" + "2" * 24
            first = state(run_id)
            self.assertEqual(store.publish_state(first), store.publish_state(first))
            second = state(
                run_id,
                stage=AutonomousStage.HYPOTHESIS_GENERATING,
                sequence=1,
            )
            store.publish_state(second)
            self.assertEqual(store.load_state(run_id), second)
            with self.assertRaisesRegex(ValueError, "sequence 必须连续递增"):
                store.publish_state(
                    state(
                        run_id,
                        stage=AutonomousStage.AWAITING_REVIEW,
                        sequence=3,
                    )
                )

    def test_terminal_state_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchControlStore(Path(directory))
            store.publish_state(
                state(
                    "autrun_" + "3" * 24,
                    stage=AutonomousStage.NO_APPROVED_HYPOTHESIS,
                )
            )
            self.assertIsNone(store.active_state())

    def test_error_and_stage_labels_must_be_chinese(self) -> None:
        with self.assertRaisesRegex(ValidationError, "中文"):
            state(
                "autrun_" + "4" * 24,
                stage=AutonomousStage.FAILED,
                last_error="provider failed",
            )
        valid = state(
            "autrun_" + "4" * 24,
            stage=AutonomousStage.FAILED,
            last_error="模型服务暂时不可用",
        )
        self.assertEqual(valid.stage_label, "运行失败")

    def test_start_authorization_binds_command_and_roles(self) -> None:
        command = ResearchCommand.start(
            requested_by="research_owner",
            requested_at=NOW,
        )
        authorization = ResearchStartAuthorization.build(
            command=command,
            model="deepseek-v4-pro",
            approver_role="research_owner",
            expires_at=NOW + timedelta(hours=12),
        )
        self.assertEqual(authorization.start_command_id, command.command_id)
        self.assertEqual(authorization.allowed_agent_roles, ("hypothesis", "expression"))
        payload = authorization.model_dump(mode="json")
        payload["authorization_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "启动授权"):
            ResearchStartAuthorization.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
