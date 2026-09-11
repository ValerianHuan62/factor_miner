"""把事前结构化测量合同编译成确定性 Γ；自然语言语义映射仍须事前审定。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.construct_validation import ObservableCondition
from factor_miner.dsl import validate_ast, CALENDAR_MONTH_LIMITS
from factor_miner.schema import FactorNode, HypothesisSpec


class ExpressionConstraint(BaseModel):
    """固定运算拓扑，每个节点只允许预登记的字段、算子及参数选择。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operators: tuple[str, ...] = Field(min_length=1)
    arguments: tuple[ExpressionConstraint, ...] = ()
    fields: tuple[str, ...] = ()
    windows: tuple[int, ...] = ()
    periods: tuple[int, ...] = ()
    constants: tuple[FiniteFloat, ...] = ()

    @model_validator(mode="after")
    def bounded(self):
        """拒绝没有明确参数域的叶子与重复选择。"""
        for values in (self.operators, self.fields, self.windows, self.periods, self.constants):
            if len(values) != len(set(values)):
                raise ValueError("Γ 节点选择不能重复")
        if any(v < 1 for v in self.windows) or any(v < 0 for v in self.periods):
            raise ValueError("Γ 窗口必须为正、滞后不能为负")
        if "field" in self.operators and (self.operators != ("field",) or not self.fields or self.arguments):
            raise ValueError("Γ 字段叶子必须明确字段选择")
        if "const" in self.operators and (self.operators != ("const",) or not self.constants or self.arguments):
            raise ValueError("Γ 常数叶子必须明确有限常数")
        return self

    def check(self, node: FactorNode, path: str = "root") -> None:
        """逐节点检查；不接受自行换窗、取负或缺少子表达式。"""
        if node.op not in self.operators or len(node.args) != len(self.arguments):
            raise ValueError(f"Γ 运算结构不符：{path}")
        for value, allowed, name in ((node.field, self.fields, "field"),
                                    (node.window, self.windows, "window"),
                                    (node.period, self.periods, "period"),
                                    (node.value, self.constants, "constant")):
            if (allowed and value not in allowed) or (not allowed and value is not None):
                raise ValueError(f"Γ {name} 不符：{path}")
        for index, (rule, child) in enumerate(zip(self.arguments, node.args, strict=True)):
            rule.check(child, f"{path}.{index}")


class StateMeasurement(BaseModel):
    """同一观察条件的真实市场状态代理，只能使用当时可得字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    expression: FactorNode
    expected_direction: Literal["increase", "decrease"]
    rationale: str = Field(min_length=1)


class ConditionContract(BaseModel):
    """公式生成之前冻结的观察条件、结构约束和验证映射。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    condition_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    observation: ObservableCondition
    required_fields: tuple[str, ...] = Field(min_length=1)
    expression_constraint: ExpressionConstraint
    state_measurements: tuple[StateMeasurement, ...] = Field(min_length=1)
    # 条件触发方向是测量状态方向，不是因子与未来收益的方向。
    activation_direction: Literal["high", "low"]
    expected_return_sign: Literal["positive", "negative"]
    measurement_rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def preregistered(self):
        """新协议不能把事后审计或空反例升级成已冻结构念合同。"""
        if self.observation.origin != "preregistered" or self.observation.response_test == "none":
            raise ValueError("新协议要求事前登记且可执行的合成响应检验")
        names = [x.name for x in self.state_measurements]
        if len(names) != len(set(names)) or len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("观察变量和必需字段不能重复")
        return self


class CompiledGamma(BaseModel):
    """约束身份绑定完整原假设与事前条件，不依赖生成者身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["hypothesis-gamma-v1", "hypothesis-gamma-calendar-month-v1"] = "hypothesis-gamma-v1"
    hypothesis_sha256: str
    condition: ConditionContract
    allowed_fields: tuple[str, ...]

    @property
    def identity(self) -> str:
        return sha256_json(self.model_dump(mode="json"))

    @property
    def dsl_limits(self):
        """版本选择只改变观察时钟，不改变复杂度或显著性门槛。"""
        return CALENDAR_MONTH_LIMITS if self.version == "hypothesis-gamma-calendar-month-v1" else None

    def validate(self, expression: FactorNode, hypothesis: HypothesisSpec) -> None:
        """先验证原假设身份，再检查必需变量、结构、参数及全局 DSL。"""
        if sha256_json(hypothesis.model_dump(mode="json")) != self.hypothesis_sha256:
            raise ValueError("Γ 绑定的原假设或预期方向发生变化")
        metadata = validate_ast(expression, self.allowed_fields, {"label", "label_o2o_1d", "label_o2o_5d", "label_o2o_20d", "forward_return", "target"}, limits=self.dsl_limits)
        if not set(self.condition.required_fields).issubset(metadata.required_fields):
            raise ValueError("Γ 缺少假设必须使用的变量")
        self.condition.expression_constraint.check(expression)


def compile_gamma(hypothesis: HypothesisSpec, condition: ConditionContract,
                  allowed_fields: tuple[str, ...], *, version: str = "hypothesis-gamma-v1") -> CompiledGamma:
    """结构化合同到 Γ 的纯函数；不调用 LLM、不读取因子结果。"""
    if not set(condition.required_fields).issubset(allowed_fields):
        raise ValueError("Γ 必需字段不属于数据合同")
    gamma = CompiledGamma(version=version, hypothesis_sha256=sha256_json(hypothesis.model_dump(mode="json")),
                         condition=condition, allowed_fields=tuple(sorted(allowed_fields)))
    for measurement in condition.state_measurements:
        validate_ast(measurement.expression, allowed_fields, {"label", "label_o2o_1d", "label_o2o_5d", "label_o2o_20d", "forward_return", "target"}, limits=gamma.dsl_limits)
    return gamma
