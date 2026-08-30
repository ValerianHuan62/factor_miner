"""人工批准假设后的固定三候选表达式编排。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.design_diversity import preflight_designs
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityRegistry,
    resolve_public_field_aliases,
)
from factor_miner.ledger import JsonlLedger, _atomic_write_immutable
from factor_miner.llm_agents import (
    build_expression_agent_request,
    build_semantic_lint_agent_request,
)
from factor_miner.llm_candidate import (
    CandidateExpressionBatch,
    CandidateExpressionDraft,
    SemanticLintBatch,
    SemanticLintDecision,
    convert_candidate,
    validate_candidate_expression,
)
from factor_miner.llm_hypothesis import RegisteredCoverageGapHypothesis
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_online import (
    LLMCampaignScopeAuthorization,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
)
from factor_miner.llm_privacy import (
    CorporateExternalResearchPolicy,
    scan_export_payload,
)
from factor_miner.llm_provider import (
    DeepSeekTransport,
    UrllibDeepSeekTransport,
    execute_recorded_call,
)
from factor_miner.llm_schema import RegisteredLLMDiscoveryResearchFamily
from factor_miner.llm_schema import EvaluationDependencyInterval
from factor_miner.llm_seal import (
    RegisteredGenerationSeal,
    build_generation_seal,
    verify_generation_seal,
)
from factor_miner.llm_state import (
    CANDIDATE_TERMINALS,
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    expected_candidate_slot_ids,
    expected_logical_hypothesis_slot_ids,
    logical_candidate_design_slot_id,
    logical_hypothesis_slot_id,
)
from factor_miner.research_evolution import require_approved_evolution_batch
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    DesignDiversityPolicy,
    ResearchEvolutionContext,
)
from factor_miner.schema import (
    RegisteredTrustedCandidate,
    registered_trusted_candidate,
)


class ExportAuthorizationBundle(BaseModel):
    """一个批准批次的精确表达式请求哈希集合。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: str = Field(pattern=r"^llmbundle_[0-9a-f]{24}$")
    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    candidate_slot_ids: tuple[str, ...] = Field(min_length=3)
    request_hashes: tuple[str, ...] = Field(min_length=1)
    policy_id: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedBatchResult(BaseModel):
    """批准批次的非正文运行摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: str
    family_id: str
    status: str
    request_hashes: tuple[str, ...] = Field(min_length=1)
    recorded_call_ids: tuple[str, ...]
    ready_candidate_slot_ids: tuple[str, ...]
    failed_candidate_slot_ids: tuple[str, ...]
    registered_candidate_ids: tuple[str, ...] = ()
    pending_request_hashes: tuple[str, ...] = ()
    generation_seal_id: str | None = None
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ApprovedBatchDependencies:
    """编排器依赖；所有路径和 payload 都应由服务器固定程序提供。"""

    artifact_root: Path
    family: RegisteredLLMDiscoveryResearchFamily
    policy: CorporateExternalResearchPolicy
    public_payload_by_hypothesis_id: Mapping[str, dict[str, object]]
    authorization_by_request_hash: Mapping[str, LLMExportAuthorization]
    scope_authorization: LLMCampaignScopeAuthorization | None = None
    field_registry: FieldAvailabilityRegistry | None = None
    transport: DeepSeekTransport | None = None
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class GenerationSealDependencies:
    """generation seal 只需要服务器产物根、family 和固定时间。"""

    artifact_root: Path
    family: RegisteredLLMDiscoveryResearchFamily
    now: datetime | None = None


class GenerationSlotFinalizationSummary(BaseModel):
    """补齐未执行槽位后的不可变摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str
    terminal_slot_ids: tuple[str, ...]
    newly_finalized_slot_ids: tuple[str, ...]
    remaining_non_terminal_slot_ids: tuple[str, ...]
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _orchestrator_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造不含请求正文的编排错误。"""

    return FactorMinerError(code, message)


def _candidate_slots(hypothesis: RegisteredCoverageGapHypothesis) -> tuple[str, ...]:
    """将 H01 映射为该 arm 固定的 C001~C003。"""

    arm_id, raw_number = hypothesis.draft.slot_id.split(":", 1)
    number = int(raw_number[1:])
    first = (number - 1) * 3 + 1
    return tuple(f"{arm_id}:C{index:03d}" for index in range(first, first + 3))


def _validated_evolution_batch(
    family_id: str,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
) -> ApprovedEvolutionHypothesisBatch | None:
    """核验严格演化路径的 family、十槽预算和上下文绑定。"""

    if approval_batch is None and evolution_context is None:
        return None
    if approval_batch is None or evolution_context is None:
        raise _orchestrator_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "严格演化路径必须同时提供 approval_batch 与 evolution_context",
        )
    if evolution_context.discovery_family_id != family_id:
        raise _orchestrator_error(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "演化上下文 family 与当前研究族不一致",
        )
    validated = require_approved_evolution_batch(
        approval_batch,
        context_sha256=evolution_context.context_sha256,
        discovery_family_id=family_id,
    )
    if tuple(item.logical_slot_id for item in validated.approvals) != expected_logical_hypothesis_slot_ids():
        raise _orchestrator_error(
            FailureCode.APPROVAL_COUNT_INVALID,
            "严格演化批准批次必须按固定顺序覆盖 H01-H10",
        )
    for name in (
        "coverage_graph_manifest_hash",
        "memory_snapshot_hash",
        "gap_report_hash",
        "design_policy_hash",
        "context_sha256",
    ):
        value = getattr(evolution_context, name)
        if not isinstance(value, str) or len(value) != 64:
            raise _orchestrator_error(
                FailureCode.EVOLUTION_CONTEXT_INVALID,
                f"严格演化上下文缺少有效 {name}",
            )
    return validated


def _strict_seal_inputs(
    family_id: str,
    seal_input: Mapping[str, object],
) -> tuple[
    ApprovedEvolutionHypothesisBatch | None,
    ResearchEvolutionContext | None,
    Mapping[str, Mapping[str, object]] | None,
]:
    """从 seal 输入中解析可选 strict evolution 绑定。"""

    approval_payload = seal_input.get("approval_batch")
    context_payload = seal_input.get("evolution_context")
    slot_objects_payload = seal_input.get("slot_objects")
    if (
        approval_payload is None
        and context_payload is None
        and slot_objects_payload is None
    ):
        return None, None, None
    if (
        approval_payload is None
        or context_payload is None
        or slot_objects_payload is None
    ):
        raise _orchestrator_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "strict evolution seal 必须同时提供 approval_batch、evolution_context 与 slot_objects",
        )
    try:
        approval_value = (
            approval_payload.model_dump(mode="json")
            if isinstance(approval_payload, ApprovedEvolutionHypothesisBatch)
            else approval_payload
        )
        context_value = (
            context_payload.model_dump(mode="json")
            if isinstance(context_payload, ResearchEvolutionContext)
            else context_payload
        )
        approval_batch = (
            ApprovedEvolutionHypothesisBatch.model_validate(approval_value)
        )
        evolution_context = (
            ResearchEvolutionContext.model_validate(context_value)
        )
    except (TypeError, ValueError) as error:
        raise _orchestrator_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            f"strict evolution seal 输入无效：{error}",
        ) from error
    if not isinstance(slot_objects_payload, Mapping):
        raise _orchestrator_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "strict evolution seal 的 slot_objects 必须是对象映射",
        )
    slot_objects: dict[str, Mapping[str, object]] = {}
    for slot_id, payload in slot_objects_payload.items():
        if not isinstance(slot_id, str) or not isinstance(payload, Mapping):
            raise _orchestrator_error(
                FailureCode.EVOLUTION_CONTEXT_INVALID,
                "strict evolution seal 的 slot_objects 条目无效",
            )
        try:
            state_slot_id = _state_slot_id(slot_id)
            object_slot_id = _object_slot_id(state_slot_id)
            normalized_payload = json.loads(canonical_json_bytes(dict(payload)))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _orchestrator_error(
                FailureCode.EVOLUTION_CONTEXT_INVALID,
                f"strict evolution seal 的槽对象不可重新验证：{slot_id}",
            ) from error
        if (
            not isinstance(normalized_payload, dict)
            or normalized_payload.get("slot_id") != object_slot_id
            or not isinstance(normalized_payload.get("status"), str)
            or not normalized_payload["status"].strip()
        ):
            raise _orchestrator_error(
                FailureCode.EVOLUTION_HASH_MISMATCH,
                f"strict evolution seal 的槽对象身份不一致：{slot_id}",
            )
        for key, value in normalized_payload.items():
            if key.endswith("_hash") and (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise _orchestrator_error(
                    FailureCode.EVOLUTION_HASH_MISMATCH,
                    f"strict evolution seal 的槽对象 hash 无效：{slot_id}.{key}",
                )
        slot_objects[state_slot_id] = normalized_payload
    return (
        _validated_evolution_batch(family_id, approval_batch, evolution_context),
        evolution_context,
        slot_objects,
    )


def _evolution_slot_binding(
    slot_id: str,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
) -> dict[str, object]:
    """为严格演化路径补全槽位绑定元数据。"""

    if approval_batch is None or evolution_context is None:
        return {}
    state_slot_id = _state_slot_id(slot_id)
    arm_id = state_slot_id.split(":", 1)[0]
    return {
        "arm_id": arm_id,
        "logical_hypothesis_id": logical_hypothesis_slot_id(state_slot_id),
        "candidate_design_slot_id": logical_candidate_design_slot_id(
            state_slot_id
        ),
        "context_sha256": evolution_context.context_sha256,
        "approval_batch_sha256": approval_batch.approval_batch_sha256,
    }


def _bound_slot_payload(
    slot_id: str,
    payload: dict[str, Any],
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
) -> dict[str, Any]:
    """给 strict evolution 路径的槽对象补齐上下文绑定。"""

    return {
        **payload,
        **_evolution_slot_binding(
            slot_id,
            approval_batch=approval_batch,
            evolution_context=evolution_context,
        ),
    }


def _validate_approved_hypotheses(
    family_id: str,
    hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
) -> None:
    """限制批准对象、每条 LLM arm 的十假设预算和唯一身份。"""

    if not hypotheses or len(hypotheses) > 20:
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "批准批次必须包含 1 至 20 个 LLM 假设",
        )
    if any(item.decision.decision != "approved" for item in hypotheses):
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "只有人工批准假设可以进入表达式编排",
        )
    if len({item.hypothesis_id for item in hypotheses}) != len(hypotheses):
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "批准假设不能重复",
        )
    if any(item.testable_prediction.multiplicity_family_id != family_id for item in hypotheses):
        raise _orchestrator_error(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "假设 multiplicity family 与编排 family 不一致",
        )
    arm_counts: dict[str, int] = {}
    for hypothesis in hypotheses:
        arm_id = hypothesis.draft.slot_id.split(":", 1)[0]
        if arm_id not in {"coverage_outcome_llm", "literature_only_llm"}:
            raise _orchestrator_error(
                FailureCode.LLM_STATE_TRANSITION_INVALID,
                "只有两个 LLM arm 可以自动生成表达式",
            )
        arm_counts[arm_id] = arm_counts.get(arm_id, 0) + 1
    if any(count > 10 for count in arm_counts.values()):
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "单条 LLM arm 最多批准 10 个假设",
        )


def _validate_scope_authorization_for_batch(
    family_id: str,
    hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    dependencies: ApprovedBatchDependencies,
    now: datetime,
) -> None:
    """核验个人试运行范围能覆盖当前批准批次。"""

    scope = dependencies.scope_authorization
    if scope is None:
        return
    required_roles = {"expression", "semantic_lint"}
    if scope.family_id != family_id:
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "范围授权与当前研究族不一致",
        )
    if scope.corporate_policy_id != dependencies.policy.policy_id:
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "范围授权与公司政策不一致",
        )
    if scope.approver_role != dependencies.policy.approver_role:
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "范围授权角色与公司政策不一致",
        )
    if not required_roles.issubset({item.value for item in scope.allowed_agent_roles}):
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "范围授权没有同时覆盖 expression 和 semantic_lint",
        )
    if len(hypotheses) > scope.max_hypotheses:
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "批准假设数量超过个人试运行范围",
        )
    if not (scope.authorized_at <= now < scope.expires_at):
        raise _orchestrator_error(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "个人试运行范围授权已过期或尚未生效",
        )


def _authorization_for_request(
    dependencies: ApprovedBatchDependencies,
    request_hash: str,
) -> LLMExportAuthorization | LLMCampaignScopeAuthorization:
    """优先使用旧的精确授权，否则使用一次性研究族范围授权。"""

    exact = dependencies.authorization_by_request_hash.get(request_hash)
    if exact is not None:
        return exact
    if dependencies.scope_authorization is not None:
        return dependencies.scope_authorization
    raise _orchestrator_error(
        FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        "批准批次没有精确请求授权或研究族范围授权",
    )


def _recorded_call_count(artifact_root: Path, bundle_id: str) -> int:
    """统计当前批次已经录制的调用，支持中断后继续使用范围预算。"""

    calls_root = artifact_root / "artifacts" / "llm_batches" / bundle_id / "calls"
    if not calls_root.is_dir():
        return 0
    return sum(
        1
        for directory in calls_root.iterdir()
        if directory.is_dir() and (directory / "call_record.json").is_file()
    )


def _batch_root(dependencies: ApprovedBatchDependencies) -> Path:
    """返回批准批次的不可变产物根目录。"""

    return (
        dependencies.artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "llm_approved_batches"
        / dependencies.family.discovery_family_id
    )


def _event_id(bundle_id: str, target: str, transition: str) -> str:
    """构造恢复时稳定、不会重复追加的事件 ID。"""

    return f"event-{bundle_id}-{target}-{transition}"


def _state_slot_id(candidate_slot_id: str) -> str:
    """把表达式层 C001 映射为状态机冻结的 001 槽身份。"""

    arm_id, raw_number = candidate_slot_id.split(":", 1)
    if raw_number.startswith("C"):
        raw_number = raw_number[1:]
    if len(raw_number) != 3 or not raw_number.isdigit():
        raise ValueError(f"候选槽格式无效：{candidate_slot_id}")
    return f"{arm_id}:{raw_number}"


def _object_slot_id(state_slot_id: str) -> str:
    """返回槽终态对象使用的统一 C001 形式。"""

    arm_id, raw_number = state_slot_id.split(":", 1)
    return f"{arm_id}:C{raw_number}"


def _ensure_generation_started(
    ledger: LLMDiscoveryLedger,
    family: RegisteredLLMDiscoveryResearchFamily,
    bundle_id: str,
    now: datetime,
) -> None:
    """首次运行时把 family 从 registered 推进到 generating。"""

    state = ledger.project_state(family.discovery_family_id)
    if state.family_generation_state is FamilyGenerationState.REGISTERED:
        ledger.append_event(
            LLMDiscoveryEvent(
                event_id=_event_id(
                    bundle_id,
                    family.discovery_family_id,
                    FamilyGenerationState.GENERATING.value,
                ),
                discovery_family_id=family.discovery_family_id,
                target_kind=DiscoveryObjectKind.FAMILY,
                target_id=family.discovery_family_id,
                to_state=FamilyGenerationState.GENERATING,
                created_at=now,
            )
        )
        return
    if state.family_generation_state is not FamilyGenerationState.GENERATING:
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "只有 generating family 可以继续批准批次",
        )


def _set_slot_state(
    ledger: LLMDiscoveryLedger,
    family_id: str,
    bundle_id: str,
    slot_id: str,
    target: CandidateSlotState,
    now: datetime,
) -> None:
    """按当前投影幂等追加候选槽状态事件。"""

    state = ledger.project_state(family_id)
    state_slot_id = _state_slot_id(slot_id)
    current = state.candidate_slot_states[state_slot_id]
    if current is target:
        return
    ledger.append_event(
        LLMDiscoveryEvent(
            event_id=_event_id(bundle_id, state_slot_id, target.value),
            discovery_family_id=family_id,
            target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
            target_id=state_slot_id,
            to_state=target,
            created_at=now,
        )
    )


def _slot_object_path(
    family: RegisteredLLMDiscoveryResearchFamily,
    dependencies: ApprovedBatchDependencies,
    slot_id: str,
) -> Path:
    """返回候选槽终态对象路径。"""

    return (
        dependencies.artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "llm_discovery_families"
        / family.discovery_family_id
        / "candidate_slots"
        / f"{slot_id.replace(':', '__')}.json"
    )


def _write_slot_object(
    family: RegisteredLLMDiscoveryResearchFamily,
    dependencies: ApprovedBatchDependencies,
    slot_id: str,
    payload: dict[str, Any],
) -> str:
    """原子写入候选槽结果或失败终止记录并返回对象哈希。"""

    serialized = canonical_json_bytes(payload)
    _atomic_write_immutable(
        _slot_object_path(family, dependencies, slot_id),
        serialized,
    )
    return sha256_json(payload)


def _failure_state(error: Exception) -> CandidateSlotState:
    """把固定程序失败映射到不可绕过的候选槽终态。"""

    if isinstance(error, FactorMinerError):
        return {
            FailureCode.FIELD_MISSING: CandidateSlotState.AVAILABILITY_FAILED,
            FailureCode.DSL_TYPE_ERROR: CandidateSlotState.SEMANTIC_TYPE_FAILED,
            FailureCode.LOOKAHEAD_DETECTED: CandidateSlotState.LOOKAHEAD_FAILED,
            FailureCode.LABEL_LEAKAGE_DETECTED: CandidateSlotState.DSL_FAILED,
        }.get(error.code, CandidateSlotState.GENERATION_FAILED)
    if isinstance(error, (ValueError, TypeError)):
        return CandidateSlotState.SCHEMA_FAILED
    return CandidateSlotState.GENERATION_FAILED


def _allowed_local_fields(registry: FieldAvailabilityRegistry) -> tuple[str, ...]:
    """返回字段注册表中允许编译的本地字段。"""

    return tuple(
        item.field_id
        for item in registry.fields
        if item.eligible_for_factor and item.point_in_time_guarantee
    )


def _require_registry(
    registry: FieldAvailabilityRegistry | None,
) -> FieldAvailabilityRegistry:
    """候选转换缺失字段合同时硬失败。"""

    if registry is None:
        raise _orchestrator_error(
            FailureCode.SPEC_SCHEMA_INVALID,
            "候选转换缺少服务器字段可用性注册表",
        )
    return registry


def _semantic_lint_payload(
    hypothesis: RegisteredCoverageGapHypothesis,
    base_payload: dict[str, object],
    batch: CandidateExpressionBatch,
) -> dict[str, object]:
    """构造不包含 outcome 的 semantic lint 最小请求体。"""

    payload = dict(base_payload)
    payload.update(
        {
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_claim": hypothesis.draft.claim,
            "expected_sign": hypothesis.testable_prediction.expected_sign,
            "observable_proxy": (
                hypothesis.draft.prediction_proposal.observable_proxy
            ),
            "candidate_expression_batch": batch.model_dump(mode="json"),
            "semantic_lint_contract": {
                "can_modify_expression": False,
                "can_modify_hypothesis": False,
                "can_read_outcome": False,
                "dimensions": (
                    "proxy_alignment",
                    "direction_alignment",
                    "availability_alignment",
                    "undeclared_exposure",
                ),
            },
        }
    )
    return payload


def _load_batch(path: Path, model: type[BaseModel]) -> BaseModel:
    """从不可变 JSON 文件加载并校验结构化批次。"""

    return model.model_validate_json(path.read_bytes())


def prepare_approved_batch_export(
    family_id: str,
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    dependencies: ApprovedBatchDependencies,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
) -> ExportAuthorizationBundle:
    """生成每个批准假设 3 个候选的精确请求集合，不访问网络。"""

    if dependencies.family.discovery_family_id != family_id:
        raise _orchestrator_error(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "编排 family 与依赖 family 不一致",
        )
    validated_batch = _validated_evolution_batch(
        family_id,
        approval_batch,
        evolution_context,
    )
    _validate_approved_hypotheses(family_id, approved_hypotheses)
    _validate_scope_authorization_for_batch(
        family_id,
        approved_hypotheses,
        dependencies,
        dependencies.now or datetime.now(timezone.utc),
    )
    request_hashes: list[str] = []
    candidate_slot_ids: list[str] = []
    batch_root = (
        dependencies.artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "llm_approved_batches"
        / family_id
    )
    for hypothesis in approved_hypotheses:
        payload = dependencies.public_payload_by_hypothesis_id.get(hypothesis.hypothesis_id)
        if payload is None:
            raise _orchestrator_error(
                FailureCode.LLM_POLICY_NOT_AUTHORIZED,
                "批准假设缺少服务器构造的公共或脱敏 payload",
            )
        scan_export_payload(payload, dependencies.policy)
        slots = _candidate_slots(hypothesis)
        request_kwargs: dict[str, object] = {
            "campaign_id": f"{family_id}:{hypothesis.draft.slot_id.split(':', 1)[0]}",
            "slot_ids": slots,
            "public_payload": payload,
        }
        if validated_batch is not None and evolution_context is not None:
            request_kwargs.update(
                {
                    "approval_batch": validated_batch,
                    "evolution_context": evolution_context,
                    "discovery_family_id": family_id,
                    "approval_batch_sha256": (
                        validated_batch.approval_batch_sha256
                    ),
                }
            )
        request = build_expression_agent_request(**request_kwargs)
        _atomic_write_immutable(
            batch_root / "requests" / f"{hypothesis.hypothesis_id}.json",
            canonical_json_bytes(request.model_dump(mode="json")),
        )
        request_hashes.append(request.request_sha256)
        candidate_slot_ids.extend(slots)
    bundle_payload = {
        "family_id": family_id,
        "hypothesis_ids": tuple(item.hypothesis_id for item in approved_hypotheses),
        "candidate_slot_ids": tuple(candidate_slot_ids),
        "request_hashes": tuple(request_hashes),
        "policy_id": dependencies.policy.policy_id,
    }
    bundle_hash = sha256_json(bundle_payload)
    bundle = ExportAuthorizationBundle(
        bundle_id=f"llmbundle_{bundle_hash[:24]}",
        **bundle_payload,
        bundle_sha256=bundle_hash,
    )
    _atomic_write_immutable(
        batch_root / "export_authorization_bundle.json",
        canonical_json_bytes(bundle.model_dump(mode="json")),
    )
    return bundle


def run_approved_batch(
    family_id: str,
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    dependencies: ApprovedBatchDependencies,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
) -> ApprovedBatchResult:
    """完成表达式、semantic lint、候选登记和槽位终态。

    真实因子计算、IC、分组回测和 Barra 归因仍必须等待完整 120 槽
    generation seal；本函数绝不读取 outcome，也不因局部批次完成而打开评价器。
    """

    bundle = prepare_approved_batch_export(
        family_id,
        approved_hypotheses,
        dependencies,
        approval_batch=approval_batch,
        evolution_context=evolution_context,
    )
    ledger = LLMDiscoveryLedger(dependencies.artifact_root)
    ledger.register_family(dependencies.family)
    now = dependencies.now or datetime.now(timezone.utc)
    _ensure_generation_started(ledger, dependencies.family, bundle.bundle_id, now)
    batch_root = _batch_root(dependencies)
    requests_root = batch_root / "requests"
    lint_requests_root = requests_root / "semantic_lint"
    output_root = batch_root / "validated"
    lint_output_root = batch_root / "semantic_lint"
    metadata_root = batch_root / "calls"
    candidate_root = batch_root / "registered_candidates"
    hypothesis_by_id = {item.hypothesis_id: item for item in approved_hypotheses}
    recorded_call_ids: list[str] = []
    ready_slots: list[str] = []
    failed_slots: list[str] = []
    registered_candidate_ids: list[str] = []
    pending_request_hashes: list[str] = []
    seen_candidate_ids: set[str] = set()
    for request_hash, hypothesis_id in zip(
        bundle.request_hashes,
        bundle.hypothesis_ids,
        strict=True,
    ):
        hypothesis = hypothesis_by_id[hypothesis_id]
        slots = _candidate_slots(hypothesis)
        state = ledger.project_state(family_id)
        if all(
            state.candidate_slot_states[_state_slot_id(slot)] in CANDIDATE_TERMINALS
            for slot in slots
        ):
            continue
        for slot in slots:
            if (
                ledger.project_state(family_id).candidate_slot_states[
                    _state_slot_id(slot)
                ]
                is CandidateSlotState.RESERVED
            ):
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    slot,
                    CandidateSlotState.GENERATION_IN_PROGRESS,
                    now,
                )

        expression_path = output_root / f"{hypothesis_id}.json"
        call_metadata_path = metadata_root / f"{hypothesis_id}.json"
        lint_call_metadata_path = metadata_root / f"{hypothesis_id}.semantic_lint.json"
        expression_call_id = "recorded-response"
        if expression_path.is_file():
            validated = CandidateExpressionBatch.model_validate_json(
                expression_path.read_bytes()
            )
            if call_metadata_path.is_file():
                expression_call_id = json.loads(
                    call_metadata_path.read_text(encoding="utf-8")
                ).get("expression_call_id", expression_call_id)
        else:
            request = PreparedDeepSeekRequest.model_validate_json(
                (requests_root / f"{hypothesis_id}.json").read_bytes()
            )
            authorization = _authorization_for_request(dependencies, request_hash)
            try:
                response = execute_recorded_call(
                    prepared=request,
                    authorization=authorization,
                    policy=dependencies.policy,
                    record_root=dependencies.artifact_root
                    / "artifacts"
                    / "llm_batches"
                    / bundle.bundle_id,
                    transport=dependencies.transport or UrllibDeepSeekTransport(),
                    now=now,
                    scope_request_count=(
                        _recorded_call_count(dependencies.artifact_root, bundle.bundle_id) + 1
                        if isinstance(
                            authorization,
                            LLMCampaignScopeAuthorization,
                        )
                        else 1
                    ),
                )
                if response.content_json is None:
                    raise _orchestrator_error(
                        FailureCode.LLM_RESPONSE_INVALID,
                        "表达式代理没有返回最终 JSON",
                    )
                validated = CandidateExpressionBatch.model_validate(
                    response.content_json
                )
                _atomic_write_immutable(
                    expression_path,
                    canonical_json_bytes(validated.model_dump(mode="json")),
                )
                _atomic_write_immutable(
                    call_metadata_path,
                    canonical_json_bytes(
                        {"expression_call_id": response.record.call_id}
                    ),
                )
                expression_call_id = response.record.call_id
                recorded_call_ids.append(response.record.call_id)
            except FactorMinerError as error:
                if error.code is not FailureCode.LLM_RESPONSE_INVALID:
                    raise
                for slot in slots:
                    _write_slot_object(
                        dependencies.family,
                        dependencies,
                        slot,
                        {
                            "slot_id": slot,
                            "status": CandidateSlotState.GENERATION_FAILED,
                            "failure_code": error.code,
                        },
                    )
                    _set_slot_state(
                        ledger,
                        family_id,
                        bundle.bundle_id,
                        slot,
                        CandidateSlotState.GENERATION_FAILED,
                        now,
                    )
                    failed_slots.append(slot)
                continue
            except (TypeError, ValueError) as error:
                for slot in slots:
                    _write_slot_object(
                        dependencies.family,
                        dependencies,
                        slot,
                        {
                            "slot_id": slot,
                            "status": CandidateSlotState.GENERATION_FAILED,
                            "failure_code": FailureCode.SPEC_SCHEMA_INVALID,
                            "message": str(error),
                        },
                    )
                    _set_slot_state(
                        ledger,
                        family_id,
                        bundle.bundle_id,
                        slot,
                        CandidateSlotState.GENERATION_FAILED,
                        now,
                    )
                    failed_slots.append(slot)
                continue

        returned = {item.candidate_slot_id for item in validated.candidates}
        if not returned.issubset(set(slots)):
            raise _orchestrator_error(
                FailureCode.LLM_RESPONSE_INVALID,
                "表达式代理返回了未授权候选槽",
            )
        for slot in sorted(returned):
            _set_slot_state(
                ledger,
                family_id,
                bundle.bundle_id,
                slot,
                CandidateSlotState.DRAFT_GENERATED,
                now,
            )
        for slot in sorted(set(slots).difference(returned)):
            _write_slot_object(
                dependencies.family,
                dependencies,
                slot,
                {
                    "slot_id": slot,
                    "status": CandidateSlotState.GENERATION_FAILED,
                    "failure_code": FailureCode.LLM_RESPONSE_INVALID,
                    "message": "表达式代理未返回预留槽",
                },
            )
            _set_slot_state(
                ledger,
                family_id,
                bundle.bundle_id,
                slot,
                CandidateSlotState.GENERATION_FAILED,
                now,
            )
            failed_slots.append(slot)

        valid_drafts: list[CandidateExpressionDraft] = []
        expression_hashes: set[str] = set()
        for draft in validated.candidates:
            if draft.candidate_slot_id not in returned:
                continue
            try:
                validation = validate_candidate_expression(
                    draft=draft,
                    hypothesis=hypothesis,
                    registry=_require_registry(dependencies.field_registry),
                )
                expression_hash = sha256_json(
                    draft.expression.model_dump(mode="json")
                )
                if expression_hash in expression_hashes:
                    raise FactorMinerError(
                        FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                        "同一批次包含重复规范 AST",
                    )
                expression_hashes.add(expression_hash)
                del validation
                valid_drafts.append(draft)
            except Exception as error:
                terminal = (
                    CandidateSlotState.DUPLICATE_FAILED
                    if isinstance(error, FactorMinerError)
                    and error.code is FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
                    else _failure_state(error)
                )
                _write_slot_object(
                    dependencies.family,
                    dependencies,
                    draft.candidate_slot_id,
                    _bound_slot_payload(
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": terminal,
                            "failure_code": (
                                error.code
                                if isinstance(error, FactorMinerError)
                                else FailureCode.SPEC_SCHEMA_INVALID
                            ),
                            "message": str(error),
                        },
                        approval_batch=approval_batch,
                        evolution_context=evolution_context,
                    ),
                )
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    draft.candidate_slot_id,
                    terminal,
                    now,
                )
                failed_slots.append(draft.candidate_slot_id)

        if not valid_drafts:
            continue
        if len(valid_drafts) == 3 and approval_batch is not None and evolution_context is not None:
            design_result = preflight_designs(
                tuple(
                    resolve_public_field_aliases(item.expression, _require_registry(dependencies.field_registry))
                    for item in valid_drafts
                ),
                policy=DesignDiversityPolicy(),
                registry=_require_registry(dependencies.field_registry),
            ).bind_slot_ids(
                tuple(item.candidate_slot_id for item in valid_drafts)
            )
            if not design_result.passed:
                for draft in valid_drafts:
                    slot_binding = _evolution_slot_binding(
                        draft.candidate_slot_id,
                        approval_batch=approval_batch,
                        evolution_context=evolution_context,
                    )
                    _write_slot_object(
                        dependencies.family,
                        dependencies,
                        draft.candidate_slot_id,
                        _bound_slot_payload(
                            draft.candidate_slot_id,
                            {
                                "slot_id": draft.candidate_slot_id,
                                "status": CandidateSlotState.DESIGN_DIVERSITY_FAILED,
                                **slot_binding,
                                "design_signatures": [
                                    {
                                        "canonical_ast_hash": item.canonical_ast_hash,
                                        "ast_without_temporal_parameters_hash": (
                                            item.ast_without_temporal_parameters_hash
                                        ),
                                        "field_set": item.field_set,
                                        "operator_topology": item.operator_topology,
                                        "temporal_roles": item.temporal_roles,
                                        "input_combinations": item.input_combinations,
                                        "node_count": item.node_count,
                                        "depth": item.depth,
                                    }
                                    for item in design_result.signatures
                                ],
                                "design_diversity_failures": [
                                    item.to_dict() for item in design_result.failures
                                ],
                                "failure_code": FailureCode.DESIGN_DIVERSITY_FAILED.value,
                                "failure_stage": "design_diversity_preflight",
                                "created_at": now.isoformat(),
                            },
                            approval_batch=approval_batch,
                            evolution_context=evolution_context,
                        ),
                    )
                    _set_slot_state(
                        ledger,
                        family_id,
                        bundle.bundle_id,
                        draft.candidate_slot_id,
                        CandidateSlotState.DESIGN_DIVERSITY_FAILED,
                        now,
                    )
                    failed_slots.append(draft.candidate_slot_id)
                continue
        lint_batch_path = lint_output_root / f"{hypothesis_id}.json"
        lint_call_id = "recorded-response"
        if lint_batch_path.is_file():
            lint_batch = SemanticLintBatch.model_validate_json(
                lint_batch_path.read_bytes()
            )
            if lint_call_metadata_path.is_file():
                lint_call_id = json.loads(
                    lint_call_metadata_path.read_text(encoding="utf-8")
                ).get("semantic_lint_call_id", lint_call_id)
        else:
            lint_batch_input = CandidateExpressionBatch(candidates=tuple(valid_drafts))
            lint_payload = _semantic_lint_payload(
                hypothesis,
                dependencies.public_payload_by_hypothesis_id[hypothesis_id],
                lint_batch_input,
            )
            lint_request = build_semantic_lint_agent_request(
                campaign_id=(
                    f"{family_id}:{hypothesis.draft.slot_id.split(':', 1)[0]}"
                ),
                slot_ids=tuple(item.candidate_slot_id for item in valid_drafts),
                public_payload=lint_payload,
            )
            _atomic_write_immutable(
                lint_requests_root / f"{hypothesis_id}.json",
                canonical_json_bytes(lint_request.model_dump(mode="json")),
            )
            try:
                authorization = _authorization_for_request(
                    dependencies,
                    lint_request.request_sha256,
                )
            except FactorMinerError:
                pending_request_hashes.append(lint_request.request_sha256)
                continue
            try:
                response = execute_recorded_call(
                    prepared=lint_request,
                    authorization=authorization,
                    policy=dependencies.policy,
                    record_root=dependencies.artifact_root
                    / "artifacts"
                    / "llm_batches"
                    / bundle.bundle_id,
                    transport=dependencies.transport or UrllibDeepSeekTransport(),
                    now=now,
                    scope_request_count=(
                        _recorded_call_count(dependencies.artifact_root, bundle.bundle_id) + 1
                        if isinstance(
                            authorization,
                            LLMCampaignScopeAuthorization,
                        )
                        else 1
                    ),
                )
                if response.content_json is None:
                    raise _orchestrator_error(
                        FailureCode.LLM_RESPONSE_INVALID,
                        "semantic lint 没有返回最终 JSON",
                    )
                lint_batch = SemanticLintBatch.model_validate(
                    response.content_json
                )
                _atomic_write_immutable(
                    lint_batch_path,
                    canonical_json_bytes(lint_batch.model_dump(mode="json")),
                )
                _atomic_write_immutable(
                    lint_call_metadata_path,
                    canonical_json_bytes(
                        {"semantic_lint_call_id": response.record.call_id}
                    ),
                )
                lint_call_id = response.record.call_id
                recorded_call_ids.append(response.record.call_id)
            except FactorMinerError as error:
                if error.code is not FailureCode.LLM_RESPONSE_INVALID:
                    raise
                for draft in valid_drafts:
                    _write_slot_object(
                        dependencies.family,
                        dependencies,
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": CandidateSlotState.GENERATION_FAILED,
                            "failure_code": error.code,
                        },
                    )
                    _set_slot_state(
                        ledger,
                        family_id,
                        bundle.bundle_id,
                        draft.candidate_slot_id,
                        CandidateSlotState.GENERATION_FAILED,
                        now,
                    )
                    failed_slots.append(draft.candidate_slot_id)
                continue
            except (TypeError, ValueError) as error:
                for draft in valid_drafts:
                    _write_slot_object(
                        dependencies.family,
                        dependencies,
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": CandidateSlotState.SCHEMA_FAILED,
                            "failure_code": FailureCode.SPEC_SCHEMA_INVALID,
                            "message": str(error),
                        },
                    )
                    _set_slot_state(
                        ledger,
                        family_id,
                        bundle.bundle_id,
                        draft.candidate_slot_id,
                        CandidateSlotState.SCHEMA_FAILED,
                        now,
                    )
                    failed_slots.append(draft.candidate_slot_id)
                continue

        lint_by_slot = {
            item.candidate_slot_id: item for item in lint_batch.decisions
        }
        valid_slot_set = {item.candidate_slot_id for item in valid_drafts}
        if not set(lint_by_slot).issubset(valid_slot_set):
            raise _orchestrator_error(
                FailureCode.LLM_RESPONSE_INVALID,
                "semantic lint 返回了未授权候选槽",
            )
        for draft in valid_drafts:
            lint = lint_by_slot.get(draft.candidate_slot_id)
            if lint is None:
                _write_slot_object(
                    dependencies.family,
                    dependencies,
                    draft.candidate_slot_id,
                    {
                        "slot_id": draft.candidate_slot_id,
                        "status": CandidateSlotState.GENERATION_FAILED,
                        "failure_code": FailureCode.LLM_RESPONSE_INVALID,
                        "message": "semantic lint 未返回该槽位",
                    },
                )
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    draft.candidate_slot_id,
                    CandidateSlotState.GENERATION_FAILED,
                    now,
                )
                failed_slots.append(draft.candidate_slot_id)
                continue
            if lint.decision == "rejected":
                _write_slot_object(
                    dependencies.family,
                    dependencies,
                    draft.candidate_slot_id,
                    _bound_slot_payload(
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": CandidateSlotState.SEMANTIC_LINT_REJECTED,
                            "lint": lint.model_dump(mode="json"),
                            "expression": draft.expression.model_dump(mode="json"),
                        },
                        approval_batch=approval_batch,
                        evolution_context=evolution_context,
                    ),
                )
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    draft.candidate_slot_id,
                    CandidateSlotState.SEMANTIC_LINT_REJECTED,
                    now,
                )
                failed_slots.append(draft.candidate_slot_id)
                continue
            try:
                registry = _require_registry(dependencies.field_registry)
                candidate = convert_candidate(
                    draft=draft,
                    hypothesis=hypothesis,
                    lint=lint,
                    registry=registry,
                    created_at=now,
                    provenance={
                        "discovery_family_id": family_id,
                        "candidate_slot_id": draft.candidate_slot_id,
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "expression_call_id": expression_call_id,
                        "semantic_lint_call_id": lint_call_id,
                    },
                )
                registered = registered_trusted_candidate(candidate)
                compile_candidate(
                    registered,
                    allowed_fields=_allowed_local_fields(registry),
                )
                if registered.candidate_id in seen_candidate_ids:
                    raise FactorMinerError(
                        FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                        "批次内规范候选重复",
                    )
                existing_path = (
                    dependencies.artifact_root.expanduser().resolve(strict=False)
                    / "state"
                    / "candidates"
                    / f"{registered.candidate_id}.json"
                )
                slot_candidate_path = (
                    candidate_root
                    / f"{draft.candidate_slot_id.replace(':', '__')}.json"
                )
                if existing_path.is_file() and not (
                    slot_candidate_path.is_file()
                    and RegisteredTrustedCandidate.model_validate_json(
                        slot_candidate_path.read_bytes()
                    )
                    == registered
                ):
                    raise FactorMinerError(
                        FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                        "候选已在其他槽位或研究中登记",
                    )
                JsonlLedger(dependencies.artifact_root).register_candidate(registered)
                _atomic_write_immutable(
                    slot_candidate_path,
                    canonical_json_bytes(registered.model_dump(mode="json")),
                )
                _write_slot_object(
                    dependencies.family,
                    dependencies,
                    draft.candidate_slot_id,
                    _bound_slot_payload(
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": CandidateSlotState.READY_FOR_REGISTRATION,
                            "candidate_id": registered.candidate_id,
                            "candidate_spec_hash": registered.spec_hash,
                            "lint": lint.model_dump(mode="json"),
                        },
                        approval_batch=approval_batch,
                        evolution_context=evolution_context,
                    ),
                )
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    draft.candidate_slot_id,
                    CandidateSlotState.READY_FOR_REGISTRATION,
                    now,
                )
                seen_candidate_ids.add(registered.candidate_id)
                registered_candidate_ids.append(registered.candidate_id)
                ready_slots.append(draft.candidate_slot_id)
            except Exception as error:
                terminal = (
                    CandidateSlotState.DUPLICATE_FAILED
                    if isinstance(error, FactorMinerError)
                    and error.code is FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
                    else (
                        CandidateSlotState.COMPILE_FAILED
                        if isinstance(error, FactorMinerError)
                        and error.code
                        in {
                            FailureCode.DSL_TYPE_ERROR,
                            FailureCode.FIELD_MISSING,
                            FailureCode.LOOKAHEAD_DETECTED,
                            FailureCode.LABEL_LEAKAGE_DETECTED,
                        }
                        else _failure_state(error)
                    )
                )
                _write_slot_object(
                    dependencies.family,
                    dependencies,
                    draft.candidate_slot_id,
                    _bound_slot_payload(
                        draft.candidate_slot_id,
                        {
                            "slot_id": draft.candidate_slot_id,
                            "status": terminal,
                            "failure_code": (
                                error.code
                                if isinstance(error, FactorMinerError)
                                else FailureCode.SPEC_SCHEMA_INVALID
                            ),
                            "message": str(error),
                        },
                        approval_batch=approval_batch,
                        evolution_context=evolution_context,
                    ),
                )
                _set_slot_state(
                    ledger,
                    family_id,
                    bundle.bundle_id,
                    draft.candidate_slot_id,
                    terminal,
                    now,
                )
                failed_slots.append(draft.candidate_slot_id)
    status = "completed" if not failed_slots else "partial"
    if pending_request_hashes:
        status = "awaiting_semantic_lint_authorization"
    result_payload = {
        "bundle_id": bundle.bundle_id,
        "family_id": family_id,
        "status": status,
        "request_hashes": bundle.request_hashes,
        "recorded_call_ids": tuple(recorded_call_ids),
        "ready_candidate_slot_ids": tuple(sorted(ready_slots)),
        "failed_candidate_slot_ids": tuple(sorted(failed_slots)),
        "registered_candidate_ids": tuple(sorted(registered_candidate_ids)),
        "pending_request_hashes": tuple(sorted(set(pending_request_hashes))),
    }
    result_hash = sha256_json(result_payload)
    return ApprovedBatchResult(**result_payload, result_sha256=result_hash)


def resume_approved_batch(
    family_id: str,
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    dependencies: ApprovedBatchDependencies,
) -> ApprovedBatchResult:
    """恢复入口；已写入的 validated 文件由幂等发布层跳过。"""

    return run_approved_batch(family_id, approved_hypotheses, dependencies)


def finalize_unexecuted_slots(
    family_id: str,
    dependencies: ApprovedBatchDependencies,
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
) -> GenerationSlotFinalizationSummary:
    """为没有批准输入或没有可执行父候选的槽写入明确终态。

    该函数只在表达式和 shadow 运行已经完成后调用。它不会把处于
    ``generation_in_progress`` 或 ``draft_generated`` 的可恢复槽静默标成失败；
    这样中断恢复仍然只能复用原请求和原候选。
    """

    if dependencies.family.discovery_family_id != family_id:
        raise _orchestrator_error(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "槽位收口 family 与依赖 family 不一致",
        )
    ledger = LLMDiscoveryLedger(dependencies.artifact_root)
    family = ledger.load_family(family_id)
    state = ledger.project_state(family_id)
    if state.family_generation_state is not FamilyGenerationState.GENERATING:
        raise _orchestrator_error(
            FailureCode.LLM_STATE_TRANSITION_INVALID,
            "只有 generating family 可以收口候选槽",
        )
    approved_slots: set[str] = set()
    for hypothesis in approved_hypotheses:
        if hypothesis.decision.decision != "approved":
            raise _orchestrator_error(
                FailureCode.LLM_STATE_TRANSITION_INVALID,
                "槽位收口只接受人工批准假设",
            )
        approved_slots.update(
            _state_slot_id(slot) for slot in _candidate_slots(hypothesis)
        )

    now = dependencies.now or datetime.now(timezone.utc)
    newly_finalized: list[str] = []
    for slot_id in expected_candidate_slot_ids(family.spec):
        current = ledger.project_state(family_id).candidate_slot_states[slot_id]
        if current in CANDIDATE_TERMINALS:
            object_path = _slot_object_path(
                family,
                dependencies,
                _object_slot_id(slot_id),
            )
            if not object_path.is_file():
                raise _orchestrator_error(
                    FailureCode.LEDGER_CORRUPT,
                    f"候选终态对象缺失：{slot_id}",
                )
            continue
        if current is not CandidateSlotState.RESERVED:
            continue
        arm_id = slot_id.split(":", 1)[0]
        if arm_id in {"coverage_outcome_llm", "literature_only_llm"}:
            target = (
                CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED
                if slot_id not in approved_slots
                else CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
            )
            reason = (
                "该假设没有进入批准批次"
                if target is CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED
                else "批准批次未能为该槽写入可恢复请求结果"
            )
        else:
            target = CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
            reason = "shadow 生成未为该槽形成可验证父候选"
        object_slot_id = _object_slot_id(slot_id)
        _write_slot_object(
            family,
            dependencies,
            object_slot_id,
            {
                "slot_id": object_slot_id,
                "status": target.value,
                "failure_code": target.value,
                "reason": reason,
            },
        )
        _set_slot_state(ledger, family_id, family_id, object_slot_id, target, now)
        newly_finalized.append(slot_id)

    final_state = ledger.project_state(family_id)
    remaining = tuple(
        slot_id
        for slot_id, slot_state in final_state.candidate_slot_states.items()
        if slot_state not in CANDIDATE_TERMINALS
    )
    payload = {
        "family_id": family_id,
        "terminal_slot_ids": tuple(
            slot_id
            for slot_id, slot_state in final_state.candidate_slot_states.items()
            if slot_state in CANDIDATE_TERMINALS
        ),
        "newly_finalized_slot_ids": tuple(newly_finalized),
        "remaining_non_terminal_slot_ids": remaining,
    }
    return GenerationSlotFinalizationSummary(
        **payload,
        summary_sha256=sha256_json(payload),
    )


def seal_completed_generation(
    family_id: str,
    dependencies: GenerationSealDependencies,
    seal_input: Mapping[str, object],
) -> RegisteredGenerationSeal:
    """由服务器固定程序核验 120 个槽并登记 generation seal。

    `seal_input` 只提供 outcome 前冻结的元数据，例如 campaign、prompt、模型
    和评价依赖区间；槽对象 hash 始终从服务器本地不可变 JSON 重新计算，调用者
    不能通过 CLI 传入一组脱离实际对象的 hash 来绕过封存检查。
    """

    approval_batch, evolution_context, slot_objects = _strict_seal_inputs(
        family_id,
        seal_input,
    )
    if dependencies.family.discovery_family_id != family_id:
        raise _orchestrator_error(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "封存 family 与依赖 family 不一致",
        )
    ledger = LLMDiscoveryLedger(dependencies.artifact_root)
    family = ledger.load_family(family_id)
    state = ledger.project_state(family_id)
    if state.family_generation_state is FamilyGenerationState.GENERATION_SEALED:
        seal = ledger.load_generation_seal(family_id)
        slot_object_hashes: dict[str, str] = {}
        for state_slot_id in expected_candidate_slot_ids(family.spec):
            object_slot_id = _object_slot_id(state_slot_id)
            path = _slot_object_path(family, dependencies, object_slot_id)
            if not path.is_file():
                raise _orchestrator_error(
                    FailureCode.LEDGER_CORRUPT,
                    f"generation seal 引用的槽对象缺失：{state_slot_id}",
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise _orchestrator_error(
                    FailureCode.LEDGER_CORRUPT,
                    f"generation seal 引用的槽对象无法解析：{state_slot_id}",
                ) from None
            if slot_objects is not None and slot_objects.get(state_slot_id) != payload:
                raise _orchestrator_error(
                    FailureCode.EVOLUTION_HASH_MISMATCH,
                    f"strict evolution seal 的槽对象与服务器对象不一致：{state_slot_id}",
                )
            slot_object_hashes[state_slot_id] = sha256_json(payload)
        verify_generation_seal(
            family,
            state,
            seal,
            slot_object_hashes=slot_object_hashes,
            approval_batch=approval_batch,
            evolution_context=evolution_context,
            slot_objects=slot_objects,
        )
        return seal
    expected_slots = expected_candidate_slot_ids(family.spec)
    if state.family_generation_state is not FamilyGenerationState.GENERATING:
        raise _orchestrator_error(
            FailureCode.LLM_FAMILY_NOT_SEALED,
            "只有 generating family 可以完成封存",
        )
    slot_object_hashes: dict[str, str] = {}
    for state_slot_id in expected_slots:
        slot_state = state.candidate_slot_states[state_slot_id]
        if slot_state not in CANDIDATE_TERMINALS:
            raise _orchestrator_error(
                FailureCode.LLM_FAMILY_NOT_SEALED,
                f"候选槽尚未进入终态：{state_slot_id}",
            )
        object_slot_id = _object_slot_id(state_slot_id)
        path = _slot_object_path(family, dependencies, object_slot_id)
        if not path.is_file():
            raise _orchestrator_error(
                FailureCode.LLM_FAMILY_NOT_SEALED,
                f"候选槽终态对象缺失：{state_slot_id}",
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise _orchestrator_error(
                FailureCode.LEDGER_CORRUPT,
                f"候选槽终态对象无法解析：{state_slot_id}",
            ) from None
        if not isinstance(payload, dict) or payload.get("slot_id") != object_slot_id:
            raise _orchestrator_error(
                FailureCode.LEDGER_CORRUPT,
                f"候选槽终态对象身份不一致：{state_slot_id}",
            )
        if payload.get("status") != slot_state.value:
            raise _orchestrator_error(
                FailureCode.LEDGER_CORRUPT,
                f"候选槽终态对象与账本状态不一致：{state_slot_id}",
            )
        if slot_objects is not None and slot_objects.get(state_slot_id) != payload:
            raise _orchestrator_error(
                FailureCode.EVOLUTION_HASH_MISMATCH,
                f"strict evolution seal 的槽对象与服务器对象不一致：{state_slot_id}",
            )
        slot_object_hashes[state_slot_id] = sha256_json(payload)

    try:
        intervals_payload = seal_input.get("evaluation_dependency_intervals", ())
        if not isinstance(intervals_payload, (tuple, list)):
            raise ValueError("evaluation_dependency_intervals 必须是数组")
        dependency_intervals = tuple(
            EvaluationDependencyInterval.model_validate(item)
            for item in intervals_payload
        )
        seal = build_generation_seal(
            family,
            state,
            slot_object_hashes=slot_object_hashes,
            slot_objects=slot_objects,
            evaluation_dependency_intervals=dependency_intervals,
            llm_campaign_spec_hashes=tuple(seal_input["llm_campaign_spec_hashes"]),
            prompt_bundle_hashes=tuple(seal_input["prompt_bundle_hashes"]),
            model_identity=str(seal_input["model_identity"]),
            generator_identity_hashes=dict(seal_input["generator_identity_hashes"]),
            approval_batch=approval_batch,
            evolution_context=evolution_context,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _orchestrator_error(
            FailureCode.LLM_FAMILY_NOT_SEALED,
            f"generation seal 元数据无效：{error}",
        ) from error
    ledger.register_generation_seal(seal)
    now = dependencies.now or datetime.now(timezone.utc)
    ledger.append_event(
        LLMDiscoveryEvent(
            event_id=f"event-generation-sealed-{seal.generation_seal_id[8:]}",
            discovery_family_id=family_id,
            target_kind=DiscoveryObjectKind.FAMILY,
            target_id=family_id,
            to_state=FamilyGenerationState.GENERATION_SEALED,
            created_at=now,
        )
    )
    return seal
