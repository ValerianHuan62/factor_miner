"""LLM 公开别名 AST 的硬校验、semantic lint 与可信候选转换。"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Literal

from factor_miner.compiler import compile_candidate
from factor_miner.design_diversity import (
    DesignDiversityResult,
    preflight_designs,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from factor_miner.dsl import AstMetadata, validate_ast
from factor_miner.dsl_semantics import SemanticAnalysis, analyse_semantic_type
from factor_miner.field_registry import (
    FieldAvailabilityRegistry,
    resolve_public_field_aliases,
)
from factor_miner.llm_hypothesis import RegisteredCoverageGapHypothesis
from factor_miner.research_evolution_schema import DesignDiversityPolicy
from factor_miner.schema import (
    AvailabilitySpec,
    FactorNode,
    HypothesisSpec,
    MechanismStatus,
    TrustedCandidateFactorSpec,
    registered_trusted_candidate,
)


class CandidateExpressionDraft(BaseModel):
    """代理 2 对一个预留槽输出的公开别名 AST。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_slot_id: str = Field(
        pattern=(
            r"^(coverage_outcome_llm|literature_only_llm|"
            r"mechanical_mutation|hypothesis_conditioned_grammar):C\d{3}$"
        )
    )
    expression: FactorNode


class SemanticLintDecision(BaseModel):
    """代理 3 只能追加批准或拒绝，不能修改输入对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_slot_id: str
    decision: Literal["approved", "rejected"]
    proxy_alignment: bool
    direction_alignment: bool
    availability_alignment: bool
    undeclared_exposure: bool
    reason_codes: tuple[str, ...]
    summary: str

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """原因代码不得重复或为空。"""

        if any(not value.strip() for value in values):
            raise ValueError("semantic lint 原因代码不能为空")
        if len(set(values)) != len(values):
            raise ValueError("semantic lint 原因代码不能重复")
        return values


class CandidateExpressionBatch(BaseModel):
    """代理 2 对同一假设的最多三个预留候选槽输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[CandidateExpressionDraft, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_unique_slots(self) -> CandidateExpressionBatch:
        """表达式批次不得重复槽位。"""

        slots = tuple(item.candidate_slot_id for item in self.candidates)
        if len(set(slots)) != len(slots):
            raise ValueError("表达式代理输出包含重复槽位")
        return self


class SemanticLintBatch(BaseModel):
    """代理 3 对一个假设下候选的批量只读审查。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[SemanticLintDecision, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_unique_slots(self) -> SemanticLintBatch:
        """语义审查批次不得重复槽位。"""

        slots = tuple(item.candidate_slot_id for item in self.decisions)
        if len(set(slots)) != len(slots):
            raise ValueError("semantic lint 输出包含重复槽位")
        return self


@dataclass(frozen=True, slots=True)
class CandidateExpressionValidation:
    """候选表达式通过固定程序硬闸门后的内部结果。"""

    semantic: SemanticAnalysis
    local_expression: FactorNode
    metadata: AstMetadata


@dataclass(frozen=True, slots=True)
class CandidateBatchPreflight:
    """批量候选在进入终态前的程序化 preflight 结果。"""

    candidates: tuple[TrustedCandidateFactorSpec, ...]
    design_result: DesignDiversityResult


def _public_fields(node: FactorNode) -> frozenset[str]:
    """收集模型输出 AST 中的公开字段别名。"""

    values = {node.field} if node.op == "field" and node.field else set()
    for child in node.args:
        values.update(_public_fields(child))
    return frozenset(values)


def _operator_families(node: FactorNode) -> frozenset[str]:
    """把具体 DSL 算子映射到冻结的粗粒度算子族。"""

    families: set[str] = set()
    if node.op in {"add", "sub", "mul", "div", "neg", "abs"}:
        families.add("arithmetic")
    elif node.op in {"delay", "delta"}:
        families.add("temporal")
    elif node.op.startswith("rolling_"):
        families.add("rolling")
    for child in node.args:
        families.update(_operator_families(child))
    return frozenset(families)


def _allowed_local_fields(registry: FieldAvailabilityRegistry) -> tuple[str, ...]:
    """提取当前注册表允许进入因子 DSL 的本地字段。"""

    return tuple(
        item.field_id
        for item in registry.fields
        if item.eligible_for_factor and item.point_in_time_guarantee
    )


def _build_trusted_candidate_spec(
    *,
    validation: CandidateExpressionValidation,
    hypothesis: RegisteredCoverageGapHypothesis,
    created_at: datetime,
    provenance: dict[str, str],
    registry: FieldAvailabilityRegistry,
) -> TrustedCandidateFactorSpec:
    """把已通过硬校验的候选装配为可信 spec。"""

    semantic = validation.semantic
    local_expression = validation.local_expression
    metadata = validation.metadata
    proposal = hypothesis.draft.prediction_proposal
    frozen_hypothesis = HypothesisSpec(
        claim=hypothesis.draft.claim,
        mechanism=hypothesis.draft.economic_mechanism,
        expected_sign=hypothesis.testable_prediction.expected_sign,
        observable_proxy=proposal.observable_proxy,
        independent_verification=hypothesis.draft.independent_verification,
        competing_explanations=hypothesis.draft.competing_explanations,
        baseline_reference="v0.4_coverage_graph_and_reference_library",
        failure_modes=hypothesis.draft.failure_modes,
        falsification_path=hypothesis.draft.falsification_path,
        source_refs=hypothesis.decision.verified_source_record_ids,
        mechanism_status=MechanismStatus.MECHANISM_UNVERIFIED,
    )
    derived_provenance = dict(provenance)
    if proposal.measurement_contract is not None:
        from factor_miner.hypothesis_constraints import compile_gamma
        gamma = compile_gamma(frozen_hypothesis, proposal.measurement_contract,
                              tuple(x.field_id for x in registry.fields if x.eligible_for_factor))
        gamma.validate(local_expression, frozen_hypothesis)
        derived_provenance.update(gamma_json=gamma.model_dump_json(), gamma_sha256=gamma.identity)
    if proposal.observable_condition is not None:
        derived_provenance["observable_condition_json"] = proposal.observable_condition.model_dump_json()
    derived_provenance.update(
        {
            "hypothesis_id": hypothesis.hypothesis_id,
            "field_registry_id": provenance.get("field_registry_id", ""),
            "data_release_id": provenance.get("data_release_id", ""),
            "semantic_unit": semantic.unit_dimension,
        }
    )
    derived_provenance = {
        key: value for key, value in derived_provenance.items() if value
    }
    return TrustedCandidateFactorSpec(
        hypothesis=frozen_hypothesis,
        expression=local_expression,
        required_fields=metadata.required_fields,
        max_lookback=metadata.lookback,
        availability=AvailabilitySpec(
            observation="close_t",
            decision="after_close_t",
            earliest_trade="open_t_plus_1",
        ),
        created_at=created_at,
        provenance=derived_provenance,
    )


def _approved_lint_for_slot(
    *,
    draft: CandidateExpressionDraft,
    lint_batch: SemanticLintBatch,
) -> SemanticLintDecision:
    """取出并验证某个槽位可进入终态的 lint 决策。"""

    lint = next(
        (
            item
            for item in lint_batch.decisions
            if item.candidate_slot_id == draft.candidate_slot_id
        ),
        None,
    )
    if lint is None:
        raise ValueError(f"semantic lint 缺少槽位 {draft.candidate_slot_id}")
    if lint.candidate_slot_id != draft.candidate_slot_id:
        raise ValueError("semantic lint 与候选槽不一致")
    if (
        lint.decision != "approved"
        or not lint.proxy_alignment
        or not lint.direction_alignment
        or not lint.availability_alignment
        or lint.undeclared_exposure
    ):
        raise ValueError("只有全部维度批准的 semantic lint 可以进入转换")
    return lint


def validate_candidate_expression(
    *,
    draft: CandidateExpressionDraft,
    hypothesis: RegisteredCoverageGapHypothesis,
    registry: FieldAvailabilityRegistry,
) -> CandidateExpressionValidation:
    """在 semantic lint 前执行不可覆盖的字段、类型、时点和 DSL 校验。"""

    proposal = hypothesis.draft.prediction_proposal
    if not _public_fields(draft.expression).issubset(
        proposal.proposed_field_aliases
    ):
        raise ValueError("候选 AST 引入假设未声明字段")
    if not _operator_families(draft.expression).issubset(
        proposal.proposed_operator_families
    ):
        raise ValueError("候选 AST 引入假设未声明算子族")

    semantic = analyse_semantic_type(draft.expression, registry)
    if semantic.earliest_decision_time != "after_close_t":
        raise ValueError("首版候选只支持收盘后决策")
    local_expression = resolve_public_field_aliases(
        draft.expression,
        registry,
    )
    allowed_fields = tuple(
        item.field_id
        for item in registry.fields
        if item.eligible_for_factor and item.point_in_time_guarantee
    )
    metadata = validate_ast(
        local_expression,
        allowed_fields=allowed_fields,
        forbidden_fields=("label_o2o_5d", "future_return"),
    )
    return CandidateExpressionValidation(
        semantic=semantic,
        local_expression=local_expression,
        metadata=metadata,
    )


def preflight_candidate_batch(
    *,
    candidate_batch: CandidateExpressionBatch,
    lint_batch: SemanticLintBatch,
    hypothesis: RegisteredCoverageGapHypothesis,
    registry: FieldAvailabilityRegistry,
    created_at: datetime,
    provenance_by_slot: dict[str, dict[str, str]],
    policy: DesignDiversityPolicy,
) -> CandidateBatchPreflight:
    """在候选槽进入终态前执行三因子批量 preflight。"""

    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("候选创建时间必须带时区")
    if len(candidate_batch.candidates) != policy.designs_per_hypothesis:
        raise ValueError(
            f"每个假设必须提供 {policy.designs_per_hypothesis} 个候选设计"
        )

    trusted_candidates: list[TrustedCandidateFactorSpec] = []
    local_expressions: list[FactorNode] = []
    allowed_fields = _allowed_local_fields(registry)

    for draft in candidate_batch.candidates:
        _approved_lint_for_slot(draft=draft, lint_batch=lint_batch)
        validation = validate_candidate_expression(
            draft=draft,
            hypothesis=hypothesis,
            registry=registry,
        )
        provenance = dict(provenance_by_slot.get(draft.candidate_slot_id, {}))
        provenance.update(
            {
                "candidate_slot_id": draft.candidate_slot_id,
                "field_registry_id": registry.registry_id,
                "data_release_id": registry.data_release_id,
            }
        )
        candidate = _build_trusted_candidate_spec(
            validation=validation,
            hypothesis=hypothesis,
            created_at=created_at,
            provenance=provenance,
            registry=registry,
        )
        compile_candidate(
            registered_trusted_candidate(candidate),
            allowed_fields=allowed_fields,
        )
        trusted_candidates.append(candidate)
        local_expressions.append(validation.local_expression)

    slot_ids = tuple(
        draft.candidate_slot_id for draft in candidate_batch.candidates
    )
    design_result = preflight_designs(
        tuple(local_expressions),  # type: ignore[arg-type]
        policy=policy,
        registry=registry,
    ).bind_slot_ids(slot_ids)  # type: ignore[arg-type]
    return CandidateBatchPreflight(
        candidates=tuple(trusted_candidates),
        design_result=design_result,
    )


def convert_candidate(
    *,
    draft: CandidateExpressionDraft,
    hypothesis: RegisteredCoverageGapHypothesis,
    lint: SemanticLintDecision,
    registry: FieldAvailabilityRegistry,
    created_at: datetime,
    provenance: dict[str, str],
) -> TrustedCandidateFactorSpec:
    """把三角色输出转成由程序推导字段、lookback 和时点的可信候选。"""

    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("候选创建时间必须带时区")
    _approved_lint_for_slot(
        draft=draft,
        lint_batch=SemanticLintBatch(decisions=(lint,)),
    )
    validation = validate_candidate_expression(
        draft=draft,
        hypothesis=hypothesis,
        registry=registry,
    )
    return _build_trusted_candidate_spec(
        registry=registry,
        validation=validation,
        hypothesis=hypothesis,
        created_at=created_at,
        provenance={
            **provenance,
            "field_registry_id": registry.registry_id,
            "data_release_id": registry.data_release_id,
        },
    )
