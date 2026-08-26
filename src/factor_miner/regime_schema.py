"""V0.3 市场状态研究、部署、诊断和解释的不可变合同。"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
import math
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from factor_miner.canonical import sha256_json


class ReturnAggregation(StrEnum):
    """全市场日收益的冻结截面聚合方式。"""

    MEDIAN = "median"
    EQUAL_WEIGHT = "equal_weight"


class TrainingWindow(StrEnum):
    """正式支持的 HMM 训练窗口。"""

    EXPANDING = "expanding"
    ROLLING_5Y = "rolling_5y"
    ROLLING_8Y = "rolling_8y"


class CovarianceKind(StrEnum):
    """Gaussian HMM 支持的 covariance 结构。"""

    DIAG = "diag"
    FULL = "full"


class MappingWeights(BaseModel):
    """跨月状态匹配成本的冻结权重。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emission: FiniteFloat = Field(default=0.70, ge=0, le=1)
    duration: FiniteFloat = Field(default=0.10, ge=0, le=1)
    occupancy: FiniteFloat = Field(default=0.05, ge=0, le=1)
    transition: FiniteFloat = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def validate_sum(self) -> MappingWeights:
        """匹配权重必须精确组成一个凸组合。"""

        if not math.isclose(
            self.emission + self.duration + self.occupancy + self.transition,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("状态匹配权重之和必须为 1")
        return self


class MappingCaps(BaseModel):
    """跨月状态距离缩放使用的冻结截断上限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emission: FiniteFloat = Field(default=5.0, gt=0)
    duration_log_ratio: FiniteFloat = Field(
        default=math.log(20.0),
        gt=0,
    )
    occupancy: FiniteFloat = Field(default=1.0, gt=0)
    transition_l1: FiniteFloat = Field(default=2.0, gt=0)


class RegimeFeaturePolicy(BaseModel):
    """四个 point-in-time 市场特征的冻结公式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["1"] = "1"
    universe_mask_column: Literal["valid_for_factor_rank"] = "valid_for_factor_rank"
    realized_volatility_window: Literal[20] = 20
    realized_volatility_ddof: Literal[1] = 1
    amount_average_window: Literal[20] = 20
    amount_ratio_transform: Literal["log"] = "log"
    observation_time: Literal["close_t"] = "close_t"
    earliest_use: Literal["next_trading_day"] = "next_trading_day"


class RegimeQualityPolicy(BaseModel):
    """HMM 数值、占比、持续时间、相似度与匹配政策。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["1"] = "1"
    probability_tolerance: FiniteFloat = Field(default=1e-8, gt=0)
    covariance_eigenvalue_hard_min: FiniteFloat = Field(default=1e-8, gt=0)
    occupancy_hard_min: FiniteFloat = Field(default=0.03, gt=0, lt=1)
    occupancy_warning_min: FiniteFloat = Field(default=0.08, gt=0, lt=1)
    core_duration_hard_min: FiniteFloat = Field(default=3.0, gt=0)
    weighted_duration_warning_min: FiniteFloat = Field(default=5.0, gt=0)
    similarity_warning_distance: FiniteFloat = Field(default=0.15, gt=0)
    redundancy_distance_max: FiniteFloat = Field(default=0.05, ge=0)
    redundancy_occupancy_difference_max: FiniteFloat = Field(
        default=0.03,
        ge=0,
        le=1,
    )
    redundancy_duration_relative_difference_max: FiniteFloat = Field(
        default=0.20,
        ge=0,
    )
    redundancy_transition_l1_max: FiniteFloat = Field(default=0.10, ge=0)
    mapping_weights: MappingWeights = Field(default_factory=MappingWeights)
    mapping_caps: MappingCaps = Field(default_factory=MappingCaps)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> RegimeQualityPolicy:
        """拒绝相互矛盾的警告、拒绝和冗余阈值。"""

        if self.occupancy_warning_min <= self.occupancy_hard_min:
            raise ValueError("状态占比警告阈值必须高于硬拒绝阈值")
        if self.similarity_warning_distance <= self.redundancy_distance_max:
            raise ValueError("相似警告距离必须高于冗余硬拒绝距离")
        return self


class RegimeResearchSpec(BaseModel):
    """结果产生前冻结的 V0.3 HMM 研究空间。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    candidate_state_counts: tuple[Literal[2, 3, 4], ...] = Field(min_length=1)
    return_aggregations: tuple[ReturnAggregation, ...] = Field(min_length=1)
    training_windows: tuple[TrainingWindow, ...] = Field(min_length=1)
    covariance_kinds: tuple[CovarianceKind, ...] = Field(min_length=1)
    seeds: tuple[int, ...] = Field(min_length=1)
    n_iter: int = Field(ge=1)
    tol: FiniteFloat = Field(gt=0)
    min_covar: FiniteFloat = Field(gt=0)
    min_daily_assets: int = Field(ge=2)
    min_oos_months: int = Field(ge=1)
    min_successful_month_ratio: FiniteFloat = Field(gt=0, le=1)
    feature_policy: RegimeFeaturePolicy
    quality_policy: RegimeQualityPolicy
    research_start: date
    research_end: date
    created_at: datetime

    @field_validator(
        "candidate_state_counts",
        "return_aggregations",
        "training_windows",
        "covariance_kinds",
        "seeds",
    )
    @classmethod
    def validate_unique_sequences(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        """所有搜索轴都必须非空且不重复。"""

        if len(set(values)) != len(values):
            raise ValueError("HMM 研究搜索轴不能包含重复值")
        return values

    @field_validator("candidate_state_counts")
    @classmethod
    def validate_state_count_order(
        cls,
        values: tuple[int, ...],
    ) -> tuple[int, ...]:
        """K 候选必须按复杂度从低到高冻结。"""

        if tuple(sorted(values)) != values:
            raise ValueError("candidate_state_counts 必须严格升序")
        return values

    @field_validator("seeds")
    @classmethod
    def validate_seeds(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        """随机种子必须是非负整数。"""

        if any(value < 0 for value in values):
            raise ValueError("HMM seeds 不能为负数")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """研究登记时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> RegimeResearchSpec:
        """研究区间必须正向。"""

        if self.research_end < self.research_start:
            raise ValueError("research_end 不能早于 research_start")
        return self


class RegisteredRegimeResearch(BaseModel):
    """内容寻址的 HMM 研究登记记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime_research_id: str = Field(pattern=r"^regresearch_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: RegimeResearchSpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredRegimeResearch:
        """登记身份必须由内嵌研究 Spec 唯一派生。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected_hash:
            raise ValueError("市场状态研究 spec hash 与内容不一致")
        if self.regime_research_id != f"regresearch_{expected_hash[:24]}":
            raise ValueError("市场状态研究 ID 与内容不一致")
        return self


class RegimeDeploymentSpec(BaseModel):
    """人工批准后固定单个模型配置的正式部署合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    regime_research_id: str = Field(pattern=r"^regresearch_[0-9a-f]{24}$")
    research_candidate_id: str = Field(pattern=r"^regcand_[0-9a-f]{24}$")
    state_count: Literal[2, 3, 4]
    return_aggregation: ReturnAggregation
    training_window: TrainingWindow
    covariance_kind: CovarianceKind
    seeds: tuple[int, ...] = Field(min_length=1)
    n_iter: int = Field(ge=1)
    tol: FiniteFloat = Field(gt=0)
    min_covar: FiniteFloat = Field(gt=0)
    min_daily_assets: int = Field(ge=2)
    feature_policy: RegimeFeaturePolicy
    quality_policy: RegimeQualityPolicy
    deployment_start: date
    visible_cutoff: date
    created_at: datetime

    @field_validator("seeds")
    @classmethod
    def validate_seeds(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        """正式 seeds 必须唯一且非负。"""

        if len(set(values)) != len(values) or any(value < 0 for value in values):
            raise ValueError("正式 seeds 必须唯一且不能为负数")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """部署登记时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> RegimeDeploymentSpec:
        """正式部署只能使用部署开始日前已经可见的数据。"""

        if self.visible_cutoff >= self.deployment_start:
            raise ValueError("visible_cutoff 必须早于 deployment_start")
        return self


class RegisteredRegimeDeployment(BaseModel):
    """内容寻址的固定 HMM 部署登记记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime_deployment_id: str = Field(pattern=r"^regdeploy_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: RegimeDeploymentSpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredRegimeDeployment:
        """登记身份必须由内嵌部署 Spec 唯一派生。"""

        expected_hash = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected_hash:
            raise ValueError("市场状态部署 spec hash 与内容不一致")
        if self.regime_deployment_id != f"regdeploy_{expected_hash[:24]}":
            raise ValueError("市场状态部署 ID 与内容不一致")
        return self


class RegimeDiagnosticSeriesManifest(BaseModel):
    """外部逐日聚合诊断序列的不可变清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    series_id: str
    source_system: str
    data_release_id: str
    algorithm_version: str
    cutoff: date
    availability: Literal["close_signal_t", "realized_during_t"]
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator(
        "series_id",
        "source_system",
        "data_release_id",
        "algorithm_version",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """清单身份文本不得为空。"""

        if not value.strip():
            raise ValueError("诊断清单文本字段不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """诊断清单创建时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value


class RegimeAnnotation(BaseModel):
    """引用具体状态快照的人工经济解释。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    regime_snapshot_id: str = Field(pattern=r"^regsnap_[0-9a-f]{24}$")
    canonical_state_id: int = Field(ge=0)
    economic_label: str
    description: str
    author: str
    created_at: datetime

    @field_validator("economic_label", "description", "author")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """人工解释文本必须非空。"""

        if not value.strip():
            raise ValueError("状态解释文本不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """人工解释时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value


def regime_research_id(spec: RegimeResearchSpec) -> str:
    """返回研究 Spec 的内容寻址 ID。"""

    return f"regresearch_{sha256_json(spec.model_dump(mode='json'))[:24]}"


def registered_regime_research(
    spec: RegimeResearchSpec,
) -> RegisteredRegimeResearch:
    """生成可验证的研究登记记录。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredRegimeResearch(
        regime_research_id=f"regresearch_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def regime_deployment_id(spec: RegimeDeploymentSpec) -> str:
    """返回固定部署 Spec 的内容寻址 ID。"""

    return f"regdeploy_{sha256_json(spec.model_dump(mode='json'))[:24]}"


def registered_regime_deployment(
    spec: RegimeDeploymentSpec,
) -> RegisteredRegimeDeployment:
    """生成可验证的固定部署登记记录。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredRegimeDeployment(
        regime_deployment_id=f"regdeploy_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )
