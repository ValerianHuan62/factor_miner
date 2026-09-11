"""因子候选研究引擎的不可变领域 schema。"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
import math
from typing import Literal, Mapping

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from factor_miner.canonical import sha256_json


class ExpectedSign(StrEnum):
    """候选因子事前冻结的预期方向。"""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class MechanismStatus(StrEnum):
    """经济机制验证状态；V0 只允许未验证状态。"""

    MECHANISM_UNVERIFIED = "mechanism_unverified"


class FactorNode(BaseModel):
    """有限 typed AST 的递归节点。

    参数：
        op: 节点操作名称，例如 field、const 或后续 DSL 算子。
        args: 子节点有序元组。
        field: field 节点引用的字段名。
        value: const 节点使用的有限数值。
        window: rolling 节点的窗口参数。
        period: delay 或 delta 节点的周期参数。
        center: rolling 是否使用居中窗口；DSL 层会拒绝 True。

    返回：
        一个冻结、禁止额外字段的 AST 节点。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str
    args: tuple[FactorNode, ...] = Field(default_factory=tuple)
    field: str | None = None
    value: float | int | None = None
    window: int | None = None
    period: int | None = None
    center: bool | None = None

    @field_validator("op")
    @classmethod
    def validate_operation_name(cls, value: str) -> str:
        """拒绝空操作名称。"""

        if not value.strip():
            raise ValueError("操作名称不能为空")
        return value

    @field_validator("field")
    @classmethod
    def validate_field_name(cls, value: str | None) -> str | None:
        """拒绝已提供但为空的字段名称。"""

        if value is not None and not value.strip():
            raise ValueError("字段名称不能为空")
        return value

    @field_validator("value")
    @classmethod
    def validate_constant(cls, value: float | int | None) -> float | int | None:
        """拒绝 NaN 和无穷常数。"""

        if value is not None and not math.isfinite(float(value)):
            raise ValueError("常数必须是有限数字")
        return value

    @model_validator(mode="after")
    def validate_leaf_shape(self) -> FactorNode:
        """校验 field 和 const 叶子节点的基本形状。"""

        if self.op == "field":
            if self.field is None:
                raise ValueError("field 节点必须包含 field")
            if (
                self.args
                or self.value is not None
                or self.window is not None
                or self.period is not None
                or self.center is not None
            ):
                raise ValueError("field 节点不能包含其他参数")
        elif self.op == "const":
            if self.value is None:
                raise ValueError("const 节点必须包含 value")
            if (
                self.args
                or self.field is not None
                or self.window is not None
                or self.period is not None
                or self.center is not None
            ):
                raise ValueError("const 节点不能包含其他参数")
        return self


FactorNode.model_rebuild()


class HypothesisSpec(BaseModel):
    """结果产生前冻结的经济假设与独立验证路径。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str
    mechanism: str
    expected_sign: ExpectedSign
    observable_proxy: str
    independent_verification: str
    competing_explanations: tuple[str, ...] = Field(min_length=1)
    baseline_reference: str
    failure_modes: tuple[str, ...] = Field(min_length=1)
    falsification_path: str
    source_refs: tuple[str, ...] = Field(min_length=1)
    mechanism_status: MechanismStatus = MechanismStatus.MECHANISM_UNVERIFIED

    @field_validator(
        "claim",
        "mechanism",
        "observable_proxy",
        "independent_verification",
        "baseline_reference",
        "falsification_path",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """校验必填叙事文本非空。"""

        if not value.strip():
            raise ValueError("假设文本不能为空")
        return value

    @field_validator("competing_explanations", "failure_modes", "source_refs")
    @classmethod
    def validate_text_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """校验列表中每个文本项非空。"""

        if not values or any(not value.strip() for value in values):
            raise ValueError("文本列表不能为空且不能包含空项")
        return values


class CandidateFactorSpec(BaseModel):
    """不可变的候选因子登记 spec。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    hypothesis: HypothesisSpec
    expression: FactorNode
    required_fields: tuple[str, ...] = Field(min_length=1)
    max_lookback: int = Field(ge=0, le=130)
    availability: str
    created_at: datetime
    provenance: dict[str, str] = Field(min_length=1)

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """校验 required fields 非空且不重复。"""

        if any(not value.strip() for value in values):
            raise ValueError("required_fields 不能包含空字段")
        if len(set(values)) != len(values):
            raise ValueError("required_fields 不能重复")
        return values

    @field_validator("availability")
    @classmethod
    def validate_availability(cls, value: str) -> str:
        """校验信号可得性声明非空。"""

        if not value.strip():
            raise ValueError("availability 不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区的创建时间。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        """校验 provenance 非空且键值均有内容。"""

        if not value or any(
            not key.strip() or not item.strip() for key, item in value.items()
        ):
            raise ValueError("provenance 不能为空且不能包含空键值")
        return value


class AvailabilitySpec(BaseModel):
    """日频候选从观察到最早交易的冻结时点合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation: Literal["close_t"]
    decision: Literal["after_close_t"]
    earliest_trade: Literal["open_t_plus_1"]


class TrustedCandidateFactorSpec(BaseModel):
    """V0.1 使用 typed availability 的不可变候选 spec。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["2", "3"] = "2"
    hypothesis: HypothesisSpec
    expression: FactorNode
    required_fields: tuple[str, ...] = Field(min_length=1)
    max_lookback: int = Field(ge=0, le=558)
    availability: AvailabilitySpec
    created_at: datetime
    provenance: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def observation_version(self):
        """长期日历观察必须显式升级；旧版本仍限130个市场观察。"""
        if self.spec_version == "2" and self.max_lookback > 130:
            raise ValueError("旧候选 max_lookback 不得超过130")
        return self

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """校验 required fields 非空且不重复。"""

        if any(not value.strip() for value in values):
            raise ValueError("required_fields 不能包含空字段")
        if len(set(values)) != len(values):
            raise ValueError("required_fields 不能重复")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区的创建时间。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        """校验 provenance 非空且键值均有内容。"""

        if not value or any(
            not key.strip() or not item.strip() for key, item in value.items()
        ):
            raise ValueError("provenance 不能为空且不能包含空键值")
        return value

class RegisteredCandidate(BaseModel):
    """带内容寻址 ID 的不可变候选登记记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: CandidateFactorSpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredCandidate:
        """验证 candidate ID 和 spec hash 与 spec 内容一致。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        expected_id = f"cand_{expected_hash[:24]}"
        if self.spec_hash != expected_hash or self.candidate_id != expected_id:
            raise ValueError("candidate ID 或 spec hash 与 spec 内容不一致")
        return self


class RegisteredTrustedCandidate(BaseModel):
    """带内容寻址 ID 的 V0.1 trusted candidate。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: TrustedCandidateFactorSpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredTrustedCandidate:
        """验证 trusted candidate 地址与 spec 内容一致。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected_hash:
            raise ValueError("trusted candidate spec hash 与内容不一致")
        if self.candidate_id != f"cand_{expected_hash[:24]}":
            raise ValueError("trusted candidate ID 与内容不一致")
        return self


class UniverseSpec(BaseModel):
    """冻结的 point-in-time 股票池合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    universe_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    membership_column: str = Field(min_length=1)
    membership_available_at: Literal["close_t"]
    point_in_time: Literal[True]


class EvaluationPolicySpec(BaseModel):
    """普通 Campaign 无权覆盖的版本化 visible 评价协议。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["1"] = "1"
    label_id: str = Field(min_length=1)
    label_column: Literal["label_o2o_5d"]
    label_horizon_sessions: Literal[5]
    label_formula_version: str = Field(min_length=1)
    rank_mask_column: Literal["valid_for_factor_rank"]
    universe: UniverseSpec
    availability: AvailabilitySpec
    alpha: FiniteFloat = Field(gt=0, le=0.05)
    hac_method: Literal["ols_hac"]
    hac_max_lags: int = Field(ge=0)
    min_valid_dates: int = Field(ge=1)
    min_names_per_date: int = Field(ge=2)
    min_median_coverage: FiniteFloat = Field(ge=0, le=1)
    min_abs_mean_rank_ic: FiniteFloat = Field(ge=0, le=1)
    neutralization: Literal["none"]
    max_abs_output_correlation: FiniteFloat = Field(ge=0, le=1)
    reference_manifest_id: str = Field(min_length=1)
    reference_factor_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("reference_factor_ids")
    @classmethod
    def validate_reference_factor_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """reference ID 必须非空、唯一且保持冻结顺序。"""

        if any(not value.strip() for value in values):
            raise ValueError("reference_factor_ids 不能包含空项")
        if len(set(values)) != len(values):
            raise ValueError("reference_factor_ids 不能重复")
        return values


class ReferenceFactorLibrarySpec(BaseModel):
    """V0.2 联合正交化使用的冻结参考因子库。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library_version: Literal["1"] = "1"
    reference_manifest_id: str = Field(min_length=1)
    reference_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_factor_ids: tuple[str, ...] = Field(min_length=1)
    factor_value_column: Literal["raw_factor"]
    basis_selection: Literal["frozen_full_basis"]
    point_in_time: Literal[True]
    created_at: datetime
    provenance: dict[str, str] = Field(min_length=1)

    @field_validator("reference_factor_ids")
    @classmethod
    def validate_reference_factor_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """参考因子 ID 必须非空、唯一且保留冻结顺序。"""

        if any(not value.strip() for value in values):
            raise ValueError("reference_factor_ids 不能包含空项")
        if len(set(values)) != len(values):
            raise ValueError("reference_factor_ids 不能重复")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """参考库冻结时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        """参考库来源不允许空键值。"""

        if not value or any(
            not key.strip() or not item.strip() for key, item in value.items()
        ):
            raise ValueError("provenance 不能为空且不能包含空键值")
        return value


class RegisteredReferenceFactorLibrary(BaseModel):
    """带内容寻址 ID 的 V0.2 参考因子库。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_factor_library_id: str = Field(pattern=r"^reflib_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: ReferenceFactorLibrarySpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredReferenceFactorLibrary:
        """验证参考库 ID 和哈希与规范内容一致。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected_hash:
            raise ValueError("参考因子库 spec hash 与内容不一致")
        if self.reference_factor_library_id != f"reflib_{expected_hash[:24]}":
            raise ValueError("参考因子库 ID 与内容不一致")
        return self


class OrthogonalizationPolicySpec(BaseModel):
    """V0.2 截面正交化的冻结数值与覆盖率合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["cross_sectional_rank_ols"]
    include_intercept: Literal[True]
    min_reference_coverage: FiniteFloat = Field(ge=0, le=1)
    min_cross_sectional_excess_names: int = Field(ge=1)
    max_condition_number: FiniteFloat = Field(gt=1)
    min_median_residual_variance_ratio: FiniteFloat = Field(ge=0, le=1)
    min_abs_mean_residual_rank_ic: FiniteFloat = Field(ge=0, le=1)


class IncrementalEvaluationPolicySpec(EvaluationPolicySpec):
    """增加冻结参考库与正交化规则的 V0.2 可见评价政策。"""

    policy_version: Literal["2"] = "2"
    reference_factor_library_id: str = Field(pattern=r"^reflib_[0-9a-f]{24}$")
    orthogonalization: OrthogonalizationPolicySpec


class HypothesisSlot(BaseModel):
    """Research family 中不可回收的一个 hypothesis slot。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_number: int = Field(ge=1)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{24}$")


class ResearchFamilySpec(BaseModel):
    """跨 Campaign 冻结的全局多重检验 family。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_version: Literal["1"] = "1"
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    global_hypothesis_budget: int = Field(ge=1)
    slots: tuple[HypothesisSlot, ...] = Field(min_length=1)
    created_at: datetime
    provenance: dict[str, str] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """family 冻结时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        """family provenance 不允许空键值。"""

        if not value or any(
            not key.strip() or not item.strip() for key, item in value.items()
        ):
            raise ValueError("provenance 不能为空且不能包含空键值")
        return value

    @model_validator(mode="after")
    def validate_budget_and_slots(self) -> ResearchFamilySpec:
        """拒绝超预算、重复 slot 或重复 candidate。"""

        slot_numbers = [slot.slot_number for slot in self.slots]
        candidate_ids = [slot.candidate_id for slot in self.slots]
        if len(set(slot_numbers)) != len(slot_numbers):
            raise ValueError("hypothesis slot number 不能重复")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("同一 candidate 不能占用多个 hypothesis slot")
        if max(slot_numbers) > self.global_hypothesis_budget:
            raise ValueError("hypothesis slot 超出全局 family budget")
        return self


class RegisteredResearchFamily(BaseModel):
    """内容寻址的 ResearchFamilySpec 文档。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_family_id: str = Field(pattern=r"^family_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: ResearchFamilySpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredResearchFamily:
        """验证 family ID 和 hash 与内容一致。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected_hash:
            raise ValueError("research family spec hash 与内容不一致")
        if self.research_family_id != f"family_{expected_hash[:24]}":
            raise ValueError("research family ID 与内容不一致")
        return self


class TrustedVisibleCampaignSpec(BaseModel):
    """只引用冻结 policy/family 的 V0.1 visible campaign。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["2"] = "2"
    visible_start: date
    visible_end: date
    next_split_start: date | None = None
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    research_family_id: str = Field(pattern=r"^family_[0-9a-f]{24}$")

    @field_validator("candidate_ids")
    @classmethod
    def validate_candidate_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """trusted candidate ID 必须合法且唯一。"""

        pattern_length = len("cand_") + 24
        if any(
            len(value) != pattern_length
            or not value.startswith("cand_")
            or any(character not in "0123456789abcdef" for character in value[5:])
            for value in values
        ):
            raise ValueError("candidate_ids 中存在非法 trusted candidate ID")
        if len(set(values)) != len(values):
            raise ValueError("candidate_ids 不能重复")
        return values

    @model_validator(mode="after")
    def validate_dates(self) -> TrustedVisibleCampaignSpec:
        """visible 日期必须正向。"""

        if self.visible_end < self.visible_start:
            raise ValueError("visible_end 不能早于 visible_start")
        if self.next_split_start is not None and self.next_split_start <= self.visible_end:
            raise ValueError("next_split_start 必须晚于 visible_end")
        return self


class CampaignSpec(BaseModel):
    """结果产生前冻结的可见评价协议与候选优先级。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    visible_start: date = Field(
        validation_alias=AliasChoices("visible_start", "visible_start_date", "start")
    )
    visible_end: date = Field(
        validation_alias=AliasChoices("visible_end", "visible_end_date", "end")
    )
    next_split_start: date | None = None
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    max_hypotheses: int = Field(ge=1)
    alpha: FiniteFloat = Field(gt=0, le=1)
    label_column: str
    rank_mask_column: str
    hac_max_lags: int = Field(ge=0)
    min_valid_dates: int = Field(ge=1)
    min_names_per_date: int = Field(ge=2)
    min_median_coverage: FiniteFloat = Field(ge=0, le=1)
    max_abs_output_correlation: FiniteFloat = Field(ge=0, le=1)
    reference_factor_columns: tuple[str, ...] = ()

    @field_validator("candidate_ids")
    @classmethod
    def validate_candidate_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """校验候选 ID 格式和唯一性，同时保留原顺序。"""

        for value in values:
            if not value.startswith("cand_") or len(value) != len("cand_") + 24:
                raise ValueError("candidate_ids 中存在格式非法的候选 ID")
            if not all(character in "0123456789abcdef" for character in value[5:]):
                raise ValueError("candidate_ids 中存在非十六进制候选 ID")
        if len(set(values)) != len(values):
            raise ValueError("candidate_ids 不能重复")
        return values

    @field_validator("label_column", "rank_mask_column")
    @classmethod
    def validate_column_name(cls, value: str) -> str:
        """校验评价列名非空。"""

        if not value.strip():
            raise ValueError("评价列名不能为空")
        return value

    @field_validator("reference_factor_columns")
    @classmethod
    def validate_reference_columns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """校验 reference factor 列名不为空且不重复。"""

        if any(not value.strip() for value in values):
            raise ValueError("reference_factor_columns 不能包含空列名")
        if len(set(values)) != len(values):
            raise ValueError("reference_factor_columns 不能重复")
        return values

    @model_validator(mode="after")
    def validate_campaign_budget(self) -> CampaignSpec:
        """校验日期顺序和预登记 hypothesis budget。"""

        if self.visible_end < self.visible_start:
            raise ValueError("visible_end 不能早于 visible_start")
        if self.next_split_start is not None and self.next_split_start <= self.visible_end:
            raise ValueError("next_split_start 必须晚于 visible_end")
        if self.max_hypotheses < len(self.candidate_ids):
            raise ValueError("max_hypotheses 不能小于已登记候选数量")
        return self


def registered_candidate(spec: CandidateFactorSpec) -> RegisteredCandidate:
    """根据候选 spec 生成内容寻址的登记记录。

    参数：
        spec: 已通过 schema 校验的候选因子 spec。

    返回：
        包含完整 spec hash 和短 candidate ID 的登记记录。
    """

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredCandidate(
        candidate_id=f"cand_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def registered_trusted_candidate(
    spec: TrustedCandidateFactorSpec,
) -> RegisteredTrustedCandidate:
    """根据 V0.1 candidate spec 生成内容寻址记录。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredTrustedCandidate(
        candidate_id=f"cand_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def evaluation_policy_id(spec: EvaluationPolicySpec) -> str:
    """返回内容寻址的评价 policy ID。"""

    return f"evalpol_{sha256_json(spec.model_dump(mode='json'))[:24]}"


def reference_factor_library_id(spec: ReferenceFactorLibrarySpec) -> str:
    """返回内容寻址的参考因子库 ID。"""

    return f"reflib_{sha256_json(spec.model_dump(mode='json'))[:24]}"


def registered_reference_factor_library(
    spec: ReferenceFactorLibrarySpec,
) -> RegisteredReferenceFactorLibrary:
    """根据冻结参考库生成内容寻址记录。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredReferenceFactorLibrary(
        reference_factor_library_id=f"reflib_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def validate_incremental_policy_library(
    policy: IncrementalEvaluationPolicySpec,
    library: RegisteredReferenceFactorLibrary,
) -> None:
    """验证 V0.2 政策与参考库身份、清单和有序因子集合一致。"""

    if policy.reference_factor_library_id != library.reference_factor_library_id:
        raise ValueError("评价政策与参考因子库 ID 不一致")
    if policy.reference_manifest_id != library.spec.reference_manifest_id:
        raise ValueError("评价政策与参考因子库清单不一致")
    if policy.reference_factor_ids != library.spec.reference_factor_ids:
        raise ValueError("评价政策与参考因子顺序不一致")


def registered_research_family(spec: ResearchFamilySpec) -> RegisteredResearchFamily:
    """根据全局 family spec 生成内容寻址记录。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredResearchFamily(
        research_family_id=f"family_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def trusted_campaign_id(spec: TrustedVisibleCampaignSpec) -> str:
    """返回 V0.1 campaign 的内容寻址 ID。"""

    return f"camp_{sha256_json(spec.model_dump(mode='json'))[:24]}"


def validate_trusted_campaign(
    campaign: TrustedVisibleCampaignSpec,
    family: RegisteredResearchFamily,
    policy: EvaluationPolicySpec,
    candidates: Mapping[str, RegisteredTrustedCandidate | RegisteredCandidate],
) -> TrustedVisibleCampaignSpec:
    """跨不可变文档校验 trusted campaign 的 policy、family 和候选归属。"""

    if campaign.evaluation_policy_id != evaluation_policy_id(policy):
        raise ValueError("trusted campaign 的 evaluation policy ID 不匹配")
    if family.research_family_id != campaign.research_family_id:
        raise ValueError("trusted campaign 的 research family ID 不匹配")
    if family.spec.evaluation_policy_id != campaign.evaluation_policy_id:
        raise ValueError("research family 与 campaign 的 evaluation policy 不匹配")
    assigned = {slot.candidate_id for slot in family.spec.slots}
    missing_slots = set(campaign.candidate_ids) - assigned
    if missing_slots:
        raise ValueError(f"trusted campaign 候选未占用 family slot：{sorted(missing_slots)}")
    for candidate_id in campaign.candidate_ids:
        candidate = candidates.get(candidate_id)
        if not isinstance(candidate, RegisteredTrustedCandidate):
            raise ValueError(f"trusted campaign 候选不是 V0.1 spec：{candidate_id}")
        if candidate.candidate_id != candidate_id:
            raise ValueError("trusted candidate mapping 键与内容不一致")
    return campaign


def campaign_id(spec: CampaignSpec) -> str:
    """根据规范化 CampaignSpec 内容生成 campaign ID。

    参数：
        spec: 已冻结且通过 schema 校验的 campaign spec。

    返回：
        由 `camp_` 前缀和内容哈希前 24 位组成的 campaign ID。
    """

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return f"camp_{spec_hash[:24]}"
