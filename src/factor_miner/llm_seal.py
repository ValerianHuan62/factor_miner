"""V0.5 不可逆生成封存与结果端口防火墙。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.research_evolution import require_approved_evolution_batch
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    ResearchEvolutionContext,
)
from factor_miner.llm_schema import (
    EvaluationDependencyInterval,
    EvaluationEvidenceTier,
    RegisteredLLMDiscoveryResearchFamily,
    validate_evaluation_relationship,
)
from factor_miner.llm_state import (
    CANDIDATE_TERMINALS,
    CandidateSlotState,
    EXPECTED_CANDIDATE_SLOT_COUNT,
    FamilyGenerationState,
    LLMDiscoveryFamilyState,
    expected_candidate_slot_ids,
    expected_logical_hypothesis_slot_ids,
    logical_candidate_design_slot_id,
    logical_hypothesis_slot_id,
)


class GenerationSealManifest(BaseModel):
    """绑定全部生成选择、终态、数据身份和评价预算的 manifest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    family_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_spec_hashes: Mapping[str, str]
    llm_campaign_spec_hashes: tuple[str, str]
    candidate_slot_states: Mapping[str, CandidateSlotState]
    slot_object_hashes: Mapping[str, str]
    prompt_bundle_hashes: tuple[str, str, str]
    model_identity: str
    generator_identity_hashes: Mapping[str, str]
    evaluation_dependency_intervals: tuple[EvaluationDependencyInterval, ...]
    discovery_context_data_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_evaluation_data_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_relationship: str
    evaluation_policy_id: str
    evaluation_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_budget_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_contract_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evolution_context_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    coverage_graph_manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    memory_snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    gap_report_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approval_batch_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    design_policy_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    logical_hypothesis_ids_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    global_statistical_trial_budget: int

    @model_validator(mode="after")
    def validate_contract(self) -> GenerationSealManifest:
        """manifest 必须保持 120 槽固定预算，strict 字段不能半填充。"""

        if len(self.candidate_slot_states) != EXPECTED_CANDIDATE_SLOT_COUNT:
            raise ValueError("generation seal manifest 必须包含完整 120 槽状态")
        if len(self.slot_object_hashes) != EXPECTED_CANDIDATE_SLOT_COUNT:
            raise ValueError("generation seal manifest 必须包含完整 120 槽对象 hash")
        if self.global_statistical_trial_budget != EXPECTED_CANDIDATE_SLOT_COUNT:
            raise ValueError("generation seal manifest 统计预算必须固定为 120")
        strict_fields = (
            self.evolution_context_hash,
            self.coverage_graph_manifest_hash,
            self.memory_snapshot_hash,
            self.gap_report_hash,
            self.approval_batch_hash,
            self.design_policy_hash,
            self.logical_hypothesis_ids_hash,
        )
        populated = tuple(value is not None for value in strict_fields)
        if any(populated) and not all(populated):
            raise ValueError("strict evolution seal 字段必须全部提供或全部省略")
        return self


class RegisteredGenerationSeal(BaseModel):
    """内容寻址且不可覆盖的 generation seal。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation_seal_id: str = Field(pattern=r"^llmseal_[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: GenerationSealManifest

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredGenerationSeal:
        """seal ID 与 hash 必须由完整 manifest 派生。"""

        expected = sha256_json(self.manifest.model_dump(mode="json"))
        if self.manifest_sha256 != expected:
            raise ValueError("generation seal manifest hash 与内容不一致")
        if self.generation_seal_id != f"llmseal_{expected[:24]}":
            raise ValueError("generation seal ID 与内容不一致")
        return self


class VerifiedDiscoveryProjection(BaseModel):
    """只有完整账本核验后才能构造并传入防火墙的投影包装。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: LLMDiscoveryFamilyState
    ledger_tip_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


OutcomeValue = TypeVar("OutcomeValue")


@dataclass(frozen=True, slots=True)
class EvaluationOpenAuthorization:
    """结果端口成功打开后的最强证据层级和端口返回值。"""

    evidence_tier: EvaluationEvidenceTier
    outcome_result: object


def _seal_error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.LLM_FAMILY_NOT_SEALED, message)


def _arm_hashes(
    family: RegisteredLLMDiscoveryResearchFamily,
) -> dict[str, str]:
    return {
        arm.arm_id: sha256_json(arm.model_dump(mode="json"))
        for arm in family.spec.arm_specs
    }


def _logical_hypothesis_ids_hash(
    logical_hypothesis_ids: tuple[str, ...],
) -> str:
    """对 H01-H10 逻辑槽集合生成稳定哈希。"""

    return sha256_json({"logical_hypothesis_ids": logical_hypothesis_ids})


def _validate_evolution_seal_contract(
    state: LLMDiscoveryFamilyState,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
    slot_objects: Mapping[str, Mapping[str, object]] | None,
) -> tuple[str, ...] | None:
    """核验严格演化路径的 seal 输入和失败对象引用。"""

    if approval_batch is None and evolution_context is None:
        return None
    if approval_batch is None or evolution_context is None:
        raise _seal_error("严格演化 seal 必须同时提供 approval_batch 与 evolution_context")
    require_approved_evolution_batch(
        approval_batch,
        context_sha256=evolution_context.context_sha256,
        discovery_family_id=evolution_context.discovery_family_id,
    )
    if slot_objects is None:
        raise _seal_error("严格演化 seal 必须提供全部槽对象用于复验")
    logical_ids = tuple(item.logical_slot_id for item in approval_batch.approvals)
    if logical_ids != expected_logical_hypothesis_slot_ids():
        raise _seal_error("严格演化 seal 必须固定覆盖 H01-H10")
    for slot_id, slot_state in state.candidate_slot_states.items():
        payload = slot_objects.get(slot_id)
        if payload is None:
            raise _seal_error(f"严格演化 seal 缺少槽对象：{slot_id}")
        if slot_state is not CandidateSlotState.DESIGN_DIVERSITY_FAILED:
            continue
        expected_hypothesis = logical_hypothesis_slot_id(slot_id)
        expected_design_slot = logical_candidate_design_slot_id(slot_id)
        required_fields = {
            "logical_hypothesis_id": expected_hypothesis,
            "arm_id": slot_id.split(":", 1)[0],
            "candidate_design_slot_id": expected_design_slot,
            "context_sha256": evolution_context.context_sha256,
            "failure_code": FailureCode.DESIGN_DIVERSITY_FAILED.value,
            "failure_stage": "design_diversity_preflight",
        }
        for field, expected in required_fields.items():
            if payload.get(field) != expected:
                raise _seal_error(
                    f"设计差异失败槽缺少或错误引用字段 {field}：{slot_id}"
                )
        signatures = payload.get("design_signatures")
        if not isinstance(signatures, list) or len(signatures) != 3:
            raise _seal_error(f"设计差异失败槽缺少三件设计签名：{slot_id}")
        failures = payload.get("design_diversity_failures")
        if not isinstance(failures, list) or not failures:
            raise _seal_error(f"设计差异失败槽缺少失败详情：{slot_id}")
        if "evaluation_dependency_interval" in payload:
            raise _seal_error(f"设计差异失败槽不得携带评价依赖区间：{slot_id}")
    return logical_ids


def build_generation_seal(
    family: RegisteredLLMDiscoveryResearchFamily,
    state: LLMDiscoveryFamilyState,
    *,
    slot_object_hashes: Mapping[str, str],
    slot_objects: Mapping[str, Mapping[str, object]] | None = None,
    evaluation_dependency_intervals: tuple[EvaluationDependencyInterval, ...],
    llm_campaign_spec_hashes: tuple[str, str],
    prompt_bundle_hashes: tuple[str, str, str],
    model_identity: str,
    generator_identity_hashes: Mapping[str, str],
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
) -> RegisteredGenerationSeal:
    """在全部 120 槽终结后生成不可逆内容身份。"""

    if state.discovery_family_id != family.discovery_family_id:
        raise _seal_error("family 与状态投影身份不一致")
    if state.family_generation_state is not FamilyGenerationState.GENERATING:
        raise _seal_error("只有 generating family 可以生成 seal")
    expected_slots = expected_candidate_slot_ids(family.spec)
    if tuple(state.candidate_slot_states) != expected_slots:
        raise _seal_error("状态投影没有按冻结顺序包含完整 120 槽")
    if len(state.candidate_slot_states) != 120 or any(
        value not in CANDIDATE_TERMINALS
        for value in state.candidate_slot_states.values()
    ):
        raise _seal_error("全部 120 个候选槽必须进入生成终态")
    if set(slot_object_hashes) != set(expected_slots) or any(
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in slot_object_hashes.values()
    ):
        raise _seal_error("全部 120 个槽必须保存对象或终止记录 hash")
    ready_slots = {
        slot_id
        for slot_id, value in state.candidate_slot_states.items()
        if value is CandidateSlotState.READY_FOR_REGISTRATION
    }
    dependency_slots = {
        interval.candidate_slot_id
        for interval in evaluation_dependency_intervals
    }
    if len(evaluation_dependency_intervals) != len(dependency_slots):
        raise _seal_error("同一 ready 候选不能重复登记原始依赖区间")
    if ready_slots != dependency_slots:
        raise _seal_error("每个 ready 候选必须且只能有一个原始依赖区间")
    logical_hypothesis_ids = _validate_evolution_seal_contract(
        state,
        approval_batch=approval_batch,
        evolution_context=evolution_context,
        slot_objects=slot_objects,
    )

    arm_hashes = _arm_hashes(family)
    if (
        any(len(value) != 64 for value in llm_campaign_spec_hashes)
        or any(len(value) != 64 for value in prompt_bundle_hashes)
        or not model_identity.strip()
        or set(generator_identity_hashes) != set(arm_hashes)
        or any(len(value) != 64 for value in generator_identity_hashes.values())
    ):
        raise _seal_error("campaign、prompt、model 或 generator 身份不完整")
    data_contract_identity_hash = sha256_json(
        {
            "discovery_context_data_identity": (
                family.spec.discovery_context_data_identity.model_dump(mode="json")
            ),
            "candidate_evaluation_data_identity": (
                family.spec.candidate_evaluation_data_identity.model_dump(mode="json")
            ),
            "evaluation_relationship": family.spec.evaluation_relationship,
        }
    )
    evaluation_policy_hash = sha256_json(
        {
            "evaluation_policy_id": family.spec.evaluation_policy_id,
            "policy_contract": family.spec.multiplicity_policy,
        }
    )
    statistical_budget_hash = sha256_json(
        {
            "global_statistical_trial_budget": (
                family.spec.global_statistical_trial_budget
            ),
            "multiplicity_policy": family.spec.multiplicity_policy,
            "outcome_informed_decision_budget": (
                family.spec.outcome_informed_decision_budget
            ),
        }
    )
    manifest = GenerationSealManifest(
        discovery_family_id=family.discovery_family_id,
        family_spec_hash=family.spec_hash,
        arm_spec_hashes=arm_hashes,
        llm_campaign_spec_hashes=llm_campaign_spec_hashes,
        candidate_slot_states=dict(state.candidate_slot_states),
        slot_object_hashes=dict(slot_object_hashes),
        prompt_bundle_hashes=prompt_bundle_hashes,
        model_identity=model_identity,
        generator_identity_hashes=dict(generator_identity_hashes),
        evaluation_dependency_intervals=evaluation_dependency_intervals,
        discovery_context_data_identity_hash=sha256_json(
            family.spec.discovery_context_data_identity.model_dump(mode="json")
        ),
        candidate_evaluation_data_identity_hash=sha256_json(
            family.spec.candidate_evaluation_data_identity.model_dump(mode="json")
        ),
        evaluation_relationship=family.spec.evaluation_relationship,
        evaluation_policy_id=family.spec.evaluation_policy_id,
        evaluation_policy_hash=evaluation_policy_hash,
        statistical_budget_hash=statistical_budget_hash,
        data_contract_identity_hash=data_contract_identity_hash,
        evolution_context_hash=(
            evolution_context.context_sha256
            if evolution_context is not None
            else None
        ),
        coverage_graph_manifest_hash=(
            evolution_context.coverage_graph_manifest_hash
            if evolution_context is not None
            else None
        ),
        memory_snapshot_hash=(
            evolution_context.memory_snapshot_hash
            if evolution_context is not None
            else None
        ),
        gap_report_hash=(
            evolution_context.gap_report_hash
            if evolution_context is not None
            else None
        ),
        approval_batch_hash=(
            approval_batch.approval_batch_sha256
            if approval_batch is not None
            else None
        ),
        design_policy_hash=(
            evolution_context.design_policy_hash
            if evolution_context is not None
            else None
        ),
        logical_hypothesis_ids_hash=(
            _logical_hypothesis_ids_hash(logical_hypothesis_ids)
            if logical_hypothesis_ids is not None
            else None
        ),
        global_statistical_trial_budget=(
            family.spec.global_statistical_trial_budget
        ),
    )
    manifest_hash = sha256_json(manifest.model_dump(mode="json"))
    return RegisteredGenerationSeal(
        generation_seal_id=f"llmseal_{manifest_hash[:24]}",
        manifest_sha256=manifest_hash,
        manifest=manifest,
    )


def verify_generation_seal(
    family: RegisteredLLMDiscoveryResearchFamily,
    state: LLMDiscoveryFamilyState,
    seal: RegisteredGenerationSeal,
    *,
    slot_object_hashes: Mapping[str, str] | None = None,
    slot_objects: Mapping[str, Mapping[str, object]] | None = None,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
) -> None:
    """核验 seal 与当前不可变 family 和槽位投影完全一致。"""

    if (
        seal.manifest.discovery_family_id != family.discovery_family_id
        or seal.manifest.family_spec_hash != family.spec_hash
        or seal.manifest.arm_spec_hashes != _arm_hashes(family)
        or dict(seal.manifest.candidate_slot_states)
        != dict(state.candidate_slot_states)
    ):
        raise _seal_error("generation seal 与当前 family 或槽位投影不一致")
    strict_fields = (
        seal.manifest.evolution_context_hash,
        seal.manifest.coverage_graph_manifest_hash,
        seal.manifest.memory_snapshot_hash,
        seal.manifest.gap_report_hash,
        seal.manifest.approval_batch_hash,
        seal.manifest.design_policy_hash,
        seal.manifest.logical_hypothesis_ids_hash,
    )
    if any(value is not None for value in strict_fields) and not all(
        value is not None for value in strict_fields
    ):
        raise _seal_error("generation seal 的 strict evolution 字段缺失")
    expected_policy_hash = sha256_json(
        {
            "evaluation_policy_id": family.spec.evaluation_policy_id,
            "policy_contract": family.spec.multiplicity_policy,
        }
    )
    expected_budget_hash = sha256_json(
        {
            "global_statistical_trial_budget": (
                family.spec.global_statistical_trial_budget
            ),
            "multiplicity_policy": family.spec.multiplicity_policy,
            "outcome_informed_decision_budget": (
                family.spec.outcome_informed_decision_budget
            ),
        }
    )
    expected_data_hash = sha256_json(
        {
            "discovery_context_data_identity": (
                family.spec.discovery_context_data_identity.model_dump(mode="json")
            ),
            "candidate_evaluation_data_identity": (
                family.spec.candidate_evaluation_data_identity.model_dump(mode="json")
            ),
            "evaluation_relationship": family.spec.evaluation_relationship,
        }
    )
    if (
        seal.manifest.evaluation_policy_hash != expected_policy_hash
        or seal.manifest.statistical_budget_hash != expected_budget_hash
        or seal.manifest.data_contract_identity_hash != expected_data_hash
        or seal.manifest.global_statistical_trial_budget != 120
    ):
        raise _seal_error("generation seal 的政策、统计预算或数据合同身份不一致")
    expected_slots = expected_candidate_slot_ids(family.spec)
    if (
        set(seal.manifest.candidate_slot_states) != set(expected_slots)
        or len(seal.manifest.candidate_slot_states) != 120
        or set(seal.manifest.slot_object_hashes) != set(expected_slots)
        or any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in seal.manifest.slot_object_hashes.values()
        )
        or seal.manifest.discovery_context_data_identity_hash
        != sha256_json(family.spec.discovery_context_data_identity.model_dump(mode="json"))
        or seal.manifest.candidate_evaluation_data_identity_hash
        != sha256_json(family.spec.candidate_evaluation_data_identity.model_dump(mode="json"))
    ):
        raise _seal_error("generation seal 的槽位对象或数据身份清单不完整")
    if seal.manifest.evaluation_dependency_intervals:
        validate_evaluation_relationship(
            family.spec.evaluation_relationship,
            family.spec.discovery_context_data_identity,
            seal.manifest.evaluation_dependency_intervals,
        )
    if slot_object_hashes is not None and dict(slot_object_hashes) != dict(
        seal.manifest.slot_object_hashes
    ):
        raise _seal_error("generation seal 与服务器本地槽对象 hash 不一致")
    logical_hypothesis_ids = _validate_evolution_seal_contract(
        state,
        approval_batch=approval_batch,
        evolution_context=evolution_context,
        slot_objects=slot_objects,
    )
    if logical_hypothesis_ids is None:
        strict_fields = (
            seal.manifest.evolution_context_hash,
            seal.manifest.coverage_graph_manifest_hash,
            seal.manifest.memory_snapshot_hash,
            seal.manifest.gap_report_hash,
            seal.manifest.approval_batch_hash,
            seal.manifest.design_policy_hash,
            seal.manifest.logical_hypothesis_ids_hash,
        )
        if any(value is not None for value in strict_fields):
            raise _seal_error("legacy seal 不得混入不完整的演化绑定字段")
        return
    if (
        seal.manifest.evolution_context_hash != evolution_context.context_sha256
        or seal.manifest.coverage_graph_manifest_hash
        != evolution_context.coverage_graph_manifest_hash
        or seal.manifest.memory_snapshot_hash
        != evolution_context.memory_snapshot_hash
        or seal.manifest.gap_report_hash != evolution_context.gap_report_hash
        or seal.manifest.approval_batch_hash
        != approval_batch.approval_batch_sha256
        or seal.manifest.design_policy_hash
        != evolution_context.design_policy_hash
        or seal.manifest.logical_hypothesis_ids_hash
        != _logical_hypothesis_ids_hash(logical_hypothesis_ids)
    ):
        raise _seal_error("generation seal 的演化上下文绑定不一致")


def authorize_evaluation_open(
    family: RegisteredLLMDiscoveryResearchFamily,
    projection: VerifiedDiscoveryProjection,
    seal: RegisteredGenerationSeal,
    outcome_opener: Callable[[], OutcomeValue],
) -> EvaluationOpenAuthorization:
    """按 seal、账本投影、时间边界顺序校验后才调用结果端口。"""

    if not isinstance(projection, VerifiedDiscoveryProjection):
        raise TypeError("必须传入 VerifiedDiscoveryProjection")
    if (
        projection.state.family_generation_state
        is not FamilyGenerationState.GENERATION_SEALED
    ):
        raise _seal_error("family 尚未处于 generation_sealed")
    verify_generation_seal(family, projection.state, seal)
    tier = validate_evaluation_relationship(
        family.spec.evaluation_relationship,
        family.spec.discovery_context_data_identity,
        seal.manifest.evaluation_dependency_intervals,
    )
    result = outcome_opener()
    return EvaluationOpenAuthorization(
        evidence_tier=tier,
        outcome_result=result,
    )
