"""轻量自主研究的十假设生成和逐条审批冻结。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.llm_online import AgentRole, PreparedDeepSeekRequest
from factor_miner.research_evolution import (
    LogicalEvolutionHypothesisDraft,
    parse_evolution_hypothesis_response,
)
from factor_miner.research_evolution_schema import ResearchEvolutionContext


_HASH = r"^[0-9a-f]{64}$"
_SLOTS = tuple(f"H{i:02d}" for i in range(1, 11))


class LightweightHypothesisProvider(Protocol):
    """Worker 使用的最小十假设 provider 端口。"""

    def generate(self, request: PreparedDeepSeekRequest) -> object:
        """返回带 record 和 content_json 的录制响应。"""


class LightweightHypothesisBatch(BaseModel):
    """通过固定 Schema 校验的十条中文假设批次。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "lightweight-hypothesis-batch-v1"
    run_id: str = Field(pattern=r"^autrun_[0-9a-f]{24}$")
    context_sha256: str = Field(pattern=_HASH)
    request_sha256: str = Field(pattern=_HASH)
    response_sha256: str = Field(pattern=_HASH)
    provider_call_id: str = Field(pattern=r"^llmcall_[0-9a-f]{24}$")
    hypotheses: tuple[LogicalEvolutionHypothesisDraft, ...] = Field(
        min_length=10,
        max_length=10,
    )
    batch_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_batch(self) -> LightweightHypothesisBatch:
        """要求 H01-H10 完整、有序且内容不重复。"""

        slots = tuple(item.logical_slot_id for item in self.hypotheses)
        if slots != _SLOTS:
            raise ValueError("轻量假设必须按顺序完整覆盖 H01-H10")
        hashes = tuple(str(item.draft_sha256) for item in self.hypotheses)
        if len(set(hashes)) != 10:
            raise ValueError("轻量假设批次包含重复内容")
        payload = self.model_dump(mode="json", exclude={"batch_sha256"})
        if self.batch_sha256 != sha256_json(payload):
            raise ValueError("轻量假设批次内容身份不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        context_sha256: str,
        request_sha256: str,
        response_sha256: str,
        provider_call_id: str,
        hypotheses: tuple[LogicalEvolutionHypothesisDraft, ...],
    ) -> LightweightHypothesisBatch:
        """构造内容寻址假设批次。"""

        payload = {
            "version": "lightweight-hypothesis-batch-v1",
            "run_id": run_id,
            "context_sha256": context_sha256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "provider_call_id": provider_call_id,
            "hypotheses": hypotheses,
        }
        draft = cls.model_construct(batch_sha256="0" * 64, **payload)
        digest = sha256_json(draft.model_dump(mode="json", exclude={"batch_sha256"}))
        return cls(**payload, batch_sha256=digest)


class LightweightHypothesisDecision(BaseModel):
    """Dashboard 对假设全文作出的不可变批准或拒绝决定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(pattern=r"^autrun_[0-9a-f]{24}$")
    context_sha256: str = Field(pattern=_HASH)
    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    draft_sha256: str = Field(pattern=_HASH)
    decision: Literal["approved", "rejected"]
    approval_role: str = Field(min_length=1)
    decided_at: datetime
    decision_sha256: str = Field(pattern=_HASH)

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        """审批时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at 必须带时区")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_identity(self) -> LightweightHypothesisDecision:
        """决定哈希必须绑定草案、上下文和审批角色。"""

        payload = self.model_dump(mode="json", exclude={"decision_sha256"})
        if self.decision_sha256 != sha256_json(payload):
            raise ValueError("假设决定内容身份不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        context_sha256: str,
        draft: LogicalEvolutionHypothesisDraft,
        decision: Literal["approved", "rejected"],
        approval_role: str,
        decided_at: datetime,
    ) -> LightweightHypothesisDecision:
        """从完整草案构造哈希绑定的决定。"""

        payload = {
            "run_id": run_id,
            "context_sha256": context_sha256,
            "logical_slot_id": draft.logical_slot_id,
            "draft_sha256": str(draft.draft_sha256),
            "decision": decision,
            "approval_role": approval_role,
            "decided_at": decided_at,
        }
        draft_model = cls.model_construct(decision_sha256="0" * 64, **payload)
        digest = sha256_json(
            draft_model.model_dump(mode="json", exclude={"decision_sha256"})
        )
        return cls(**payload, decision_sha256=digest)


class LightweightReviewBatch(BaseModel):
    """完整覆盖十假设且可含拒绝项的冻结审批集合。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "lightweight-review-batch-v1"
    run_id: str = Field(pattern=r"^autrun_[0-9a-f]{24}$")
    context_sha256: str = Field(pattern=_HASH)
    hypothesis_batch_sha256: str = Field(pattern=_HASH)
    decisions: tuple[LightweightHypothesisDecision, ...] = Field(
        min_length=10,
        max_length=10,
    )
    decision_count: int = Field(ge=0)
    approved_hypothesis_count: int = Field(ge=0)
    rejected_hypothesis_count: int = Field(ge=0)
    candidate_family_size: int = Field(ge=0)
    approved_slot_ids: tuple[str, ...]
    rejected_slot_ids: tuple[str, ...]
    review_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_review(self) -> LightweightReviewBatch:
        """十条决定、计数、顺序和内容身份必须一致。"""

        slots = tuple(item.logical_slot_id for item in self.decisions)
        if slots != _SLOTS:
            raise ValueError("轻量审批决定必须按顺序完整覆盖 H01-H10")
        approved = tuple(
            item.logical_slot_id for item in self.decisions if item.decision == "approved"
        )
        rejected = tuple(
            item.logical_slot_id for item in self.decisions if item.decision == "rejected"
        )
        if (
            self.decision_count != 10
            or self.approved_hypothesis_count != len(approved)
            or self.rejected_hypothesis_count != len(rejected)
            or self.candidate_family_size != len(approved) * 3
            or self.approved_slot_ids != approved
            or self.rejected_slot_ids != rejected
        ):
            raise ValueError("轻量审批计数与决定集合不一致")
        payload = self.model_dump(mode="json", exclude={"review_sha256"})
        if self.review_sha256 != sha256_json(payload):
            raise ValueError("轻量审批批次内容身份不一致")
        return self


def generate_lightweight_hypotheses(
    *,
    run_id: str,
    context: ResearchEvolutionContext,
    prepared: PreparedDeepSeekRequest,
    provider: LightweightHypothesisProvider,
) -> LightweightHypothesisBatch:
    """调用一次录制 provider，解析并冻结十条中文假设。"""

    if prepared.agent_role is not AgentRole.HYPOTHESIS:
        raise ValueError("轻量假设请求角色必须是 hypothesis")
    if prepared.slot_ids != _SLOTS:
        raise ValueError("轻量假设请求必须覆盖 H01-H10")
    response = provider.generate(prepared)
    record = getattr(response, "record", None)
    content_json = getattr(response, "content_json", None)
    if record is None or not isinstance(content_json, dict):
        raise ValueError("轻量假设 provider 没有返回有效录制 JSON")
    if getattr(record, "request_sha256", None) != prepared.request_sha256:
        raise ValueError("轻量假设录制请求身份不一致")
    hypotheses = parse_evolution_hypothesis_response(content_json, context=context)
    logical_hypotheses = tuple(
        LogicalEvolutionHypothesisDraft.model_validate(item.model_dump(mode="json"))
        for item in hypotheses
    )
    return LightweightHypothesisBatch.build(
        run_id=run_id,
        context_sha256=str(context.context_sha256),
        request_sha256=prepared.request_sha256,
        response_sha256=str(getattr(record, "response_sha256")),
        provider_call_id=str(getattr(record, "call_id")),
        hypotheses=logical_hypotheses,
    )


def freeze_lightweight_review(
    hypotheses: LightweightHypothesisBatch,
    decisions: tuple[LightweightHypothesisDecision, ...],
) -> LightweightReviewBatch:
    """核对十条决定与原草案后冻结动态候选预算。"""

    ordered = tuple(sorted(decisions, key=lambda item: item.logical_slot_id))
    if len(ordered) != 10 or tuple(item.logical_slot_id for item in ordered) != _SLOTS:
        raise ValueError("轻量审批决定必须完整覆盖 H01-H10")
    draft_by_slot = {item.logical_slot_id: item for item in hypotheses.hypotheses}
    for decision in ordered:
        draft = draft_by_slot[decision.logical_slot_id]
        if (
            decision.run_id != hypotheses.run_id
            or decision.context_sha256 != hypotheses.context_sha256
            or decision.draft_sha256 != draft.draft_sha256
        ):
            raise ValueError("轻量审批决定没有绑定原始假设全文")
    approved = tuple(
        item.logical_slot_id for item in ordered if item.decision == "approved"
    )
    rejected = tuple(
        item.logical_slot_id for item in ordered if item.decision == "rejected"
    )
    payload = {
        "version": "lightweight-review-batch-v1",
        "run_id": hypotheses.run_id,
        "context_sha256": hypotheses.context_sha256,
        "hypothesis_batch_sha256": hypotheses.batch_sha256,
        "decisions": ordered,
        "decision_count": 10,
        "approved_hypothesis_count": len(approved),
        "rejected_hypothesis_count": len(rejected),
        "candidate_family_size": len(approved) * 3,
        "approved_slot_ids": approved,
        "rejected_slot_ids": rejected,
    }
    draft = LightweightReviewBatch.model_construct(review_sha256="0" * 64, **payload)
    digest = sha256_json(draft.model_dump(mode="json", exclude={"review_sha256"}))
    return LightweightReviewBatch(**payload, review_sha256=digest)
