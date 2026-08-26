"""批准假设后的三候选表达式编排测试。"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.llm_online import (
    DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    AgentRole,
    DEEPSEEK_MODEL,
    LLMExportAuthorization,
    registered_llm_campaign_scope_authorization,
)
from factor_miner.errors import FactorMinerError
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.llm_orchestrator import (
    ApprovedBatchDependencies,
    GenerationSealDependencies,
    prepare_approved_batch_export,
    run_approved_batch,
    seal_completed_generation,
)
from factor_miner.llm_privacy import registered_corporate_external_research_policy
from factor_miner.llm_hypothesis import (
    compose_testable_prediction,
    registered_coverage_gap_hypothesis,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_state import CandidateSlotState
from factor_miner.llm_state import (
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    expected_candidate_slot_ids,
)
from tests.test_llm_candidate import hypothesis
from tests.test_llm_schema import valid_family_spec
from tests.test_llm_seal import seal_inputs
from factor_miner.canonical import sha256_json
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    EvolutionHypothesisApproval,
    build_evolution_context,
)


NOW = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)


def policy():
    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=("public_capability_only",),
        forbidden_information_classes=("raw_market_data",),
        allowed_models=(DEEPSEEK_MODEL,),
        maximum_authorized_campaigns=2,
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        approver_role="research_owner",
        approval_reference="orchestrator-synthetic-policy",
    )


def approved_for(family_id: str):
    base = hypothesis()
    return registered_coverage_gap_hypothesis(
        base.draft,
        compose_testable_prediction(
            base.draft.prediction_proposal,
            company_a_share_visible_policy(),
            discovery_family_id=family_id,
        ),
        base.decision,
    )


def dependencies(root: Path) -> ApprovedBatchDependencies:
    family = registered_llm_discovery_family(valid_family_spec())
    approved = approved_for(family.discovery_family_id)
    payload = {
        "information_class": "public_capability_only",
        "public_question": approved.draft.claim,
        "allowed_field_aliases": ["price_close"],
        "allowed_operators": ["rolling", "temporal"],
    }
    return ApprovedBatchDependencies(
        artifact_root=root,
        family=family,
        policy=policy(),
        public_payload_by_hypothesis_id={approved.hypothesis_id: payload},
        authorization_by_request_hash={},
        now=NOW,
    )


def price_registry() -> FieldAvailabilityRegistry:
    """构造仅含合成收盘价的字段注册表。"""

    return FieldAvailabilityRegistry(
        registry_id="field-registry-orchestrator-v1",
        data_release_id="release-orchestrator-v1",
        fields=(
            FieldAvailabilityEntry(
                field_id="close",
                public_alias="price_close",
                economic_type="market_price",
                unit_dimension="price",
                panel_shape="asset_date_scalar",
                event_time="close_t",
                source_publish_time="close_t",
                vendor_available_time="after_close_t",
                revision_policy="immutable_daily",
                point_in_time_guarantee=True,
                earliest_decision_time="after_close_t",
                eligible_for_factor=True,
            ),
        ),
    )


def strict_seal_inputs(family_id: str, slot_objects: dict[str, dict[str, object]]) -> dict[str, object]:
    """构造绑定十个逻辑假设和全部槽对象的 strict seal 输入。"""

    context = build_evolution_context(
        discovery_family_id=family_id,
        coverage_graph_manifest_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        gap_report_hash="c" * 64,
        field_registry_hash="d" * 64,
        evaluation_policy_hash="e" * 64,
        design_policy_hash="f" * 64,
        external_model_redaction_policy="sanitized-bands-v1",
    )
    approvals = tuple(
        EvolutionHypothesisApproval(
            logical_slot_id=f"H{index:02d}",
            context_sha256=context.context_sha256,
            draft_sha256="1" * 64,
            discovery_family_id=family_id,
            approval_role="research_owner",
            approved_at=NOW,
            decision="approved",
        )
        for index in range(1, 11)
    )
    batch = ApprovedEvolutionHypothesisBatch(
        context_sha256=context.context_sha256,
        discovery_family_id=family_id,
        approvals=approvals,
    )
    return {
        "approval_batch": batch,
        "evolution_context": context,
        "slot_objects": slot_objects,
    }


def terminal_generation(root: Path):
    """创建 120 个合成终态槽对象，完全不读取行情。"""

    family = registered_llm_discovery_family(valid_family_spec())
    ledger = LLMDiscoveryLedger(root)
    ledger.register_family(family)
    ledger.append_event(
        LLMDiscoveryEvent(
            event_id="event-generating",
            discovery_family_id=family.discovery_family_id,
            target_kind=DiscoveryObjectKind.FAMILY,
            target_id=family.discovery_family_id,
            to_state=FamilyGenerationState.GENERATING,
            created_at=NOW,
        )
    )
    family_root = (
        root
        / "state"
        / "llm_discovery_families"
        / family.discovery_family_id
        / "candidate_slots"
    )
    slot_objects: dict[str, dict[str, object]] = {}
    for index, slot_id in enumerate(expected_candidate_slot_ids(family.spec), start=1):
        object_slot_id = f"{slot_id.split(':', 1)[0]}:C{slot_id.split(':', 1)[1]}"
        ledger.append_event(
            LLMDiscoveryEvent(
                event_id=f"event-slot-{index:03d}",
                discovery_family_id=family.discovery_family_id,
                target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                target_id=slot_id,
                to_state=CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
                created_at=NOW,
            )
        )
        payload = {
            "slot_id": object_slot_id,
            "status": CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL.value,
            "index": index,
        }
        slot_objects[slot_id] = payload
        path = family_root / f"{object_slot_id.replace(':', '__')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return family, slot_objects


def authorization_for(request, policy):
    """为合成测试中的精确请求生成限时授权。"""

    return LLMExportAuthorization(
        authorization_id="authorization_" + request.request_sha256[:24],
        campaign_id=request.campaign_id,
        request_sha256=request.request_sha256,
        corporate_policy_id=policy.policy_id,
        approver_role=policy.approver_role,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
    )


class RecordedBatchTransport:
    """按请求角色返回固定合成 JSON 的假 DeepSeek 传输。"""

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        self.last_payload = json.loads(request_bytes)
        user_payload = json.loads(self.last_payload["messages"][1]["content"])
        if "candidate_expression_batch" in user_payload:
            decisions = [
                {
                    "candidate_slot_id": item["candidate_slot_id"],
                    "decision": "approved",
                    "proxy_alignment": True,
                    "direction_alignment": True,
                    "availability_alignment": True,
                    "undeclared_exposure": False,
                    "reason_codes": [],
                    "summary": "合成 semantic lint 通过。",
                }
                for item in user_payload["candidate_expression_batch"]["candidates"]
            ]
            content = {"decisions": decisions}
        else:
            content = {
                "candidates": [
                    {
                        "candidate_slot_id": "coverage_outcome_llm:C001",
                        "expression": {"op": "field", "field": "price_close"},
                    },
                    {
                        "candidate_slot_id": "coverage_outcome_llm:C002",
                        "expression": {
                            "op": "delta",
                            "args": [
                                {"op": "field", "field": "price_close"}
                            ],
                            "period": 20,
                        },
                    },
                    {
                        "candidate_slot_id": "coverage_outcome_llm:C003",
                        "expression": {
                            "op": "neg",
                            "args": [
                                {"op": "field", "field": "price_close"}
                            ],
                        },
                    },
                ]
            }
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        ).encode()


class InvalidExpressionTransport:
    """返回 Schema 不合法的表达式批次，用于验证失败终态。"""

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        """返回缺少候选的 JSON object。"""

        del request_bytes, api_key
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": '{"candidates": []}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        ).encode()


class LLMOrchestratorTest(unittest.TestCase):
    """编排器必须固定 3 个槽并要求精确授权。"""

    def test_prepare_allocates_three_candidate_slots_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deps = dependencies(root)
            result = prepare_approved_batch_export(
                deps.family.discovery_family_id,
                (approved_for(deps.family.discovery_family_id),),
                deps,
            )
            self.assertEqual(result.candidate_slot_ids, (
                "coverage_outcome_llm:C001",
                "coverage_outcome_llm:C002",
                "coverage_outcome_llm:C003",
            ))
            self.assertEqual(len(result.request_hashes), 1)
            self.assertTrue(
                (root / "state" / "llm_approved_batches" / deps.family.discovery_family_id / "export_authorization_bundle.json").is_file()
            )

    def test_execution_requires_exact_request_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deps = dependencies(root)
            with patch("factor_miner.llm_orchestrator.platform.system", return_value="Linux"):
                approved = approved_for(deps.family.discovery_family_id)
                with self.assertRaisesRegex(Exception, "LLM_EXPORT_NOT_AUTHORIZED"):
                    run_approved_batch(deps.family.discovery_family_id, (approved,), deps)

    def test_expression_lint_registration_and_resume_are_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family = registered_llm_discovery_family(valid_family_spec())
            approved = approved_for(family.discovery_family_id)
            base = dependencies(root)
            transport = RecordedBatchTransport()
            expression_request = prepare_approved_batch_export(
                family.discovery_family_id,
                (approved,),
                base,
            )
            auth_map = {
                expression_request.request_hashes[0]: authorization_for(
                    PreparedRequestProxy(
                        request_sha256=expression_request.request_hashes[0],
                        campaign_id=(
                            f"{family.discovery_family_id}:coverage_outcome_llm"
                        ),
                    ),
                    base.policy,
                )
            }
            deps = ApprovedBatchDependencies(
                artifact_root=root,
                family=family,
                policy=base.policy,
                public_payload_by_hypothesis_id=base.public_payload_by_hypothesis_id,
                authorization_by_request_hash=auth_map,
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
                self.assertEqual(first.status, "awaiting_semantic_lint_authorization")
                self.assertEqual(len(first.pending_request_hashes), 1)
                auth_map[first.pending_request_hashes[0]] = authorization_for(
                    PreparedRequestProxy(
                        request_sha256=first.pending_request_hashes[0],
                        campaign_id=(
                            f"{family.discovery_family_id}:coverage_outcome_llm"
                        ),
                    ),
                    base.policy,
                )
                second = run_approved_batch(
                    family.discovery_family_id,
                    (approved,),
                    deps,
                )
            self.assertEqual(second.status, "completed")
            self.assertEqual(len(second.registered_candidate_ids), 3)
            projected = LLMDiscoveryLedger(root).project_state(
                family.discovery_family_id
            )
            self.assertEqual(
                projected.candidate_slot_states["coverage_outcome_llm:001"],
                CandidateSlotState.READY_FOR_REGISTRATION,
            )
            self.assertEqual(
                len(tuple((root / "state" / "candidates").glob("cand_*.json"))),
                3,
            )
            for request_path in (
                root
                / "state"
                / "llm_approved_batches"
                / family.discovery_family_id
            ).rglob("request.json"):
                self.assertNotIn("outcome", request_path.read_text())

    def test_scope_authorization_runs_expression_and_lint_without_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family = registered_llm_discovery_family(valid_family_spec())
            approved = approved_for(family.discovery_family_id)
            base = dependencies(root)
            scope = registered_llm_campaign_scope_authorization(
                family_id=family.discovery_family_id,
                corporate_policy_id=base.policy.policy_id,
                approver_role=base.policy.approver_role,
                allowed_agent_roles=(
                    AgentRole.EXPRESSION,
                    AgentRole.SEMANTIC_LINT,
                ),
                allowed_information_classes=("public_capability_only",),
                max_hypotheses=10,
                max_requests=4,
                authorized_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=30),
            )
            transport = RecordedBatchTransport()
            deps = ApprovedBatchDependencies(
                artifact_root=root,
                family=family,
                policy=base.policy,
                public_payload_by_hypothesis_id=base.public_payload_by_hypothesis_id,
                authorization_by_request_hash={},
                scope_authorization=scope,
                field_registry=price_registry(),
                transport=transport,
                now=NOW,
            )
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic"}), patch(
                "factor_miner.llm_orchestrator.platform.system",
                return_value="Linux",
            ):
                result = run_approved_batch(
                    family.discovery_family_id,
                    (approved,),
                    deps,
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(result.registered_candidate_ids), 3)
            call_root = (
                root
                / "artifacts"
                / "llm_batches"
                / result.bundle_id
                / "calls"
            )
            self.assertEqual(len(tuple(call_root.iterdir())), 2)
            self.assertEqual(result.pending_request_hashes, ())

    def test_invalid_expression_schema_becomes_generation_failed_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family = registered_llm_discovery_family(valid_family_spec())
            approved = approved_for(family.discovery_family_id)
            base = dependencies(root)
            scope = registered_llm_campaign_scope_authorization(
                family_id=family.discovery_family_id,
                corporate_policy_id=base.policy.policy_id,
                approver_role=base.policy.approver_role,
                allowed_agent_roles=(
                    AgentRole.EXPRESSION,
                    AgentRole.SEMANTIC_LINT,
                ),
                allowed_information_classes=("public_capability_only",),
                max_hypotheses=10,
                max_requests=4,
                authorized_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=30),
            )
            deps = ApprovedBatchDependencies(
                artifact_root=root,
                family=family,
                policy=base.policy,
                public_payload_by_hypothesis_id=base.public_payload_by_hypothesis_id,
                authorization_by_request_hash={},
                scope_authorization=scope,
                field_registry=price_registry(),
                transport=InvalidExpressionTransport(),
                now=NOW,
            )
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic"}), patch(
                "factor_miner.llm_orchestrator.platform.system",
                return_value="Linux",
            ):
                result = run_approved_batch(
                    family.discovery_family_id,
                    (approved,),
                    deps,
                )

            self.assertEqual(result.status, "partial")
            self.assertEqual(len(result.failed_candidate_slot_ids), 3)
            projected = LLMDiscoveryLedger(root).project_state(
                family.discovery_family_id
            )
            for slot_id in (
                "coverage_outcome_llm:001",
                "coverage_outcome_llm:002",
                "coverage_outcome_llm:003",
            ):
                self.assertEqual(
                    projected.candidate_slot_states[slot_id],
                    CandidateSlotState.GENERATION_FAILED,
                )

    def test_server_seal_recomputes_slot_hashes_from_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family = registered_llm_discovery_family(valid_family_spec())
            ledger = LLMDiscoveryLedger(root)
            ledger.register_family(family)
            ledger.append_event(
                LLMDiscoveryEvent(
                    event_id="event-generating",
                    discovery_family_id=family.discovery_family_id,
                    target_kind=DiscoveryObjectKind.FAMILY,
                    target_id=family.discovery_family_id,
                    to_state=FamilyGenerationState.GENERATING,
                    created_at=NOW,
                )
            )
            family_root = (
                root
                / "state"
                / "llm_discovery_families"
                / family.discovery_family_id
                / "candidate_slots"
            )
            for index, slot_id in enumerate(
                expected_candidate_slot_ids(family.spec),
                start=1,
            ):
                object_slot_id = f"{slot_id.split(':', 1)[0]}:C{slot_id.split(':', 1)[1]}"
                ledger.append_event(
                    LLMDiscoveryEvent(
                        event_id=f"event-slot-{index:03d}",
                        discovery_family_id=family.discovery_family_id,
                        target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
                        target_id=slot_id,
                        to_state=CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
                        created_at=NOW,
                    )
                )
                path = family_root / f"{object_slot_id.replace(':', '__')}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "slot_id": object_slot_id,
                            "status": CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL.value,
                            "index": index,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            with patch(
                "factor_miner.llm_orchestrator.platform.system",
                return_value="Linux",
            ):
                seal = seal_completed_generation(
                    family.discovery_family_id,
                    GenerationSealDependencies(
                        artifact_root=root,
                        family=family,
                        now=NOW,
                    ),
                    {
                        **seal_inputs(),
                        "evaluation_dependency_intervals": [],
                    },
                )
            self.assertEqual(seal.manifest.slot_object_hashes.__len__(), 120)
            self.assertEqual(
                LLMDiscoveryLedger(root).project_state(
                    family.discovery_family_id
                ).family_generation_state,
                FamilyGenerationState.GENERATION_SEALED,
            )

    def test_strict_seal_build_replay_binds_all_evolution_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family, slot_objects = terminal_generation(root)
            strict = strict_seal_inputs(family.discovery_family_id, slot_objects)
            seal_input = {
                **seal_inputs(),
                **strict,
                "evaluation_dependency_intervals": [],
            }
            with patch(
                "factor_miner.llm_orchestrator.platform.system",
                return_value="Linux",
            ):
                tampered_context = strict["evolution_context"].model_copy(
                    update={"design_policy_hash": "0" * 64}
                )
                with self.assertRaises(FactorMinerError):
                    seal_completed_generation(
                        family.discovery_family_id,
                        GenerationSealDependencies(
                            artifact_root=root,
                            family=family,
                            now=NOW,
                        ),
                        {
                            **seal_input,
                            "evolution_context": tampered_context,
                        },
                    )
                seal = seal_completed_generation(
                    family.discovery_family_id,
                    GenerationSealDependencies(
                        artifact_root=root,
                        family=family,
                        now=NOW,
                    ),
                    seal_input,
                )

                manifest = seal.manifest
                self.assertEqual(manifest.evolution_context_hash, strict["evolution_context"].context_sha256)
                self.assertEqual(
                    manifest.coverage_graph_manifest_hash,
                    strict["evolution_context"].coverage_graph_manifest_hash,
                )
                self.assertEqual(
                    manifest.memory_snapshot_hash,
                    strict["evolution_context"].memory_snapshot_hash,
                )
                self.assertEqual(
                    manifest.gap_report_hash,
                    strict["evolution_context"].gap_report_hash,
                )
                self.assertEqual(
                    manifest.approval_batch_hash,
                    strict["approval_batch"].approval_batch_sha256,
                )
                self.assertEqual(
                    manifest.design_policy_hash,
                    strict["evolution_context"].design_policy_hash,
                )
                self.assertEqual(
                    manifest.logical_hypothesis_ids_hash,
                    sha256_json({"logical_hypothesis_ids": tuple(f"H{i:02d}" for i in range(1, 11))}),
                )
                self.assertTrue(
                    all(
                        value
                        for value in (
                            manifest.evolution_context_hash,
                            manifest.coverage_graph_manifest_hash,
                            manifest.memory_snapshot_hash,
                            manifest.gap_report_hash,
                            manifest.approval_batch_hash,
                            manifest.design_policy_hash,
                            manifest.logical_hypothesis_ids_hash,
                        )
                    )
                )

                for field in (
                    "coverage_graph_manifest_hash",
                    "memory_snapshot_hash",
                    "gap_report_hash",
                ):
                    tampered_context = strict["evolution_context"].model_copy(
                        update={field: "0" * 64}
                    )
                    with self.assertRaises(FactorMinerError):
                        seal_completed_generation(
                            family.discovery_family_id,
                            GenerationSealDependencies(
                                artifact_root=root,
                                family=family,
                                now=NOW,
                            ),
                            {
                                **seal_input,
                                "evolution_context": tampered_context,
                            },
                        )


class PreparedRequestProxy:
    """只为合成授权测试提供模型请求的身份字段。"""

    def __init__(self, *, request_sha256: str, campaign_id: str) -> None:
        self.request_sha256 = request_sha256
        self.campaign_id = campaign_id


if __name__ == "__main__":
    unittest.main()
