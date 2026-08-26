"""将合法 typed AST 编译为确定性的 Polars 表达式计划。"""

from __future__ import annotations

from typing import Any, Collection

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
from factor_miner.dsl import (
    canonical_ast,
    canonical_ast_hash,
    validate_ast,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import (
    CandidateFactorSpec,
    FactorNode,
    RegisteredCandidate,
    RegisteredTrustedCandidate,
    TrustedCandidateFactorSpec,
    registered_candidate,
    registered_trusted_candidate,
)


class CompiledFactorPlan(BaseModel):
    """不可变的候选编译计划及其可审计元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    ast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_fields: tuple[str, ...]
    required_lookback: int = Field(ge=0)
    availability: str | dict[str, str]
    expression: dict[str, Any]
    expression_metadata: dict[str, Any]


def _compiler_error(code: FailureCode, message: str) -> None:
    """抛出编译阶段的稳定错误。

    参数：
        code: 编译失败代码。
        message: 中文失败说明。

    返回：
        无。该函数始终抛出 `FactorMinerError`。
    """

    raise FactorMinerError(code, message)


def _node_period(node: FactorNode) -> int:
    """读取已校验时序节点的周期参数。

    参数：
        node: delay 或 delta 节点。

    返回：
        非负周期整数。
    """

    period = node.period if node.period is not None else node.window
    if period is None or period < 0:
        _compiler_error(FailureCode.DSL_TYPE_ERROR, "时序节点缺少合法周期")
    return period


def _rolling_window(node: FactorNode) -> int:
    """读取已校验 rolling 节点的窗口参数。

    参数：
        node: rolling 节点。

    返回：
        正整数窗口。
    """

    if node.window is None or node.window <= 0:
        _compiler_error(FailureCode.DSL_TYPE_ERROR, "rolling 节点缺少合法窗口")
    if node.center is True:
        _compiler_error(FailureCode.LOOKAHEAD_DETECTED, "禁止编译 centered rolling")
    return node.window


def _safe_division(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """构造除数为零时返回 null 的安全除法表达式。

    参数：
        numerator: 分子表达式。
        denominator: 分母表达式。

    返回：
        不添加 epsilon、除零返回 null 的 Polars 表达式。
    """

    return pl.when(denominator == 0).then(pl.lit(None)).otherwise(numerator / denominator)


def build_polars_expr(node: FactorNode) -> pl.Expr:
    """将单个白名单 AST 节点映射为 Polars 表达式。

    参数：
        node: 已通过 DSL 校验的 FactorNode。

    返回：
        可应用于按 asset/date 排序面板的 Polars 表达式。

    异常：
        节点算子或参数不属于白名单时抛出 `FactorMinerError`。
    """

    if node.op == "field":
        if node.field is None:
            _compiler_error(FailureCode.DSL_TYPE_ERROR, "field 节点缺少字段名")
        return pl.col(node.field)
    if node.op == "const":
        if node.value is None:
            _compiler_error(FailureCode.DSL_TYPE_ERROR, "const 节点缺少数值")
        return pl.lit(node.value)

    child_expressions = [build_polars_expr(child) for child in node.args]

    if node.op == "add":
        return child_expressions[0] + child_expressions[1]
    if node.op == "sub":
        return child_expressions[0] - child_expressions[1]
    if node.op == "mul":
        return child_expressions[0] * child_expressions[1]
    if node.op == "div":
        return _safe_division(child_expressions[0], child_expressions[1])
    if node.op == "neg":
        return -child_expressions[0]
    if node.op == "abs":
        return child_expressions[0].abs()
    if node.op == "delay":
        return child_expressions[0].shift(_node_period(node)).over("asset")
    if node.op == "delta":
        return child_expressions[0].diff(_node_period(node)).over("asset")

    window = _rolling_window(node) if node.op.startswith("rolling_") else None
    if node.op == "rolling_sum":
        return child_expressions[0].rolling_sum(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_mean":
        return child_expressions[0].rolling_mean(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_std":
        return child_expressions[0].rolling_std(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_min":
        return child_expressions[0].rolling_min(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_max":
        return child_expressions[0].rolling_max(
            window_size=window, min_samples=window
        ).over("asset")
    if node.op == "rolling_corr":
        return pl.rolling_corr(
            child_expressions[0],
            child_expressions[1],
            window_size=window,
            min_samples=window,
        ).over("asset")

    _compiler_error(FailureCode.DSL_TYPE_ERROR, f"未知编译算子：{node.op}")


def _candidate_record(
    candidate: RegisteredCandidate
    | CandidateFactorSpec
    | RegisteredTrustedCandidate
    | TrustedCandidateFactorSpec,
) -> RegisteredCandidate | RegisteredTrustedCandidate:
    """将候选输入统一为已登记记录。

    参数：
        candidate: 已登记候选或尚未包装的合法候选 spec。

    返回：
        可用于编译审计的 `RegisteredCandidate`。
    """

    if isinstance(candidate, (RegisteredCandidate, RegisteredTrustedCandidate)):
        return candidate
    if isinstance(candidate, TrustedCandidateFactorSpec):
        return registered_trusted_candidate(candidate)
    return registered_candidate(candidate)


def compile_candidate(
    candidate: RegisteredCandidate
    | CandidateFactorSpec
    | RegisteredTrustedCandidate
    | TrustedCandidateFactorSpec,
    allowed_fields: Collection[str],
) -> CompiledFactorPlan:
    """校验并编译候选，生成确定性 Polars 计划。

    参数：
        candidate: 已登记候选或合法候选 spec。
        allowed_fields: 当前数据合同允许的原始字段。

    返回：
        记录 AST 哈希、计划哈希、字段、lookback 和可得性的编译计划。

    异常：
        AST 非法、标签泄漏、字段声明不一致或 lookback 超限时抛出错误。
    """

    record = _candidate_record(candidate)
    forbidden_fields = {
        "label",
        "label_o2o_5d",
        "target",
        "forward_return",
    }
    metadata = validate_ast(
        record.spec.expression,
        allowed_fields,
        forbidden_fields,
    )
    declared_fields = tuple(sorted(record.spec.required_fields))
    if declared_fields != metadata.required_fields:
        _compiler_error(
            FailureCode.FIELD_MISSING,
            "CandidateFactorSpec.required_fields 与 AST 实际字段不一致",
        )
    if metadata.lookback > record.spec.max_lookback:
        _compiler_error(
            FailureCode.DSL_TYPE_ERROR,
            "AST 实际 lookback 超过 CandidateFactorSpec 声明值",
        )

    expression = canonical_ast(record.spec.expression)
    expression_metadata = {
        "node_count": metadata.node_count,
        "depth": metadata.depth,
        "operator_signature": metadata.operator_signature,
        "field_signature": metadata.field_signature,
    }
    ast_hash = canonical_ast_hash(record.spec.expression)
    plan_payload = {
        "candidate_id": record.candidate_id,
        "ast_hash": ast_hash,
        "required_fields": metadata.required_fields,
        "required_lookback": metadata.lookback,
        "availability": (
            record.spec.availability.model_dump(mode="json")
            if hasattr(record.spec.availability, "model_dump")
            else record.spec.availability
        ),
        "expression": expression,
        "expression_metadata": expression_metadata,
    }
    return CompiledFactorPlan(
        candidate_id=record.candidate_id,
        ast_hash=ast_hash,
        plan_hash=sha256_json(plan_payload),
        required_fields=metadata.required_fields,
        required_lookback=metadata.lookback,
        availability=(
            record.spec.availability.model_dump(mode="json")
            if hasattr(record.spec.availability, "model_dump")
            else record.spec.availability
        ),
        expression=expression,
        expression_metadata=expression_metadata,
    )
