"""V0.5 DSL 单位、时间轴、面板形状和可得时点语义分析。"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import FieldAvailabilityRegistry
from factor_miner.schema import FactorNode


@dataclass(frozen=True, slots=True)
class _Unit:
    """由基础单位整数指数构成的可组合单位。"""

    powers: tuple[tuple[str, int], ...]

    @classmethod
    def base(cls, name: str) -> _Unit:
        """从注册表基础单位构造内部表示。"""

        if name == "dimensionless":
            return cls(())
        return cls(((name, 1),))

    def multiply(self, other: _Unit, sign: int = 1) -> _Unit:
        """乘法累加指数，除法以负号累加。"""

        result = dict(self.powers)
        for name, power in other.powers:
            result[name] = result.get(name, 0) + sign * power
            if result[name] == 0:
                del result[name]
        return _Unit(tuple(sorted(result.items())))

    def render(self) -> str:
        """输出稳定单位字符串。"""

        if not self.powers:
            return "dimensionless"
        return "*".join(
            name if power == 1 else f"{name}^{power}"
            for name, power in self.powers
        )


class SemanticAnalysis(BaseModel):
    """完整 AST 的确定性语义结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_dimension: str
    axis_type: str
    panel_shape: str
    earliest_decision_time: str


@dataclass(frozen=True, slots=True)
class _SemanticNode:
    """递归分析使用的内部结果。"""

    unit: _Unit
    earliest_decision_time: str


def _type_error(message: str) -> None:
    """抛出稳定的 DSL 语义类型错误。"""

    raise FactorMinerError(FailureCode.DSL_TYPE_ERROR, message)


def _analyse(
    node: FactorNode,
    registry: FieldAvailabilityRegistry,
) -> _SemanticNode:
    """递归传播单位和首版可得时点。"""

    if node.op == "field":
        entry = registry.by_public_alias(node.field or "")
        if entry.earliest_decision_time != "after_close_t":
            _type_error("首版候选只允许 after_close_t 可得字段")
        return _SemanticNode(
            unit=_Unit.base(entry.unit_dimension),
            earliest_decision_time=entry.earliest_decision_time,
        )
    if node.op == "const":
        return _SemanticNode(
            unit=_Unit.base("dimensionless"),
            earliest_decision_time="after_close_t",
        )

    children = tuple(_analyse(child, registry) for child in node.args)
    if not children:
        _type_error(f"算子缺少语义参数：{node.op}")
    if any(
        child.earliest_decision_time != "after_close_t"
        for child in children
    ):
        _type_error("AST 依赖存在首版不支持的可得时点")

    if node.op == "hlc_spread":
        if len(children) != 5 or any(child.unit != _Unit.base("price") for child in children):
            _type_error("hlc_spread 需要五个相容价格参数：前收盘、前高低价和当日高低价")
        unit = _Unit.base("dimensionless")
    elif node.op in {"add", "sub", "gt"}:
        if len(children) != 2 or children[0].unit != children[1].unit:
            _type_error(f"算子 {node.op} 要求两个参数单位相容")
        unit = _Unit.base("dimensionless") if node.op == "gt" else children[0].unit
    elif node.op in {"mul", "rolling_cov"}:
        if len(children) != 2:
            _type_error(f"{node.op} 需要两个参数")
        unit = children[0].unit.multiply(children[1].unit)
    elif node.op == "div":
        if len(children) != 2:
            _type_error("div 需要两个参数")
        unit = children[0].unit.multiply(children[1].unit, sign=-1)
    elif node.op == "rolling_salience_value":
        if len(children) != 2 or any(child.unit != _Unit.base("dimensionless") for child in children):
            _type_error("rolling_salience_value 需要两个小数制收益参数，固定theta不适用于价格单位")
        unit = _Unit.base("dimensionless")
    elif node.op == "rolling_negative_semibeta":
        if len(children) != 2:
            _type_error("rolling_negative_semibeta 需要两个参数")
        unit = children[0].unit.multiply(children[1].unit, sign=-1)
    elif node.op == "rolling_lower_tail_overlap":
        if len(children) != 2:
            _type_error("rolling_lower_tail_overlap 需要两个参数")
        unit = _Unit.base("dimensionless")
    elif node.op == "rolling_explained_increment":
        if len(children) not in {3, 4, 5, 6}:
            _type_error("rolling_explained_increment 需要被解释变量、基准解释变量及一到四个新增解释变量")
        unit = _Unit.base("dimensionless")
    elif node.op in {"rolling_residual_std", "rolling_residual_last"}:
        if len(children) != 2:
            _type_error(f"{node.op} 需要两个参数")
        unit = children[0].unit
    elif node.op == "rolling_partial_beta":
        if len(children) not in {3, 4}:
            _type_error("rolling_partial_beta 需要三个或四个参数")
        unit = children[0].unit.multiply(children[1].unit, sign=-1)
    elif node.op in {"rolling_corr", "rolling_partial_corr", "sign", "rolling_skew", "rolling_argmax", "rolling_time_corr", "rolling_drawdown_recovery"}:
        arity = {"rolling_corr": 2, "rolling_partial_corr": 3, "sign": 1, "rolling_skew": 1, "rolling_argmax": 1, "rolling_time_corr": 1, "rolling_drawdown_recovery": 1}[node.op]
        if len(children) != arity:
            _type_error(f"{node.op} 需要 {arity} 个参数")
        unit = _Unit.base("dimensionless")
    elif node.op in {
        "neg",
        "abs",
        "delay",
        "calendar_delay",
        "calendar_month_delay",
        "calendar_delta",
        "delta",
        "rolling_sum",
        "rolling_mean",
        "rolling_lower_tail_mean",
        "rolling_std",
        "rolling_min",
        "rolling_max",
    }:
        if len(children) != 1:
            _type_error(f"算子 {node.op} 需要一个参数")
        unit = children[0].unit
    else:
        _type_error(f"未知语义算子：{node.op}")
        raise AssertionError("unreachable")
    return _SemanticNode(
        unit=unit,
        earliest_decision_time="after_close_t",
    )


def analyse_semantic_type(
    node: FactorNode,
    registry: FieldAvailabilityRegistry,
) -> SemanticAnalysis:
    """分析公开别名 AST；不执行公式，也不接触 outcome。"""

    result = _analyse(node, registry)
    return SemanticAnalysis(
        unit_dimension=result.unit.render(),
        axis_type="single_asset_time_series",
        panel_shape="asset_date_scalar",
        earliest_decision_time=result.earliest_decision_time,
    )
