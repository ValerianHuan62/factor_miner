"""版本化字段公开别名、单位与 point-in-time 可用性注册表。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import FactorNode


class FieldAvailabilityEntry(BaseModel):
    """一个本地字段及其可外发别名和最早决策时点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str
    public_alias: str
    economic_type: str
    unit_dimension: str
    panel_shape: str
    event_time: str
    source_publish_time: str
    vendor_available_time: str
    revision_policy: str
    point_in_time_guarantee: bool
    earliest_decision_time: str
    eligible_for_factor: bool

    @field_validator(
        "field_id",
        "public_alias",
        "economic_type",
        "unit_dimension",
        "panel_shape",
        "event_time",
        "source_publish_time",
        "vendor_available_time",
        "revision_policy",
        "earliest_decision_time",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """注册表文本不能为空。"""

        if not value.strip():
            raise ValueError("字段注册表文本不能为空")
        return value


class FieldAvailabilityRegistry(BaseModel):
    """绑定数据发布的不可变字段注册表。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_id: str
    data_release_id: str
    fields: tuple[FieldAvailabilityEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> FieldAvailabilityRegistry:
        """本地字段和公开别名都必须一一对应。"""

        field_ids = tuple(item.field_id for item in self.fields)
        aliases = tuple(item.public_alias for item in self.fields)
        if len(set(field_ids)) != len(field_ids):
            raise ValueError("字段注册表包含重复 field_id")
        if len(set(aliases)) != len(aliases):
            raise ValueError("字段注册表包含重复 public_alias")
        return self

    def by_public_alias(self, alias: str) -> FieldAvailabilityEntry:
        """解析公开别名，并执行首版 point-in-time 硬闸门。"""

        item = next(
            (field for field in self.fields if field.public_alias == alias),
            None,
        )
        if (
            item is None
            or not item.eligible_for_factor
            or not item.point_in_time_guarantee
            or item.panel_shape != "asset_date_scalar"
        ):
            raise FactorMinerError(
                FailureCode.FIELD_MISSING,
                f"公开字段别名不可用于候选：{alias}",
            )
        return item

    def by_field_id(self, field_id: str) -> FieldAvailabilityEntry:
        """读取已经准入的本地字段。"""

        for item in self.fields:
            if item.field_id == field_id:
                return item
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"字段注册表不存在本地字段：{field_id}",
        )


def intraday_daily_field_entries() -> tuple[FieldAvailabilityEntry, ...]:
    """返回收盘后可得、下一交易日使用的分钟聚合日级字段。"""

    fields = (
        ("intraday_open_30m_return", "return"),
        ("intraday_close_30m_amount_share", "ratio"),
        ("intraday_realized_volatility", "volatility"),
        ("intraday_close_vwap_deviation", "return"),
    )
    return tuple(
        FieldAvailabilityEntry(
            field_id=field_id,
            public_alias=field_id,
            economic_type="intraday_aggregate",
            unit_dimension=unit,
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="after_close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_intraday_daily_aggregate",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        )
        for field_id, unit in fields
    )


def resolve_public_field_aliases(
    node: FactorNode,
    registry: FieldAvailabilityRegistry,
) -> FactorNode:
    """把 LLM 可见别名递归替换为服务器本地字段 ID。"""

    if node.op == "field":
        entry = registry.by_public_alias(node.field or "")
        return FactorNode(op="field", field=entry.field_id)
    if node.op == "const":
        return node
    return FactorNode(
        op=node.op,
        args=tuple(
            resolve_public_field_aliases(child, registry)
            for child in node.args
        ),
        field=node.field,
        value=node.value,
        window=node.window,
        period=node.period,
        center=node.center,
    )
