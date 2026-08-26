"""V0.5 对象级状态机与发现研究族确定性投影。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_schema import (
    LLMDiscoveryResearchFamilySpec,
    RegisteredLLMDiscoveryResearchFamily,
)

EXPECTED_LOGICAL_HYPOTHESIS_SLOT_IDS = tuple(
    f"H{index:02d}" for index in range(1, 11)
)
EXPECTED_CANDIDATE_SLOT_COUNT = 120


class FamilyGenerationState(StrEnum):
    """发现研究族从登记到评价完成的唯一顺序。"""

    REGISTERED = "registered"
    GENERATING = "generating"
    GENERATION_SEALED = "generation_sealed"
    EVALUATION_OPEN = "evaluation_open"
    EVALUATED = "evaluated"


class CampaignOperationalState(StrEnum):
    """LLM campaign 的运行状态，不混入质量判断。"""

    REGISTERED = "registered"
    BRIEF_VERIFIED = "brief_verified"
    EXPORT_AUTHORIZED = "export_authorized"
    IN_PROGRESS = "in_progress"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    PACKAGED_WITH_PARTIAL_RESULTS = "packaged_with_partial_results"
    OPERATIONALLY_COMPLETED = "operationally_completed"
    OPERATIONALLY_FAILED = "operationally_failed"


class CampaignQualityAssessment(StrEnum):
    """与运行状态正交的生成质量标签。"""

    NOT_ASSESSED = "not_assessed"
    PASSED = "passed"
    FAILED = "failed"


class HypothesisSlotState(StrEnum):
    """固定十槽假设的生命周期。"""

    RESERVED = "reserved"
    GENERATION_IN_PROGRESS = "generation_in_progress"
    DRAFT_GENERATED = "draft_generated"
    EMPTY = "empty"
    SOURCE_UNVERIFIED = "source_unverified"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    HUMAN_REJECTED = "human_rejected"
    HUMAN_APPROVED = "human_approved"
    FROZEN = "frozen"
    GENERATION_FAILED = "generation_failed"
    NOT_EXECUTED = "not_executed"


class CandidateSlotState(StrEnum):
    """固定 120 个候选槽的生成和固定校验生命周期。"""

    RESERVED = "reserved"
    GENERATION_IN_PROGRESS = "generation_in_progress"
    DRAFT_GENERATED = "draft_generated"
    SCHEMA_FAILED = "schema_failed"
    SEMANTIC_TYPE_FAILED = "semantic_type_failed"
    AVAILABILITY_FAILED = "availability_failed"
    DSL_FAILED = "dsl_failed"
    LOOKAHEAD_FAILED = "lookahead_failed"
    DUPLICATE_FAILED = "duplicate_failed"
    COMPILE_FAILED = "compile_failed"
    SEMANTIC_LINT_REJECTED = "semantic_lint_rejected"
    GENERATION_FAILED = "generation_failed"
    DESIGN_DIVERSITY_FAILED = "design_diversity_failed"
    NOT_EXECUTED_HYPOTHESIS_REJECTED = "not_executed_hypothesis_rejected"
    NOT_EXECUTED_INFRASTRUCTURE_TERMINAL = "not_executed_infrastructure_terminal"
    READY_FOR_REGISTRATION = "ready_for_registration"


class LogicalCallState(StrEnum):
    """外部逻辑调用的离线状态合同。"""

    RESERVED = "reserved"
    REQUEST_PERSISTED = "request_persisted"
    IN_FLIGHT = "in_flight"
    TRANSPORT_RETRYABLE = "transport_retryable"
    REPAIRABLE = "repairable"
    RESPONSE_PERSISTED = "response_persisted"
    VALIDATED = "validated"
    TERMINAL_FAILED = "terminal_failed"


class LiteratureQueryState(StrEnum):
    """文献查询与人工来源核验状态。"""

    RESERVED = "reserved"
    IN_FLIGHT = "in_flight"
    IDENTITY_VERIFIED = "identity_verified"
    ABSTRACT_CONSISTENT = "abstract_consistent"
    PASSAGE_VERIFIED = "passage_verified"
    CLAIM_SUPPORT_VERIFIED = "claim_support_verified"
    SOURCE_UNVERIFIED = "source_unverified"
    TRANSPORT_RETRYABLE = "transport_retryable"
    TERMINAL_FAILED = "terminal_failed"


class DiscoveryObjectKind(StrEnum):
    """发现事件能够修改的对象类型。"""

    FAMILY = "family"
    CAMPAIGN_OPERATIONAL = "campaign_operational"
    CAMPAIGN_QUALITY = "campaign_quality"
    HYPOTHESIS_SLOT = "hypothesis_slot"
    CANDIDATE_SLOT = "candidate_slot"


FAMILY_EDGES = {
    FamilyGenerationState.REGISTERED: frozenset({FamilyGenerationState.GENERATING}),
    FamilyGenerationState.GENERATING: frozenset(
        {FamilyGenerationState.GENERATION_SEALED}
    ),
    FamilyGenerationState.GENERATION_SEALED: frozenset(
        {FamilyGenerationState.EVALUATION_OPEN}
    ),
    FamilyGenerationState.EVALUATION_OPEN: frozenset(
        {FamilyGenerationState.EVALUATED}
    ),
    FamilyGenerationState.EVALUATED: frozenset(),
}
CAMPAIGN_EDGES = {
    CampaignOperationalState.REGISTERED: frozenset(
        {CampaignOperationalState.BRIEF_VERIFIED}
    ),
    CampaignOperationalState.BRIEF_VERIFIED: frozenset(
        {
            CampaignOperationalState.EXPORT_AUTHORIZED,
            CampaignOperationalState.OPERATIONALLY_FAILED,
        }
    ),
    CampaignOperationalState.EXPORT_AUTHORIZED: frozenset(
        {
            CampaignOperationalState.IN_PROGRESS,
            CampaignOperationalState.OPERATIONALLY_FAILED,
        }
    ),
    CampaignOperationalState.IN_PROGRESS: frozenset(
        {
            CampaignOperationalState.AWAITING_HUMAN_REVIEW,
            CampaignOperationalState.PACKAGED_WITH_PARTIAL_RESULTS,
            CampaignOperationalState.OPERATIONALLY_COMPLETED,
            CampaignOperationalState.OPERATIONALLY_FAILED,
        }
    ),
    CampaignOperationalState.AWAITING_HUMAN_REVIEW: frozenset(
        {
            CampaignOperationalState.IN_PROGRESS,
            CampaignOperationalState.PACKAGED_WITH_PARTIAL_RESULTS,
            CampaignOperationalState.OPERATIONALLY_FAILED,
        }
    ),
    CampaignOperationalState.PACKAGED_WITH_PARTIAL_RESULTS: frozenset(
        {CampaignOperationalState.OPERATIONALLY_COMPLETED}
    ),
    CampaignOperationalState.OPERATIONALLY_COMPLETED: frozenset(),
    CampaignOperationalState.OPERATIONALLY_FAILED: frozenset(),
}
QUALITY_EDGES = {
    CampaignQualityAssessment.NOT_ASSESSED: frozenset(
        {CampaignQualityAssessment.PASSED, CampaignQualityAssessment.FAILED}
    ),
    CampaignQualityAssessment.PASSED: frozenset(),
    CampaignQualityAssessment.FAILED: frozenset(),
}
HYPOTHESIS_EDGES = {
    HypothesisSlotState.RESERVED: frozenset(
        {
            HypothesisSlotState.GENERATION_IN_PROGRESS,
            HypothesisSlotState.NOT_EXECUTED,
        }
    ),
    HypothesisSlotState.GENERATION_IN_PROGRESS: frozenset(
        {
            HypothesisSlotState.DRAFT_GENERATED,
            HypothesisSlotState.EMPTY,
            HypothesisSlotState.GENERATION_FAILED,
        }
    ),
    HypothesisSlotState.DRAFT_GENERATED: frozenset(
        {
            HypothesisSlotState.SOURCE_UNVERIFIED,
            HypothesisSlotState.AWAITING_HUMAN_REVIEW,
        }
    ),
    HypothesisSlotState.AWAITING_HUMAN_REVIEW: frozenset(
        {
            HypothesisSlotState.HUMAN_REJECTED,
            HypothesisSlotState.HUMAN_APPROVED,
        }
    ),
    HypothesisSlotState.HUMAN_APPROVED: frozenset({HypothesisSlotState.FROZEN}),
    HypothesisSlotState.EMPTY: frozenset(),
    HypothesisSlotState.SOURCE_UNVERIFIED: frozenset(),
    HypothesisSlotState.HUMAN_REJECTED: frozenset(),
    HypothesisSlotState.FROZEN: frozenset(),
    HypothesisSlotState.GENERATION_FAILED: frozenset(),
    HypothesisSlotState.NOT_EXECUTED: frozenset(),
}
CANDIDATE_TERMINALS = frozenset(
    {
        CandidateSlotState.SCHEMA_FAILED,
        CandidateSlotState.SEMANTIC_TYPE_FAILED,
        CandidateSlotState.AVAILABILITY_FAILED,
        CandidateSlotState.DSL_FAILED,
        CandidateSlotState.LOOKAHEAD_FAILED,
        CandidateSlotState.DUPLICATE_FAILED,
        CandidateSlotState.COMPILE_FAILED,
        CandidateSlotState.SEMANTIC_LINT_REJECTED,
        CandidateSlotState.GENERATION_FAILED,
        CandidateSlotState.DESIGN_DIVERSITY_FAILED,
        CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
        CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
        CandidateSlotState.READY_FOR_REGISTRATION,
    }
)
CANDIDATE_EDGES = {
    CandidateSlotState.RESERVED: frozenset(
        {
            CandidateSlotState.GENERATION_IN_PROGRESS,
            CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
            CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
        }
    ),
    CandidateSlotState.GENERATION_IN_PROGRESS: frozenset(
        {
            CandidateSlotState.DRAFT_GENERATED,
            CandidateSlotState.GENERATION_FAILED,
            CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
        }
    ),
    CandidateSlotState.DRAFT_GENERATED: CANDIDATE_TERMINALS.difference(
        {
            CandidateSlotState.GENERATION_FAILED,
            CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
            CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL,
        }
    ),
    **{state: frozenset() for state in CANDIDATE_TERMINALS},
}


class LLMDiscoveryEvent(BaseModel):
    """尚未由账本补全哈希链字段的领域状态事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    target_kind: DiscoveryObjectKind
    target_id: str
    to_state: str
    created_at: datetime
    supersedes_event_id: str | None = None
    sequence: int = Field(default=0, ge=0)
    previous_event_hash: str | None = None
    event_hash: str | None = None

    @field_validator("event_id", "target_id", "to_state")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """事件目标和状态不得为空。"""

        if not value.strip():
            raise ValueError("发现事件文本不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """事件时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value


class LLMDiscoveryFamilyState(BaseModel):
    """由已验证事件序列唯一投影的发现研究族状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_family_id: str
    family_generation_state: FamilyGenerationState
    candidate_slot_states: Mapping[str, CandidateSlotState]
    hypothesis_slot_states: Mapping[str, HypothesisSlotState]
    campaign_operational_states: Mapping[str, CampaignOperationalState]
    campaign_quality_assessments: Mapping[str, CampaignQualityAssessment]
    applied_event_ids: tuple[str, ...]
    family_closed_at: datetime | None = None


def expected_candidate_slot_ids(
    spec: LLMDiscoveryResearchFamilySpec,
) -> tuple[str, ...]:
    """从冻结 arm 顺序生成恰好 120 个候选槽 ID。"""

    return tuple(
        f"{arm.arm_id}:{index:03d}"
        for arm in spec.arm_specs
        for index in range(1, arm.candidate_slots + 1)
    )


def expected_logical_hypothesis_slot_ids() -> tuple[str, ...]:
    """返回严格演化路径固定的 H01-H10 逻辑槽顺序。"""

    return EXPECTED_LOGICAL_HYPOTHESIS_SLOT_IDS


def logical_hypothesis_slot_id(candidate_slot_id: str) -> str:
    """把固定 001-030 槽位映射回逻辑假设 H01-H10。"""

    _, raw_number = candidate_slot_id.split(":", 1)
    number = int(raw_number)
    hypothesis_number = ((number - 1) // 3) + 1
    return f"H{hypothesis_number:02d}"


def logical_candidate_design_slot_id(candidate_slot_id: str) -> str:
    """把固定 001-030 槽位映射回组内 C001-C003。"""

    _, raw_number = candidate_slot_id.split(":", 1)
    number = int(raw_number)
    design_number = ((number - 1) % 3) + 1
    return f"C{design_number:03d}"


def initial_discovery_family_state(
    family: RegisteredLLMDiscoveryResearchFamily,
) -> LLMDiscoveryFamilyState:
    """创建不读取外部状态的初始投影。"""

    candidates = {
        slot_id: CandidateSlotState.RESERVED
        for slot_id in expected_candidate_slot_ids(family.spec)
    }
    hypotheses = {
        f"{arm_id}:H{index:02d}": HypothesisSlotState.RESERVED
        for arm_id in ("coverage_outcome_llm", "literature_only_llm")
        for index in range(1, 11)
    }
    campaigns = {
        arm_id: CampaignOperationalState.REGISTERED
        for arm_id in ("coverage_outcome_llm", "literature_only_llm")
    }
    qualities = {
        arm_id: CampaignQualityAssessment.NOT_ASSESSED
        for arm_id in campaigns
    }
    return LLMDiscoveryFamilyState(
        discovery_family_id=family.discovery_family_id,
        family_generation_state=FamilyGenerationState.REGISTERED,
        candidate_slot_states=candidates,
        hypothesis_slot_states=hypotheses,
        campaign_operational_states=campaigns,
        campaign_quality_assessments=qualities,
        applied_event_ids=(),
    )


def _transition_error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.LLM_STATE_TRANSITION_INVALID, message)


def _next_state(
    current: StrEnum,
    requested: str,
    enum_type: type[StrEnum],
    edges: Mapping[StrEnum, frozenset[StrEnum]],
) -> StrEnum:
    """解析并核验一条显式允许边。"""

    try:
        target = enum_type(requested)
    except ValueError as error:
        raise _transition_error(f"未知目标状态：{requested}") from error
    if target not in edges[current]:
        suffix = "；当前状态是终态" if not edges[current] else ""
        raise _transition_error(
            f"不允许从 {current.value} 转到 {target.value}{suffix}"
        )
    return target


def apply_discovery_event(
    state: LLMDiscoveryFamilyState,
    event: LLMDiscoveryEvent,
) -> LLMDiscoveryFamilyState:
    """纯函数应用一个事件并拒绝未知对象、重复事件和状态逆转。"""

    if event.discovery_family_id != state.discovery_family_id:
        raise _transition_error("事件 discovery_family_id 与投影不一致")
    if event.event_id in state.applied_event_ids:
        raise _transition_error(f"重复 event_id：{event.event_id}")
    if event.supersedes_event_id is not None:
        if event.supersedes_event_id not in state.applied_event_ids:
            raise _transition_error("纠错事件没有指向已应用事件")
        raise _transition_error("纠错事件不能改变研究状态，只能更正非状态元数据")
    update: dict[str, object] = {
        "applied_event_ids": state.applied_event_ids + (event.event_id,)
    }
    if event.target_kind is DiscoveryObjectKind.FAMILY:
        if event.target_id != state.discovery_family_id:
            raise _transition_error("family 事件目标不存在")
        target = _next_state(
            state.family_generation_state,
            event.to_state,
            FamilyGenerationState,
            FAMILY_EDGES,
        )
        update["family_generation_state"] = target
        if target is FamilyGenerationState.GENERATION_SEALED:
            update["family_closed_at"] = event.created_at
    elif event.target_kind is DiscoveryObjectKind.CANDIDATE_SLOT:
        values = dict(state.candidate_slot_states)
        if event.target_id not in values:
            raise _transition_error(f"候选槽不存在：{event.target_id}")
        values[event.target_id] = _next_state(
            values[event.target_id],
            event.to_state,
            CandidateSlotState,
            CANDIDATE_EDGES,
        )
        update["candidate_slot_states"] = values
    elif event.target_kind is DiscoveryObjectKind.HYPOTHESIS_SLOT:
        values = dict(state.hypothesis_slot_states)
        if event.target_id not in values:
            raise _transition_error(f"假设槽不存在：{event.target_id}")
        values[event.target_id] = _next_state(
            values[event.target_id],
            event.to_state,
            HypothesisSlotState,
            HYPOTHESIS_EDGES,
        )
        update["hypothesis_slot_states"] = values
    elif event.target_kind is DiscoveryObjectKind.CAMPAIGN_OPERATIONAL:
        values = dict(state.campaign_operational_states)
        if event.target_id not in values:
            raise _transition_error(f"LLM campaign 不存在：{event.target_id}")
        values[event.target_id] = _next_state(
            values[event.target_id],
            event.to_state,
            CampaignOperationalState,
            CAMPAIGN_EDGES,
        )
        update["campaign_operational_states"] = values
    elif event.target_kind is DiscoveryObjectKind.CAMPAIGN_QUALITY:
        values = dict(state.campaign_quality_assessments)
        if event.target_id not in values:
            raise _transition_error(f"LLM campaign 不存在：{event.target_id}")
        values[event.target_id] = _next_state(
            values[event.target_id],
            event.to_state,
            CampaignQualityAssessment,
            QUALITY_EDGES,
        )
        update["campaign_quality_assessments"] = values
    return state.model_copy(update=update)


def validate_transition(
    current: LLMDiscoveryFamilyState,
    event: LLMDiscoveryEvent,
) -> None:
    """在事件落盘前核验当前投影与下一状态。

    这是账本写入边界使用的纯预检入口。它故意不返回新投影，避免调用者
    把尚未进入哈希链的内存状态误当成已提交状态。
    """

    if (
        event.target_kind is DiscoveryObjectKind.FAMILY
        and event.to_state == FamilyGenerationState.GENERATION_SEALED.value
    ):
        if len(current.candidate_slot_states) != EXPECTED_CANDIDATE_SLOT_COUNT:
            raise _transition_error("发现研究族候选槽数量不是冻结的 120，不能追加 generation_sealed")
        if any(
            state not in CANDIDATE_TERMINALS
            for state in current.candidate_slot_states.values()
        ):
            raise _transition_error("120 个候选槽未全部进入终态，不能追加 generation_sealed")
    apply_discovery_event(current, event)


def project_discovery_family_state(
    family: RegisteredLLMDiscoveryResearchFamily,
    events: tuple[LLMDiscoveryEvent, ...],
) -> LLMDiscoveryFamilyState:
    """按给定顺序重放事件并得到确定性状态。"""

    state = initial_discovery_family_state(family)
    for event in events:
        state = apply_discovery_event(state, event)
    return state
