"""自主研究 Worker 的审批冻结与阶段恢复测试。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.autonomous_schema import ResearchCommand, ResearchCommandType
from factor_miner.lightweight_expressions import (
    build_lightweight_expression_request,
    candidate_bindings_from_expression_batch,
    parse_lightweight_expression_response,
)
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisDecision,
    generate_lightweight_hypotheses,
)
from factor_miner.lightweight_schema import (
    LightweightResearchConfig,
    build_lightweight_batch_manifest,
)
from factor_miner.llm_online import AgentRole, build_deepseek_request
from factor_miner.llm_state import CandidateSlotState
from factor_miner.research_campaign_runner import (
    CampaignSlotEvaluation,
    LightweightCampaignEvaluationResult,
)
from factor_miner.research_control import ResearchControlStore
from factor_miner.research_worker import ResearchWorker, ResearchWorkerConfig
from tests.test_design_diversity import _registry
from tests.test_lightweight_hypotheses import FakeProvider
from tests.test_research_evolution import context, response_payload


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


class FakeDependencies:
    """使用合成输入并记录昂贵阶段调用次数。"""

    def __init__(
        self,
        *,
        fail_projection_once: bool = False,
        fail_hypotheses_once: bool = False,
        all_expressions_invalid: bool = False,
        fail_manifest_once: bool = False,
    ) -> None:
        self.calls = {
            name: 0
            for name in (
                "context", "hypotheses", "expressions", "manifest", "evaluate",
                "publish", "control", "project", "refresh",
                "refresh_rejected",
            )
        }
        self.fail_projection_once = fail_projection_once
        self.fail_hypotheses_once = fail_hypotheses_once
        self.all_expressions_invalid = all_expressions_invalid
        self.fail_manifest_once = fail_manifest_once
        self.projected_stages: list[str] = []

    def prepare_context(self, state):
        self.calls["context"] += 1
        return {
            "memory_snapshot_hash": "a" * 64,
            "coverage_graph_hash": "b" * 64,
        }

    def generate_hypotheses(self, state, source_context):
        self.calls["hypotheses"] += 1
        if self.fail_hypotheses_once:
            self.fail_hypotheses_once = False
            raise RuntimeError("合成假设模型暂时不可用")
        prepared = build_deepseek_request(
            campaign_id=f"{state.run_id}:hypothesis",
            agent_role=AgentRole.HYPOTHESIS,
            slot_ids=tuple(f"H{i:02d}" for i in range(1, 11)),
            system_prompt="只输出包含十条中文假设的 JSON object。",
            user_payload={"information_class": "gap_brief"},
            audit_payload={"information_class": "gap_brief"},
        )
        return generate_lightweight_hypotheses(
            run_id=state.run_id,
            context=context(),
            prepared=prepared,
            provider=FakeProvider(response_payload()),
        )

    def generate_expressions(self, hypotheses, review):
        self.calls["expressions"] += 1
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        groups = []
        for logical_slot in review.approved_slot_ids:
            groups.append(
                {
                    "logical_slot_id": logical_slot,
                    "designs": [
                        {
                            "candidate_slot_id": f"{logical_slot}:C001",
                            "expression": {"op": "div", "args": [
                                {"op": "delta", "args": [{"op": "field", "field": "price_close"}], "period": 20},
                                {"op": "rolling_std", "args": [{"op": "field", "field": "price_close"}], "window": 20},
                            ]},
                        },
                        {
                            "candidate_slot_id": f"{logical_slot}:C002",
                            "expression": {"op": "mul", "args": [
                                {"op": "div", "args": [
                                    {"op": "field", "field": "volume"},
                                    {"op": "rolling_mean", "args": [{"op": "field", "field": "volume"}], "window": 20},
                                ]},
                                {"op": "div", "args": [
                                    {"op": "field", "field": "price_close"},
                                    {"op": "rolling_mean", "args": [{"op": "field", "field": "price_close"}], "window": 20},
                                ]},
                            ]},
                        },
                        {
                            "candidate_slot_id": f"{logical_slot}:C003",
                            "expression": {"op": "neg", "args": [{
                                "op": "rolling_corr", "args": [
                                    {"op": "delta", "args": [{"op": "field", "field": "price_close"}], "period": 5},
                                    {"op": "delta", "args": [{"op": "field", "field": "volume"}], "period": 5},
                                ], "window": 20,
                            }]},
                        },
                    ],
                }
            )
        if self.all_expressions_invalid:
            for group in groups:
                for design in group["designs"]:
                    design["expression"] = {
                        "op": "rolling_mean",
                        "args": [{"op": "field", "field": "price_close"}],
                        "window": 60,
                    }
        return parse_lightweight_expression_response(
            prepared=prepared,
            response={"hypotheses": groups},
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )

    def freeze_manifest(self, source_context, expressions):
        self.calls["manifest"] += 1
        if self.fail_manifest_once:
            self.fail_manifest_once = False
            raise RuntimeError("合成清单暂时不可用")
        return build_lightweight_batch_manifest(
            config=LightweightResearchConfig(
                hypothesis_count=expressions.family_size // 3,
                candidates_per_hypothesis=3,
            ),
            candidate_bindings=candidate_bindings_from_expression_batch(expressions),
            evaluation_policy_hash="c" * 64,
            memory_snapshot_hash=str(source_context["memory_snapshot_hash"]),
            coverage_graph_hash=str(source_context["coverage_graph_hash"]),
        )

    def evaluate(self, manifest):
        self.calls["evaluate"] += 1
        return LightweightCampaignEvaluationResult.build(
            manifest=manifest,
            slot_evaluations=tuple(
                CampaignSlotEvaluation(
                    slot_id=item.slot_id,
                    slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                    status="evaluated" if item.terminal_status == "ready" else "failed",
                    failure_reason=item.failure_reason,
                )
                for item in manifest.candidate_bindings
            ),
        )

    def publish(self, manifest, evaluation):
        self.calls["publish"] += 1
        return {
            "published_run_id": "run_" + "1" * 24,
            "manifest": manifest.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }

    def project_control_state(self, state):
        self.calls["control"] += 1
        self.projected_stages.append(state.stage.value)

    def project(self, publication):
        self.calls["project"] += 1
        if self.fail_projection_once:
            self.fail_projection_once = False
            raise RuntimeError("合成数据库暂时不可用")
        return {"projection_status": "projected"}

    def refresh_evolution(self, publication, source_context):
        self.calls["refresh"] += 1
        return {"memory_snapshot_id": "memory_next", "coverage_graph_id": "graph_next"}

    def refresh_rejected_hypotheses(self, hypotheses, review, source_context):
        self.calls["refresh_rejected"] += 1
        return {"memory_snapshot_id": "memory_rejected_next", "hypothesis_count": 10}


class ResearchWorkerTest(unittest.TestCase):
    """结果揭晓前冻结审批，重启后不重复昂贵阶段。"""

    def _worker(self, root: Path, dependencies: FakeDependencies) -> ResearchWorker:
        return ResearchWorker(
            ResearchWorkerConfig(artifact_root=root),
            dependencies=dependencies,
            now=lambda: NOW + timedelta(minutes=1),
        )

    def _start(self, root: Path, worker: ResearchWorker) -> str:
        store = ResearchControlStore(root)
        store.submit(ResearchCommand.start(requested_by="research_owner", requested_at=NOW))
        result = worker.process_once()
        self.assertEqual(result.stage.value, "awaiting_review")
        assert result.run_id is not None
        return result.run_id

    def _review(self, root: Path, run_id: str, approved_count: int) -> None:
        store = ResearchControlStore(root)
        batch_path = store.runs_root / run_id / "objects" / "hypotheses.json"
        from factor_miner.lightweight_hypotheses import LightweightHypothesisBatch
        batch = LightweightHypothesisBatch.model_validate_json(batch_path.read_bytes())
        for index, draft in enumerate(batch.hypotheses):
            decision = LightweightHypothesisDecision.build(
                run_id=run_id,
                context_sha256=batch.context_sha256,
                draft=draft,
                decision="approved" if index < approved_count else "rejected",
                approval_role="research_owner",
                decided_at=NOW,
            )
            store.submit(
                ResearchCommand.build(
                    command_type=ResearchCommandType.REVIEW_DECISION,
                    target_run_id=run_id,
                    requested_by="research_owner",
                    requested_at=NOW + timedelta(seconds=index + 1),
                    body=decision.model_dump(mode="json"),
                )
            )
        store.submit(
            ResearchCommand.build(
                command_type=ResearchCommandType.FREEZE_REVIEW,
                target_run_id=run_id,
                requested_by="research_owner",
                requested_at=NOW + timedelta(seconds=20),
            )
        )

    def test_start_calls_hypothesis_provider_once_then_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies()
            worker = self._worker(root, dependencies)
            self._start(root, worker)
            worker.process_once()
            self.assertEqual(dependencies.calls["hypotheses"], 1)
            self.assertEqual(dependencies.calls["expressions"], 0)

    def test_all_rejected_ends_without_expression_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies()
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=0)
            result = worker.process_once()
            self.assertEqual(result.stage.value, "no_approved_hypothesis")
            self.assertEqual(dependencies.calls["expressions"], 0)
            self.assertEqual(dependencies.calls["refresh_rejected"], 1)

    def test_completed_state_is_projected_before_worker_becomes_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies()
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=1)

            for _ in range(8):
                worker.process_once()

            state = ResearchControlStore(root).load_state(run_id)
            self.assertEqual(state.stage.value, "completed")
            self.assertEqual(dependencies.projected_stages[-1], "completed")

    def test_zero_legal_expressions_fails_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies(all_expressions_invalid=True)
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=1)

            worker.process_once()
            result = worker.process_once()

            self.assertEqual(result.stage.value, "failed")
            self.assertEqual(dependencies.calls["evaluate"], 0)
            self.assertEqual(dependencies.calls["publish"], 0)
            state = ResearchControlStore(root).load_state(run_id)
            self.assertIn("没有任何候选通过", str(state.last_error))
            expression_path = (
                ResearchControlStore(root).runs_root
                / run_id / "objects" / "expressions.json"
            )
            self.assertFalse(expression_path.exists())

    def test_manifest_failure_resume_reuses_frozen_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies(fail_manifest_once=True)
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=1)
            worker.process_once()
            failed_result = worker.process_once()
            self.assertEqual(failed_result.stage.value, "failed")
            store = ResearchControlStore(root)
            failed = store.load_state(run_id)
            store.submit(ResearchCommand.build(
                command_type=ResearchCommandType.RESUME,
                target_run_id=run_id,
                requested_by="research_owner",
                requested_at=NOW + timedelta(minutes=2),
                body={"failed_state_sha256": failed.state_sha256},
            ))
            resumed = worker.process_once()
            self.assertEqual(resumed.stage.value, "manifest_frozen")
            self.assertEqual(dependencies.calls["expressions"], 1)
            self.assertEqual(dependencies.calls["manifest"], 2)

    def test_seven_approved_freezes_twenty_one_slots_and_restarts_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies()
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=7)
            worker.process_once()
            worker.process_once()
            manifest_path = ResearchControlStore(root).runs_root / run_id / "objects" / "manifest.json"
            from factor_miner.lightweight_schema import LightweightBatchManifest
            manifest = LightweightBatchManifest.model_validate_json(manifest_path.read_bytes())
            self.assertEqual(manifest.family_size, 21)
            self.assertEqual(dependencies.calls["expressions"], 1)
            restarted = self._worker(root, dependencies)
            for _ in range(5):
                restarted.process_once()
            calls = dict(dependencies.calls)
            restarted.process_once()
            self.assertEqual(dependencies.calls, calls)
            self.assertEqual(dependencies.calls["evaluate"], 1)
            self.assertEqual(dependencies.calls["publish"], 1)

    def test_projection_failure_resume_retries_only_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies(fail_projection_once=True)
            worker = self._worker(root, dependencies)
            run_id = self._start(root, worker)
            self._review(root, run_id, approved_count=1)
            for _ in range(5):
                worker.process_once()
            failed = ResearchControlStore(root).load_state(run_id)
            self.assertEqual(failed.stage.value, "failed")
            ResearchControlStore(root).submit(
                ResearchCommand.build(
                    command_type=ResearchCommandType.RESUME,
                    target_run_id=run_id,
                    requested_by="research_owner",
                    requested_at=NOW + timedelta(minutes=2),
                )
            )
            worker.process_once()
            self.assertEqual(dependencies.calls["publish"], 1)
            self.assertEqual(dependencies.calls["project"], 2)

    def test_hypothesis_failure_resume_retries_generation_without_rebuilding_context(self) -> None:
        """生成阶段失败后必须从该阶段续跑，不能永久停在等待。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies(fail_hypotheses_once=True)
            worker = self._worker(root, dependencies)
            store = ResearchControlStore(root)
            store.submit(
                ResearchCommand.start(
                    requested_by="research_owner",
                    requested_at=NOW,
                )
            )
            failed_result = worker.process_once()
            self.assertEqual(failed_result.stage.value, "failed")
            failed = store.load_state(str(failed_result.run_id))
            store.submit(
                ResearchCommand.build(
                    command_type=ResearchCommandType.RESUME,
                    target_run_id=failed.run_id,
                    requested_by="research_owner",
                    requested_at=NOW + timedelta(minutes=2),
                    body={"failed_state_sha256": failed.state_sha256},
                )
            )

            resumed = worker.process_once()

            self.assertEqual(resumed.stage.value, "awaiting_review")
            self.assertEqual(dependencies.calls["context"], 1)
            self.assertEqual(dependencies.calls["hypotheses"], 2)


if __name__ == "__main__":
    unittest.main()
