"""Dashboard 全文审批、命令和 PostgreSQL 控制投影测试。"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.autonomous_schema import (
    AutonomousResearchState,
    AutonomousStage,
    ResearchCommandType,
)
from factor_miner.research_control import ResearchControlStore
from dashboard.pg_store import PostgresDashboardStore
from dashboard.research_control import (
    build_review_decision,
    hypothesis_detail_payload,
    hypothesis_summary_payload,
    load_hypothesis_rows,
    load_semantic_coverage_view,
    submit_approve_remaining_and_freeze,
    submit_freeze_command,
    submit_review_decision,
    submit_start_command,
)
from tests.test_lightweight_expressions import reviewed
from tests.test_research_evolution import response_payload_with_semantic_tags


NOW = datetime(2026, 8, 14, 14, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _Cursor()


class _Psycopg:
    def __init__(self, connection):
        self.connection = connection

    def connect(self, dsn):
        return self.connection


def _state(run_id: str) -> AutonomousResearchState:
    return AutonomousResearchState.build(
        run_id=run_id,
        sequence=2,
        stage=AutonomousStage.AWAITING_REVIEW,
        created_at=NOW,
        updated_at=NOW,
        command_ids=("researchcmd_" + "1" * 24,),
        stage_refs={"假设批次": "a" * 64},
    )


class DashboardResearchControlTest(unittest.TestCase):
    """摘要无动作，只有全文身份才能形成审批命令。"""

    def test_hypothesis_detail_contains_every_review_field(self) -> None:
        hypotheses, _ = reviewed(approved_count=2)
        draft = hypotheses.hypotheses[0]
        detail = hypothesis_detail_payload(draft)
        self.assertEqual(
            set(detail),
            {
                "主张", "机制", "预期方向", "可观察代理",
                "机制验证方案（不改变统一回测协议）",
                "竞争解释", "失效方式", "证伪路径", "来源与边界",
                "金融语义标签",
            },
        )
        self.assertNotIn("独立验证", detail)
        summary = hypothesis_summary_payload(draft)
        encoded = str(summary).lower()
        for forbidden in ("批准", "拒绝", "token", "usage", "raw_response"):
            self.assertNotIn(forbidden, encoded)

    def test_dashboard_builds_semantic_heatmap_and_duplicate_regions(self) -> None:
        hypotheses, _ = reviewed(
            approved_count=2,
            hypothesis_response=response_payload_with_semantic_tags(),
        )
        rows = tuple(
            {
                "logical_slot_id": draft.logical_slot_id,
                "semantic_plan": (
                    draft.semantic_plan.model_dump(mode="json")
                    if draft.semantic_plan is not None
                    else None
                ),
            }
            for draft in hypotheses.hypotheses
        )
        with tempfile.TemporaryDirectory() as directory:
            coverage = load_semantic_coverage_view(Path(directory), rows)

        self.assertEqual(coverage.tagged_hypothesis_count, 10)
        self.assertEqual(
            coverage.event_context_counts["breakout"]["recent_extreme"],
            5,
        )
        self.assertEqual(len(coverage.duplicate_regions), 2)

    def test_duplicate_buttons_submit_same_content_addressed_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = submit_start_command(
                root, requested_by="research_owner", requested_at=NOW
            )
            second = submit_start_command(
                root, requested_by="research_owner", requested_at=NOW
            )
            self.assertEqual(first.command_id, second.command_id)

    def test_dashboard_decision_serializes_aware_datetime_before_hashing(self) -> None:
        hypotheses, _ = reviewed(approved_count=2)
        draft = hypotheses.hypotheses[0]
        decision = build_review_decision(
            run_id=hypotheses.run_id,
            context_sha256=hypotheses.context_sha256,
            logical_slot_id=draft.logical_slot_id,
            draft_sha256=draft.draft_sha256,
            decision="approved",
            approval_role="research_owner",
            decided_at=NOW,
        )
        self.assertEqual(decision.decision, "approved")
        self.assertEqual(decision.decided_at, NOW)
        self.assertEqual(len(decision.decision_sha256), 64)

    def test_dashboard_reads_worker_decisions_from_objects_directory(self) -> None:
        hypotheses, review = reviewed(approved_count=10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResearchControlStore(root)
            objects = store.runs_root / hypotheses.run_id / "objects"
            decisions = objects / "decisions"
            decisions.mkdir(parents=True)
            (objects / "hypotheses.json").write_text(
                hypotheses.model_dump_json(), encoding="utf-8"
            )
            for decision in review.decisions:
                (decisions / f"{decision.logical_slot_id}.json").write_text(
                    decision.model_dump_json(), encoding="utf-8"
                )

            rows = load_hypothesis_rows(root, hypotheses.run_id)

        self.assertEqual(len(rows), 10)
        self.assertEqual(
            tuple(row["decision"] for row in rows),
            ("approved",) * 10,
        )

    def test_review_and_freeze_commands_bind_full_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hypotheses, review = reviewed(approved_count=2)
            decision = review.decisions[0]
            command = submit_review_decision(
                root,
                decision=decision,
                requested_by="research_owner",
                requested_at=NOW,
            )
            self.assertEqual(command.command_type, ResearchCommandType.REVIEW_DECISION)
            self.assertEqual(command.body["draft_sha256"], decision.draft_sha256)
            freeze = submit_freeze_command(
                root,
                review=review,
                requested_by="research_owner",
                requested_at=NOW,
            )
            self.assertEqual(freeze.command_type, ResearchCommandType.FREEZE_REVIEW)
            self.assertEqual(len(freeze.body["decision_sha256"]), 10)

    def test_one_click_approves_all_undecided_hypotheses_and_freezes(self) -> None:
        hypotheses, _ = reviewed(approved_count=2)
        rows = tuple(
            {
                "logical_slot_id": draft.logical_slot_id,
                "context_sha256": hypotheses.context_sha256,
                "hypothesis_batch_sha256": hypotheses.batch_sha256,
                "draft_sha256": draft.draft_sha256,
                "decision": None,
                "decision_sha256": None,
            }
            for draft in hypotheses.hypotheses
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands, freeze = submit_approve_remaining_and_freeze(
                root,
                run_id=hypotheses.run_id,
                rows=rows,
                approval_role="research_owner",
                requested_by="research_owner",
                requested_at=NOW,
            )
            pending = ResearchControlStore(root).pending_commands()

        self.assertEqual(len(commands), 10)
        self.assertTrue(
            all(item.command_type == ResearchCommandType.REVIEW_DECISION for item in commands)
        )
        self.assertEqual(freeze.command_type, ResearchCommandType.FREEZE_REVIEW)
        self.assertEqual(len(freeze.body["decision_sha256"]), 10)
        self.assertEqual(len(pending), 11)

    def test_one_click_preserves_existing_rejection_and_approves_only_remaining(self) -> None:
        hypotheses, review = reviewed(approved_count=9)
        existing = review.decisions[-1]
        rows = tuple(
            {
                "logical_slot_id": draft.logical_slot_id,
                "context_sha256": hypotheses.context_sha256,
                "hypothesis_batch_sha256": hypotheses.batch_sha256,
                "draft_sha256": draft.draft_sha256,
                "decision": existing.decision if draft.logical_slot_id == existing.logical_slot_id else None,
                "decision_sha256": (
                    existing.decision_sha256
                    if draft.logical_slot_id == existing.logical_slot_id
                    else None
                ),
            }
            for draft in hypotheses.hypotheses
        )
        with tempfile.TemporaryDirectory() as directory:
            commands, freeze = submit_approve_remaining_and_freeze(
                Path(directory),
                run_id=hypotheses.run_id,
                rows=rows,
                approval_role="research_owner",
                requested_by="research_owner",
                requested_at=NOW,
            )

        self.assertEqual(len(commands), 9)
        self.assertEqual(freeze.body["decision_sha256"][-1], existing.decision_sha256)

    def test_postgres_projects_chinese_hypotheses_and_control_state(self) -> None:
        hypotheses, review = reviewed(approved_count=2)
        connection = _Connection()
        store = PostgresDashboardStore.__new__(PostgresDashboardStore)
        store._psycopg = _Psycopg(connection)
        store._dsn = "postgresql://synthetic"
        store._market_id = "a_share"
        store.project_control_state(
            _state(hypotheses.run_id),
            hypotheses=hypotheses,
            review=review,
            heartbeat_at=NOW,
        )
        inserts = [item for item in connection.calls if "INSERT INTO research_hypotheses" in item[0]]
        self.assertEqual(len(inserts), 10)
        sql = " ".join(item[0] for item in connection.calls).lower()
        self.assertIn("claim", inserts[0][0])
        self.assertIn("mechanism", inserts[0][0])
        for forbidden in ("token", "sha256", "heartbeat", "worker"):
            self.assertNotIn(forbidden, sql)
        run_insert = next(
            params for sql, params in connection.calls
            if "INSERT INTO research_runs" in sql
        )
        self.assertEqual(run_insert[5], 0)

    def test_actual_factor_count_is_updated_after_successful_evaluation(self) -> None:
        connection = _Connection()
        store = PostgresDashboardStore.__new__(PostgresDashboardStore)
        store._psycopg = _Psycopg(connection)
        store._dsn = "postgresql://synthetic"
        store._market_id = "a_share"

        store.update_research_run_factor_count("autrun_" + "1" * 24, 12)

        sql, params = connection.calls[-1]
        self.assertIn("UPDATE research_runs SET factor_count", sql)
        self.assertEqual(params, (12, "autrun_" + "1" * 24))

    def test_migration_keeps_complete_metrics_and_adds_chinese_control_view(self) -> None:
        sql = Path("dashboard/migrations/009_autonomous_research_control.sql").read_text("utf-8")
        self.assertIn("research_hypotheses", sql)
        self.assertIn("latest_research_run_zh", sql)
        self.assertIn("mechanism_unverified", sql)
        self.assertNotIn("token", sql.lower())
        old = Path("dashboard/migrations/008_complete_chinese_candidate_views.sql").read_text("utf-8")
        self.assertIn("ic_mean IS NOT NULL", old)
        self.assertIn("information_ratio IS NOT NULL", old)

    def test_worker_heartbeat_is_not_stored(self) -> None:
        connection = _Connection()
        store = PostgresDashboardStore.__new__(PostgresDashboardStore)
        store._psycopg = _Psycopg(connection)
        store._dsn = "postgresql://synthetic"
        store.project_worker_heartbeat(NOW)
        statements = " ".join(item[0] for item in connection.calls)
        self.assertEqual(statements, "")


if __name__ == "__main__":
    unittest.main()
