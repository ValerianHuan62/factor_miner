"""记忆辅助研究演化的不可变、脱敏合同。

本模块只定义治理层 schema 和内容寻址 builder，不读取数据、不计算缺口，也不
编排研究流程。所有公开摘要都使用离散标签；原始逐日结果必须留在评价产物中。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.semantic_coverage import SemanticPlanTags

POLLUTED_DISCOVERY_FAMILY_ID = "llmfamily_1bae19965638a6ac9620e0b0"
_HASH = r"^[0-9a-f]{64}$"
_ID = r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$"
_DISCRETE_BANDS = {"none", "unknown", "low", "medium", "high", "short", "long", "stable", "unstable", "available", "unavailable"}
_PUBLIC_LABELS = {
    "coverage", "structural", "market_regime", "data_availability", "regime",
    "field_set", "operator_topology", "temporal_role", "input_combination",
    "missing", "stale", "constant", "redundant", "insufficient_coverage",
    "no_coverage", "low_coverage", "high_coverage",
}
_PUBLIC_FIELD_ALIASES = {
    "close", "price_close", "open", "price_open", "high", "price_high", "low", "price_low",
    "volume", "amount", "turnover", "market_cap", "vwap", "returns",
}
_PUBLIC_OPERATOR_FAMILIES = {
    "arithmetic", "rolling", "temporal", "cross_sectional", "rank", "ranking", "interaction", "aggregation", "conditional",
}
_PUBLIC_WINDOW_BINS = {"short", "medium", "long", "unknown"}
_PUBLIC_RISKS = {"none", "missing", "stale", "constant", "redundant", "insufficient_coverage", "lookahead", "invalid"}
_COUNT_BANDS = {"none", "unknown", "low", "medium", "high", "0", "1", "2_4", "5_plus"}
_SUMMARY_BANDS = _DISCRETE_BANDS | {"supported", "reversed", "inconclusive", "passed", "failed"}


def _fail(code: FailureCode, message: str) -> None:
    """抛出带稳定失败码的合同错误。"""

    raise FactorMinerError(code, message)


def _validate_public_values(values: tuple[str, ...], field: str, allowed: set[str]) -> tuple[str, ...]:
    """只允许登记过的公开词，杜绝依赖黑名单的脱敏边界。"""

    invalid = tuple(value for value in values if value not in allowed)
    if invalid:
        _fail(FailureCode.COVERAGE_GAP_INVALID, f"{field} 包含未登记公开词：{invalid}")
    return values


class _FrozenModel(BaseModel):
    """所有演化合同共享的冻结配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class GapCategory(StrEnum):
    """覆盖缺口的固定分类。"""

    STRUCTURAL = "structural"
    MARKET_REGIME = "market_regime"
    DATA_AVAILABILITY = "data_availability"


class DesignDiversityPolicy(_FrozenModel):
    """每轮设计数量和结构差异的冻结政策。"""

    hypotheses_per_round: int = 10
    designs_per_hypothesis: int = 3
    arms_per_round: int = 4
    total_slots: int = 120
    allowed_structure_axes: tuple[str, ...] = (
        "field_set", "operator_topology", "temporal_role", "input_combination",
        "node_count", "depth_range",
    )
    forbid_temporal_only_change: bool = True
    max_single_hypothesis_output_correlation: FiniteFloat = 0.80
    failure_code_version: str = "evolution-v1"

    @model_validator(mode="after")
    def validate_fixed_budget(self) -> DesignDiversityPolicy:
        """确保设计政策不能改变研究预算或结构轴。"""

        expected = {
            "hypotheses_per_round": 10, "designs_per_hypothesis": 3,
            "arms_per_round": 4, "total_slots": 120,
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                _fail(FailureCode.DESIGN_DIVERSITY_FAILED, f"{field} 必须固定为 {value}")
        required = {"field_set", "operator_topology", "temporal_role", "input_combination", "node_count", "depth_range"}
        if set(self.allowed_structure_axes) != required:
            _fail(FailureCode.DESIGN_DIVERSITY_FAILED, "allowed_structure_axes 必须完整覆盖六个结构轴")
        if not self.forbid_temporal_only_change:
            _fail(FailureCode.DESIGN_DIVERSITY_FAILED, "forbid_temporal_only_change 必须为 true")
        if not 0 <= self.max_single_hypothesis_output_correlation < 1:
            _fail(FailureCode.DESIGN_DIVERSITY_FAILED, "max_single_hypothesis_output_correlation 必须在 [0, 1) 内")
        return self


def _nonempty_sorted(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    """校验脱敏枚举集合非空、唯一且稳定排序。"""

    if not values or any(not item.strip() for item in values) or tuple(sorted(set(values))) != values:
        _fail(FailureCode.COVERAGE_GAP_INVALID, f"{field} 必须非空、唯一且按字典序排序")
    return values


class CoverageGapCard(_FrozenModel):
    """不含逐日数值、个股或路径的单张覆盖缺口卡片。"""

    gap_id: str
    gap_category: GapCategory
    sanitized_labels: tuple[str, ...] = Field(min_length=1)
    allowed_field_aliases: tuple[str, ...] = Field(min_length=1)
    allowed_operator_families: tuple[str, ...] = Field(min_length=1)
    temporal_window_bins: tuple[str, ...] = Field(min_length=1)
    structure_cluster_count_band: str
    signal_cluster_count_band: str
    missing_or_failure_risk: tuple[str, ...] = Field(min_length=1)
    card_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="before")
    @classmethod
    def validate_category(cls, values: Any) -> Any:
        """把未知 gap 类别转换为稳定合同失败。"""

        if isinstance(values, dict) and values.get("gap_category") not in {item.value for item in GapCategory}:
            _fail(FailureCode.COVERAGE_GAP_INVALID, f"未知 gap_category：{values.get('gap_category')}")
        return values

    @field_validator("sanitized_labels", "allowed_field_aliases", "allowed_operator_families", "temporal_window_bins", "missing_or_failure_risk")
    @classmethod
    def validate_lists(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        """禁止空项和不稳定排序，确保简报可复验。"""

        values = _nonempty_sorted(value, info.field_name)
        allowed = {
            "sanitized_labels": _PUBLIC_LABELS,
            "allowed_field_aliases": _PUBLIC_FIELD_ALIASES,
            "allowed_operator_families": _PUBLIC_OPERATOR_FAMILIES,
            "temporal_window_bins": _PUBLIC_WINDOW_BINS,
            "missing_or_failure_risk": _PUBLIC_RISKS,
        }[info.field_name]
        return _validate_public_values(values, info.field_name, allowed)

    @field_validator("structure_cluster_count_band", "signal_cluster_count_band")
    @classmethod
    def validate_band(cls, value: str) -> str:
        """数量字段只能使用离散带。"""

        if value not in _COUNT_BANDS:
            _fail(FailureCode.COVERAGE_GAP_INVALID, f"数量带不是允许的离散带：{value}")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> CoverageGapCard:
        """卡片创建时即复核内容 hash。"""

        expected = _identity(self, "card_sha256")
        if self.card_sha256 is not None and self.card_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"card_sha256 hash 不一致：实际 {self.card_sha256}，期望 {expected}")
        object.__setattr__(self, "card_sha256", expected)
        return self


class CoverageGapReport(_FrozenModel):
    """按固定顺序发布的覆盖缺口报告身份。"""

    coverage_graph_id: str = Field(pattern=_ID)
    graph_manifest_hash: str = Field(pattern=_HASH)
    regime_snapshot_hash: str = Field(pattern=_HASH)
    memory_snapshot_hash: str = Field(pattern=_HASH)
    gap_cards: tuple[CoverageGapCard, ...] = Field(min_length=1)
    truncated_count: int = Field(ge=0)
    truncation_reason: str
    internal_sort_policy_hash: str = Field(pattern=_HASH)
    report_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_sorted_cards(self) -> CoverageGapReport:
        """报告中的卡片必须按类别、风险和卡片身份稳定排序。"""

        keys = tuple((card.gap_category.value, card.card_sha256, card.gap_id) for card in self.gap_cards)
        if keys != tuple(sorted(keys)):
            _fail(FailureCode.COVERAGE_GAP_INVALID, "gap_cards 必须按固定顺序排序")
        if not self.truncation_reason.strip():
            _fail(FailureCode.COVERAGE_GAP_INVALID, "truncation_reason 不能为空")
        if self.truncated_count == 0 and self.truncation_reason != "未发生截断":
            _fail(FailureCode.COVERAGE_GAP_INVALID, "未截断报告必须显式记录“未发生截断”")
        expected = _identity(self, "report_sha256")
        if self.report_sha256 is not None and self.report_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"report_sha256 hash 不一致：实际 {self.report_sha256}，期望 {expected}")
        object.__setattr__(self, "report_sha256", expected)
        return self


class EvaluationSummary(_FrozenModel):
    """仅允许公开的离散评价摘要，不保存原始序列或未分桶数值。"""

    outcome_band: str
    rank_ic_band: str
    hac_significance_band: str
    portfolio_band: str
    redundancy_band: str

    @field_validator("outcome_band", "rank_ic_band", "hac_significance_band", "portfolio_band", "redundancy_band")
    @classmethod
    def validate_summary_band(cls, value: str) -> str:
        """摘要必须是非空标签。"""

        if value not in _SUMMARY_BANDS:
            _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"评价摘要不是允许的离散带：{value}")
        return value


class DataIdentitySummary(_FrozenModel):
    """数据合同的身份摘要，不保存原始路径和样本。"""

    release_hash: str = Field(pattern=_HASH)
    manifest_hash: str = Field(pattern=_HASH)
    field_registry_hash: str = Field(pattern=_HASH)
    cutoff_band: str

    @field_validator("cutoff_band")
    @classmethod
    def validate_cutoff_band(cls, value: str) -> str:
        """数据截止信息只能以离散带公开。"""

        if value not in _DISCRETE_BANDS:
            _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"cutoff_band 不是允许的离散带：{value}")
        return value


class ResearchMemoryEntry(_FrozenModel):
    """单个候选槽的不可变研究记忆条目。"""

    memory_entry_id: str = Field(pattern=_ID)
    entry_kind: str
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    generation_seal_id: str = Field(pattern=_ID)
    run_id: str = Field(pattern=_ID)
    hypothesis_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    candidate_slot_id: str
    hypothesis_spec_hash: str = Field(pattern=_HASH)
    candidate_spec_hash: str = Field(pattern=_HASH)
    ast_hash: str = Field(pattern=_HASH)
    coverage_graph_id: str = Field(pattern=_ID)
    memory_snapshot_id: str = Field(pattern=_ID)
    field_signature: tuple[str, ...] = Field(min_length=1)
    operator_signature: tuple[str, ...] = Field(min_length=1)
    temporal_signature: tuple[str, ...] = Field(min_length=1)
    structure_signature: tuple[str, ...] = Field(min_length=1)
    gap_labels: tuple[str, ...] = Field(min_length=1)
    hypothesis_direction: Literal["positive", "negative"] | None = None
    selected_direction: Literal["positive", "negative"] | None = None
    direction_relation: Literal["supported", "reversed", "unresolved"] | None = None
    direction_source: Literal[
        "discovery_window_frozen", "discovery_window_unresolved"
    ] | None = None
    direction_record_sha256: str | None = Field(default=None, pattern=_HASH)
    terminal_state: str
    failure_reason: str | None = None
    duplicate_of: str | None = None
    evaluation_summary: EvaluationSummary | None = None
    data_identity_summary: DataIdentitySummary
    supersedes_entry_id: str | None = Field(default=None, pattern=_ID)
    created_at: datetime
    entry_sha256: str | None = Field(default=None, pattern=_HASH)

    @field_validator("created_at")
    @classmethod
    def validate_timezone(cls, value: datetime) -> datetime:
        """记忆创建时间必须明确带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "created_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> ResearchMemoryEntry:
        """条目创建时即复核内容 hash 和污染隔离。"""

        if self.discovery_family_id == POLLUTED_DISCOVERY_FAMILY_ID:
            _fail(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得创建记忆条目")
        direction_values = (
            self.hypothesis_direction,
            self.selected_direction,
            self.direction_relation,
            self.direction_source,
            self.direction_record_sha256,
        )
        if any(value is not None for value in direction_values):
            if self.direction_relation == "unresolved":
                if (
                    self.hypothesis_direction is None
                    or self.selected_direction is not None
                    or self.direction_source != "discovery_window_unresolved"
                    or self.direction_record_sha256 is not None
                ):
                    _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "未决方向记忆必须保留事前方向且不得伪造冻结记录")
            else:
                if any(value is None for value in direction_values):
                    _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "方向记忆必须完整绑定冻结发现记录")
                if self.direction_source != "discovery_window_frozen":
                    _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "已冻结方向必须标记 discovery_window_frozen")
                expected_relation = (
                    "supported"
                    if self.hypothesis_direction == self.selected_direction
                    else "reversed"
                )
                if self.direction_relation != expected_relation:
                    _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "方向关系与事前方向及冻结发现方向不一致")
        expected = _identity(self, "entry_sha256")
        if self.entry_sha256 is not None and self.entry_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"entry_sha256 hash 不一致：实际 {self.entry_sha256}，期望 {expected}")
        object.__setattr__(self, "entry_sha256", expected)
        return self


class ResearchMemorySnapshot(_FrozenModel):
    """按冻结规则挑选并带显式配对关系的不可变记忆快照。"""

    class EntryBinding(_FrozenModel):
        """快照内单条记忆身份与内容 hash 的冻结配对。"""

        memory_entry_id: str = Field(pattern=_ID)
        entry_sha256: str = Field(pattern=_HASH)

    memory_snapshot_id: str = Field(pattern=_ID)
    created_at: datetime
    cutoff_at: datetime
    source_family_ids: tuple[str, ...]
    source_seal_ids: tuple[str, ...]
    source_run_ids: tuple[str, ...]
    entry_ids: tuple[str, ...]
    entry_hashes: tuple[str, ...]
    entry_bindings: tuple[EntryBinding, ...] = ()
    exclusion_rules: tuple[str, ...] = Field(min_length=1)
    entry_count: int = Field(ge=0)
    coverage_graph_id: str = Field(pattern=_ID)
    coverage_graph_manifest_hash: str = Field(pattern=_HASH)
    snapshot_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_snapshot(self) -> ResearchMemorySnapshot:
        """校验时间、条目配对和严格排序。"""

        for field in ("created_at", "cutoff_at"):
            value = getattr(self, field)
            if value.tzinfo is None or value.utcoffset() is None:
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"{field} 必须带时区")
        if self.cutoff_at > self.created_at:
            _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "cutoff_at 不能晚于 created_at")
        if (
            self.entry_count != len(self.entry_ids)
            or len(self.entry_ids) != len(self.entry_hashes)
            or len(self.entry_ids) != len(self.entry_bindings)
        ):
            _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "entry_count、entry_ids、entry_hashes 和 entry_bindings 数量不一致")
        binding_ids = tuple(binding.memory_entry_id for binding in self.entry_bindings)
        binding_hashes = tuple(binding.entry_sha256 for binding in self.entry_bindings)
        if self.entry_count == 0:
            if self.entry_ids or self.entry_hashes or self.entry_bindings:
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "空快照不得携带 entry_ids、entry_hashes 或 entry_bindings")
        else:
            if not self.entry_bindings:
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "非空快照必须保存 entry_bindings")
            if tuple(sorted(binding_ids)) != binding_ids or len(set(binding_ids)) != len(binding_ids):
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "entry_bindings 必须按 memory_entry_id 严格排序且不能重复")
            if self.entry_ids != binding_ids or self.entry_hashes != binding_hashes:
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "entry_ids、entry_hashes 与 entry_bindings 配对不一致")
            if tuple(sorted(self.entry_ids)) != self.entry_ids:
                _fail(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "entry_ids 必须严格排序")
        if any(family_id == POLLUTED_DISCOVERY_FAMILY_ID for family_id in self.source_family_ids):
            _fail(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得进入记忆快照")
        expected = _identity(self, "snapshot_sha256")
        if self.snapshot_sha256 is not None and self.snapshot_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"snapshot_sha256 hash 不一致：实际 {self.snapshot_sha256}，期望 {expected}")
        object.__setattr__(self, "snapshot_sha256", expected)
        return self


class ResearchEvolutionContext(_FrozenModel):
    """一轮演化在任何结果揭晓前冻结的完整上下文。"""

    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    coverage_graph_manifest_hash: str = Field(pattern=_HASH)
    memory_snapshot_hash: str = Field(pattern=_HASH)
    gap_report_hash: str = Field(pattern=_HASH)
    field_registry_hash: str = Field(pattern=_HASH)
    evaluation_policy_hash: str = Field(pattern=_HASH)
    design_policy_hash: str = Field(pattern=_HASH)
    hypotheses_per_round: int = 10
    designs_per_hypothesis: int = 3
    arms_per_round: int = 4
    total_slots: int = 120
    external_model_redaction_policy: str
    context_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_budget(self) -> ResearchEvolutionContext:
        """上下文预算必须与设计政策完全一致。"""

        if (self.hypotheses_per_round, self.designs_per_hypothesis, self.arms_per_round, self.total_slots) != (10, 3, 4, 120):
            _fail(FailureCode.EVOLUTION_CONTEXT_INVALID, "演化上下文预算必须为 10/3/4/120")
        if not self.external_model_redaction_policy.strip():
            _fail(FailureCode.EVOLUTION_CONTEXT_INVALID, "external_model_redaction_policy 不能为空")
        if self.discovery_family_id == POLLUTED_DISCOVERY_FAMILY_ID:
            _fail(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得创建演化上下文")
        expected = _identity(self, "context_sha256")
        if self.context_sha256 is not None and self.context_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"context_sha256 hash 不一致：实际 {self.context_sha256}，期望 {expected}")
        object.__setattr__(self, "context_sha256", expected)
        return self


class EvolutionHypothesisDraft(_FrozenModel):
    """等待人工批准的逻辑假设草案。"""

    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    mechanism_unverified: bool
    prior_claim: str
    mechanism: str
    expected_direction: str
    observable_proxy: str
    independent_verification: str
    competing_explanations: tuple[str, ...] = Field(min_length=1)
    failure_modes: tuple[str, ...] = Field(min_length=1)
    falsification_path: str
    gap_ids: tuple[str, ...] = Field(min_length=1)
    semantic_plan: SemanticPlanTags | None = None
    draft_sha256: str | None = Field(default=None, pattern=_HASH)

    @field_validator("prior_claim", "mechanism", "expected_direction", "observable_proxy", "independent_verification", "falsification_path")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """事前假设文本不得为空。"""

        if not value.strip():
            _fail(FailureCode.APPROVAL_COUNT_INVALID, "假设草案关键字段不能为空")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> EvolutionHypothesisDraft:
        """草案 hash 必须覆盖全部事前主张。"""

        payload = self.model_dump(mode="json", exclude={"draft_sha256"})
        # 旧草案没有 semantic_plan 字段；缺失标签时沿用旧身份，保证历史对象可重放。
        if self.semantic_plan is None:
            payload.pop("semantic_plan", None)
        expected = sha256_json(payload)
        if self.draft_sha256 is not None and self.draft_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"draft_sha256 hash 不一致：实际 {self.draft_sha256}，期望 {expected}")
        object.__setattr__(self, "draft_sha256", expected)
        return self


class EvolutionHypothesisApproval(_FrozenModel):
    """绑定草案和演化上下文的人工审批记录。"""

    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    context_sha256: str = Field(pattern=_HASH)
    draft_sha256: str = Field(pattern=_HASH)
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    approval_role: str
    approved_at: datetime
    decision: str
    decision_sha256: str | None = Field(default=None, pattern=_HASH)

    @field_validator("approved_at")
    @classmethod
    def validate_approval_time(cls, value: datetime) -> datetime:
        """审批时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            _fail(FailureCode.APPROVAL_COUNT_INVALID, "approved_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> EvolutionHypothesisApproval:
        """审批决定 hash 必须覆盖审批绑定内容。"""

        if self.discovery_family_id == POLLUTED_DISCOVERY_FAMILY_ID:
            _fail(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得创建审批记录")
        expected = _identity(self, "decision_sha256")
        if self.decision_sha256 is not None and self.decision_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"decision_sha256 hash 不一致：实际 {self.decision_sha256}，期望 {expected}")
        object.__setattr__(self, "decision_sha256", expected)
        return self


class ApprovedEvolutionHypothesisBatch(_FrozenModel):
    """恰好十个逻辑槽的完整人工批准批次。"""

    context_sha256: str = Field(pattern=_HASH)
    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    approvals: tuple[EvolutionHypothesisApproval, ...] = Field(min_length=10, max_length=10)
    approval_batch_sha256: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_approvals(self) -> ApprovedEvolutionHypothesisBatch:
        """批准批次必须完整覆盖 H01 到 H10 且不能重复。"""

        slots = tuple(item.logical_slot_id for item in self.approvals)
        expected = tuple(f"H{i:02d}" for i in range(1, 11))
        if len(set(slots)) != 10:
            _fail(FailureCode.APPROVAL_COUNT_INVALID, f"批准批次存在重复逻辑槽位：{slots}")
        if set(slots) != set(expected):
            _fail(FailureCode.APPROVAL_COUNT_INVALID, f"批准批次必须完整覆盖 H01-H10，实际槽位：{slots}")
        if any(item.context_sha256 != self.context_sha256 for item in self.approvals):
            _fail(FailureCode.APPROVAL_COUNT_INVALID, "approval context_sha256 与批次不一致")
        if any(item.discovery_family_id != self.discovery_family_id for item in self.approvals):
            _fail(FailureCode.APPROVAL_COUNT_INVALID, "approval discovery_family_id 与批次不一致")
        if self.discovery_family_id == POLLUTED_DISCOVERY_FAMILY_ID:
            _fail(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得创建批准批次")
        expected = _identity(self, "approval_batch_sha256")
        if self.approval_batch_sha256 is not None and self.approval_batch_sha256 != expected:
            _fail(FailureCode.EVOLUTION_HASH_MISMATCH, f"approval_batch_sha256 hash 不一致：实际 {self.approval_batch_sha256}，期望 {expected}")
        object.__setattr__(self, "approval_batch_sha256", expected)
        return self


T = TypeVar("T", bound=BaseModel)


def _identity(model: T, identity_field: str) -> str:
    """根据去除身份字段后的规范内容计算 SHA-256。"""

    payload = model.model_dump(mode="json", exclude={identity_field})
    return sha256_json(payload)


def _reject_family(family_id: str) -> None:
    """拒绝已知污染 family。"""

    if family_id == POLLUTED_DISCOVERY_FAMILY_ID:
        raise FactorMinerError(FailureCode.POLLUTED_FAMILY_REJECTED, f"污染 family 不得进入演化合同：{family_id}")


def _require_hash(actual: str, expected: str, field: str) -> None:
    """报告具体 hash 字段的不一致。"""

    if actual != expected:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{field} hash 不一致：实际 {actual}，期望 {expected}")


def build_gap_report_identity(report: CoverageGapReport) -> CoverageGapReport:
    """计算并复核覆盖缺口报告 hash。"""

    expected = _identity(report, "report_sha256")
    _require_hash(report.report_sha256, expected, "report_sha256")
    for card in report.gap_cards:
        _require_hash(card.card_sha256, _identity(card, "card_sha256"), f"card_sha256[{card.gap_id}]")
    return report


def build_memory_entry_identity(entry: ResearchMemoryEntry) -> ResearchMemoryEntry:
    """计算并复核记忆条目 hash，同时拒绝污染 family。"""

    _reject_family(entry.discovery_family_id)
    _require_hash(entry.entry_sha256, _identity(entry, "entry_sha256"), "entry_sha256")
    return entry


def build_memory_snapshot_identity(snapshot: ResearchMemorySnapshot) -> ResearchMemorySnapshot:
    """计算并复核不可变记忆快照 hash。"""

    for family_id in snapshot.source_family_ids:
        _reject_family(family_id)
    _require_hash(snapshot.snapshot_sha256, _identity(snapshot, "snapshot_sha256"), "snapshot_sha256")
    return snapshot


def build_evolution_context(
    *,
    discovery_family_id: str,
    coverage_graph_manifest_hash: str,
    memory_snapshot_hash: str,
    gap_report_hash: str,
    field_registry_hash: str,
    evaluation_policy_hash: str,
    design_policy_hash: str,
    external_model_redaction_policy: str,
) -> ResearchEvolutionContext:
    """创建内容寻址的演化上下文；任何输入缺失或污染都会硬失败。"""

    _reject_family(discovery_family_id)
    policy = DesignDiversityPolicy()
    values: dict[str, Any] = {
        "discovery_family_id": discovery_family_id,
        "coverage_graph_manifest_hash": coverage_graph_manifest_hash,
        "memory_snapshot_hash": memory_snapshot_hash,
        "gap_report_hash": gap_report_hash,
        "field_registry_hash": field_registry_hash,
        "evaluation_policy_hash": evaluation_policy_hash,
        "design_policy_hash": design_policy_hash,
        "hypotheses_per_round": policy.hypotheses_per_round,
        "designs_per_hypothesis": policy.designs_per_hypothesis,
        "arms_per_round": policy.arms_per_round,
        "total_slots": policy.total_slots,
        "external_model_redaction_policy": external_model_redaction_policy,
    }
    if any(not isinstance(value, str) or not value.strip() for key, value in values.items() if key.endswith("hash") or key == "external_model_redaction_policy"):
        raise FactorMinerError(FailureCode.EVOLUTION_CONTEXT_INVALID, "演化上下文缺少必需 hash 或脱敏政策字段")
    values["context_sha256"] = sha256_json(values)
    return ResearchEvolutionContext.model_validate(values)
