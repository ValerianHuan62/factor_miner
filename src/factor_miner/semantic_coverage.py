"""非阻断的金融语义标签、覆盖矩阵与固定稀疏配额。"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SemanticEvent(StrEnum):
    """因子试图捕捉的主要市场事件。"""

    TREND = "trend"
    REVERSAL = "reversal"
    BREAKOUT = "breakout"
    RANGE_POSITION = "range_position"
    VOLATILITY_CHANGE = "volatility_change"
    VOLUME_CHANGE = "volume_change"
    LIQUIDITY_CHANGE = "liquidity_change"
    PRICE_VOLUME_DIVERGENCE = "price_volume_divergence"


class SemanticContext(StrEnum):
    """解释市场事件时使用的主要背景。"""

    RECENT_EXTREME = "recent_extreme"
    TREND_REGIME = "trend_regime"
    VOLATILITY_REGIME = "volatility_regime"
    LIQUIDITY_REGIME = "liquidity_regime"
    VOLUME_REGIME = "volume_regime"
    MARKET_REGIME = "market_regime"
    CROSS_SECTIONAL_POSITION = "cross_sectional_position"
    VWAP_REGION = "vwap_region"


class SemanticQuality(StrEnum):
    """用于增强或验证语义事件的辅助条件。"""

    VOLUME_CONFIRMATION = "volume_confirmation"
    MULTI_HORIZON_CONSISTENCY = "multi_horizon_consistency"
    VOLATILITY_CONFIRMATION = "volatility_confirmation"
    LIQUIDITY_CONFIRMATION = "liquidity_confirmation"
    PRICE_VOLUME_CONSISTENCY = "price_volume_consistency"
    PERSISTENCE_CONFIRMATION = "persistence_confirmation"
    OUTLIER_ROBUSTNESS = "outlier_robustness"


class SemanticDirection(StrEnum):
    """事件之后的经济方向，不替代收益符号假设。"""

    CONTINUATION = "continuation"
    REVERSAL = "reversal"
    OSCILLATION = "oscillation"


class SemanticOutput(StrEnum):
    """语义机制的预期信号表达形式。"""

    CONTINUOUS_SCORE = "continuous_score"
    EVENT_STRENGTH = "event_strength"
    DISTANCE = "distance"
    DECAY = "decay"
    CROSS_SECTIONAL_RANK = "cross_sectional_rank"


EVENT_LABELS = {
    "trend": "趋势形成",
    "reversal": "价格反转",
    "breakout": "区间突破",
    "range_position": "区间位置变化",
    "volatility_change": "波动变化",
    "volume_change": "成交量变化",
    "liquidity_change": "流动性变化",
    "price_volume_divergence": "量价背离",
}
CONTEXT_LABELS = {
    "recent_extreme": "近期极值附近",
    "trend_regime": "趋势状态",
    "volatility_regime": "波动状态",
    "liquidity_regime": "流动性状态",
    "volume_regime": "成交量状态",
    "market_regime": "市场状态",
    "cross_sectional_position": "截面位置",
    "vwap_region": "VWAP 区域",
}
QUALITY_LABELS = {
    "volume_confirmation": "成交量确认",
    "multi_horizon_consistency": "多周期一致",
    "volatility_confirmation": "波动确认",
    "liquidity_confirmation": "流动性确认",
    "price_volume_consistency": "量价一致",
    "persistence_confirmation": "持续性确认",
    "outlier_robustness": "异常值稳健",
}
DIRECTION_LABELS = {
    "continuation": "延续",
    "reversal": "反转",
    "oscillation": "区间震荡",
}
OUTPUT_LABELS = {
    "continuous_score": "连续分数",
    "event_strength": "事件强度",
    "distance": "距离",
    "decay": "事件衰减",
    "cross_sectional_rank": "截面排名",
}


class SemanticPlanTags(BaseModel):
    """完整但整体可选的 E/C/Q/D/O 规划标签。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_tag: SemanticEvent
    context_tag: SemanticContext
    quality_tags: tuple[SemanticQuality, ...] = Field(default=(), max_length=3)
    direction_tag: SemanticDirection
    output_tag: SemanticOutput

    @field_validator("quality_tags")
    @classmethod
    def canonicalize_qualities(
        cls,
        values: tuple[SemanticQuality, ...],
    ) -> tuple[SemanticQuality, ...]:
        """质量标签必须唯一并按稳定机器值排序。"""

        if len(values) != len(set(values)):
            raise ValueError("quality_tags 不能重复")
        return tuple(sorted(values, key=lambda item: item.value))

    def semantic_key(self) -> tuple[str, str, tuple[str, ...], str, str]:
        """返回用于精确语义重复检查的规范键。"""

        return (
            self.event_tag.value,
            self.context_tag.value,
            tuple(item.value for item in self.quality_tags),
            self.direction_tag.value,
            self.output_tag.value,
        )

    def chinese_labels(self) -> dict[str, object]:
        """返回 Dashboard 使用的中文标签。"""

        return {
            "事件": EVENT_LABELS[self.event_tag.value],
            "上下文": CONTEXT_LABELS[self.context_tag.value],
            "质量条件": tuple(QUALITY_LABELS[item.value] for item in self.quality_tags),
            "方向语义": DIRECTION_LABELS[self.direction_tag.value],
            "输出形式": OUTPUT_LABELS[self.output_tag.value],
        }


class SemanticDuplicateRegion(BaseModel):
    """完全相同 E/C/Q/D/O 组合的重复区域。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_plan: SemanticPlanTags
    count: int = Field(ge=2)


class SemanticCoverage(BaseModel):
    """不含任何 IC 或收益的累计金融语义覆盖摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tagged_hypothesis_count: int = Field(ge=0)
    untagged_hypothesis_count: int = Field(ge=0)
    event_context_counts: dict[str, dict[str, int]]
    duplicate_regions: tuple[SemanticDuplicateRegion, ...] = ()

    @model_validator(mode="after")
    def validate_matrix(self) -> SemanticCoverage:
        """矩阵必须完整、非负，且总数与已标注假设一致。"""

        expected_events = {item.value for item in SemanticEvent}
        expected_contexts = {item.value for item in SemanticContext}
        if set(self.event_context_counts) != expected_events:
            raise ValueError("event_context_counts 必须完整覆盖事件词表")
        for values in self.event_context_counts.values():
            if set(values) != expected_contexts:
                raise ValueError("event_context_counts 必须完整覆盖上下文词表")
            if any(not isinstance(count, int) or count < 0 for count in values.values()):
                raise ValueError("event_context_counts 只能包含非负整数")
        if sum(
            count
            for values in self.event_context_counts.values()
            for count in values.values()
        ) != self.tagged_hypothesis_count:
            raise ValueError("语义覆盖矩阵总数与已标注假设数不一致")
        return self


class SemanticQuotaSlot(BaseModel):
    """下一轮一个逻辑假设槽的确定性语义配额。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    quota_kind: Literal["uncovered", "sparse", "free"]
    event_tag: SemanticEvent | None = None
    context_tag: SemanticContext | None = None

    @model_validator(mode="after")
    def validate_target(self) -> SemanticQuotaSlot:
        """自由槽不得伪造目标，其他槽必须完整指定 E×C。"""

        if self.quota_kind == "free":
            if self.event_tag is not None or self.context_tag is not None:
                raise ValueError("free quota 不得指定 event/context")
        elif self.event_tag is None or self.context_tag is None:
            raise ValueError("稀疏 quota 必须指定 event/context")
        return self


class SemanticQuotaPlan(BaseModel):
    """固定 6/3/1 的十槽语义探索配额。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["semantic-quota-6-3-1-v1"] = "semantic-quota-6-3-1-v1"
    slots: tuple[SemanticQuotaSlot, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_fixed_quota(self) -> SemanticQuotaPlan:
        expected_slots = tuple(f"H{index:02d}" for index in range(1, 11))
        if tuple(item.logical_slot_id for item in self.slots) != expected_slots:
            raise ValueError("semantic quota 必须按 H01-H10 完整排序")
        kinds = tuple(item.quota_kind for item in self.slots)
        if kinds != ("uncovered",) * 6 + ("sparse",) * 3 + ("free",):
            raise ValueError("semantic quota 必须固定为 6/3/1")
        targets = tuple(
            (item.event_tag, item.context_tag)
            for item in self.slots
            if item.quota_kind != "free"
        )
        if len(targets) != len(set(targets)):
            raise ValueError("同一轮 semantic quota 目标不能重复")
        return self


def build_semantic_coverage(
    plans: Sequence[SemanticPlanTags | None],
) -> SemanticCoverage:
    """从假设级标签构造包含零覆盖单元格的 E×C 矩阵。"""

    tagged = tuple(item for item in plans if item is not None)
    pair_counts = Counter(
        (item.event_tag.value, item.context_tag.value) for item in tagged
    )
    matrix = {
        event.value: {
            context.value: pair_counts[(event.value, context.value)]
            for context in SemanticContext
        }
        for event in SemanticEvent
    }
    exact_counts = Counter(item.semantic_key() for item in tagged)
    by_key = {item.semantic_key(): item for item in tagged}
    duplicates = tuple(
        SemanticDuplicateRegion(semantic_plan=by_key[key], count=count)
        for key, count in sorted(exact_counts.items())
        if count >= 2
    )
    return SemanticCoverage(
        tagged_hypothesis_count=len(tagged),
        untagged_hypothesis_count=len(plans) - len(tagged),
        event_context_counts=matrix,
        duplicate_regions=duplicates,
    )


def build_semantic_quota(coverage: SemanticCoverage) -> SemanticQuotaPlan:
    """按累计 E×C 次数确定性分配 6 未覆盖、3 最稀疏、1 自由槽。"""

    event_order = {item.value: index for index, item in enumerate(SemanticEvent)}
    context_order = {item.value: index for index, item in enumerate(SemanticContext)}
    pairs = tuple(
        (event.value, context.value)
        for event in SemanticEvent
        for context in SemanticContext
    )
    ranked = sorted(
        pairs,
        key=lambda item: (
            coverage.event_context_counts[item[0]][item[1]],
            event_order[item[0]],
            context_order[item[1]],
        ),
    )
    uncovered = [
        item
        for item in ranked
        if coverage.event_context_counts[item[0]][item[1]] == 0
    ][:6]
    selected = set(uncovered)
    if len(uncovered) < 6:
        uncovered.extend(item for item in ranked if item not in selected)
        uncovered = uncovered[:6]
        selected = set(uncovered)
    sparse = [item for item in ranked if item not in selected][:3]
    slots: list[SemanticQuotaSlot] = []
    for index, (event_tag, context_tag) in enumerate(uncovered, start=1):
        slots.append(
            SemanticQuotaSlot(
                logical_slot_id=f"H{index:02d}",
                quota_kind="uncovered",
                event_tag=event_tag,
                context_tag=context_tag,
            )
        )
    for index, (event_tag, context_tag) in enumerate(sparse, start=7):
        slots.append(
            SemanticQuotaSlot(
                logical_slot_id=f"H{index:02d}",
                quota_kind="sparse",
                event_tag=event_tag,
                context_tag=context_tag,
            )
        )
    slots.append(
        SemanticQuotaSlot(
            logical_slot_id="H10",
            quota_kind="free",
        )
    )
    return SemanticQuotaPlan(slots=tuple(slots))
