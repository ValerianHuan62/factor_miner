"""V0.4 双层因子覆盖图谱的不可变领域合同。"""

from __future__ import annotations

from datetime import date, datetime
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
from factor_miner.regime_schema import RegimeAnnotation


class CoveragePairPolicy(BaseModel):
    """每日 IC 逐因子对比较的冻结规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_method: Literal["spearman"] = "spearman"
    min_overlap_dates: int = Field(default=60, ge=2)
    max_missing_ratio: FiniteFloat = Field(default=0.20, ge=0, lt=1)
    direction_alignment: Literal["orientation_sign"] = "orientation_sign"
    strong_signal_correlation: FiniteFloat = Field(default=0.70, gt=0, le=1)


class CoverageStructuralPolicy(BaseModel):
    """数学结构关系的冻结权重与强边阈值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_weight: FiniteFloat = Field(default=0.35, ge=0, le=1)
    operator_weight: FiniteFloat = Field(default=0.30, ge=0, le=1)
    window_weight: FiniteFloat = Field(default=0.20, ge=0, le=1)
    category_weight: FiniteFloat = Field(default=0.15, ge=0, le=1)
    window_scale: FiniteFloat = Field(default=20.0, gt=0)
    strong_structure_similarity: FiniteFloat = Field(default=0.70, gt=0, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> CoverageStructuralPolicy:
        """结构权重必须构成凸组合。"""

        total = (
            self.field_weight
            + self.operator_weight
            + self.window_weight
            + self.category_weight
        )
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("结构相似度权重之和必须为 1")
        return self


class CoverageClusterPolicy(BaseModel):
    """双层确定性连通分量的冻结规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["deterministic_connected_components"] = (
        "deterministic_connected_components"
    )
    cluster_id_version: Literal["1"] = "1"


class CoverageRegimePolicy(BaseModel):
    """状态概率进入因子画像的冻结覆盖规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_valid_dates_per_state: int = Field(default=60, ge=1)
    min_probability_mass: FiniteFloat = Field(default=20.0, gt=0)
    probability_tolerance: FiniteFloat = Field(default=1e-8, gt=0)


class CoverageFactorNode(BaseModel):
    """进入图谱的单个因子及其结构来源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_id: str
    factor_name_cn: str
    node_status: Literal["active", "inactive", "failed", "candidate"]
    structure_source: Literal["typed_ast", "legacy_formula_metadata"]
    formula_expr: str
    formula_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ast_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_fields: tuple[str, ...] = Field(min_length=1)
    operator_tags: tuple[str, ...]
    windows: tuple[int, ...]
    lookback_window: int = Field(ge=0)
    lag_days: int = Field(ge=0)
    category: str
    subcategory: str
    description: str
    preprocess_method: str
    neutralization: str
    orientation_sign: Literal[-1, 1]
    orientation_source: Literal[
        "preregistered_expected_sign",
        "discovery_window_frozen",
        "legacy_visible_mean_ic",
    ]
    legacy_mean_ic: FiniteFloat | None = None
    legacy_icir: FiniteFloat | None = None
    source: str
    parent_factor_ids: tuple[str, ...] = ()
    research_family_id: str | None = None

    @field_validator(
        "factor_id",
        "factor_name_cn",
        "formula_expr",
        "category",
        "subcategory",
        "description",
        "preprocess_method",
        "neutralization",
        "source",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """关键文本字段不得为空。"""

        if not value.strip():
            raise ValueError("覆盖图谱因子文本字段不能为空")
        return value.strip()

    @field_validator("input_fields", "operator_tags", "parent_factor_ids")
    @classmethod
    def validate_unique_text_tuple(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """结构标记必须非空、唯一且稳定排序。"""

        if any(not value.strip() for value in values):
            raise ValueError("覆盖图谱结构序列不能包含空值")
        if tuple(sorted(set(values))) != values:
            raise ValueError("覆盖图谱结构序列必须唯一且按字典序排序")
        return values

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        """窗口必须为正整数、唯一且递增。"""

        if any(value <= 0 for value in values):
            raise ValueError("因子窗口必须为正整数")
        if tuple(sorted(set(values))) != values:
            raise ValueError("因子窗口必须唯一且递增")
        return values

    @model_validator(mode="after")
    def validate_structure_source(self) -> CoverageFactorNode:
        """自由文本旧因子不得伪装成类型化 AST。"""

        if self.structure_source == "typed_ast" and self.ast_hash is None:
            raise ValueError("typed_ast 因子必须提供 ast_hash")
        if (
            self.structure_source == "legacy_formula_metadata"
            and self.ast_hash is not None
        ):
            raise ValueError("旧自由文本公式不得声明 ast_hash")
        return self


class CoverageGraphSpec(BaseModel):
    """结果产生前冻结的 V0.4 图谱构建合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal["1"] = "1"
    factor_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    daily_ic_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regime_snapshot_id: str = Field(pattern=r"^regsnap_[0-9a-f]{24}$")
    regime_annotations: tuple[RegimeAnnotation, ...] = Field(min_length=2)
    evaluation_policy_id: str = Field(pattern=r"^evalpol_[0-9a-f]{24}$")
    data_release_id: str
    label_id: str
    visible_start: date
    visible_end: date
    pair_policy: CoveragePairPolicy
    structural_policy: CoverageStructuralPolicy
    cluster_policy: CoverageClusterPolicy
    regime_policy: CoverageRegimePolicy = Field(default_factory=CoverageRegimePolicy)
    algorithm_version: str
    random_seed: Literal[0] = 0
    runtime_provenance: dict[str, str]
    created_at: datetime

    @field_validator("data_release_id", "label_id", "algorithm_version")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """输入身份与算法版本不得为空。"""

        if not value.strip():
            raise ValueError("覆盖图谱身份文本不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """创建时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value

    @field_validator("runtime_provenance")
    @classmethod
    def validate_runtime_provenance(cls, value: dict[str, str]) -> dict[str, str]:
        """图谱必须冻结代码、配置、锁文件和数据发布身份。"""

        required = {
            "code_commit",
            "config_hash",
            "uv_lock_sha256",
            "release_manifest_sha256",
        }
        if set(value) != required or any(not item.strip() for item in value.values()):
            raise ValueError(
                "runtime_provenance 必须完整记录代码、配置、锁文件和发布清单"
            )
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> CoverageGraphSpec:
        """校验日期和状态快照专属中文注释。"""

        if self.visible_end < self.visible_start:
            raise ValueError("visible_end 不能早于 visible_start")
        state_ids = tuple(
            annotation.canonical_state_id
            for annotation in self.regime_annotations
        )
        if len(set(state_ids)) != len(state_ids):
            raise ValueError("每个 canonical state 只能有一条状态注释")
        labels = tuple(
            annotation.economic_label.strip()
            for annotation in self.regime_annotations
        )
        if len(set(labels)) != len(labels):
            raise ValueError("每个 canonical state 必须有唯一中文名称")
        if any(
            annotation.regime_snapshot_id != self.regime_snapshot_id
            for annotation in self.regime_annotations
        ):
            raise ValueError("状态注释必须引用本次 regime_snapshot_id")
        if any(not _contains_cjk(label) for label in labels):
            raise ValueError("状态 economic_label 必须包含中文描述")
        return self


class RegisteredCoverageGraphSpec(BaseModel):
    """内容寻址的 V0.4 图谱构建登记。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_spec_id: str = Field(pattern=r"^covspec_[0-9a-f]{24}$")
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: CoverageGraphSpec

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredCoverageGraphSpec:
        """登记 ID 必须与内嵌 Spec 完全一致。"""

        expected = sha256_json(self.spec.model_dump(mode="json"))
        if self.spec_hash != expected:
            raise ValueError("覆盖图谱 spec hash 与内容不一致")
        if self.coverage_spec_id != f"covspec_{expected[:24]}":
            raise ValueError("覆盖图谱 spec ID 与内容不一致")
        return self


def registered_coverage_graph_spec(
    spec: CoverageGraphSpec,
) -> RegisteredCoverageGraphSpec:
    """为冻结图谱 Spec 生成稳定内容身份。"""

    spec_hash = sha256_json(spec.model_dump(mode="json"))
    return RegisteredCoverageGraphSpec(
        coverage_spec_id=f"covspec_{spec_hash[:24]}",
        spec_hash=spec_hash,
        spec=spec,
    )


def _contains_cjk(value: str) -> bool:
    """判断展示名是否至少包含一个中日韩统一表意字符。"""

    return any("\u4e00" <= character <= "\u9fff" for character in value)
