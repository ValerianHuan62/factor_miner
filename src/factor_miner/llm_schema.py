"""V0.5 发现研究族、数据身份和时间留出的不可变合同。"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


SHANGHAI = ZoneInfo("Asia/Shanghai")
ARM_SEQUENCE = (
    "coverage_outcome_llm",
    "literature_only_llm",
    "mechanical_mutation",
    "hypothesis_conditioned_grammar",
)
ARM_GENERATORS = {
    "coverage_outcome_llm": "llm",
    "literature_only_llm": "llm",
    "mechanical_mutation": "deterministic",
    "hypothesis_conditioned_grammar": "deterministic",
}


class EvaluationRelationship(StrEnum):
    """发现上下文与候选评价数据的冻结关系。"""

    STRICT_TEMPORAL_HOLDOUT = "strict_temporal_holdout"
    REUSED_DISCOVERY_DATA = "reused_discovery_data"


class EvaluationEvidenceTier(StrEnum):
    """数据关系允许使用的最强结果表述。"""

    FRESH_VISIBLE_VALIDATION = "fresh_visible_validation"
    EXPLORATORY_FILTER_ONLY = "exploratory_filter_only"


class DiscoveryDataIdentity(BaseModel):
    """生成覆盖图谱时实际使用的全部信息身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_graph_ids: tuple[str, ...] = Field(min_length=1)
    data_release_id: str
    universe_id: str
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    label_id: str
    visible_start: date
    visible_end: date
    latest_information_timestamp_used: datetime
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "coverage_graph_ids",
    )
    @classmethod
    def validate_graph_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """图谱身份必须非空、唯一并使用稳定顺序。"""

        if any(not value.strip() for value in values):
            raise ValueError("coverage_graph_ids 不能包含空值")
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage_graph_ids 必须唯一且按字典序排序")
        return values

    @field_validator("data_release_id", "universe_id", "label_id")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """数据身份文本不得为空。"""

        if not value.strip():
            raise ValueError("数据身份文本不能为空")
        return value

    @field_validator("latest_information_timestamp_used")
    @classmethod
    def validate_information_timestamp(cls, value: datetime) -> datetime:
        """最晚信息时点必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("latest_information_timestamp_used 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> DiscoveryDataIdentity:
        """可见区间必须向前且信息截止不能早于可见末日。"""

        if self.visible_end < self.visible_start:
            raise ValueError("discovery visible_end 不能早于 visible_start")
        visible_end_start = datetime.combine(
            self.visible_end,
            time.min,
            tzinfo=SHANGHAI,
        )
        if (
            self.latest_information_timestamp_used.astimezone(SHANGHAI)
            < visible_end_start
        ):
            raise ValueError("最晚信息时点不能早于 discovery visible_end")
        return self


class CandidateEvaluationDataIdentity(BaseModel):
    """候选评价计划使用的独立数据身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_release_id: str
    universe_id: str
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    label_id: str
    planned_formation_start: date
    planned_formation_end: date
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("data_release_id", "universe_id", "label_id")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """评价身份文本不得为空。"""

        if not value.strip():
            raise ValueError("候选评价身份文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> CandidateEvaluationDataIdentity:
        """计划形成区间必须向前。"""

        if self.planned_formation_end < self.planned_formation_start:
            raise ValueError("planned_formation_end 不能早于 planned_formation_start")
        return self


class EvaluationDependencyInterval(BaseModel):
    """单个封存候选计算和标签实际依赖的原始日期区间。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_slot_id: str = Field(
        pattern=(
            r"^(coverage_outcome_llm|literature_only_llm|"
            r"mechanical_mutation|hypothesis_conditioned_grammar):"
            r"(00[1-9]|0[12][0-9]|030)$"
        )
    )
    factor_input_start: date
    factor_input_end: date
    label_input_start: date
    label_input_end: date
    candidate_generated_at: datetime

    @field_validator("candidate_generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        """生成时间只作审计，但仍必须具备明确时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("candidate_generated_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_intervals(self) -> EvaluationDependencyInterval:
        """因子和标签的原始依赖区间必须各自向前。"""

        if self.factor_input_end < self.factor_input_start:
            raise ValueError("因子原始输入区间不能反向")
        if self.label_input_end < self.label_input_start:
            raise ValueError("标签原始输入区间不能反向")
        return self

    @property
    def earliest_raw_observation(self) -> date:
        """返回因子回看和标签输入共同依赖的最早原始日期。"""

        return min(self.factor_input_start, self.label_input_start)


class DiscoveryArmSpec(BaseModel):
    """发现研究族中一个预登记生成臂。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_id: Literal[
        "coverage_outcome_llm",
        "literature_only_llm",
        "mechanical_mutation",
        "hypothesis_conditioned_grammar",
    ]
    generator_kind: Literal["llm", "deterministic"]
    candidate_slots: Literal[30] = 30

    @model_validator(mode="after")
    def validate_generator(self) -> DiscoveryArmSpec:
        """每个 arm 必须使用设计冻结的生成器类别。"""

        if self.generator_kind != ARM_GENERATORS[self.arm_id]:
            raise ValueError(f"{self.arm_id} 的 generator_kind 不符合冻结合同")
        return self


class LLMDiscoveryResearchFamilySpec(BaseModel):
    """任何生成或结果暴露前冻结的 V0.5 发现研究族。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    discovery_context_data_identity: DiscoveryDataIdentity
    candidate_evaluation_data_identity: CandidateEvaluationDataIdentity
    evaluation_relationship: EvaluationRelationship
    coverage_graph_ids: tuple[str, ...] = Field(min_length=1)
    arm_specs: tuple[DiscoveryArmSpec, ...] = Field(min_length=4, max_length=4)
    maximum_llm_campaigns: Literal[2] = 2
    arm_run_count: Literal[4] = 4
    candidate_slots_per_arm: Literal[30] = 30
    global_statistical_trial_budget: Literal[120] = 120
    multiplicity_policy: Literal["bonferroni_over_frozen_family_budget"]
    outcome_informed_decision_budget: int = Field(ge=0, le=10)
    family_max_api_requests: int = Field(ge=0)
    family_max_literature_queries: int = Field(ge=0)
    family_max_total_input_tokens: int = Field(ge=0)
    family_max_total_output_tokens: int = Field(ge=0)
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """登记时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value

    @model_validator(mode="after")
    def validate_family(self) -> LLMDiscoveryResearchFamilySpec:
        """冻结四臂顺序、预算以及图谱和评价政策绑定。"""

        actual_arms = tuple(arm.arm_id for arm in self.arm_specs)
        if actual_arms != ARM_SEQUENCE:
            raise ValueError("发现研究族必须按固定顺序包含四个 arm")
        if (
            sum(arm.candidate_slots for arm in self.arm_specs)
            != self.global_statistical_trial_budget
        ):
            raise ValueError("全局统计预算必须等于四个 arm 的候选槽总数")
        if self.coverage_graph_ids != (
            self.discovery_context_data_identity.coverage_graph_ids
        ):
            raise ValueError("family coverage_graph_ids 与 discovery identity 不一致")
        policy_ids = {
            self.evaluation_policy_id,
            self.discovery_context_data_identity.evaluation_policy_id,
            self.candidate_evaluation_data_identity.evaluation_policy_id,
        }
        if len(policy_ids) != 1:
            raise ValueError("discovery、evaluation 与 family 的评价政策必须一致")
        return self


class RegisteredLLMDiscoveryResearchFamily(BaseModel):
    """内容寻址的 V0.5 发现研究族登记。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: LLMDiscoveryResearchFamilySpec

    @model_validator(mode="after")
    def validate_content_address(
        self,
    ) -> RegisteredLLMDiscoveryResearchFamily:
        """登记 ID 与 hash 必须和内嵌 spec 完全一致。"""

        expected = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected:
            raise ValueError("V0.5 family spec_hash 与内容不一致")
        if self.discovery_family_id != f"llmfamily_{expected[:24]}":
            raise ValueError("V0.5 family ID 与内容不一致")
        return self


def registered_llm_discovery_family(
    spec: LLMDiscoveryResearchFamilySpec,
) -> RegisteredLLMDiscoveryResearchFamily:
    """为不可变发现研究族生成稳定内容身份。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredLLMDiscoveryResearchFamily(
        discovery_family_id=f"llmfamily_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def validate_evaluation_relationship(
    relationship: EvaluationRelationship,
    discovery: DiscoveryDataIdentity,
    dependencies: tuple[EvaluationDependencyInterval, ...],
) -> EvaluationEvidenceTier:
    """按原始依赖区间核验评价数据是否真正晚于 discovery 信息。

    候选生成时间不参与判定。因子 lookback 与未来五日标签依赖都必须
    体现在 `dependencies` 中，任何候选的最早原始观测跨界都会失败。
    """

    if not dependencies:
        raise FactorMinerError(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "缺少候选原始依赖区间，无法判断评价关系",
        )
    if relationship is EvaluationRelationship.REUSED_DISCOVERY_DATA:
        return EvaluationEvidenceTier.EXPLORATORY_FILTER_ONLY

    cutoff = discovery.latest_information_timestamp_used.astimezone(SHANGHAI)
    for dependency in dependencies:
        earliest = datetime.combine(
            dependency.earliest_raw_observation,
            time.min,
            tzinfo=SHANGHAI,
        )
        if earliest <= cutoff:
            raise FactorMinerError(
                FailureCode.LLM_DATA_IDENTITY_OVERLAP,
                (
                    f"候选 {dependency.candidate_slot_id} 的最早原始观测 "
                    f"{dependency.earliest_raw_observation.isoformat()} "
                    "没有严格晚于 discovery 最晚信息时点 "
                    f"{cutoff.isoformat()}"
                ),
            )
    return EvaluationEvidenceTier.FRESH_VISIBLE_VALIDATION
