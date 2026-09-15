"""十槽逻辑演化假设的脱敏请求、严格解析与人工批准编排。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.construct_validation import ObservableCondition
from factor_miner.hypothesis_constraints import ConditionContract
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_online import (
    AgentRole,
    LLMEvolutionRequestAuthorization,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    build_deepseek_request,
)
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapCard,
    DesignDiversityPolicy,
    EvolutionHypothesisApproval,
    EvolutionHypothesisDraft,
    ResearchEvolutionContext,
)
from factor_miner.semantic_coverage import (
    SemanticContext,
    SemanticCoverage,
    SemanticDirection,
    SemanticEvent,
    SemanticOutput,
    SemanticQuality,
    SemanticQuotaPlan,
    build_semantic_quota,
)


_EXPECTED_SLOTS = tuple(f"H{index:02d}" for index in range(1, 11))
_ALLOWED_CARD_FIELDS = (
    "gap_id",
    "gap_category",
    "sanitized_labels",
    "allowed_field_aliases",
    "allowed_operator_families",
    "temporal_window_bins",
    "structure_cluster_count_band",
    "signal_cluster_count_band",
    "missing_or_failure_risk",
)
_FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "alpha",
        "backtest",
        "data_contract",
        "data_release_id",
        "evaluation_policy",
        "evaluation_policy_hash",
        "evaluation_policy_id",
        "field_registry_hash",
        "hac",
        "hac_lags",
        "manifest_hash",
        "memory_snapshot_hash",
        "multiplicity_family_id",
        "portfolio",
        "publish",
        "raw_market_data",
        "release_hash",
    }
)
_MEMORY_BANDS = frozenset({"unknown", "low", "medium", "high"})
_MEMORY_FAILURES = frozenset(
    {
        "none",
        "invalid_complexity",
        "unit_mismatch",
        "invalid_window",
        "ast_limit",
        "duplicate_structure",
    }
)
_MEMORY_GUIDANCE = frozenset(
    {
        "avoid_previous_ast_duplicates",
        "prioritize_positive_excess_information_ratio",
        "respect_minimum_structural_complexity",
        "respect_unit_compatibility",
        "respect_ast_and_window_limits",
        "优先尝试更长窗口",
        "优先尝试更短窗口",
        "同单位场景优先波动算子",
        "同单位场景优先极值算子",
        "同单位场景优先均值算子",
    }
)


def _response_error(message: str) -> FactorMinerError:
    """构造稳定的模型响应错误。"""

    return FactorMinerError(FailureCode.LLM_RESPONSE_INVALID, message)


def _approval_error(message: str) -> FactorMinerError:
    """构造稳定的批准计数错误。"""

    return FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, message)


def _sorted_unique(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    """把非空字符串集合规范成稳定排序的元组。"""

    cleaned = tuple(
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    )
    if not cleaned:
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            f"{field_name} 不能为空",
        )
    return tuple(sorted(set(cleaned)))


def _as_mapping(value: object, *, field_name: str) -> dict[str, object]:
    """把 Pydantic 模型或 Mapping 统一为 dict。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise FactorMinerError(
        FailureCode.COVERAGE_GAP_INVALID,
        f"{field_name} 必须是 Mapping 或 BaseModel",
    )


class EvolutionSourceRecordPlan(BaseModel):
    """逻辑假设要求后续检索的来源记录任务。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_record_id: str
    claim_fragment: str
    rationale: str
    query_terms: tuple[str, ...] = Field(min_length=1, max_length=8)
    year_start: int = Field(ge=1900, le=2100)
    year_end: int = Field(ge=1900, le=2100)
    result_limit: int = Field(default=3, ge=1, le=5)

    @field_validator("source_record_id", "claim_fragment", "rationale")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """来源记录任务关键文本不得为空。"""

        if not value.strip():
            raise ValueError("来源记录任务文本不能为空")
        return value

    @field_validator("query_terms")
    @classmethod
    def validate_query_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """查询词不得为空或重复。"""

        if any(not value.strip() for value in values):
            raise ValueError("query_terms 不能包含空项")
        normalized = tuple(value.casefold() for value in values)
        if len(set(normalized)) != len(values):
            raise ValueError("query_terms 不能重复")
        return values

    @model_validator(mode="after")
    def validate_year_interval(self) -> EvolutionSourceRecordPlan:
        """年份区间必须正向。"""

        if self.year_end < self.year_start:
            raise ValueError("来源记录年份区间不能反向")
        return self


class LogicalEvolutionHypothesisDraft(EvolutionHypothesisDraft):
    """带来源记录任务的逻辑演化假设草案。"""

    source_records: tuple[EvolutionSourceRecordPlan, ...] = Field(min_length=1)
    observable_condition: ObservableCondition | None = Field(default=None, exclude_if=lambda value: value is None)
    measurement_contract: ConditionContract | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("source_records")
    @classmethod
    def validate_source_records(
        cls,
        values: tuple[EvolutionSourceRecordPlan, ...],
    ) -> tuple[EvolutionSourceRecordPlan, ...]:
        """单个逻辑假设内的 source_record_id 不得重复。"""

        record_ids = tuple(item.source_record_id for item in values)
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("source_record_id 不能重复")
        return values


class EvolutionHypothesisResponse(BaseModel):
    """演化假设代理输出的严格根对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypotheses: tuple[LogicalEvolutionHypothesisDraft, ...] = Field(
        min_length=10,
        max_length=10,
    )


def sanitize_evolution_gap_brief(
    gap_brief: Mapping[str, object],
) -> dict[str, object]:
    """从 gap brief 中投影只允许外发的脱敏字段。"""

    raw_cards = gap_brief.get("gap_cards")
    if not isinstance(raw_cards, Sequence) or not raw_cards:
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            "gap_brief.gap_cards 必须提供至少一张脱敏 gap card",
        )
    sanitized_cards: list[dict[str, object]] = []
    aggregated_fields: set[str] = set()
    aggregated_families: set[str] = set()
    aggregated_windows: set[str] = set()
    aggregated_risks: set[str] = set()
    for index, raw_card in enumerate(raw_cards):
        values = _as_mapping(raw_card, field_name=f"gap_cards[{index}]")
        card_payload = {
            key: values[key] for key in _ALLOWED_CARD_FIELDS if key in values
        }
        missing = tuple(
            key for key in _ALLOWED_CARD_FIELDS if key not in card_payload
        )
        if missing:
            raise FactorMinerError(
                FailureCode.COVERAGE_GAP_INVALID,
                f"gap_cards[{index}] 缺少字段：{missing}",
            )
        card = CoverageGapCard.model_validate(card_payload)
        # `CoverageGapCard` 会自动计算 card_sha256；它是本地内容身份，不能
        # 随脱敏 brief 暴露给模型。卡片的公开字段仍由上面的白名单决定。
        sanitized_cards.append(
            card.model_dump(mode="json", exclude={"card_sha256"})
        )
        aggregated_fields.update(card.allowed_field_aliases)
        aggregated_families.update(card.allowed_operator_families)
        aggregated_windows.update(card.temporal_window_bins)
        aggregated_risks.update(card.missing_or_failure_risk)
    policy = DesignDiversityPolicy()
    result = {
        "gap_cards": sorted(
            sanitized_cards,
            key=lambda item: (
                str(item["gap_category"]),
                str(item["gap_id"]),
            ),
        ),
        "allowed_field_aliases": tuple(sorted(aggregated_fields)),
        "allowed_operator_families": tuple(sorted(aggregated_families)),
        "data_availability_tags": tuple(sorted(aggregated_risks)),
        "temporal_window_bins": tuple(sorted(aggregated_windows)),
        "required_structure_axes": policy.allowed_structure_axes,
        "three_designs_must_differ": True,
    }
    raw_feedback = gap_brief.get("memory_feedback")
    if raw_feedback is not None:
        feedback = _as_mapping(raw_feedback, field_name="memory_feedback")
        band_fields = (
            "candidate_history_band",
            "evaluated_share_band",
            "ic_quality_band",
            "rank_ic_quality_band",
            "portfolio_quality_band",
            "direction_reversal_band",
        )
        sanitized_feedback: dict[str, object] = {}
        for field in band_fields:
            value = str(feedback.get(field, ""))
            if value not in _MEMORY_BANDS:
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    f"memory_feedback.{field} 不是允许的离散带",
                )
            sanitized_feedback[field] = value
        for field, allowed in (
            ("dominant_failure_patterns", _MEMORY_FAILURES),
            ("generation_guidance", _MEMORY_GUIDANCE),
        ):
            raw_values = feedback.get(field)
            if not isinstance(raw_values, Sequence) or isinstance(
                raw_values, (str, bytes)
            ):
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    f"memory_feedback.{field} 必须是数组",
                )
            values = tuple(sorted({str(item) for item in raw_values}))
            if not values or any(item not in allowed for item in values):
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    f"memory_feedback.{field} 包含未登记标签",
                )
            sanitized_feedback[field] = values
        semantic_coverage = feedback.get("semantic_coverage")
        semantic_quota = feedback.get("semantic_quota")
        if semantic_coverage is not None or semantic_quota is not None:
            if semantic_coverage is None or semantic_quota is None:
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    "memory_feedback 的 semantic_coverage 与 semantic_quota 必须同时提供",
                )
            try:
                coverage_model = SemanticCoverage.model_validate(semantic_coverage)
                quota_model = SemanticQuotaPlan.model_validate(semantic_quota)
            except ValidationError as error:
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    f"memory_feedback 金融语义覆盖无效：{error}",
                ) from error
            expected_quota = build_semantic_quota(coverage_model)
            if quota_model != expected_quota:
                raise FactorMinerError(
                    FailureCode.COVERAGE_GAP_INVALID,
                    "memory_feedback.semantic_quota 与累计稀疏度不一致",
                )
            sanitized_feedback["semantic_coverage"] = coverage_model.model_dump(mode="json")
            sanitized_feedback["semantic_quota"] = quota_model.model_dump(mode="json")
        result["memory_feedback"] = sanitized_feedback
    return result


def evolution_gap_brief_sha256(gap_brief: Mapping[str, object]) -> str:
    """计算外发脱敏 gap brief 的稳定 hash。"""

    return sha256_json(sanitize_evolution_gap_brief(gap_brief))


def _authorization_scope_payload(
    authorization: (
        LLMEvolutionRequestAuthorization
        | LLMExportAuthorization
    ),
) -> dict[str, str]:
    """把不同授权统一投影成请求中可审计的 scope。"""

    if isinstance(authorization, LLMEvolutionRequestAuthorization):
        return {
            "authorization_id": authorization.authorization_id,
            "scope_kind": "evolution_request",
            "scope_sha256": authorization.authorization_sha256,
        }
    return {
        "authorization_id": authorization.authorization_id,
        "scope_kind": "exact_request",
        "scope_sha256": authorization.request_sha256,
    }


def build_evolution_hypothesis_request(
    context: ResearchEvolutionContext,
    gap_brief: Mapping[str, object],
    *,
    authorization: (
        LLMEvolutionRequestAuthorization
        | LLMExportAuthorization
    ),
) -> PreparedDeepSeekRequest:
    """构造十个逻辑演化假设的脱敏固定请求。"""

    sanitized_brief = sanitize_evolution_gap_brief(gap_brief)
    brief_sha256 = sha256_json(sanitized_brief)
    if isinstance(authorization, LLMEvolutionRequestAuthorization):
        if authorization.discovery_family_id != context.discovery_family_id:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权 family 与上下文不一致",
            )
        if authorization.context_sha256 != context.context_sha256:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权 context hash 与上下文不一致",
            )
        if authorization.gap_report_hash != context.gap_report_hash:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权 gap report hash 与上下文不一致",
            )
        if authorization.memory_snapshot_hash != context.memory_snapshot_hash:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权 memory snapshot hash 与上下文不一致",
            )
        if authorization.brief_sha256 != brief_sha256:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权 brief hash 与脱敏简报不一致",
            )
        if authorization.allowed_agent_role is not AgentRole.HYPOTHESIS:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "演化授权角色必须固定为 hypothesis",
            )
    public_payload: dict[str, object] = {
        "information_class": "evolution_gap_brief",
        "gap_brief": sanitized_brief,
        "allowed_field_aliases": sanitized_brief["allowed_field_aliases"],
        "allowed_operator_families": sanitized_brief[
            "allowed_operator_families"
        ],
        "data_availability_tags": sanitized_brief[
            "data_availability_tags"
        ],
        "temporal_window_bins": sanitized_brief["temporal_window_bins"],
        "structure_axes": sanitized_brief["required_structure_axes"],
        "logical_slots": _EXPECTED_SLOTS,
        "design_contract": {
            "hypotheses_per_round": 10,
            "designs_per_hypothesis": 3,
            "three_designs_must_differ": True,
            "allowed_structure_axes": DesignDiversityPolicy().allowed_structure_axes,
            "forbid_temporal_only_change": True,
        },
        "semantic_vocabulary": {
            "event_tag": tuple(item.value for item in SemanticEvent),
            "context_tag": tuple(item.value for item in SemanticContext),
            "quality_tags": tuple(item.value for item in SemanticQuality),
            "direction_tag": tuple(item.value for item in SemanticDirection),
            "output_tag": tuple(item.value for item in SemanticOutput),
        },
    }
    audit_payload: dict[str, object] = {
        "information_class": "evolution_gap_brief",
        "audit_kind": "evolution_hypothesis_request",
        "public_payload_sha256": sha256_json(public_payload),
        "discovery_family_id": context.discovery_family_id,
        "context_sha256": context.context_sha256,
        "coverage_graph_manifest_hash": context.coverage_graph_manifest_hash,
        "memory_snapshot_hash": context.memory_snapshot_hash,
        "gap_report_hash": context.gap_report_hash,
        "brief_sha256": brief_sha256,
        "authorization_scope": _authorization_scope_payload(authorization),
        "evolution_binding": {
            "discovery_family_id": context.discovery_family_id,
            "context_sha256": context.context_sha256,
            "gap_report_hash": context.gap_report_hash,
            "memory_snapshot_hash": context.memory_snapshot_hash,
            "brief_sha256": brief_sha256,
            "authorization_scope_sha256": _authorization_scope_payload(
                authorization
            )["scope_sha256"],
        },
        "model_scope": {
            "agent_role": AgentRole.HYPOTHESIS.value,
            "tools_allowed": False,
        },
        "redaction_policy": context.external_model_redaction_policy,
    }
    return build_deepseek_request(
        campaign_id=f"{context.discovery_family_id}:evolution_hypothesis",
        agent_role=AgentRole.HYPOTHESIS,
        slot_ids=_EXPECTED_SLOTS,
        system_prompt=(
            "你是逻辑演化假设代理。只能基于给定脱敏 gap brief 输出正好十个逻辑假设 "
            "H01-H10 的 JSON object。根对象必须严格是 {\"hypotheses\":[...]}；每个数组元素"
            "必须包含 JSON 字段 \"logical_slot_id\"、\"mechanism_unverified\"、\"prior_claim\"、"
            "\"mechanism\"、\"expected_direction\"、\"observable_proxy\"、"
            "\"independent_verification\"、\"competing_explanations\"、\"failure_modes\"、"
            "\"falsification_path\"、\"gap_ids\" 和 \"source_records\"。可以额外输出完整"
            " \"semantic_plan\" 作为非阻断 E/C/Q/D/O 标签，原则上每条都应输出；该对象包含 \"event_tag\"、"
            "\"context_tag\"、\"quality_tags\"、\"direction_tag\" 和 \"output_tag\"；"
            "标签值只能来自 user payload 的 semantic_vocabulary，quality_tags 最多三项；"
            "若无法可靠分类可以省略，省略不影响假设审批或候选评价。\"source_records\" 的"
            "每项必须包含 \"source_record_id\"、\"claim_fragment\"、\"rationale\"、"
            "\"query_terms\"、\"year_start\"、\"year_end\"、\"result_limit\"。其中 "
            "\"competing_explanations\"、\"failure_modes\"、\"gap_ids\"、\"query_terms\""
            "必须是 JSON 数组，\"source_records\" 必须是非空 JSON 数组；\"query_terms\""
            "为 1-8 个字符串，\"result_limit\" 为 1-5 的整数，年份为 1900-2100 的整数。"
            "所有自然语言叙事字段必须使用简体中文，包括 prior_claim、mechanism、"
            "observable_proxy、independent_verification、competing_explanations、"
            "failure_modes、falsification_path 以及 source_records 中的 claim_fragment 和"
            "rationale；JSON 字段名和 query_terms 可以保留英文。"
            "mechanism_unverified 不是叙事字段，必须固定输出 JSON 布尔值 true；"
            "不得在该字段填写机制说明，也不得输出 false。"
            "expected_direction 必须以“正向：”或“负向：”开头，随后用中文明确描述"
            "因子值与统一未来收益标签的预期关系；不得只写相关、单调或波动方向。"
            "为避免响应截断，每条假设的 source_records 恰好一项，gap_ids 恰好一项，"
            "competing_explanations 和 failure_modes 各恰好一项，query_terms 使用 2-4 个"
            "短词；每个自然语言字符串不超过 60 个汉字，不要重复解释。"
            "如果 gap_brief 含 memory_feedback，必须避开其中记录的失败模式和重复结构，"
            "优先探索组合表现薄弱但尚未充分覆盖的结构；反馈只用于设计，不得推断或"
            "编造具体回测数值。"
            "如果 memory_feedback 含 semantic_quota，应让 H01-H06 优先覆盖指定的未覆盖"
            " Event×Context，H07-H09 优先覆盖指定的低覆盖组合，H10 保留自由探索；"
            "该 quota 只约束往哪里探索，不得改变 IC、收益方向或统计通过标准。"
            "只能输出这十条"
            "结构化草案，不得输出表达式、回测、发布动作，"
            "不得修改 evaluation policy、alpha、HAC、multiplicity family 或数据合同。"
        ),
        user_payload=public_payload,
        audit_payload=audit_payload,
        thinking="disabled",
    )


def _find_forbidden_response_keys(value: object) -> set[str]:
    """递归搜集模型响应中试图覆盖治理合同的键名。"""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = key.casefold()
                if normalized in _FORBIDDEN_RESPONSE_KEYS:
                    found.add(key)
            found.update(_find_forbidden_response_keys(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for child in value:
            found.update(_find_forbidden_response_keys(child))
    return found


def _has_chinese(value: object) -> bool:
    """判断叙事文本是否至少包含一个中文字符。"""

    return isinstance(value, str) and any("\u3400" <= char <= "\u9fff" for char in value)


def validate_chinese_narrative(
    drafts: Sequence[LogicalEvolutionHypothesisDraft],
) -> None:
    """要求假设叙事和来源说明使用中文，检索词仍可使用英文。"""

    narrative_fields = (
        "prior_claim",
        "mechanism",
        "observable_proxy",
        "independent_verification",
        "falsification_path",
    )
    for draft in drafts:
        for field in narrative_fields:
            if not _has_chinese(getattr(draft, field)):
                raise _response_error(
                    f"{draft.logical_slot_id} 的 {field} 必须使用中文"
                )
        for field in ("competing_explanations", "failure_modes"):
            values = getattr(draft, field)
            if any(not _has_chinese(value) for value in values):
                raise _response_error(
                    f"{draft.logical_slot_id} 的 {field} 必须使用中文"
                )
        for source in draft.source_records:
            for field in ("claim_fragment", "rationale"):
                if not _has_chinese(getattr(source, field)):
                    raise _response_error(
                        f"{draft.logical_slot_id}.source_records.{field} 必须使用中文"
                    )


def parse_evolution_hypothesis_response(
    response: Mapping[str, object],
    *,
    context: ResearchEvolutionContext,
) -> tuple[EvolutionHypothesisDraft, ...]:
    """严格解析正好十个逻辑槽的演化假设响应。"""

    forbidden_keys = _find_forbidden_response_keys(response)
    if forbidden_keys:
        raise _response_error(
            f"模型响应试图覆盖冻结政策或数据合同：{tuple(sorted(forbidden_keys))}"
        )
    normalized = dict(response)
    raw_hypotheses = response.get("hypotheses")
    if isinstance(raw_hypotheses, Sequence) and not isinstance(raw_hypotheses, str | bytes):
        hypotheses = []
        for raw in raw_hypotheses:
            item = dict(raw) if isinstance(raw, Mapping) else raw
            if isinstance(item, dict):
                # 机制状态属于本地冻结治理，不允许模型通过正文或 false 改写。
                item["mechanism_unverified"] = True
                for singleton_field in ("competing_explanations", "failure_modes"):
                    singleton_value = item.get(singleton_field)
                    if isinstance(singleton_value, str):
                        item[singleton_field] = [singleton_value]
            hypotheses.append(item)
        normalized["hypotheses"] = hypotheses
    try:
        parsed = EvolutionHypothesisResponse.model_validate(normalized)
    except ValidationError as error:
        raise _response_error(f"演化假设响应结构无效：{error}") from error
    validate_chinese_narrative(parsed.hypotheses)
    slots = tuple(item.logical_slot_id for item in parsed.hypotheses)
    if len(set(slots)) != len(slots) or set(slots) != set(_EXPECTED_SLOTS):
        raise _response_error(
            f"演化假设必须完整且仅覆盖 H01-H10，实际槽位：{slots}"
        )
    content_hashes = tuple(
        sha256_json(
            item.model_dump(
                mode="json",
                exclude={"logical_slot_id", "draft_sha256"},
            )
        )
        for item in parsed.hypotheses
    )
    if len(set(content_hashes)) != len(content_hashes):
        raise _response_error("模型响应包含重复逻辑内容")
    if context.discovery_family_id.startswith("llmfamily_1bae19965638a6ac9620e0b0"):
        raise FactorMinerError(
            FailureCode.POLLUTED_FAMILY_REJECTED,
            "污染 family 不得解析演化假设",
        )
    return tuple(
        sorted(parsed.hypotheses, key=lambda item: item.logical_slot_id)
    )


def approve_evolution_hypotheses(
    drafts: tuple[EvolutionHypothesisDraft, ...],
    decisions: tuple[EvolutionHypothesisApproval, ...],
    *,
    context: ResearchEvolutionContext,
    authorization: LLMEvolutionRequestAuthorization | None = None,
    expected_approval_role: str | None = None,
) -> ApprovedEvolutionHypothesisBatch:
    """核验十槽逻辑假设及其人工批准，生成不可变批准批次。"""

    def normalized_role(value: str | None, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise _approval_error("approval_required")
        return value.strip()

    authorized_role: str | None = None
    if authorization is not None:
        if (
            authorization.discovery_family_id != context.discovery_family_id
            or authorization.context_sha256 != context.context_sha256
        ):
            raise _approval_error("approval_required")
        authorized_role = normalized_role(
            authorization.approver_role,
            field_name="authorization.approver_role",
        )
    expected_role = (
        normalized_role(
            expected_approval_role,
            field_name="expected_approval_role",
        )
        if expected_approval_role is not None
        else None
    )
    if authorized_role is None and expected_role is None:
        raise _approval_error("approval_required")
    if (
        authorized_role is not None
        and expected_role is not None
        and authorized_role != expected_role
    ):
        raise _approval_error("approval_required")
    required_role = authorized_role or expected_role

    draft_by_slot = {
        item.logical_slot_id: item
        for item in sorted(drafts, key=lambda draft: draft.logical_slot_id)
    }
    if len(drafts) != 10 or tuple(draft_by_slot) != _EXPECTED_SLOTS:
        raise _approval_error("hypothesis_budget_invalid")
    if len(draft_by_slot) != len(drafts):
        raise _approval_error("hypothesis_budget_invalid")
    if len(decisions) != 10:
        raise _approval_error("approval_required")
    decision_by_slot = {
        item.logical_slot_id: item
        for item in sorted(decisions, key=lambda decision: decision.logical_slot_id)
    }
    if tuple(decision_by_slot) != _EXPECTED_SLOTS or len(decision_by_slot) != 10:
        raise _approval_error("approval_required")
    approval_roles = {
        normalized_role(
            item.approval_role,
            field_name="approval_role",
        )
        for item in decision_by_slot.values()
    }
    if len(approval_roles) != 1 or approval_roles != {required_role}:
        raise _approval_error("approval_required")
    ordered_approvals: list[EvolutionHypothesisApproval] = []
    for slot in _EXPECTED_SLOTS:
        draft = draft_by_slot[slot]
        decision = decision_by_slot[slot]
        if decision.decision != "approved":
            raise _approval_error("approval_required")
        if decision.context_sha256 != context.context_sha256:
            raise _approval_error("approval_required")
        if decision.discovery_family_id != context.discovery_family_id:
            raise _approval_error("approval_required")
        if decision.draft_sha256 != draft.draft_sha256:
            raise _approval_error("approval_required")
        # 入口只接受规范化角色；重新构造审批记录以同步重算决定 hash，
        # 避免把带空白的原对象写入不可变批准批次。
        ordered_approvals.append(
            EvolutionHypothesisApproval(
                logical_slot_id=decision.logical_slot_id,
                context_sha256=decision.context_sha256,
                draft_sha256=decision.draft_sha256,
                discovery_family_id=decision.discovery_family_id,
                approval_role=required_role,
                approved_at=decision.approved_at,
                decision=decision.decision,
            )
        )
    return ApprovedEvolutionHypothesisBatch(
        context_sha256=context.context_sha256,
        discovery_family_id=context.discovery_family_id,
        approvals=tuple(ordered_approvals),
    )


def require_approved_evolution_batch(
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    *,
    context_sha256: str | None = None,
    discovery_family_id: str | None = None,
    approval_batch_sha256: str | None = None,
) -> ApprovedEvolutionHypothesisBatch:
    """在演化表达式入口核验完整、已批准且 hash 绑定的批次。"""

    if approval_batch is None:
        raise _approval_error("approval_required")
    slots = tuple(item.logical_slot_id for item in approval_batch.approvals)
    if len(approval_batch.approvals) != 10 or set(slots) != set(_EXPECTED_SLOTS):
        raise _approval_error("hypothesis_budget_invalid")
    if any(item.decision != "approved" for item in approval_batch.approvals):
        raise _approval_error("approval_required")
    if (
        context_sha256 is not None
        and approval_batch.context_sha256 != context_sha256
    ):
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "evolution_approval_binding_mismatch",
        )
    if (
        discovery_family_id is not None
        and approval_batch.discovery_family_id != discovery_family_id
    ):
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "evolution_approval_binding_mismatch",
        )
    if (
        approval_batch_sha256 is not None
        and approval_batch.approval_batch_sha256 != approval_batch_sha256
    ):
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "evolution_approval_binding_mismatch",
        )
    return approval_batch
