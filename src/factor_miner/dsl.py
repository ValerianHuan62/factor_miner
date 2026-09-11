"""候选因子 typed DSL 的白名单校验与规范化 AST。"""

from dataclasses import dataclass
from typing import Any, Collection

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import FactorNode


@dataclass(frozen=True, slots=True)
class DslLimits:
    """V0 typed DSL 的复杂度和窗口限制。"""

    allowed_windows: tuple[int, ...] = (5, 10, 20, 40, 60, 120)
    max_nodes: int = 15
    max_depth: int = 5
    max_lookback: int = 130
    calendar_month_periods: tuple[int, ...] = ()


CALENDAR_MONTH_LIMITS = DslLimits(calendar_month_periods=(1, 6, 7, 18))


@dataclass(frozen=True, slots=True)
class AstMetadata:
    """校验后的 AST 结构、字段和时序元数据。"""

    required_fields: tuple[str, ...]
    lookback: int
    node_count: int
    depth: int
    operator_signature: tuple[str, ...]
    field_signature: tuple[str, ...]
    canonical: dict[str, Any]

    @property
    def nodes(self) -> int:
        """返回节点数量的简写属性。"""

        return self.node_count

    @property
    def max_depth(self) -> int:
        """返回 AST 最大深度的简写属性。"""

        return self.depth


@dataclass(frozen=True, slots=True)
class _NodeAnalysis:
    """递归校验过程中的内部分析结果。"""

    required_fields: frozenset[str]
    lookback: int
    node_count: int
    depth: int
    operator_signature: tuple[str, ...]
    canonical: dict[str, Any]


_BINARY_OPERATORS = frozenset({"add", "sub", "mul", "div", "gt"})
_UNARY_OPERATORS = frozenset({"neg", "abs", "sign"})
_TEMPORAL_OPERATORS = frozenset({"delay", "delta", "calendar_delay", "calendar_delta", "calendar_month_delay"})
_ROLLING_OPERATORS = frozenset(
    {
        "rolling_sum",
        "rolling_mean",
        "rolling_lower_tail_mean",
        "rolling_lower_tail_overlap",
        "rolling_negative_semibeta",
        "rolling_salience_value",
        "rolling_std",
        "rolling_residual_std",
        "rolling_residual_last",
        "rolling_explained_increment",
        "rolling_skew",
        "rolling_min",
        "rolling_max",
        "rolling_argmax",
        "rolling_drawdown_recovery",
        "rolling_time_corr",
        "rolling_corr",
        "rolling_cov",
        "rolling_partial_corr",
        "rolling_partial_beta",
    }
)
_OPERATORS = (
    _BINARY_OPERATORS
    | _UNARY_OPERATORS
    | _TEMPORAL_OPERATORS
    | _ROLLING_OPERATORS
    | frozenset({"hlc_spread"})
)
_TEMPORAL_PARAMETER_KEYS = frozenset({"period", "window"})


def _dsl_error(code: FailureCode, message: str) -> None:
    """抛出带稳定代码的 DSL 错误。

    参数：
        code: DSL 失败代码。
        message: 面向调用方的中文错误说明。

    返回：
        无。该函数始终抛出 `FactorMinerError`。
    """

    raise FactorMinerError(code, message)


def _canonical_children(node: FactorNode) -> list[dict[str, Any]]:
    """生成节点子树的规范化表示并处理交换律。

    参数：
        node: 当前 AST 节点。

    返回：
        规范化后的子树列表。
    """

    children = [canonical_ast(child) for child in node.args]
    if node.op in {"add", "mul"}:
        children.sort(key=sha256_json)
    return children


def canonical_ast(node: FactorNode) -> dict[str, Any]:
    """将 FactorNode 转换为确定性的 JSON 兼容 AST。

    参数：
        node: 要规范化的 typed AST 节点。

    返回：
        只包含语义字段的规范化 AST 字典；add 和 mul 的子节点按哈希排序。
    """

    if node.op == "field":
        return {"op": "field", "field": node.field}
    if node.op == "const":
        return {"op": "const", "value": node.value}

    canonical: dict[str, Any] = {
        "op": node.op,
        "args": _canonical_children(node),
    }
    if node.op in _TEMPORAL_OPERATORS:
        period = node.period if node.period is not None else node.window
        if period is not None:
            canonical["period"] = period
    elif node.window is not None:
        canonical["window"] = node.window
    if node.center is True:
        canonical["center"] = True
    return canonical


def canonical_ast_hash(node: FactorNode) -> str:
    """返回规范化 AST 的完整 SHA-256 哈希。

    参数：
        node: 要计算哈希的 typed AST 节点。

    返回：
        小写十六进制 SHA-256 哈希。
    """

    return sha256_json(canonical_ast(node))


def iter_ast_nodes(node: FactorNode) -> tuple[FactorNode, ...]:
    """按前序遍历返回 AST 全部节点。

    参数：
        node: 待遍历的 typed AST 根节点。

    返回：
        包含当前节点及全部子节点的稳定前序元组。
    """

    nodes = [node]
    for child in node.args:
        nodes.extend(iter_ast_nodes(child))
    return tuple(nodes)


def strip_temporal_parameters_from_canonical(
    canonical: dict[str, Any],
) -> dict[str, Any]:
    """递归删除规范化 AST 中仅表示时间长度的参数。

    参数：
        canonical: `canonical_ast` 生成的 JSON 兼容 AST。

    返回：
        删除 `period`、`window` 后的新 AST；其他语义字段保持不变。
    """

    stripped: dict[str, Any] = {}
    for key, value in canonical.items():
        if key in _TEMPORAL_PARAMETER_KEYS:
            continue
        if key == "args":
            stripped[key] = tuple(
                strip_temporal_parameters_from_canonical(child)
                for child in value
            )
            continue
        stripped[key] = value
    return stripped


def canonical_ast_without_temporal_parameters(node: FactorNode) -> dict[str, Any]:
    """返回移除时间参数后的规范化 AST。

    参数：
        node: 要处理的 typed AST 节点。

    返回：
        与 `canonical_ast(node)` 拓扑一致，但不含 `period`/`window` 的字典。
    """

    return strip_temporal_parameters_from_canonical(canonical_ast(node))


def _validate_parameter_shape(node: FactorNode, op: str) -> None:
    """拒绝算子携带不属于自身的参数。

    参数：
        node: 当前 AST 节点。
        op: 当前算子名称。

    返回：
        无。参数形状非法时抛出 DSL 错误。
    """

    if op not in {"field", "const"} and (node.field is not None or node.value is not None):
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 不能携带 field 或 value")
    if op not in _ROLLING_OPERATORS and node.center is not None:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 不能携带 center")
    if op not in _ROLLING_OPERATORS and op not in _TEMPORAL_OPERATORS and node.window is not None:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 不能携带 window")
    if op not in _TEMPORAL_OPERATORS and node.period is not None:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 不能携带 period")


def _temporal_period(node: FactorNode, op: str) -> int:
    """读取 delay 或 delta 的周期参数。

    参数：
        node: 当前时序节点。
        op: delay 或 delta。

    返回：
        非负周期整数。
    """

    if node.period is not None and node.window is not None:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 不能同时设置 period 和 window")
    period = node.period if node.period is not None else node.window
    if period is None:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"算子 {op} 缺少 period")
    if period < 0:
        _dsl_error(FailureCode.LOOKAHEAD_DETECTED, f"算子 {op} 不允许负周期")
    return period


def _analyse_node(
    node: FactorNode,
    allowed_fields: set[str],
    forbidden_fields: set[str],
    limits: DslLimits,
) -> _NodeAnalysis:
    """递归校验节点并计算其结构元数据。

    参数：
        node: 当前递归节点。
        allowed_fields: 允许引用的字段集合。
        forbidden_fields: 无论是否允许都必须拒绝的字段集合。
        limits: 复杂度和时序限制。

    返回：
        当前子树的字段、lookback、复杂度和规范化信息。
    """

    op = node.op
    if op == "field":
        _validate_parameter_shape(node, op)
        if node.field in forbidden_fields:
            _dsl_error(FailureCode.LABEL_LEAKAGE_DETECTED, f"禁止引用字段：{node.field}")
        if node.field not in allowed_fields:
            _dsl_error(FailureCode.FIELD_MISSING, f"字段不在允许集合中：{node.field}")
        return _NodeAnalysis(
            required_fields=frozenset({node.field}),
            lookback=0,
            node_count=1,
            depth=1,
            operator_signature=(op,),
            canonical=canonical_ast(node),
        )

    if op == "const":
        _validate_parameter_shape(node, op)
        return _NodeAnalysis(
            required_fields=frozenset(),
            lookback=0,
            node_count=1,
            depth=1,
            operator_signature=(op,),
            canonical=canonical_ast(node),
        )

    if op not in _OPERATORS:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"未知算子：{op}")
    _validate_parameter_shape(node, op)

    expected_arity = 2 if op in _BINARY_OPERATORS else 1
    if op == "hlc_spread":
        expected_arity = 5
    elif op in {"rolling_corr", "rolling_cov", "rolling_residual_std", "rolling_residual_last", "rolling_lower_tail_overlap", "rolling_negative_semibeta", "rolling_salience_value"}:
        expected_arity = 2
    elif op in {"rolling_partial_corr", "rolling_partial_beta"}:
        expected_arity = 3
    allowed_arities = ((3, 4, 5, 6) if op == "rolling_explained_increment" else
                       (3, 4) if op == "rolling_partial_beta" else (expected_arity,))
    if len(node.args) not in allowed_arities:
        _dsl_error(
            FailureCode.DSL_TYPE_ERROR,
            f"算子 {op} 需要 {allowed_arities} 个参数，实际为 {len(node.args)} 个",
        )
    if op in {"calendar_delay", "calendar_delta", "calendar_month_delay"} and node.args[0].op != "field":
        _dsl_error(FailureCode.DSL_TYPE_ERROR, f"{op} 当前只支持原始字段，不接受嵌套计算")

    children = [
        _analyse_node(child, allowed_fields, forbidden_fields, limits)
        for child in node.args
    ]
    required_fields = frozenset().union(*(child.required_fields for child in children))
    node_count = 1 + sum(child.node_count for child in children)
    depth = 1 + max(child.depth for child in children)
    lookback = max(child.lookback for child in children)

    child_has_month = any("calendar_month_delay" in c.operator_signature for c in children)
    if child_has_month and op in (_TEMPORAL_OPERATORS | _ROLLING_OPERATORS):
        _dsl_error(FailureCode.DSL_TYPE_ERROR, "日历月观察不得再嵌套时序算子")
    if op == "calendar_month_delay":
        period = _temporal_period(node, op)
        if period not in limits.calendar_month_periods:
            _dsl_error(FailureCode.DSL_TYPE_ERROR, "日历月观察需要显式协议及允许的月份偏移")
        # 用自然日上界保守表示所需市场观察跨度，绝不用于实际端点定位。
        lookback = 31 * period
    elif op in _TEMPORAL_OPERATORS:
        lookback += _temporal_period(node, op)
    elif op in _ROLLING_OPERATORS:
        if node.period is not None:
            _dsl_error(FailureCode.DSL_TYPE_ERROR, f"rolling 算子 {op} 不能使用 period")
        if node.window is None:
            _dsl_error(FailureCode.DSL_TYPE_ERROR, f"rolling 算子 {op} 缺少 window")
        if node.window not in limits.allowed_windows:
            _dsl_error(FailureCode.DSL_TYPE_ERROR, f"不允许的 rolling 窗口：{node.window}")
        if node.center is True:
            _dsl_error(FailureCode.LOOKAHEAD_DETECTED, "禁止使用 centered rolling")
        lookback += node.window - 1

    if node_count > limits.max_nodes:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, "AST 节点数量超过限制")
    if depth > limits.max_depth:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, "AST 深度超过限制")
    bound = max(limits.max_lookback, 31 * max(limits.calendar_month_periods, default=0)) if (op == "calendar_month_delay" or child_has_month) else limits.max_lookback
    if lookback > bound:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, "AST lookback 超过限制")

    return _NodeAnalysis(
        required_fields=required_fields,
        lookback=lookback,
        node_count=node_count,
        depth=depth,
        operator_signature=(op,)
        + tuple(operator for child in children for operator in child.operator_signature),
        canonical=canonical_ast(node),
    )


def validate_ast(
    node: FactorNode,
    allowed_fields: Collection[str],
    forbidden_fields: Collection[str],
    limits: DslLimits | None = None,
) -> AstMetadata:
    """校验 typed AST 并返回编译所需的结构元数据。

    参数：
        node: 待校验的递归 AST。
        allowed_fields: 允许被引用的原始数据字段。
        forbidden_fields: 必须拒绝的标签或未来字段。
        limits: 可选复杂度限制；省略时使用 V0 默认值。

    返回：
        包含 required fields、lookback、节点数、深度和签名的 `AstMetadata`。

    异常：
        结构、时序、字段或标签泄漏违反协议时抛出 `FactorMinerError`。
    """

    active_limits = limits or DslLimits()
    analysis = _analyse_node(
        node,
        set(allowed_fields),
        set(forbidden_fields),
        active_limits,
    )
    if not analysis.required_fields:
        _dsl_error(FailureCode.DSL_TYPE_ERROR, "因子表达式不能只包含常数")
    return AstMetadata(
        required_fields=tuple(sorted(analysis.required_fields)),
        lookback=analysis.lookback,
        node_count=analysis.node_count,
        depth=analysis.depth,
        operator_signature=analysis.operator_signature,
        field_signature=tuple(sorted(analysis.required_fields)),
        canonical=analysis.canonical,
    )
