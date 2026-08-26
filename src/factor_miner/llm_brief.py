"""V0.5 脱敏覆盖摘要与确定性 coverage gap 选择。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json


COUNT_BAND_RANK = {"0": 0, "1": 1, "2_4": 2}
FAILURE_RISK_RANK = {"none": 0, "low": 1, "medium": 2}


class GapSelectionPolicy(BaseModel):
    """覆盖空白卡片的冻结选择规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cards: int = Field(default=10, ge=1, le=10)
    max_cards_per_category: Literal[2] = 2


class CoverageGapCard(BaseModel):
    """不包含真实因子或精确表现的单张覆盖空白卡片。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    category: str
    subcategory: str
    field_aliases: tuple[str, ...] = Field(min_length=1)
    operator_families: tuple[str, ...] = Field(min_length=1)
    window_band: str
    hypothesis_axis: Literal["cross_sectional"]
    structure_count_band: Literal["0", "1", "2_4", "5_plus"]
    signal_cluster_count_band: Literal["0", "1", "2_4", "5_plus"]
    failure_risk: Literal["none", "low", "medium", "high"]
    performance_direction_band: str
    stability_band: str
    regime_difference_band: str
    canonical_gap_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "gap_id",
        "category",
        "subcategory",
        "window_band",
        "performance_direction_band",
        "stability_band",
        "regime_difference_band",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """卡片文本不得为空。"""

        if not value.strip():
            raise ValueError("coverage gap 文本不能为空")
        return value

    @field_validator("field_aliases", "operator_families")
    @classmethod
    def validate_sorted_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """公开别名必须唯一并稳定排序。"""

        if any(not value.strip() for value in values):
            raise ValueError("coverage gap 别名不能为空")
        if tuple(sorted(set(values))) != values:
            raise ValueError("coverage gap 别名必须唯一且按字典序排序")
        return values


class LLMCoverageBriefSpec(BaseModel):
    """本地构建 brief 使用且不外发的冻结身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_coverage_graph_id: str = Field(pattern=r"^covgraph_[0-9a-f]{24}$")
    algorithm_version: str
    created_at: datetime

    @field_validator("algorithm_version")
    @classmethod
    def validate_algorithm_version(cls, value: str) -> str:
        """算法版本不得为空。"""

        if not value.strip():
            raise ValueError("algorithm_version 不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """创建时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        return value


class LLMCoverageBrief(BaseModel):
    """允许进入外发预览的脱敏覆盖摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_version: Literal["1"] = "1"
    taxonomy_summary: tuple[str, ...] = Field(min_length=1)
    coverage_gap_cards: tuple[CoverageGapCard, ...] = Field(min_length=1, max_length=10)
    failure_pattern_cards: tuple[str, ...]
    allowed_field_aliases: tuple[str, ...] = Field(min_length=1)
    allowed_operators: tuple[str, ...] = Field(min_length=1)
    allowed_windows: tuple[int, ...] = Field(min_length=1)
    hypothesis_budget: Literal[10] = 10
    candidate_budget_per_hypothesis: Literal[3] = 3

    @field_validator(
        "taxonomy_summary",
        "failure_pattern_cards",
        "allowed_field_aliases",
        "allowed_operators",
    )
    @classmethod
    def validate_sorted_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """摘要序列不得包含空项或重复项。"""

        if any(not value.strip() for value in values):
            raise ValueError("brief 文本序列不能包含空项")
        if len(set(values)) != len(values):
            raise ValueError("brief 文本序列不能重复")
        return values

    @field_validator("allowed_windows")
    @classmethod
    def validate_windows(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        """窗口必须为正数、唯一且递增。"""

        if any(value <= 0 for value in values):
            raise ValueError("allowed_windows 必须为正整数")
        if tuple(sorted(set(values))) != values:
            raise ValueError("allowed_windows 必须唯一且递增")
        return values


class RegisteredLLMCoverageBrief(BaseModel):
    """绑定本地 build spec 与外发 payload 的内容寻址 brief。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_id: str = Field(pattern=r"^brief_[0-9a-f]{24}$")
    build_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    export_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_spec: LLMCoverageBriefSpec
    export_payload: LLMCoverageBrief

    @model_validator(mode="after")
    def validate_content_address(self) -> RegisteredLLMCoverageBrief:
        """本地身份与外发 payload 必须共同决定 brief ID。"""

        expected_spec = sha256_json(self.build_spec.model_dump(mode="json"))
        expected_payload = sha256_json(self.export_payload.model_dump(mode="json"))
        expected_id_hash = sha256_json(
            {
                "build_spec_hash": expected_spec,
                "export_payload_sha256": expected_payload,
            }
        )
        if self.build_spec_hash != expected_spec:
            raise ValueError("brief build spec hash 与内容不一致")
        if self.export_payload_sha256 != expected_payload:
            raise ValueError("brief export payload hash 与内容不一致")
        if self.brief_id != f"brief_{expected_id_hash[:24]}":
            raise ValueError("brief ID 与内容不一致")
        return self


def select_coverage_gaps(
    gaps: tuple[CoverageGapCard, ...],
    policy: GapSelectionPolicy,
) -> tuple[CoverageGapCard, ...]:
    """按冻结稀疏度、风险与动态类别配额选择 gap。"""

    eligible_by_hash: dict[str, CoverageGapCard] = {}
    for item in gaps:
        if item.structure_count_band not in COUNT_BAND_RANK:
            continue
        if item.signal_cluster_count_band not in COUNT_BAND_RANK:
            continue
        if item.failure_risk not in FAILURE_RISK_RANK:
            continue
        eligible_by_hash.setdefault(item.canonical_gap_hash, item)
    remaining = list(eligible_by_hash.values())

    def base_key(item: CoverageGapCard) -> tuple[int, int, int, str]:
        return (
            COUNT_BAND_RANK[item.structure_count_band],
            COUNT_BAND_RANK[item.signal_cluster_count_band],
            FAILURE_RISK_RANK[item.failure_risk],
            item.canonical_gap_hash,
        )

    selected: list[CoverageGapCard] = []
    categories = sorted({item.category for item in remaining})
    for category in categories:
        if len(selected) == policy.max_cards:
            break
        candidates = [item for item in remaining if item.category == category]
        chosen = min(candidates, key=base_key)
        selected.append(chosen)
        remaining.remove(chosen)

    category_order = {category: index for index, category in enumerate(categories)}
    while len(selected) < policy.max_cards:
        allocation = Counter(item.category for item in selected)
        candidates = [
            item
            for item in remaining
            if allocation[item.category] < policy.max_cards_per_category
        ]
        if not candidates:
            break
        chosen = min(
            candidates,
            key=lambda item: (
                COUNT_BAND_RANK[item.structure_count_band],
                COUNT_BAND_RANK[item.signal_cluster_count_band],
                FAILURE_RISK_RANK[item.failure_risk],
                allocation[item.category],
                category_order[item.category],
                item.canonical_gap_hash,
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)
    return tuple(selected)


def registered_llm_coverage_brief(
    spec: LLMCoverageBriefSpec,
    brief: LLMCoverageBrief,
) -> RegisteredLLMCoverageBrief:
    """登记绑定私有图谱来源但不把来源放入外发体的 brief。"""

    build_spec_hash = sha256_json(spec.model_dump(mode="json"))
    export_payload_sha256 = sha256_json(brief.model_dump(mode="json"))
    identity_hash = sha256_json(
        {
            "build_spec_hash": build_spec_hash,
            "export_payload_sha256": export_payload_sha256,
        }
    )
    return RegisteredLLMCoverageBrief(
        brief_id=f"brief_{identity_hash[:24]}",
        build_spec_hash=build_spec_hash,
        export_payload_sha256=export_payload_sha256,
        build_spec=spec,
        export_payload=brief,
    )
