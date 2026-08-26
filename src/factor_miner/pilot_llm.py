"""阶段 B：DeepSeek 假设与表达式来源的独立 Pilot 编排。

本模块只负责公开/脱敏请求、人工批准、表达式硬校验和阶段 A 入口适配。
它不读取行情、IC、收益、Sharpe、回撤或 Barra，也不写正式 discovery family
账本。真实计算由 ``run_fixed_pilot`` 在公司 Linux 服务器完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityRegistry,
    resolve_public_field_aliases,
)
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.llm_online import (
    AgentRole,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    build_deepseek_request,
)
from factor_miner.llm_privacy import CorporateExternalResearchPolicy
from factor_miner.llm_provider import (
    DeepSeekTransport,
    RecordedLLMResponse,
    UrllibDeepSeekTransport,
    execute_recorded_call,
)
from factor_miner.pilot_runner import FixedPilotPublication, run_fixed_pilot
from factor_miner.pilot_schema import (
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotRunRequest,
    PilotSourcePaths,
)
from factor_miner.schema import CandidateFactorSpec, FactorNode, HypothesisSpec


PILOT_CANDIDATE_SLOTS = (
    "pilot_fixed_001",
    "pilot_fixed_002",
    "pilot_fixed_003",
)
PILOT_ALLOWED_OPERATORS = (
    "abs",
    "add",
    "delta",
    "delay",
    "div",
    "mul",
    "neg",
    "rolling_corr",
    "rolling_max",
    "rolling_mean",
    "rolling_min",
    "rolling_std",
    "rolling_sum",
    "sub",
)
SHA256_PATTERN = r"^[0-9a-f]{64}$"
REQUEST_ID_PATTERN = r"^pilotreq_[0-9a-f]{24}$"
CALL_ID_PATTERN = r"^llmcall_[0-9a-f]{24}$"
APPROVAL_ID_PATTERN = r"^pilotapproval_[0-9a-f]{24}$"
STAGE_ID_PATTERN = r"^pilotstage_[0-9a-f]{24}$"


def _pilot_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造阶段 B 稳定错误。"""

    return FactorMinerError(code, message)


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _validate_hypothesis_readability(hypothesis: HypothesisSpec) -> None:
    """拒绝英文占位假设，确保 Dashboard 展示字段可直接阅读。"""

    narratives = (
        hypothesis.claim,
        hypothesis.mechanism,
        hypothesis.observable_proxy,
        hypothesis.independent_verification,
        hypothesis.baseline_reference,
        hypothesis.falsification_path,
        *hypothesis.competing_explanations,
        *hypothesis.failure_modes,
    )
    if any(not _CJK_RE.search(text) for text in narratives):
        raise _pilot_error(
            FailureCode.LLM_RESPONSE_INVALID,
            "DeepSeek 假设自由文本必须包含中文；字段名和专业名词可保留英文",
        )
    forbidden_source_markers = {
        "public_source_pending_review",
        "source_pending_review",
        "pending_review",
        "unknown_source",
    }
    if any(str(ref).strip().lower() in forbidden_source_markers for ref in hypothesis.source_refs):
        raise _pilot_error(
            FailureCode.LLM_RESPONSE_INVALID,
            "DeepSeek 假设不能使用来源占位符，必须提供可核验来源或明确无外部来源",
        )


def _request_identity(prefix: str, payload: object) -> tuple[str, str]:
    """根据不含运行结果的公开请求内容生成内容寻址身份。"""

    request_sha256 = sha256_json(payload)
    return f"{prefix}_{request_sha256[:24]}", request_sha256


class PilotFieldCapability(BaseModel):
    """可向 DeepSeek 暴露的字段能力摘要，不包含本地字段 ID 或数据值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_alias: str = Field(min_length=1)
    economic_type: str = Field(min_length=1)
    unit_dimension: str = Field(min_length=1)
    panel_shape: str = Field(min_length=1)
    event_time: str = Field(min_length=1)
    earliest_decision_time: str = Field(min_length=1)


def public_field_capabilities(
    registry: FieldAvailabilityRegistry,
) -> tuple[PilotFieldCapability, ...]:
    """将服务器字段注册表投影为最小公开能力摘要。"""

    return tuple(
        PilotFieldCapability(
            public_alias=field.public_alias,
            economic_type=field.economic_type,
            unit_dimension=field.unit_dimension,
            panel_shape=field.panel_shape,
            event_time=field.event_time,
            earliest_decision_time=field.earliest_decision_time,
        )
        for field in registry.fields
        if field.eligible_for_factor
        and field.point_in_time_guarantee
        and field.panel_shape == "asset_date_scalar"
    )


class HypothesisGenerationRequest(BaseModel):
    """DeepSeek 生成一个待人工批准的公开假设请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    public_brief: str = Field(min_length=1)
    verified_source_refs: tuple[str, ...] = Field(min_length=1)
    output_schema: str = "pilot-hypothesis-v1"
    max_hypotheses: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def validate_identity(self) -> HypothesisGenerationRequest:
        """请求身份必须只绑定公开输入。"""

        payload = {
            "public_brief": self.public_brief,
            "verified_source_refs": self.verified_source_refs,
            "output_schema": self.output_schema,
            "max_hypotheses": self.max_hypotheses,
        }
        expected_id, expected_hash = _request_identity("pilotreq", payload)
        if self.request_id != expected_id or self.request_sha256 != expected_hash:
            raise ValueError("假设请求身份与公开输入不一致")
        if self.output_schema != "pilot-hypothesis-v1":
            raise ValueError("假设输出 Schema 版本未冻结")
        return self


class ExpressionGenerationRequest(BaseModel):
    """一个已批准假设对应的固定三槽表达式请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis: HypothesisSpec
    field_capabilities: tuple[PilotFieldCapability, ...] = Field(min_length=1)
    allowed_operators: tuple[str, ...] = Field(min_length=1)
    max_lookback: int = Field(ge=0, le=130)
    candidate_slot_ids: tuple[str, ...] = Field(min_length=3, max_length=3)
    output_schema: str = "pilot-expression-response-v1"

    @model_validator(mode="after")
    def validate_identity_and_budget(self) -> ExpressionGenerationRequest:
        """固定三个槽、算子白名单和请求哈希，不允许模型控制预算。"""

        if self.candidate_slot_ids != PILOT_CANDIDATE_SLOTS:
            raise ValueError("阶段 B 必须严格使用三个 Pilot 槽位")
        if self.allowed_operators != PILOT_ALLOWED_OPERATORS:
            raise ValueError("阶段 B 算子白名单未按冻结顺序提供")
        if self.output_schema != "pilot-expression-response-v1":
            raise ValueError("表达式输出 Schema 版本未冻结")
        payload = _expression_request_payload(self)
        expected_id, expected_hash = _request_identity("pilotreq", payload)
        if self.request_id != expected_id or self.request_sha256 != expected_hash:
            raise ValueError("表达式请求身份与公开输入不一致")
        return self


class HypothesisGenerationResponse(BaseModel):
    """DeepSeek 假设响应的程序包装；正文只允许一个假设。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    model: str = Field(min_length=1)
    response_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis: HypothesisSpec


class ExpressionDesign(BaseModel):
    """DeepSeek 返回的一个公开 AST 设计及其自报元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_slot_id: str
    expression: FactorNode
    required_fields: tuple[str, ...] = Field(min_length=1)
    max_lookback: int = Field(ge=0, le=130)


class SemanticLintDiagnostic(BaseModel):
    """非阻塞语义诊断；不得改变 AST 或候选计数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ExpressionGenerationResponse(BaseModel):
    """DeepSeek 表达式响应和不可变调用摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    provider_call_id: str = Field(pattern=CALL_ID_PATTERN)
    model: str = Field(min_length=1)
    response_sha256: str = Field(pattern=SHA256_PATTERN)
    designs: tuple[ExpressionDesign, ...] = Field(min_length=3, max_length=3)
    lint_diagnostics: tuple[SemanticLintDiagnostic, ...] = ()

    @model_validator(mode="after")
    def validate_three_slots(self) -> ExpressionGenerationResponse:
        """响应必须恰好覆盖三个固定槽且不重复。"""

        slots = tuple(item.candidate_slot_id for item in self.designs)
        if slots != PILOT_CANDIDATE_SLOTS:
            raise ValueError("表达式响应必须按顺序恰好覆盖三个 Pilot 槽位")
        return self


class PilotHypothesisApproval(BaseModel):
    """绑定假设内容哈希的人工批准记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(pattern=APPROVAL_ID_PATTERN)
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    decision: str
    approver_role: str = Field(min_length=1)
    approved_at: datetime
    hypothesis_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis: HypothesisSpec
    approval_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_approval_identity(self) -> PilotHypothesisApproval:
        """批准记录必须绑定精确假设内容，不能批准另一份草案。"""

        if self.decision not in {"approved", "rejected"}:
            raise ValueError("假设决定只能是 approved 或 rejected")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("人工批准时间必须带时区")
        expected_hypothesis_hash = sha256_json(self.hypothesis.model_dump(mode="json"))
        if self.hypothesis_sha256 != expected_hypothesis_hash:
            raise ValueError("批准记录的假设哈希不一致")
        payload = {
            "request_id": self.request_id,
            "decision": self.decision,
            "approver_role": self.approver_role,
            "approved_at": self.approved_at.isoformat(),
            "hypothesis_sha256": self.hypothesis_sha256,
        }
        expected_sha = sha256_json(payload)
        if self.approval_sha256 != expected_sha:
            raise ValueError("批准记录哈希不一致")
        if self.approval_id != f"pilotapproval_{expected_sha[:24]}":
            raise ValueError("批准记录 ID 不一致")
        return self


class PilotStageBState(BaseModel):
    """供控制台只读展示的阶段 B 状态，不保存行情或完整结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "pilot-stage-b-state-v1"
    sequence: int = Field(default=0, ge=0)
    stage_run_id: str = Field(pattern=STAGE_ID_PATTERN)
    status: str
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    hypothesis_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_call_id: str | None = Field(default=None, pattern=CALL_ID_PATTERN)
    approval_id: str | None = Field(default=None, pattern=APPROVAL_ID_PATTERN)
    candidate_ids: tuple[str, ...] = ()
    pilot_run_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    hypothesis: HypothesisSpec | None = None
    expression_response: ExpressionGenerationResponse | None = None


@dataclass(frozen=True, slots=True)
class ApprovedPilotRequest:
    """批准假设后进入阶段 A 的完整服务器端依赖。"""

    hypothesis: HypothesisSpec
    approval: PilotHypothesisApproval
    expression_request: ExpressionGenerationRequest
    pilot_request: PilotRunRequest
    source_paths: PilotSourcePaths
    artifact_root: Path


class PilotServerConfig(BaseModel):
    """阶段 A 真实输入路径和评价区间的服务器私有配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paths: PilotSourcePaths
    request: PilotRunRequest


@dataclass(frozen=True, slots=True)
class PilotRunResult:
    """阶段 B 表达式来源和阶段 A 发布的组合结果。"""

    expression_response: ExpressionGenerationResponse
    candidates: PilotFixedCandidateFile
    publication: FixedPilotPublication


class DeepSeekExpressionProvider(Protocol):
    """只生成三个公开 typed AST 设计的最小 provider。"""

    def generate_three(
        self,
        request: ExpressionGenerationRequest,
    ) -> ExpressionGenerationResponse:
        """生成三个设计；禁止 provider 读取或接收任何结果数据。"""


class DeepSeekHypothesisProvider(Protocol):
    """只生成一个待人工批准假设的最小 provider。"""

    def generate_one(
        self,
        request: HypothesisGenerationRequest,
    ) -> HypothesisGenerationResponse:
        """生成一个假设草案。"""


def _hypothesis_request_payload(
    request: HypothesisGenerationRequest,
) -> dict[str, object]:
    """返回不含请求身份的公开假设 payload。"""

    return {
        "information_class": "public_capability_only",
        "public_brief": request.public_brief,
        "verified_source_refs": list(request.verified_source_refs),
        "output_schema": request.output_schema,
        "max_hypotheses": request.max_hypotheses,
    }


def _expression_request_payload(
    request: ExpressionGenerationRequest,
) -> dict[str, object]:
    """返回不含请求身份的公开表达式 payload。"""

    return {
        "information_class": "public_capability_only",
        "approved_hypothesis": request.hypothesis.model_dump(mode="json"),
        "field_capabilities": [
            item.model_dump(mode="json") for item in request.field_capabilities
        ],
        "allowed_operators": list(request.allowed_operators),
        "max_lookback": request.max_lookback,
        "candidate_slot_ids": list(request.candidate_slot_ids),
        "output_schema": request.output_schema,
    }


def build_hypothesis_generation_request(
    *,
    public_brief: str,
    verified_source_refs: tuple[str, ...],
) -> HypothesisGenerationRequest:
    """创建一个只能生成单个假设的内容寻址请求。"""

    payload = {
        "public_brief": public_brief,
        "verified_source_refs": verified_source_refs,
        "output_schema": "pilot-hypothesis-v1",
        "max_hypotheses": 1,
    }
    request_id, request_sha256 = _request_identity("pilotreq", payload)
    return HypothesisGenerationRequest(
        request_id=request_id,
        request_sha256=request_sha256,
        **payload,
    )


def build_expression_generation_request(
    *,
    hypothesis: HypothesisSpec,
    registry: FieldAvailabilityRegistry,
    max_lookback: int = 130,
) -> ExpressionGenerationRequest:
    """由程序冻结字段能力、算子集合和三个槽位。"""

    capabilities = public_field_capabilities(registry)
    payload = {
        "information_class": "public_capability_only",
        "approved_hypothesis": hypothesis.model_dump(mode="json"),
        "field_capabilities": [item.model_dump(mode="json") for item in capabilities],
        "allowed_operators": PILOT_ALLOWED_OPERATORS,
        "max_lookback": max_lookback,
        "candidate_slot_ids": PILOT_CANDIDATE_SLOTS,
        "output_schema": "pilot-expression-response-v1",
    }
    request_id, request_sha256 = _request_identity("pilotreq", payload)
    return ExpressionGenerationRequest(
        request_id=request_id,
        request_sha256=request_sha256,
        hypothesis=hypothesis,
        field_capabilities=capabilities,
        allowed_operators=PILOT_ALLOWED_OPERATORS,
        max_lookback=max_lookback,
        candidate_slot_ids=PILOT_CANDIDATE_SLOTS,
    )


def _prepared_request_from_payload(
    *,
    request_id: str,
    role: AgentRole,
    slot_ids: tuple[str, ...],
    system_prompt: str,
    payload: dict[str, object],
) -> PreparedDeepSeekRequest:
    """构造既可精确授权又不携带内部路径的 DeepSeek 请求。"""

    return build_deepseek_request(
        campaign_id=request_id,
        agent_role=role,
        slot_ids=slot_ids,
        system_prompt=system_prompt,
        user_payload=payload,
    )


def build_prepared_hypothesis_request(
    request: HypothesisGenerationRequest,
) -> PreparedDeepSeekRequest:
    """构造单假设脱敏请求，不访问网络。"""

    return _prepared_request_from_payload(
        request_id=request.request_id,
        role=AgentRole.HYPOTHESIS,
        slot_ids=("pilot_hypothesis_001",),
        system_prompt=(
            "你是受控的金融研究假设代理。只能使用给定的公开 brief 和已核验的"
            "公开来源标识；不得访问行情、个股、统计结果或任何服务器文件。"
            "不得声称因果已经验证。只输出一个 JSON object，且根节点只能有一个"
            "\"hypothesis\" 字段；hypothesis 对象只能包含 \"claim\"、\"mechanism\"、"
            "\"expected_sign\"、\"observable_proxy\"、\"independent_verification\"、"
            "\"competing_explanations\"、\"baseline_reference\"、\"failure_modes\"、"
            "\"falsification_path\"、\"source_refs\"、\"mechanism_status\"。"
            "\"expected_sign\" 只能是 positive 或 negative；\"mechanism_status\""
            "必须是 mechanism_unverified；competing_explanations、"
            "failure_modes、source_refs 必须是非空字符串数组；source_refs 必须逐字"
            "取自输入的 verified_source_refs；所有自由文本必须使用中文，字段名、公式算子、"
            "数据别名、URL、DOI 和标准缩写可以保留原文。source_refs 必须是可阅读的来源"
            "描述或明确说明本批次没有可核验外部来源，禁止输出 public_source_pending_review"
            "等占位符。禁止输出 hypothesis_id、status、"
            "expected_direction、baseline、failure_mode、freeze_operator 或任何其他字段。"
        ),
        payload=_hypothesis_request_payload(request),
    )


def build_prepared_expression_request(
    request: ExpressionGenerationRequest,
) -> PreparedDeepSeekRequest:
    """构造单批准假设的三表达式脱敏请求，不访问网络。"""

    return _prepared_request_from_payload(
        request_id=request.request_id,
        role=AgentRole.EXPRESSION,
        slot_ids=PILOT_CANDIDATE_SLOTS,
        system_prompt=(
            "你是受控的数学表达式代理。不得访问工具、行情、个股、标签、IC、"
            "收益、Sharpe、回撤或 Barra。只能使用给定公开字段别名和白名单算子，"
            "只输出一个符合 pilot-expression-response-v1 的 JSON object，不要自我"
            "修复或请求更多数据。根节点只能包含 \"designs\" 和 \"lint_diagnostics\"；\"designs\""
            "必须是按 pilot_fixed_001、pilot_fixed_002、pilot_fixed_003 顺序排列的"
            "三个对象。每个 design 只能包含 \"candidate_slot_id\"、\"expression\"、"
            "\"required_fields\"、\"max_lookback\"。\"expression\" 必须使用 FactorNode 结构："
            "field 节点为 {\"op\":\"field\",\"field\":\"公开字段别名\"}，"
            "const 节点为 {\"op\":\"const\",\"value\":有限数值}，其他节点为"
            "{\"op\":\"白名单算子\",\"args\":[FactorNode,...]}，rolling 节点"
            "另外使用 window，delta/delay 节点另外使用 period，rolling 节点的"
            "center 必须为 false 或省略。\"required_fields\" 必须是 AST 中实际出现的"
            "公开字段别名数组。max_lookback 必须递归计算：field/const 为 0，"
            "delay/delta 为子树最大 lookback 加 period，rolling 为子树最大 lookback"
            "加 window-1，其他算子取子树最大值；例如 rolling_mean window=20 的"
            "max_lookback 是 19。允许的"
            "算子只有给定白名单。AST 还必须满足 max_depth 不超过 5、节点总数不超过 15；"
            "深度必须按程序规则计算：field/const 节点深度为 1，父节点深度为 1 + max(子节点深度)。"
            "例如 rolling_corr(delta(field,1), delay(delta(field,1),1)) 的深度是 4；"
            "不要把 rolling_corr 放在 neg、mul 或 div 的深层子树中，否则很容易超过深度 5。"
            "优先使用最小的浅层 AST；可以采用 sub(div(field(volume), rolling_mean(field(volume), window)), "
            "rolling_corr(window, delta(field(price_close),1), delay(delta(field(price_close),1),1))) "
            "这种根节点为 sub 的浅层结构，并只变化允许的 window。"
            "\"lint_diagnostics\" 可以是空数组；每个诊断只能包含"
            "\"code\"、\"severity\"、\"message\"。禁止使用 type、operator、operands、literal"
            "或任何未声明字段。"
        ),
        payload=_expression_request_payload(request),
    )


def request_from_prepared_hypothesis(
    prepared: PreparedDeepSeekRequest,
) -> HypothesisGenerationRequest:
    """从已准备请求恢复阶段 B 假设合同，不访问网络。"""

    if prepared.agent_role is not AgentRole.HYPOTHESIS:
        raise _pilot_error(FailureCode.SPEC_SCHEMA_INVALID, "请求角色不是 hypothesis")
    payload = dict(prepared.export_payload)
    try:
        request = build_hypothesis_generation_request(
            public_brief=str(payload["public_brief"]),
            verified_source_refs=tuple(str(item) for item in payload["verified_source_refs"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _pilot_error(FailureCode.SPEC_SCHEMA_INVALID, "假设请求 payload 无效") from error
    if request.request_id != prepared.campaign_id:
        raise _pilot_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "假设请求 ID 与 prepared request 不一致")
    return request


def request_from_prepared_expression(
    prepared: PreparedDeepSeekRequest,
) -> ExpressionGenerationRequest:
    """从已准备请求恢复阶段 B 表达式合同，不访问网络。"""

    if prepared.agent_role is not AgentRole.EXPRESSION:
        raise _pilot_error(FailureCode.SPEC_SCHEMA_INVALID, "请求角色不是 expression")
    payload = dict(prepared.export_payload)
    try:
        request = ExpressionGenerationRequest(
            request_id=prepared.campaign_id,
            request_sha256=sha256_json(payload),
            hypothesis=HypothesisSpec.model_validate(payload["approved_hypothesis"]),
            field_capabilities=tuple(
                PilotFieldCapability.model_validate(item)
                for item in payload["field_capabilities"]
            ),
            allowed_operators=tuple(str(item) for item in payload["allowed_operators"]),
            max_lookback=int(payload["max_lookback"]),
            candidate_slot_ids=tuple(str(item) for item in payload["candidate_slot_ids"]),
            output_schema=str(payload["output_schema"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _pilot_error(FailureCode.SPEC_SCHEMA_INVALID, "表达式请求 payload 无效") from error
    if request.request_id != prepared.campaign_id:
        raise _pilot_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "表达式请求 ID 与 prepared request 不一致")
    return request


def _content_json(response: RecordedLLMResponse) -> dict[str, Any]:
    """要求 provider 返回最终 JSON，不接受工具调用或隐藏补全。"""

    if response.content_json is None or response.tool_calls:
        raise _pilot_error(
            FailureCode.LLM_RESPONSE_INVALID,
            "阶段 B provider 没有返回最终 JSON object",
        )
    return response.content_json


def _write_provider_failure(record_root: Path, request_id: str, error: Exception) -> None:
    """保留失败状态而不保存任何密钥或完整结果。"""

    payload = {
        "request_id": request_id,
        "status": "failed",
        "error_code": (
            error.code.value if isinstance(error, FactorMinerError) else "UNCLASSIFIED"
        ),
        "error_message": str(error),
    }
    _atomic_write_immutable(
        record_root / "pilot_stage_b" / request_id / "failure.json",
        canonical_json_bytes(payload),
    )


@dataclass(frozen=True, slots=True)
class RecordedDeepSeekHypothesisProvider:
    """用既有固定 DeepSeek transport 录制一个假设响应。"""

    policy: CorporateExternalResearchPolicy
    authorization: LLMExportAuthorization
    record_root: Path
    transport: DeepSeekTransport | None = None
    now: datetime | None = None

    def generate_one(
        self,
        request: HypothesisGenerationRequest,
    ) -> HypothesisGenerationResponse:
        """执行一次已精确授权的脱敏假设请求。"""

        prepared = build_prepared_hypothesis_request(request)
        try:
            response = execute_recorded_call(
                prepared=prepared,
                authorization=self.authorization,
                policy=self.policy,
                record_root=self.record_root,
                transport=self.transport or UrllibDeepSeekTransport(),
                now=self.now or datetime.now(timezone.utc),
            )
            content = _content_json(response)
            hypothesis = HypothesisSpec.model_validate(content["hypothesis"])
            _validate_hypothesis_readability(hypothesis)
            if not set(hypothesis.source_refs).issubset(
                set(request.verified_source_refs)
            ):
                raise _pilot_error(
                    FailureCode.LLM_RESPONSE_INVALID,
                    "DeepSeek 假设引用了请求外的来源标识",
                )
            return HypothesisGenerationResponse(
                request_id=request.request_id,
                request_sha256=request.request_sha256,
                provider_call_id=response.record.call_id,
                model=response.record.model,
                response_sha256=response.record.response_sha256,
                hypothesis=hypothesis,
            )
        except Exception as error:
            _write_provider_failure(self.record_root, request.request_id, error)
            if isinstance(error, FactorMinerError):
                raise
            raise _pilot_error(
                FailureCode.LLM_RESPONSE_INVALID,
                "DeepSeek 假设响应 Schema 无效",
            ) from error


@dataclass(frozen=True, slots=True)
class RecordedDeepSeekExpressionProvider:
    """用既有固定 DeepSeek transport 录制三个表达式响应。"""

    policy: CorporateExternalResearchPolicy
    authorization: LLMExportAuthorization
    record_root: Path
    transport: DeepSeekTransport | None = None
    now: datetime | None = None

    def generate_three(
        self,
        request: ExpressionGenerationRequest,
    ) -> ExpressionGenerationResponse:
        """执行一次已批准假设的脱敏三表达式请求。"""

        prepared = build_prepared_expression_request(request)
        try:
            response = execute_recorded_call(
                prepared=prepared,
                authorization=self.authorization,
                policy=self.policy,
                record_root=self.record_root,
                transport=self.transport or UrllibDeepSeekTransport(),
                now=self.now or datetime.now(timezone.utc),
            )
            content = _content_json(response)
            raw_designs = content.get("designs")
            if not isinstance(raw_designs, list):
                raise _pilot_error(
                    FailureCode.LLM_RESPONSE_INVALID,
                    "DeepSeek 表达式响应缺少 designs 数组",
                )
            parsed = ExpressionGenerationResponse(
                request_id=request.request_id,
                provider_call_id=response.record.call_id,
                model=response.record.model,
                response_sha256=response.record.response_sha256,
                designs=tuple(ExpressionDesign.model_validate(item) for item in raw_designs),
                lint_diagnostics=tuple(
                    SemanticLintDiagnostic.model_validate(item)
                    for item in content.get("lint_diagnostics", [])
                ),
            )
            return parsed
        except Exception as error:
            _write_provider_failure(self.record_root, request.request_id, error)
            if isinstance(error, FactorMinerError):
                raise
            raise _pilot_error(
                FailureCode.LLM_RESPONSE_INVALID,
                "DeepSeek 表达式响应 Schema 无效",
            ) from error


def _public_fields(node: FactorNode) -> tuple[str, ...]:
    """读取公开 AST 中出现的字段别名。"""

    values: set[str] = set()
    if node.op == "field" and node.field is not None:
        values.add(node.field)
    for child in node.args:
        values.update(_public_fields(child))
    return tuple(sorted(values))


def _create_candidate_spec(
    *,
    design: ExpressionDesign,
    response: ExpressionGenerationResponse,
    hypothesis: HypothesisSpec,
    request: ExpressionGenerationRequest,
    registry: FieldAvailabilityRegistry,
    created_at: datetime,
) -> CandidateFactorSpec:
    """对单个设计执行公开别名、typed AST、lookback 和 compiler 闸门。"""

    if design.candidate_slot_id not in PILOT_CANDIDATE_SLOTS:
        raise _pilot_error(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"未知 Pilot 槽位：{design.candidate_slot_id}",
        )
    public_metadata = _validate_public_design(design, request, registry)
    local_expression = resolve_public_field_aliases(design.expression, registry)
    from factor_miner.dsl import validate_ast

    local_metadata = validate_ast(
        local_expression,
        allowed_fields=tuple(
            item.field_id
            for item in registry.fields
            if item.eligible_for_factor and item.point_in_time_guarantee
        ),
        forbidden_fields=("label", "future_return", "label_o2o_5d"),
    )
    if local_metadata.lookback != public_metadata["lookback"]:
        raise _pilot_error(
            FailureCode.DSL_TYPE_ERROR,
            "公开字段解析前后的 lookback 不一致",
        )
    candidate = CandidateFactorSpec(
        hypothesis=hypothesis,
        expression=local_expression,
        required_fields=local_metadata.required_fields,
        max_lookback=local_metadata.lookback,
        availability="next_open",
        created_at=created_at,
        provenance={
            "source": "deepseek_pilot_stage_b",
            "candidate_slot_id": design.candidate_slot_id,
            "pilot_request_id": request.request_id,
            "provider_call_id": response.provider_call_id,
            "response_sha256": response.response_sha256,
            "field_registry_id": registry.registry_id,
            "data_release_id": registry.data_release_id,
        },
    )
    try:
        compile_candidate(candidate, allowed_fields=tuple(
            item.field_id
            for item in registry.fields
            if item.eligible_for_factor and item.point_in_time_guarantee
        ))
    except FactorMinerError:
        raise
    except Exception as error:
        raise _pilot_error(
            FailureCode.DSL_TYPE_ERROR,
            f"候选编译失败：{error}",
        ) from error
    return candidate


def _validate_public_design(
    design: ExpressionDesign,
    request: ExpressionGenerationRequest,
    registry: FieldAvailabilityRegistry,
) -> dict[str, Any]:
    """在本地字段解析前检查模型自报字段和 lookback。"""

    allowed_aliases = tuple(
        item.public_alias
        for item in public_field_capabilities(registry)
    )
    from factor_miner.dsl import validate_ast

    try:
        metadata = validate_ast(
            design.expression,
            allowed_fields=allowed_aliases,
            forbidden_fields=("label", "future_return", "label_o2o_5d"),
        )
    except FactorMinerError:
        raise
    except Exception as error:
        raise _pilot_error(
            FailureCode.DSL_TYPE_ERROR,
            f"公开 AST 校验失败：{error}",
        ) from error
    expected_fields = tuple(sorted(_public_fields(design.expression)))
    if tuple(sorted(design.required_fields)) != expected_fields:
        raise _pilot_error(
            FailureCode.FIELD_MISSING,
            "表达式 required_fields 与 AST 实际字段不一致",
        )
    if metadata.required_fields != expected_fields:
        raise _pilot_error(
            FailureCode.FIELD_MISSING,
            "表达式字段未通过公开字段能力白名单",
        )
    if design.max_lookback != metadata.lookback:
        raise _pilot_error(
            FailureCode.DSL_TYPE_ERROR,
            "表达式 max_lookback 与 AST 实际 lookback 不一致",
        )
    if metadata.lookback > request.max_lookback:
        raise _pilot_error(
            FailureCode.DSL_TYPE_ERROR,
            "表达式 lookback 超出冻结请求上限",
        )
    return {
        "required_fields": metadata.required_fields,
        "lookback": metadata.lookback,
    }


def normalize_expression_response(
    response: ExpressionGenerationResponse,
    hypothesis: HypothesisSpec,
    field_registry: FieldAvailabilityRegistry,
    request: ExpressionGenerationRequest | None = None,
    *,
    created_at: datetime | None = None,
) -> tuple[CandidateFactorSpec, CandidateFactorSpec, CandidateFactorSpec]:
    """将三个 DeepSeek 设计规范化为三个可编译 ``CandidateFactorSpec``。"""

    if request is None:
        request = build_expression_generation_request(hypothesis=hypothesis, registry=field_registry)
    if response.request_id != request.request_id:
        raise _pilot_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "响应请求身份不一致")
    if response.provider_call_id.startswith("llmcall_") is False:
        raise _pilot_error(FailureCode.LLM_RESPONSE_INVALID, "响应缺少 provider call ID")
    timestamp = created_at or datetime.now(timezone.utc)
    candidates = tuple(
        _create_candidate_spec(
            design=design,
            response=response,
            hypothesis=hypothesis,
            request=request,
            registry=field_registry,
            created_at=timestamp,
        )
        for design in response.designs
    )
    if len(candidates) != 3:
        raise _pilot_error(FailureCode.LLM_RESPONSE_INVALID, "候选数量不是三个")
    return candidates  # type: ignore[return-value]


def approve_hypothesis(
    response: HypothesisGenerationResponse,
    *,
    approver_role: str,
    decision: str = "approved",
    approved_at: datetime | None = None,
) -> PilotHypothesisApproval:
    """为精确假设响应创建不可变人工决定。"""

    if not approver_role.strip():
        raise ValueError("approver_role 不能为空")
    timestamp = approved_at or datetime.now(timezone.utc)
    hypothesis_sha256 = sha256_json(response.hypothesis.model_dump(mode="json"))
    payload = {
        "request_id": response.request_id,
        "decision": decision,
        "approver_role": approver_role,
        "approved_at": timestamp.isoformat(),
        "hypothesis_sha256": hypothesis_sha256,
    }
    approval_sha256 = sha256_json(payload)
    return PilotHypothesisApproval(
        approval_id=f"pilotapproval_{approval_sha256[:24]}",
        request_id=response.request_id,
        decision=decision,
        approver_role=approver_role,
        approved_at=timestamp,
        hypothesis_sha256=hypothesis_sha256,
        hypothesis=response.hypothesis,
        approval_sha256=approval_sha256,
    )


def _candidate_file(
    candidates: tuple[CandidateFactorSpec, CandidateFactorSpec, CandidateFactorSpec],
) -> PilotFixedCandidateFile:
    """把阶段 B 三个候选装入阶段 A 的统一三个 Pilot 槽位。"""

    return PilotFixedCandidateFile(
        version="pilot-fixed-candidates-v1",
        candidates=tuple(
            PilotFixedCandidate(candidate_id=slot, spec=spec)
            for slot, spec in zip(PILOT_CANDIDATE_SLOTS, candidates, strict=True)
        ),
    )


def _verify_approval(
    request: ApprovedPilotRequest,
) -> None:
    """执行批准假设、表达式请求和阶段 A 运行身份的前置闸门。"""

    if request.approval.decision != "approved":
        raise _pilot_error(FailureCode.LLM_STATE_TRANSITION_INVALID, "假设尚未人工批准")
    if request.approval.hypothesis_sha256 != sha256_json(
        request.hypothesis.model_dump(mode="json")
    ):
        raise _pilot_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "批准假设内容已改变")
    if request.expression_request.hypothesis != request.hypothesis:
        raise _pilot_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "表达式请求未绑定批准假设")
    if request.pilot_request.artifact_root != request.artifact_root:
        raise _pilot_error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, "Pilot 产物根目录身份不一致")


def run_approved_pilot(
    request: ApprovedPilotRequest,
    provider: DeepSeekExpressionProvider,
    *,
    registry: FieldAvailabilityRegistry,
    created_at: datetime | None = None,
) -> PilotRunResult:
    """批准假设后生成三个候选，并无分叉地进入阶段 A ``run_fixed_pilot``。"""

    _verify_approval(request)
    response = provider.generate_three(request.expression_request)
    candidates = _candidate_file(
        normalize_expression_response(
            response,
            request.hypothesis,
            registry,
            request.expression_request,
            created_at=created_at,
        )
    )
    candidate_file_sha256 = sha256_json(candidates.model_dump(mode="json"))
    frozen_run_request = request.pilot_request.model_copy(
        update={"candidate_file_sha256": candidate_file_sha256}
    )
    state_dir = (
        request.artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "pilot_stage_b"
        / request.expression_request.request_id
    )
    _atomic_write_immutable(
        state_dir / "generated_candidates.json",
        canonical_json_bytes(candidates.model_dump(mode="json")),
    )
    publication = run_fixed_pilot(
        candidates=candidates,
        paths=request.source_paths,
        request=frozen_run_request,
    )
    return PilotRunResult(
        expression_response=response,
        candidates=candidates,
        publication=publication,
    )


def build_stage_run_id(request_id: str) -> str:
    """把阶段 B 请求身份映射为独立、非 family 的运行身份。"""

    if not request_id.startswith("pilotreq_"):
        raise ValueError("阶段 B request_id 格式无效")
    return f"pilotstage_{request_id.removeprefix('pilotreq_')}"


def save_stage_state(
    artifact_root: Path,
    state: PilotStageBState,
) -> Path:
    """原子保存阶段 B 控制台可读状态。"""

    state_root = (
        artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "pilot_stage_b"
        / state.stage_run_id
    )
    state_hash = sha256_json(state.model_dump(mode="json"))
    path = state_root / "snapshots" / f"{state.sequence:04d}_{state_hash[:24]}.json"
    _atomic_write_immutable(path, canonical_json_bytes(state.model_dump(mode="json")))
    return path


def load_stage_state(artifact_root: Path, stage_run_id: str) -> PilotStageBState:
    """读取并验证阶段 B 状态，不读取任何原始行情。"""

    state_root = (
        artifact_root.expanduser().resolve(strict=False)
        / "state"
        / "pilot_stage_b"
        / stage_run_id
    )
    try:
        snapshots = sorted((state_root / "snapshots").glob("*.json"))
        if not snapshots:
            raise OSError("没有阶段 B 状态快照")
        return PilotStageBState.model_validate_json(snapshots[-1].read_bytes())
    except (OSError, ValueError) as error:
        raise _pilot_error(FailureCode.LEDGER_CORRUPT, "阶段 B 状态缺失或损坏") from error
