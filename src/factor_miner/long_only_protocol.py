"""目标多头五年研究的冻结区间与方向决定合同。"""

from __future__ import annotations

from datetime import date
import math
from typing import Any, ClassVar, Literal, Mapping

from pydantic import BaseModel, ConfigDict, model_validator


class LongOnlyResearchProtocol(BaseModel):
    """结果揭晓前冻结的目标多头研究区间和全市场身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["long-only-v1"] = "long-only-v1"
    discovery_start: date = date(2021, 1, 1)
    discovery_end: date = date(2023, 12, 31)
    confirmation_start: date = date(2024, 1, 1)
    confirmation_end: date = date(2026, 6, 30)
    recent_start: date = date(2025, 1, 1)
    recent_end: date = date(2026, 6, 30)
    universe: Literal["SSE_SZSE_WHOLE_MARKET"] = "SSE_SZSE_WHOLE_MARKET"

    _FROZEN_CONTRACT: ClassVar[tuple[object, ...]] = (
        "long-only-v1",
        date(2021, 1, 1),
        date(2023, 12, 31),
        date(2024, 1, 1),
        date(2026, 6, 30),
        date(2025, 1, 1),
        date(2026, 6, 30),
        "SSE_SZSE_WHOLE_MARKET",
    )

    @model_validator(mode="after")
    def validate_frozen_contract(self) -> LongOnlyResearchProtocol:
        """拒绝构造或反序列化时改写任何冻结研究边界。"""

        actual = (
            self.version,
            self.discovery_start,
            self.discovery_end,
            self.confirmation_start,
            self.confirmation_end,
            self.recent_start,
            self.recent_end,
            self.universe,
        )
        if actual != self._FROZEN_CONTRACT:
            raise ValueError("long-only-v1 的研究区间和股票池必须使用冻结合同")
        if self.discovery_end >= self.confirmation_start:
            raise ValueError("发现期不得与确认期重叠")
        if not (
            self.confirmation_start
            <= self.recent_start
            <= self.recent_end
            <= self.confirmation_end
        ):
            raise ValueError("最近期必须位于确认期内")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> LongOnlyResearchProtocol:
        """复制后重新校验，避免 Pydantic 快捷复制绕过冻结合同。"""

        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.model_dump(mode="python"))


class DiscoveryRankICSummary(BaseModel):
    """绑定研究协议发现期的 RankIC 摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: LongOnlyResearchProtocol
    window_start: date
    window_end: date
    rank_ic: float

    @model_validator(mode="after")
    def validate_discovery_identity_and_rank_ic(self) -> DiscoveryRankICSummary:
        """只允许以冻结发现期的非零有限 RankIC 选择方向。"""

        if (
            self.window_start != self.protocol.discovery_start
            or self.window_end != self.protocol.discovery_end
        ):
            raise ValueError("方向选择的 RankIC 摘要必须精确绑定发现期")
        if not math.isfinite(self.rank_ic) or self.rank_ic == 0:
            raise ValueError("发现期 RankIC 必须为非零有限值")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> DiscoveryRankICSummary:
        """复制后重新校验，防止发现窗口身份被快捷复制改写。"""

        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.model_dump(mode="python"))


class DirectionDecision(BaseModel):
    """发现区间方向选择及其与冻结假设方向的关系。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery_summary: DiscoveryRankICSummary
    hypothesis_direction: Literal["positive", "negative"]
    selected_direction: Literal["positive", "negative"]
    hypothesis_relation: Literal["supported", "reversed"]

    @property
    def discovery_rank_ic_mean(self) -> float:
        """返回用于冻结方向的发现期五日 RankIC 均值。"""

        return self.discovery_summary.rank_ic


def select_direction(
    discovery_summary: DiscoveryRankICSummary,
    *,
    hypothesis_direction: Literal["positive", "negative"],
) -> DirectionDecision:
    """由发现区间 RankIC 决定方向，同时保留候选的事前假设方向。"""

    if not isinstance(discovery_summary, DiscoveryRankICSummary):
        raise TypeError("discovery_summary 必须是 DiscoveryRankICSummary")
    selected_direction: Literal["positive", "negative"] = (
        "positive" if discovery_summary.rank_ic >= 0 else "negative"
    )
    hypothesis_relation: Literal["supported", "reversed"] = (
        "supported"
        if selected_direction == hypothesis_direction
        else "reversed"
    )
    return DirectionDecision(
        discovery_summary=discovery_summary,
        hypothesis_direction=hypothesis_direction,
        selected_direction=selected_direction,
        hypothesis_relation=hypothesis_relation,
    )
