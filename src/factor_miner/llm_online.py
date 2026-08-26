"""V0.5 DeepSeek 固定请求、精确外发授权与调用记录合同。"""

from __future__ import annotations

import json

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import canonical_json_bytes
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_privacy import (
    CorporateExternalResearchPolicy,
    scan_export_payload,
)


DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT = (
    "https://api.deepseek.com/chat/completions"
)
DEEPSEEK_MODEL = "deepseek-v4-pro"
ROLE_MAX_TOKENS = {
    "hypothesis": 8_000,
    "expression": 12_000,
    "semantic_lint": 4_000,
    "format_repair": 4_000,
}


class AgentRole(StrEnum):
    """受控在线流程中的固定运行时角色。"""

    HYPOTHESIS = "hypothesis"
    EXPRESSION = "expression"
    SEMANTIC_LINT = "semantic_lint"
    FORMAT_REPAIR = "format_repair"


def _public_payload_matches_body(
    body: dict[str, Any],
    export_payload: dict[str, Any],
) -> bool:
    """校验审计中的公开 payload hash 是否绑定真实 user message。"""

    if "public_payload_sha256" not in export_payload:
        return True
    declared_hash = export_payload["public_payload_sha256"]
    if (
        not isinstance(declared_hash, str)
        or len(declared_hash) != 64
        or any(
            character not in "0123456789abcdef"
            for character in declared_hash
        )
    ):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return False
    user_message = messages[1]
    if (
        not isinstance(user_message, dict)
        or user_message.get("role") != "user"
    ):
        return False
    content = user_message.get("content")
    if not isinstance(content, str):
        return False
    try:
        public_payload = json.loads(content)
        if not isinstance(public_payload, dict):
            return False
        actual_hash = sha256(
            canonical_json_bytes(public_payload)
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    return declared_hash == actual_hash


class PreparedDeepSeekRequest(BaseModel):
    """已经确定内容身份、但尚未获准发送的 DeepSeek 请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str
    agent_role: AgentRole
    slot_ids: tuple[str, ...] = Field(min_length=1)
    endpoint: Literal[
        "https://api.deepseek.com/chat/completions"
    ] = DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT
    body: dict[str, Any]
    export_payload: dict[str, Any]
    audit_payload_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_content_identity(self) -> PreparedDeepSeekRequest:
        """请求哈希必须绑定即将发送的精确规范字节。"""

        expected = sha256(canonical_json_bytes(self.body)).hexdigest()
        if self.request_sha256 != expected:
            raise ValueError("DeepSeek 请求哈希与 body 不一致")
        if self.audit_payload_sha256 is not None:
            expected_audit = sha256(
                canonical_json_bytes(self.export_payload)
            ).hexdigest()
            if self.audit_payload_sha256 != expected_audit:
                raise ValueError("DeepSeek 审计 payload 哈希与内容不一致")
        if not _public_payload_matches_body(self.body, self.export_payload):
            raise ValueError("DeepSeek 公开 payload 哈希与 body user content 不一致")
        return self


class LLMExportAuthorization(BaseModel):
    """匿名人工角色对一个精确请求哈希的限时授权。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    campaign_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corporate_policy_id: str
    approver_role: str
    authorized_at: datetime
    expires_at: datetime

    @field_validator("authorized_at", "expires_at")
    @classmethod
    def validate_timezone(cls, value: datetime) -> datetime:
        """授权时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("授权时间必须带时区")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> LLMExportAuthorization:
        """授权有效期必须正向。"""

        if self.expires_at <= self.authorized_at:
            raise ValueError("授权失效时间必须晚于授权时间")
        return self


def _scope_identity_payload(
    *,
    family_id: str,
    corporate_policy_id: str,
    approver_role: str,
    allowed_agent_roles: tuple[AgentRole, ...],
    allowed_information_classes: tuple[str, ...],
    max_hypotheses: int,
    max_requests: int,
    authorized_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """构造个人试运行范围授权的内容身份。"""

    return {
        "family_id": family_id,
        "corporate_policy_id": corporate_policy_id,
        "approver_role": approver_role,
        "allowed_agent_roles": tuple(item.value for item in allowed_agent_roles),
        "allowed_information_classes": allowed_information_classes,
        "max_hypotheses": max_hypotheses,
        "max_requests": max_requests,
        "authorized_at": authorized_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


class LLMCampaignScopeAuthorization(BaseModel):
    """个人试运行对一个研究族的批量外发范围授权。

    它不批准任意请求。固定程序仍会逐次扫描 payload、校验角色、研究族、
    请求次数、模型和有效期；它只是把人工动作从逐请求哈希改成一次性批次范围。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(pattern=r"^llmscope_[0-9a-f]{24}$")
    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    corporate_policy_id: str
    approver_role: str
    allowed_agent_roles: tuple[AgentRole, ...] = Field(min_length=1)
    allowed_information_classes: tuple[str, ...] = Field(min_length=1)
    max_hypotheses: int = Field(ge=1, le=20)
    max_requests: int = Field(ge=1, le=100)
    authorized_at: datetime
    expires_at: datetime
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("authorized_at", "expires_at")
    @classmethod
    def validate_scope_timezone(cls, value: datetime) -> datetime:
        """范围授权时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("范围授权时间必须带时区")
        return value

    @field_validator("allowed_agent_roles")
    @classmethod
    def validate_scope_roles(
        cls,
        values: tuple[AgentRole, ...],
    ) -> tuple[AgentRole, ...]:
        """角色集合必须唯一且稳定排序。"""

        if tuple(sorted(set(values), key=lambda item: item.value)) != values:
            raise ValueError("allowed_agent_roles 必须唯一且按字典序排列")
        return values

    @field_validator("allowed_information_classes")
    @classmethod
    def validate_scope_information_classes(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """信息类别集合必须唯一且稳定排序。"""

        if tuple(sorted(set(values))) != values:
            raise ValueError(
                "allowed_information_classes 必须唯一且按字典序排列"
            )
        return values

    @model_validator(mode="after")
    def validate_scope_identity(self) -> LLMCampaignScopeAuthorization:
        """范围授权必须绑定不可变内容身份和正向有效期。"""

        if self.expires_at <= self.authorized_at:
            raise ValueError("范围授权失效时间必须晚于授权时间")
        payload = _scope_identity_payload(
            family_id=self.family_id,
            corporate_policy_id=self.corporate_policy_id,
            approver_role=self.approver_role,
            allowed_agent_roles=self.allowed_agent_roles,
            allowed_information_classes=self.allowed_information_classes,
            max_hypotheses=self.max_hypotheses,
            max_requests=self.max_requests,
            authorized_at=self.authorized_at,
            expires_at=self.expires_at,
        )
        expected = sha256(canonical_json_bytes(payload)).hexdigest()
        if self.scope_sha256 != expected:
            raise ValueError("范围授权哈希与内容不一致")
        if self.authorization_id != f"llmscope_{expected[:24]}":
            raise ValueError("范围授权 ID 与内容不一致")
        return self


def registered_llm_campaign_scope_authorization(
    *,
    family_id: str,
    corporate_policy_id: str,
    approver_role: str,
    allowed_agent_roles: tuple[AgentRole, ...],
    allowed_information_classes: tuple[str, ...],
    max_hypotheses: int,
    max_requests: int,
    authorized_at: datetime,
    expires_at: datetime,
) -> LLMCampaignScopeAuthorization:
    """生成一次性个人试运行范围授权。"""

    payload = _scope_identity_payload(
        family_id=family_id,
        corporate_policy_id=corporate_policy_id,
        approver_role=approver_role,
        allowed_agent_roles=allowed_agent_roles,
        allowed_information_classes=allowed_information_classes,
        max_hypotheses=max_hypotheses,
        max_requests=max_requests,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )
    scope_hash = sha256(canonical_json_bytes(payload)).hexdigest()
    return LLMCampaignScopeAuthorization(
        authorization_id=f"llmscope_{scope_hash[:24]}",
        scope_sha256=scope_hash,
        **payload,
    )


def _evolution_authorization_identity_payload(
    *,
    discovery_family_id: str,
    context_sha256: str,
    gap_report_hash: str,
    memory_snapshot_hash: str,
    brief_sha256: str,
    corporate_policy_id: str,
    approver_role: str,
    allowed_agent_role: AgentRole,
    authorized_model: str,
    authorized_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """构造演化假设请求授权的内容身份。"""

    return {
        "discovery_family_id": discovery_family_id,
        "context_sha256": context_sha256,
        "gap_report_hash": gap_report_hash,
        "memory_snapshot_hash": memory_snapshot_hash,
        "brief_sha256": brief_sha256,
        "corporate_policy_id": corporate_policy_id,
        "approver_role": approver_role,
        "allowed_agent_role": allowed_agent_role.value,
        "authorized_model": authorized_model,
        "authorized_at": authorized_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


class LLMEvolutionRequestAuthorization(BaseModel):
    """绑定演化 context/gap/memory/brief hash 的单次请求授权。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(pattern=r"^llmevoauth_[0-9a-f]{24}$")
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gap_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corporate_policy_id: str
    approver_role: str
    allowed_agent_role: AgentRole
    authorized_model: str = DEEPSEEK_MODEL
    authorized_at: datetime
    expires_at: datetime
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("authorized_at", "expires_at")
    @classmethod
    def validate_evolution_timezone(cls, value: datetime) -> datetime:
        """演化授权时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("演化授权时间必须带时区")
        return value

    @model_validator(mode="after")
    def validate_evolution_identity(self) -> LLMEvolutionRequestAuthorization:
        """演化授权必须绑定 hash、角色、模型和正向有效期。"""

        if self.authorized_model != DEEPSEEK_MODEL:
            raise ValueError("演化授权模型必须固定为官方 DeepSeek 模型")
        if self.expires_at <= self.authorized_at:
            raise ValueError("演化授权失效时间必须晚于授权时间")
        payload = _evolution_authorization_identity_payload(
            discovery_family_id=self.discovery_family_id,
            context_sha256=self.context_sha256,
            gap_report_hash=self.gap_report_hash,
            memory_snapshot_hash=self.memory_snapshot_hash,
            brief_sha256=self.brief_sha256,
            corporate_policy_id=self.corporate_policy_id,
            approver_role=self.approver_role,
            allowed_agent_role=self.allowed_agent_role,
            authorized_model=self.authorized_model,
            authorized_at=self.authorized_at,
            expires_at=self.expires_at,
        )
        expected = sha256(canonical_json_bytes(payload)).hexdigest()
        if self.authorization_sha256 != expected:
            raise ValueError("演化授权哈希与内容不一致")
        if self.authorization_id != f"llmevoauth_{expected[:24]}":
            raise ValueError("演化授权 ID 与内容不一致")
        return self


def registered_llm_evolution_request_authorization(
    *,
    discovery_family_id: str,
    context_sha256: str,
    gap_report_hash: str,
    memory_snapshot_hash: str,
    brief_sha256: str,
    corporate_policy_id: str,
    approver_role: str,
    allowed_agent_role: AgentRole,
    authorized_at: datetime,
    expires_at: datetime,
) -> LLMEvolutionRequestAuthorization:
    """生成绑定演化 hash 的单次授权。"""

    payload = _evolution_authorization_identity_payload(
        discovery_family_id=discovery_family_id,
        context_sha256=context_sha256,
        gap_report_hash=gap_report_hash,
        memory_snapshot_hash=memory_snapshot_hash,
        brief_sha256=brief_sha256,
        corporate_policy_id=corporate_policy_id,
        approver_role=approver_role,
        allowed_agent_role=allowed_agent_role,
        authorized_model=DEEPSEEK_MODEL,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )
    authorization_hash = sha256(canonical_json_bytes(payload)).hexdigest()
    return LLMEvolutionRequestAuthorization(
        authorization_id=f"llmevoauth_{authorization_hash[:24]}",
        authorization_sha256=authorization_hash,
        **payload,
    )


def _validate_request_inputs(
    *,
    campaign_id: str,
    slot_ids: tuple[str, ...],
    system_prompt: str,
) -> None:
    """拒绝空身份、重复槽和未明确要求 JSON 的提示。"""

    if not campaign_id.strip():
        raise ValueError("campaign_id 不能为空")
    if not slot_ids or any(not item.strip() for item in slot_ids):
        raise ValueError("slot_ids 不能为空")
    if len(set(slot_ids)) != len(slot_ids):
        raise ValueError("slot_ids 不能重复")
    if "json" not in system_prompt.casefold():
        raise ValueError("system prompt 必须明确要求 JSON object")


def build_deepseek_request(
    *,
    campaign_id: str,
    agent_role: AgentRole,
    slot_ids: tuple[str, ...],
    system_prompt: str,
    user_payload: dict[str, object],
    tools: tuple[dict[str, object], ...] = (),
    audit_payload: dict[str, object] | None = None,
    thinking: Literal["enabled", "disabled"] = "enabled",
) -> PreparedDeepSeekRequest:
    """构造端点、模型、推理参数和预算均不可覆盖的规范请求。"""

    _validate_request_inputs(
        campaign_id=campaign_id,
        slot_ids=slot_ids,
        system_prompt=system_prompt,
    )
    if tools and agent_role is not AgentRole.HYPOTHESIS:
        raise ValueError("只有假设代理可以获得文献工具")
    if agent_role is AgentRole.HYPOTHESIS and tools:
        names = tuple(
            item.get("function", {}).get("name")
            for item in tools
            if isinstance(item.get("function"), dict)
        )
        if names != ("search_literature",):
            raise ValueError("假设代理只允许 search_literature 工具")

    body: dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": canonical_json_bytes(user_payload).decode("utf-8"),
            },
        ],
        "thinking": {"type": thinking},
        "stream": False,
        "response_format": {"type": "json_object"},
        "max_tokens": ROLE_MAX_TOKENS[agent_role.value],
    }
    if thinking == "enabled":
        body["reasoning_effort"] = "high"
    if tools:
        body["tools"] = list(tools)
    request_hash = sha256(canonical_json_bytes(body)).hexdigest()
    stored_audit_payload = (
        user_payload if audit_payload is None else audit_payload
    )
    audit_hash = sha256(
        canonical_json_bytes(stored_audit_payload)
    ).hexdigest()
    return PreparedDeepSeekRequest(
        campaign_id=campaign_id,
        agent_role=agent_role,
        slot_ids=slot_ids,
        body=body,
        export_payload=stored_audit_payload,
        audit_payload_sha256=audit_hash,
        request_sha256=request_hash,
    )


def verify_export_authorization(
    request: PreparedDeepSeekRequest,
    policy: CorporateExternalResearchPolicy,
    authorization: LLMExportAuthorization,
    now: datetime,
) -> bytes:
    """核验政策、精确请求哈希和有效期后返回可发送规范字节。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("核验时间必须带时区")
    scan_export_payload(request.export_payload, policy)
    authorized = (
        policy.provider == "deepseek"
        and policy.allowed_endpoint == request.endpoint
        and request.body["model"] in policy.allowed_models
        and policy.valid_from <= now < policy.valid_until
        and authorization.campaign_id == request.campaign_id
        and authorization.request_sha256 == request.request_sha256
        and authorization.corporate_policy_id == policy.policy_id
        and authorization.approver_role == policy.approver_role
        and authorization.authorized_at <= now < authorization.expires_at
    )
    if not authorized:
        raise FactorMinerError(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "请求内容、公司政策或人工授权不匹配",
        )
    return canonical_json_bytes(request.body)


def verify_scope_export_authorization(
    request: PreparedDeepSeekRequest,
    policy: CorporateExternalResearchPolicy,
    authorization: LLMCampaignScopeAuthorization,
    now: datetime,
    *,
    request_count: int = 1,
) -> bytes:
    """核验个人试运行范围后返回本次规范请求字节。

    范围授权放宽的是人工操作频率，不放宽固定程序的 payload 扫描和请求边界。
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("核验时间必须带时区")
    if request_count < 1:
        raise ValueError("范围授权请求序号必须从 1 开始")
    payload_classes = set()
    if isinstance(request.export_payload, dict):
        raw_class = request.export_payload.get("information_class")
        if isinstance(raw_class, str) and raw_class.strip():
            payload_classes.add(raw_class)
    scan_export_payload(request.export_payload, policy)
    family_prefix = f"{authorization.family_id}:"
    authorized = (
        policy.provider == "deepseek"
        and policy.allowed_endpoint == request.endpoint
        and request.body["model"] in policy.allowed_models
        and policy.valid_from <= now < policy.valid_until
        and authorization.corporate_policy_id == policy.policy_id
        and authorization.approver_role == policy.approver_role
        and request.campaign_id.startswith(family_prefix)
        and request.agent_role in authorization.allowed_agent_roles
        and payload_classes.issubset(
            set(authorization.allowed_information_classes)
        )
        and authorization.authorized_at <= now < authorization.expires_at
        and request_count <= authorization.max_requests
    )
    if not authorized:
        raise FactorMinerError(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "研究族范围授权、公司政策或本次请求不匹配",
        )
    return canonical_json_bytes(request.body)


def verify_evolution_export_authorization(
    request: PreparedDeepSeekRequest,
    policy: CorporateExternalResearchPolicy,
    authorization: LLMEvolutionRequestAuthorization,
    now: datetime,
) -> bytes:
    """核验演化请求的 context/gap/memory/brief 绑定后返回规范字节。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("核验时间必须带时区")
    scan_export_payload(request.export_payload, policy)
    if not _public_payload_matches_body(request.body, request.export_payload):
        raise FactorMinerError(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "演化请求授权、公司政策或请求绑定内容不匹配",
        )
    binding = (
        request.export_payload.get("evolution_binding")
        if isinstance(request.export_payload, dict)
        else None
    )
    model_scope = (
        request.export_payload.get("model_scope")
        if isinstance(request.export_payload, dict)
        else None
    )
    authorized = (
        policy.provider == "deepseek"
        and policy.allowed_endpoint == request.endpoint
        and request.body["model"] == authorization.authorized_model
        and request.body["model"] in policy.allowed_models
        and policy.valid_from <= now < policy.valid_until
        and authorization.corporate_policy_id == policy.policy_id
        and authorization.approver_role == policy.approver_role
        and authorization.authorized_at <= now < authorization.expires_at
        and request.agent_role is authorization.allowed_agent_role
        and request.campaign_id
        == f"{authorization.discovery_family_id}:evolution_hypothesis"
        and binding
        == {
            "discovery_family_id": authorization.discovery_family_id,
            "context_sha256": authorization.context_sha256,
            "gap_report_hash": authorization.gap_report_hash,
            "memory_snapshot_hash": authorization.memory_snapshot_hash,
            "brief_sha256": authorization.brief_sha256,
            "authorization_scope_sha256": authorization.authorization_sha256,
        }
        and model_scope
        == {
            "agent_role": authorization.allowed_agent_role.value,
            "tools_allowed": False,
        }
    )
    if not authorized:
        raise FactorMinerError(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "演化请求授权、公司政策或请求绑定内容不匹配",
        )
    return canonical_json_bytes(request.body)


def build_deepseek_continuation(
    *,
    root_request: PreparedDeepSeekRequest,
    assistant_message: dict[str, Any],
    tool_messages: tuple[dict[str, Any], ...],
    export_payload: dict[str, object],
) -> PreparedDeepSeekRequest:
    """为代理 1 的受限工具结果构造新的、需单独授权的精确请求。"""

    if root_request.agent_role is not AgentRole.HYPOTHESIS:
        raise ValueError("只有假设代理可以构造工具 continuation")
    if not tool_messages or any(
        message.get("role") != "tool" for message in tool_messages
    ):
        raise ValueError("continuation 必须包含合法 tool message")
    body = dict(root_request.body)
    body["messages"] = [
        *root_request.body["messages"],
        assistant_message,
        *tool_messages,
    ]
    return PreparedDeepSeekRequest(
        campaign_id=root_request.campaign_id,
        agent_role=root_request.agent_role,
        slot_ids=root_request.slot_ids,
        body=body,
        export_payload=export_payload,
        request_sha256=sha256(canonical_json_bytes(body)).hexdigest(),
    )
