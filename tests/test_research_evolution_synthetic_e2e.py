"""Task 9 纯合成演化闭环验收。

本文件只使用脱敏覆盖图谱、固定响应和程序生成的状态对象；不读取行情，
不计算真实 IC、冗余或回测，也不把合成结果解释为 A 股研究结论。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from factor_miner.canonical import sha256_json
from factor_miner.cli import _draft_batch_sha256, app
from factor_miner.dashboard_projection import project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.design_diversity import preflight_designs
from factor_miner.llm_brief import GapSelectionPolicy
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_online import AgentRole, registered_llm_evolution_request_authorization
from factor_miner.llm_orchestrator import GenerationSealDependencies, seal_completed_generation
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_seal import RegisteredGenerationSeal
from factor_miner.llm_state import (
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    expected_candidate_slot_ids,
)
from factor_miner.research_evolution import (
    approve_evolution_hypotheses,
    build_evolution_hypothesis_request,
    evolution_gap_brief_sha256,
    parse_evolution_hypothesis_response,
)
from factor_miner.research_evolution_schema import (
    DataIdentitySummary,
    DesignDiversityPolicy,
    EvaluationSummary,
    EvolutionHypothesisApproval,
    ResearchMemoryEntry,
    ResearchMemorySnapshot,
    build_evolution_context,
    build_memory_entry_identity,
    build_memory_snapshot_identity,
)
from factor_miner.research_gap import (
    build_coverage_gap_report,
    build_sanitized_gap_brief,
    load_verified_coverage_graph,
)
from factor_miner.portfolio_artifacts import publish_run_artifacts
from factor_miner.research_memory import MemorySnapshotPolicy, ResearchMemoryStore, publish_campaign_memory
from factor_miner.schema import FactorNode
from dashboard.pg_store import PostgresDashboardStore
from tests.test_campaign_design_diversity import _registry
from tests.test_llm_orchestrator import NOW, policy
from tests.test_llm_schema import valid_family_spec
from tests.test_research_evolution import response_payload
from tests.test_research_gap import _memory_snapshot, _publish_graph


POLLUTED_FAMILY = "llmfamily_1bae19965638a6ac9620e0b0"
FAMILY_ID = "llmfamily_" + "9" * 24


def _context(report_hash: str, memory_hash: str, *, family_id: str = FAMILY_ID):
    return build_evolution_context(
        discovery_family_id=family_id,
        coverage_graph_manifest_hash="a" * 64,
        memory_snapshot_hash=memory_hash,
        gap_report_hash=report_hash,
        field_registry_hash="d" * 64,
        evaluation_policy_hash="e" * 64,
        design_policy_hash=sha256_json(DesignDiversityPolicy().model_dump(mode="json")),
        external_model_redaction_policy="evolution-gap-brief-v1",
    )


def _approvals(context, drafts):
    return tuple(
        EvolutionHypothesisApproval(
            logical_slot_id=draft.logical_slot_id,
            context_sha256=context.context_sha256,
            draft_sha256=draft.draft_sha256,
            discovery_family_id=context.discovery_family_id,
            approval_role="research_reviewer",
            approved_at=NOW,
            decision="approved",
        )
        for draft in drafts
    )


def _request_brief(report):
    """把已核验报告投影为演化请求入口所需的卡片合同。"""

    return {"gap_cards": [card.model_dump(mode="json") for card in report.gap_cards]}


def _synthetic_slot_objects(root: Path, family, context, batch):
    """登记四臂 120 个终态对象，其中一个设计失败、一个记录跨臂重复。"""

    ledger = LLMDiscoveryLedger(root)
    ledger.register_family(family)
    ledger.append_event(
        LLMDiscoveryEvent(
            event_id="event-generating-task9",
            discovery_family_id=family.discovery_family_id,
            target_kind=DiscoveryObjectKind.FAMILY,
            target_id=family.discovery_family_id,
            to_state=FamilyGenerationState.GENERATING,
            created_at=NOW,
        )
    )
    family_root = root / "state" / "llm_discovery_families" / family.discovery_family_id / "candidate_slots"
    objects = {}
    for index, slot_id in enumerate(expected_candidate_slot_ids(family.spec), start=1):
        status = CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
        object_slot_id = f"{slot_id.split(':', 1)[0]}:C{slot_id.split(':', 1)[1]}"
        payload = {"slot_id": object_slot_id, "status": status.value, "logical_hypothesis_id": f"H{((index - 1) % 30) // 3 + 1:02d}"}
        if index == 1:
            status = CandidateSlotState.DESIGN_DIVERSITY_FAILED
            payload.update({
                "status": status.value,
                "logical_hypothesis_id": "H01",
                "arm_id": slot_id.split(":", 1)[0],
                "candidate_design_slot_id": "C001",
                "context_sha256": context.context_sha256,
                "failure_code": "DESIGN_DIVERSITY_FAILED",
                "failure_stage": "design_diversity_preflight",
                "design_signatures": [{"canonical_ast_hash": "1" * 64}, {"canonical_ast_hash": "2" * 64}, {"canonical_ast_hash": "3" * 64}],
                "design_diversity_failures": [{"pair": ["C001", "C002"], "axis": "temporal_only"}],
            })
        if index == 3:
            payload["redundancy_code"] = "cross_arm_redundant"
        if index == 1:
            ledger.append_event(
                LLMDiscoveryEvent(
                    event_id="event-slot-task9-001-progress",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                    target_id=slot_id,
                    to_state=CandidateSlotState.GENERATION_IN_PROGRESS,
                    created_at=NOW,
                )
            )
            ledger.append_event(
                LLMDiscoveryEvent(
                    event_id="event-slot-task9-001-draft",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                    target_id=slot_id,
                    to_state=CandidateSlotState.DRAFT_GENERATED,
                    created_at=NOW,
                )
            )
        ledger.append_event(
            LLMDiscoveryEvent(
                event_id=f"event-slot-task9-{index:03d}",
                discovery_family_id=family.discovery_family_id,
                target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                target_id=slot_id,
                to_state=status,
                created_at=NOW,
            )
        )
        path = family_root / f"{object_slot_id.replace(':', '__')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        objects[slot_id] = payload
    return objects


class ResearchEvolutionSyntheticE2ETest(unittest.TestCase):
    """验证生成前冻结、120 槽封存和发布后的下一轮边界。"""

    def test_nine_approvals_stop_before_expression_or_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = load_verified_coverage_graph(_publish_graph(Path(directory), dense=True))
            source_memory = _memory_snapshot(graph.root)
            report = build_coverage_gap_report(graph, source_memory, policy=GapSelectionPolicy(max_cards=10))
            brief = build_sanitized_gap_brief(report, field_registry_hash="d" * 64, design_policy=DesignDiversityPolicy())
            context = _context(report.report_sha256, source_memory.snapshot_sha256)
            request_brief = _request_brief(report)
            authorization = registered_llm_evolution_request_authorization(
                discovery_family_id=context.discovery_family_id, context_sha256=context.context_sha256,
                gap_report_hash=report.report_sha256, memory_snapshot_hash=source_memory.snapshot_sha256,
                brief_sha256=evolution_gap_brief_sha256(request_brief), corporate_policy_id=policy().policy_id,
                approver_role="research_owner", allowed_agent_role=AgentRole.HYPOTHESIS,
                authorized_at=NOW, expires_at=NOW.replace(hour=23),
            )
            request = build_evolution_hypothesis_request(context, request_brief, authorization=authorization)
            drafts = parse_evolution_hypothesis_response(response_payload(), context=context)
            self.assertEqual(len(drafts), 10)
            self.assertNotIn("rank_ic", json.dumps(request.body, ensure_ascii=False))
            self.assertNotIn("/data/", json.dumps(request.body, ensure_ascii=False))
            self.assertNotIn("api_key", json.dumps(request.body, ensure_ascii=False))
            self.assertNotIn("模型原文", json.dumps(request.body, ensure_ascii=False))

            # 9/10 必须穿过正式 CLI；直接调用批准函数不能证明人工入口没有副作用。
            root = Path(directory)
            context_path = root / "context.json"
            draft_path = root / "draft-batch.json"
            decisions_path = root / "decisions.json"
            output_path = root / "approved.json"
            runtime_root = root / "runtime"
            context_path.write_text(json.dumps({
                "context": context.model_dump(mode="json"),
                "gap_report": report.model_dump(mode="json"),
            }), encoding="utf-8")
            draft_payload = {
                "request_sha256": "7" * 64,
                "context_sha256": context.context_sha256,
                "discovery_family_id": context.discovery_family_id,
                "report_sha256": report.report_sha256,
                "gap_card_sha256": {card.gap_id: card.card_sha256 for card in report.gap_cards},
                "drafts": [draft.model_dump(mode="json") for draft in drafts],
            }
            draft_payload["draft_batch_sha256"] = _draft_batch_sha256(draft_payload)
            draft_path.write_text(json.dumps(draft_payload), encoding="utf-8")
            all_decisions = [item.model_dump(mode="json") for item in _approvals(context, drafts)]
            decisions_path.write_text(json.dumps({
                "approval_role": "research_reviewer",
                "decisions": all_decisions[:-1],
            }), encoding="utf-8")
            before = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
            result = CliRunner().invoke(app, [
                "evolution", "approve-hypotheses",
                "--context", str(context_path), "--draft-batch", str(draft_path),
                "--decisions", str(decisions_path), "--output", str(output_path),
            ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertFalse(output_path.exists())
            after = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            for forbidden in (runtime_root, root / "expression", root / "backtest", root / "publication"):
                self.assertFalse(forbidden.exists(), f"不应创建批准后的产物路径：{forbidden}")

    def test_full_120_slot_strict_seal_memory_and_next_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = load_verified_coverage_graph(_publish_graph(root, dense=True))
            source_memory = _memory_snapshot(graph.root)
            report = build_coverage_gap_report(graph, source_memory, policy=GapSelectionPolicy(max_cards=10))
            brief = build_sanitized_gap_brief(report, field_registry_hash="d" * 64, design_policy=DesignDiversityPolicy())
            family = registered_llm_discovery_family(valid_family_spec())
            context = _context(report.report_sha256, source_memory.snapshot_sha256, family_id=family.discovery_family_id)
            drafts = parse_evolution_hypothesis_response(response_payload(), context=context)
            approvals = _approvals(context, drafts)
            batch = approve_evolution_hypotheses(drafts, approvals, context=context, expected_approval_role="research_reviewer")
            self.assertEqual(len(batch.approvals), 10)

            window_only = tuple(FactorNode(op="rolling_mean", args=(FactorNode(op="field", field="close"),), window=window, center=False) for window in (5, 10, 20))
            diversity = preflight_designs(window_only, policy=DesignDiversityPolicy(), registry=_registry())
            self.assertFalse(diversity.passed)
            self.assertEqual(diversity.failure_code, "DESIGN_DIVERSITY_FAILED")

            # seal 合同要求 family、graph、memory、approval 和全部槽对象同一条身份链。
            objects = _synthetic_slot_objects(root, family, context, batch)
            seal_input = {
                "approval_batch": batch, "evolution_context": context, "slot_objects": objects,
                "llm_campaign_spec_hashes": ("1" * 64, "2" * 64),
                "prompt_bundle_hashes": ("3" * 64, "4" * 64, "5" * 64),
                "model_identity": "synthetic-recorded-provider",
                "generator_identity_hashes": {arm.arm_id: sha256_json(arm.model_dump(mode="json")) for arm in family.spec.arm_specs},
                "evaluation_dependency_intervals": (),
            }
            with patch("factor_miner.llm_orchestrator.platform.system", return_value="Linux"):
                seal = seal_completed_generation(family.discovery_family_id, GenerationSealDependencies(root, family, NOW), seal_input)
            self.assertIsInstance(seal, RegisteredGenerationSeal)
            self.assertEqual(len(seal.manifest.slot_object_hashes), 120)

            # Task7 正式 API：先把同轮条目追加进 ResearchMemoryStore，再由真实
            # build_snapshot/verify 计算下一轮可见边界。这里仅发布合成 input manifest，
            # 不触发任何行情、IC 或回测。
            formal_run_id = "run_" + "c" * 24
            publish_run_artifacts(root, formal_run_id, {
                "run/input_manifest.json": json.dumps({
                    "family_id": family.discovery_family_id,
                    "generation_seal_id": seal.generation_seal_id,
                    "generation_manifest_sha256": seal.manifest_sha256,
                    "evaluation_policy_id": "synthetic-task9",
                    "data_contract_identity_hash": seal.manifest.data_contract_identity_hash,
                    "statistical_budget_hash": "7" * 64,
                    "published_at": NOW.isoformat(),
                }, sort_keys=True).encode(),
            })
            candidate_slot_id = next(iter(seal.manifest.slot_object_hashes))
            formal_entry = ResearchMemoryEntry(
                memory_entry_id="mementry_" + "c" * 24,
                entry_kind="evaluation_result",
                discovery_family_id=family.discovery_family_id,
                generation_seal_id=seal.generation_seal_id,
                run_id=formal_run_id,
                hypothesis_slot_id="H01",
                candidate_slot_id=candidate_slot_id,
                hypothesis_spec_hash="1" * 64,
                candidate_spec_hash="2" * 64,
                ast_hash="3" * 64,
                coverage_graph_id=graph.manifest.coverage_graph_id,
                memory_snapshot_id="memsnap_" + "c" * 24,
                field_signature=("close",), operator_signature=("rolling",),
                temporal_signature=("short",), structure_signature=("field_set",),
                gap_labels=("coverage",), terminal_state="completed",
                evaluation_summary=EvaluationSummary(
                    outcome_band="passed", rank_ic_band="high",
                    hac_significance_band="supported", portfolio_band="medium",
                    redundancy_band="low",
                ),
                data_identity_summary=DataIdentitySummary(
                    release_hash="4" * 64, manifest_hash="5" * 64,
                    field_registry_hash="6" * 64, cutoff_band="stable",
                ),
                created_at=NOW,
            )
            build_memory_entry_identity(formal_entry)
            memory_store = ResearchMemoryStore(root)
            memory_store.append_entry(formal_entry)
            same_round = memory_store.build_snapshot(
                cutoff=NOW, source_family_ids=(family.discovery_family_id,),
                coverage_graph_id=graph.manifest.coverage_graph_id,
                policy=MemorySnapshotPolicy(allowed_source_family_ids=(family.discovery_family_id,)),
                created_at=NOW,
            )
            self.assertEqual(same_round.entry_ids, (formal_entry.memory_entry_id,))
            memory_store.verify()
            next_round = memory_store.build_snapshot(
                cutoff=NOW - timedelta(seconds=1),
                source_family_ids=(family.discovery_family_id,),
                coverage_graph_id=graph.manifest.coverage_graph_id,
                policy=MemorySnapshotPolicy(allowed_source_family_ids=(family.discovery_family_id,)),
                created_at=NOW,
            )
            self.assertEqual(next_round.entry_count, 0)
            self.assertIn("cutoff:created_at_lte", next_round.exclusion_rules)
            self.assertTrue(set(next_round.entry_ids).isdisjoint(same_round.entry_ids))

            slots = []
            for index, slot_id in enumerate(expected_candidate_slot_ids(family.spec), start=1):
                slots.append({"slot_id": slot_id, "status": "not_executed", "failure_reason": "DESIGN_DIVERSITY_FAILED" if index == 1 else ("cross_arm_redundant" if index == 3 else None), "candidate_spec_hash": sha256_json({"slot": slot_id}), "ast_hash": sha256_json({"ast": slot_id})})
            dashboard_root = root
            dashboard_run_id = "run_" + "a" * 24
            publish_run_artifacts(dashboard_root, dashboard_run_id, {
                "run/input_manifest.json": json.dumps({
                    "family_id": family.discovery_family_id,
                    "generation_seal_id": seal.generation_seal_id,
                    "generation_manifest_sha256": seal.manifest_sha256,
                    "evaluation_policy_id": "synthetic-task9",
                    "data_contract_identity_hash": seal.manifest.data_contract_identity_hash,
                    "statistical_budget_hash": "7" * 64,
                    "published_at": NOW.isoformat(),
                }, sort_keys=True).encode(),
            })
            published = publish_campaign_memory(dashboard_root, run_id=dashboard_run_id, family_id=family.discovery_family_id, generation_seal_id=seal.generation_seal_id, source_manifest_sha256="6" * 64, data_contract_identity_hash=seal.manifest.data_contract_identity_hash, slot_evaluations=slots, published_at=NOW, evolution_metadata={"context_id": f"evolutionctx_{context.context_sha256[:24]}", "context_sha256": context.context_sha256, "approval_batch_hash": batch.approval_batch_sha256, "approval_count": 10, "coverage_graph_id": graph.manifest.coverage_graph_id, "coverage_graph_manifest_hash": context.coverage_graph_manifest_hash, "memory_snapshot_hash": context.memory_snapshot_hash, "gap_report_hash": report.report_sha256, "design_policy_hash": context.design_policy_hash, "hypothesis_count": 10, "slot_count": 120, "gap_summary": {"category_counts": {"structural": 1}, "sanitized_labels": ["coverage"], "truncated_count": 0}})
            self.assertEqual(published["entry_count"], 120)
            memory_store = ResearchMemoryStore(dashboard_root)
            memory_store.verify()
            memory_snapshot = memory_store.load_snapshot(published["memory_snapshot_id"])
            self.assertEqual(len(memory_snapshot.entry_ids), 120)
            self.assertIn("DESIGN_DIVERSITY_FAILED", json.dumps(slots))
            self.assertIn("cross_arm_redundant", json.dumps(slots))
            self.assertIn("not_executed", json.dumps(slots))

            dashboard = InMemoryDashboardStore()
            projected = project_run_artifacts(dashboard_root, dashboard_run_id, dashboard)
            loaded_entries = ResearchMemoryStore(dashboard_root).load_snapshot(published["memory_snapshot_id"])
            self.assertEqual(len(loaded_entries.entry_ids), 120)
            dashboard_payload = dashboard.load_run_snapshot(dashboard_run_id)
            self.assertIsNotNone(dashboard_payload)
            self.assertEqual(dashboard_payload["memory_snapshot"]["entry_count"], 120)
            self.assertEqual(dashboard_payload["evolution_metadata"]["approval_count"], 10)
            self.assertEqual(dashboard_payload["evolution_metadata"]["slot_count"], 120)
            serialized_dashboard = json.dumps(dashboard_payload, ensure_ascii=False)
            for forbidden in ("模型原文", "api_key", "/data/", "000001", "raw_model_response"):
                self.assertNotIn(forbidden, serialized_dashboard)

            class FakeConnection:
                def __init__(self):
                    self.sql = []
                    self.params = []

                def execute(self, statement, params=()):
                    self.sql.append(statement)
                    self.params.append(params)
                    return type("Cursor", (), {"fetchone": lambda self: None})()

            connection = FakeConnection()
            PostgresDashboardStore._project_evolution_memory(connection, dashboard_run_id, projected.model_dump(mode="json"))
            joined_sql = "\n".join(connection.sql)
            for table in ("research_evolution_batches", "research_gap_summaries", "research_memory_snapshots", "research_memory_entries"):
                self.assertIn(f"INSERT INTO {table}", joined_sql)
            projection_payload = json.dumps(connection.params, ensure_ascii=False)
            self.assertIn(context.context_sha256, projection_payload)
            self.assertIn(report.report_sha256, projection_payload)
            self.assertIn('"approval_count": 10', serialized_dashboard)
            self.assertIn("120", projection_payload)
            for forbidden in ("模型原文", "api_key", "/data/", "000001", "raw_model_response"):
                self.assertNotIn(forbidden, projection_payload)
            self.assertNotIn(POLLUTED_FAMILY, serialized_dashboard)
            polluted_before = {
                path.relative_to(dashboard_root)
                for path in dashboard_root.rglob("*")
                if path.is_file()
            }
            with self.assertRaises(Exception):
                publish_campaign_memory(dashboard_root, run_id="run_" + "b" * 24, family_id=POLLUTED_FAMILY, generation_seal_id=seal.generation_seal_id, source_manifest_sha256="6" * 64, data_contract_identity_hash=seal.manifest.data_contract_identity_hash, slot_evaluations=(), published_at=NOW)
            polluted_after = {
                path.relative_to(dashboard_root)
                for path in dashboard_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(polluted_after, polluted_before)


if __name__ == "__main__":
    unittest.main()
