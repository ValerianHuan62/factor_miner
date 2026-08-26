"""V0.5 假设提案、固定预测合成与文献用途合同。"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from factor_miner.canonical import sha256_json
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapCard,
)
from factor_miner.schema import (
    EvaluationPolicySpec,
    ExpectedSign,
    evaluation_policy_id,
)


class HypothesisLiteratureStatus(StrEnum):
    """假设获得的最强文献支持类型。"""

    MECHANISM_CLAIM_SUPPORTED = "mechanism_claim_supported"
    PROXY_CHOICE_SUPPORTED = "proxy_choice_supported"
    BACKGROUND_ONLY = "background_only"
    NOVEL_UNVERIFIED = "novel_unverified"


class CandidatePredictionOutcome(StrEnum):
    """候选层的严格结果或探索性筛选状态。"""

    SUPPORTED = "supported_on_fresh_visible_validation"
    REVERSE = "falsified_on_fresh_visible_validation"
    INCONCLUSIVE = "inconclusive_on_fresh_visible_validation"
    PASSES_EXPLORATORY_FILTER = "passes_exploratory_filter"
    FAILS_EXPLORATORY_FILTER = "fails_exploratory_filter"
    INCONCLUSIVE_EXPLORATORY = "inconclusive_exploratory"


class HypothesisDiscoverySummary(StrEnum):
    """多个候选结果在假设层的无因果含义摘要。"""

    NO_VALID_CANDIDATE = "no_valid_candidate"
    HAS_SUPPORTED_CANDIDATE = "has_supported_candidate"
    MIXED_CANDIDATE_EVIDENCE = "mixed_candidate_evidence"
    ALL_CANDIDATES_INCONCLUSIVE = "all_candidates_inconclusive"
    ALL_VALID_CANDIDATES_REVERSE = "all_valid_candidates_reverse"


class PredictionProposal(BaseModel):
    """LLM 仅可建议、不能覆盖评价规则的预测字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observable_proxy: str
    expected_sign: ExpectedSign
    proposed_field_aliases: tuple[str, ...] = Field(min_length=1)
    proposed_operator_families: tuple[str, ...] = Field(min_length=1)
    optional_conditioning_claim: str | None = None

    @field_validator("observable_proxy")
    @classmethod
    def validate_proxy(cls, value: str) -> str:
        """可观察代理不得为空。"""

        if not value.strip():
            raise ValueError("observable_proxy 不能为空")
        return value

    @field_validator("proposed_field_aliases", "proposed_operator_families")
    @classmethod
    def validate_sorted_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """字段和算子族必须非空、唯一且排序。"""

        if any(not value.strip() for value in values):
            raise ValueError("预测提案序列不能包含空项")
        if tuple(sorted(set(values))) != values:
            raise ValueError("预测提案序列必须唯一且按字典序排序")
        return values

    @field_validator("optional_conditioning_claim")
    @classmethod
    def validate_optional_claim(cls, value: str | None) -> str | None:
        """已提供的条件主张不得为空字符串。"""

        if value is not None and not value.strip():
            raise ValueError("optional_conditioning_claim 不能是空字符串")
        return value


class TestablePredictionSpec(BaseModel):
    """由固定程序注入完整评价规则的机器可执行预测合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    universe_id: str
    applicable_market: str
    hypothesis_axis: str
    formation_time: str
    prediction_start: str
    horizon_sessions: int = Field(ge=1)
    conditioning_variables: tuple[str, ...]
    effect_definition: str
    expected_sign: ExpectedSign
    primary_metric: str
    null_hypothesis: str
    minimum_effect_size: FiniteFloat = Field(ge=0)
    alpha: FiniteFloat = Field(gt=0, le=0.05)
    multiplicity_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    minimum_valid_dates: int = Field(ge=1)
    minimum_names_per_date: int = Field(ge=2)
    minimum_median_coverage: FiniteFloat = Field(ge=0, le=1)
    support_rule: str
    falsification_rule: str
    inconclusive_rule: str
    allowed_robustness_checks: tuple[str, ...]


class CitationSupportRecord(BaseModel):
    """人工核验到具体段落及论断用途的公开来源记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_record_id: str
    identity_status: str
    passage_locator: str
    minimal_excerpt: str
    support_kind: HypothesisLiteratureStatus
    supported_claim_fragment: str
    applicable_market: str
    applicable_sample: str
    counterevidence_search_performed: bool
    counterevidence_queries: tuple[str, ...]
    counterevidence_source_ids: tuple[str, ...]
    counterevidence_cutoff: date | None
    counterevidence_summary: str
    counterevidence_limitations: str
    reviewer_role: str
    created_at: datetime

    @field_validator("support_kind")
    @classmethod
    def reject_hypothesis_only_status(
        cls,
        value: HypothesisLiteratureStatus,
    ) -> HypothesisLiteratureStatus:
        """单条引用不能自称 novel_unverified。"""

        if value is HypothesisLiteratureStatus.NOVEL_UNVERIFIED:
            raise ValueError("novel_unverified 只能是人工批准的假设级状态")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """人工核验时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_counterevidence(self) -> CitationSupportRecord:
        """执行反证检索时必须保留查询、截止日和已知局限。"""

        if self.counterevidence_search_performed:
            if (
                not self.counterevidence_queries
                or self.counterevidence_cutoff is None
                or not self.counterevidence_limitations.strip()
            ):
                raise ValueError("反证检索必须记录查询、截止日和局限")
        return self


class CoverageGapHypothesisDraft(BaseModel):
    """代理 1 在固定槽中生成、尚未经人工批准的假设草案。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_id: str = Field(
        pattern=r"^(coverage_outcome_llm|literature_only_llm):H(0[1-9]|10)$"
    )
    gap_id: str
    hypothesis_origin: str
    claim: str
    economic_mechanism: str
    independent_verification: str
    competing_explanations: tuple[str, ...] = Field(min_length=1)
    failure_modes: tuple[str, ...] = Field(min_length=1)
    falsification_path: str
    source_record_ids: tuple[str, ...] = Field(min_length=1)
    prediction_proposal: PredictionProposal

    @field_validator(
        "gap_id",
        "hypothesis_origin",
        "claim",
        "economic_mechanism",
        "independent_verification",
        "falsification_path",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """假设关键文本不得为空。"""

        if not value.strip():
            raise ValueError("假设草案文本不能为空")
        return value

    @field_validator(
        "competing_explanations",
        "failure_modes",
        "source_record_ids",
    )
    @classmethod
    def validate_sequences(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """假设序列不得包含空项或重复项。"""

        if any(not value.strip() for value in values):
            raise ValueError("假设草案序列不能包含空项")
        if len(set(values)) != len(values):
            raise ValueError("假设草案序列不能重复")
        return values


class HypothesisDecision(BaseModel):
    """人工对假设草案追加的不可变批准或拒绝事件内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{24}$")
    decision: str
    reason: str
    verified_source_record_ids: tuple[str, ...]
    literature_status: HypothesisLiteratureStatus
    reviewer_role: str
    created_at: datetime

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        """人工决定只允许批准或拒绝。"""

        if value not in {"approved", "rejected"}:
            raise ValueError("decision 只能为 approved 或 rejected")
        return value

    @field_validator("reason", "reviewer_role")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """决定理由和匿名角色不得为空。"""

        if not value.strip():
            raise ValueError("人工决定文本不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """人工决定时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value


class RegisteredCoverageGapHypothesis(BaseModel):
    """人工批准、机制仍未验证的内容寻址假设。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str = Field(pattern=r"^llmhyp_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft: CoverageGapHypothesisDraft
    testable_prediction: TestablePredictionSpec
    decision: HypothesisDecision
    mechanism_status: str

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredCoverageGapHypothesis:
        """假设身份必须绑定草案、预测和人工决定。"""

        payload = {
            "draft": self.draft.model_dump(mode="json"),
            "testable_prediction": self.testable_prediction.model_dump(mode="json"),
            "decision": self.decision.model_dump(mode="json"),
            "mechanism_status": self.mechanism_status,
        }
        expected = sha256_json(payload)
        if self.spec_hash != expected:
            raise ValueError("登记假设 spec_hash 与内容不一致")
        if self.hypothesis_id != f"llmhyp_{expected[:24]}":
            raise ValueError("登记假设 ID 与内容不一致")
        if self.mechanism_status != "mechanism_unverified":
            raise ValueError("V0.5 假设机制必须保持 mechanism_unverified")
        return self


class ArmScopedApprovedHypothesis(BaseModel):
    """同一逻辑批准批次适配出的 arm 侧旧假设草案包装。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_hypothesis_id: str = Field(pattern=r"^llmevohyp_[0-9a-f]{24}$")
    approval_batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    arm_id: str = Field(
        pattern=r"^(coverage_outcome_llm|literature_only_llm)$"
    )
    generation_route: str
    literature_task_ids: tuple[str, ...] = ()
    draft: CoverageGapHypothesisDraft


def _logical_hypothesis_id(
    *,
    draft: LogicalEvolutionHypothesisDraft,
    approval_batch: ApprovedEvolutionHypothesisBatch,
) -> str:
    """基于逻辑草案和批准批次生成跨 arm 共享的 hypothesis_id。"""

    identity_hash = sha256_json(
        {
            "discovery_family_id": approval_batch.discovery_family_id,
            "context_sha256": approval_batch.context_sha256,
            "approval_batch_sha256": approval_batch.approval_batch_sha256,
            "draft_sha256": draft.draft_sha256,
        }
    )
    return f"llmevohyp_{identity_hash[:24]}"


def _expected_sign_from_direction(expected_direction: str) -> ExpectedSign:
    """把逻辑方向映射到旧预测提案的 ExpectedSign。"""

    normalized = expected_direction.strip().casefold()
    if normalized in {"positive", "正向", "up"}:
        return ExpectedSign.POSITIVE
    if normalized in {"negative", "负向", "down"}:
        return ExpectedSign.NEGATIVE
    raise ValueError("当前旧接口只接受 positive/negative 方向")


def adapt_approved_evolution_hypotheses_for_arm(
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch,
    drafts: tuple[LogicalEvolutionHypothesisDraft, ...],
    gap_cards_by_id: dict[str, CoverageGapCard],
    arm_id: str,
    generation_route: str,
    literature_task_ids_by_slot: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ArmScopedApprovedHypothesis, ...]:
    """把一个逻辑批准批次显式适配为单个 arm 的旧假设草案。"""

    if arm_id not in {"coverage_outcome_llm", "literature_only_llm"}:
        raise ValueError("只允许为两个 LLM arm 适配旧假设草案")
    if not generation_route.strip():
        raise ValueError("generation_route 不能为空")
    draft_by_slot = {item.logical_slot_id: item for item in drafts}
    if len(draft_by_slot) != 10:
        raise ValueError("逻辑草案必须正好十个且槽位唯一")
    approval_by_slot = {
        item.logical_slot_id: item for item in approval_batch.approvals
    }
    if tuple(sorted(draft_by_slot)) != tuple(sorted(approval_by_slot)):
        raise ValueError("逻辑草案与批准批次槽位不一致")
    adapted: list[ArmScopedApprovedHypothesis] = []
    for logical_slot_id in sorted(draft_by_slot):
        draft = draft_by_slot[logical_slot_id]
        approval = approval_by_slot[logical_slot_id]
        if approval.draft_sha256 != draft.draft_sha256:
            raise ValueError("批准记录与逻辑草案 hash 不一致")
        gap_cards = tuple(
            gap_cards_by_id[gap_id] for gap_id in draft.gap_ids
        )
        field_aliases = tuple(
            sorted(
                {
                    alias
                    for card in gap_cards
                    for alias in card.allowed_field_aliases
                }
            )
        )
        operator_families = tuple(
            sorted(
                {
                    family
                    for card in gap_cards
                    for family in card.allowed_operator_families
                }
            )
        )
        task_ids = (
            literature_task_ids_by_slot.get(logical_slot_id, ())
            if literature_task_ids_by_slot is not None
            else ()
        )
        if not task_ids:
            task_ids = tuple(
                item.source_record_id for item in draft.source_records
            )
        legacy_draft = CoverageGapHypothesisDraft(
            slot_id=f"{arm_id}:{logical_slot_id}",
            gap_id=draft.gap_ids[0],
            hypothesis_origin=generation_route,
            claim=draft.prior_claim,
            economic_mechanism=draft.mechanism,
            independent_verification=draft.independent_verification,
            competing_explanations=draft.competing_explanations,
            failure_modes=draft.failure_modes,
            falsification_path=draft.falsification_path,
            source_record_ids=task_ids,
            prediction_proposal=PredictionProposal(
                observable_proxy=draft.observable_proxy,
                expected_sign=_expected_sign_from_direction(
                    draft.expected_direction
                ),
                proposed_field_aliases=field_aliases,
                proposed_operator_families=operator_families,
                optional_conditioning_claim=None,
            ),
        )
        adapted.append(
            ArmScopedApprovedHypothesis(
                logical_hypothesis_id=_logical_hypothesis_id(
                    draft=draft,
                    approval_batch=approval_batch,
                ),
                approval_batch_sha256=approval_batch.approval_batch_sha256,
                context_sha256=approval_batch.context_sha256,
                discovery_family_id=approval_batch.discovery_family_id,
                arm_id=arm_id,
                generation_route=generation_route,
                literature_task_ids=task_ids,
                draft=legacy_draft,
            )
        )
    return tuple(adapted)


def compose_testable_prediction(
    proposal: PredictionProposal,
    policy: EvaluationPolicySpec,
    *,
    discovery_family_id: str,
) -> TestablePredictionSpec:
    """由代码内置评价政策合成预测合同，不接受模型复述配置。"""

    return TestablePredictionSpec(
        evaluation_policy_id=evaluation_policy_id(policy),
        universe_id=policy.universe.universe_id,
        applicable_market="company_a_share",
        hypothesis_axis="cross_sectional",
        formation_time=policy.availability.decision,
        prediction_start=policy.availability.earliest_trade,
        horizon_sessions=policy.label_horizon_sessions,
        conditioning_variables=(
            (proposal.optional_conditioning_claim,)
            if proposal.optional_conditioning_claim is not None
            else ()
        ),
        effect_definition="daily_cross_sectional_spearman_factor_vs_o2o_5d",
        expected_sign=proposal.expected_sign,
        primary_metric="mean_daily_rank_ic",
        null_hypothesis="oriented_mean_rank_ic_le_zero",
        minimum_effect_size=policy.min_abs_mean_rank_ic,
        alpha=policy.alpha,
        multiplicity_family_id=discovery_family_id,
        minimum_valid_dates=policy.min_valid_dates,
        minimum_names_per_date=policy.min_names_per_date,
        minimum_median_coverage=policy.min_median_coverage,
        support_rule="effect_and_hac_bonferroni_and_incremental_gates_pass",
        falsification_rule="significant_minimum_effect_in_reverse_direction",
        inconclusive_rule="all_other_results",
        allowed_robustness_checks=(
            "annual_rank_ic_summary",
            "regime_weighted_rank_ic_summary",
            "raw_vs_residual_rank_ic_comparison",
        ),
    )


def registered_coverage_gap_hypothesis(
    draft: CoverageGapHypothesisDraft,
    prediction: TestablePredictionSpec,
    decision: HypothesisDecision,
) -> RegisteredCoverageGapHypothesis:
    """把人工批准草案登记为不可变、机制未验证的假设。"""

    if decision.decision != "approved":
        raise ValueError("只有人工批准的草案可以登记")
    if not set(decision.verified_source_record_ids).issubset(
        draft.source_record_ids
    ):
        raise ValueError("人工核验来源必须来自草案 source_record_ids")
    payload = {
        "draft": draft.model_dump(mode="json"),
        "testable_prediction": prediction.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "mechanism_status": "mechanism_unverified",
    }
    spec_hash = sha256_json(payload)
    return RegisteredCoverageGapHypothesis(
        hypothesis_id=f"llmhyp_{spec_hash[:24]}",
        spec_hash=spec_hash,
        draft=draft,
        testable_prediction=prediction,
        decision=decision,
        mechanism_status="mechanism_unverified",
    )


def validate_novel_unverified_quota(
    statuses: tuple[HypothesisLiteratureStatus, ...],
) -> None:
    """限制每个十槽 LLM arm 最多两个新颖未验证假设。"""

    if len(statuses) > 10:
        raise ValueError("单个 LLM arm 最多包含 10 个假设槽")
    if statuses.count(HypothesisLiteratureStatus.NOVEL_UNVERIFIED) > 2:
        raise ValueError("每个 10 槽 LLM arm 的 novel_unverified 最多 2 个")


def summarize_hypothesis_candidates(
    outcomes: tuple[CandidatePredictionOutcome, ...],
) -> HypothesisDiscoverySummary:
    """按冻结优先级汇总严格候选结果，不推断经济机制。"""

    exploratory = {
        CandidatePredictionOutcome.PASSES_EXPLORATORY_FILTER,
        CandidatePredictionOutcome.FAILS_EXPLORATORY_FILTER,
        CandidatePredictionOutcome.INCONCLUSIVE_EXPLORATORY,
    }
    if any(outcome in exploratory for outcome in outcomes):
        raise ValueError("探索性筛选不能汇总成假设支持状态")
    if not outcomes:
        return HypothesisDiscoverySummary.NO_VALID_CANDIDATE
    unique = set(outcomes)
    if unique == {CandidatePredictionOutcome.INCONCLUSIVE}:
        return HypothesisDiscoverySummary.ALL_CANDIDATES_INCONCLUSIVE
    if unique == {CandidatePredictionOutcome.REVERSE}:
        return HypothesisDiscoverySummary.ALL_VALID_CANDIDATES_REVERSE
    if CandidatePredictionOutcome.REVERSE in unique:
        return HypothesisDiscoverySummary.MIXED_CANDIDATE_EVIDENCE
    return HypothesisDiscoverySummary.HAS_SUPPORTED_CANDIDATE
