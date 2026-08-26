import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.dsl import (
    DslLimits,
    canonical_ast,
    canonical_ast_hash,
    validate_ast,
)
from factor_miner.schema import FactorNode


def field_node(name: str = "close") -> FactorNode:
    """构造一个字段叶子节点。"""

    return FactorNode(op="field", field=name)


def unary_chain(length: int) -> FactorNode:
    """构造指定层数的 neg 节点链。"""

    node = field_node()
    for _ in range(length):
        node = FactorNode(op="neg", args=(node,))
    return node


class DslTest(unittest.TestCase):
    """typed DSL 语法、时序和规范化测试。"""

    def test_valid_20_day_close_momentum(self) -> None:
        """验证合法的二十日收盘价动量 AST。"""
        node = FactorNode(op="delta", args=(field_node(),), period=20)
        metadata = validate_ast(node, {"close"}, {"label_o2o_5d"})
        self.assertEqual(metadata.required_fields, ("close",))
        self.assertEqual(metadata.lookback, 20)
        self.assertEqual(metadata.node_count, 2)
        self.assertEqual(metadata.depth, 2)

    def test_negative_delay_is_rejected_as_lookahead(self) -> None:
        """验证负 delay 被识别为未来信息。"""
        node = FactorNode(op="delay", args=(field_node(),), period=-1)
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close"}, set())
        self.assertEqual(context.exception.code, FailureCode.LOOKAHEAD_DETECTED)

    def test_centered_rolling_is_rejected_as_lookahead(self) -> None:
        """验证居中 rolling 被识别为未来信息。"""
        node = FactorNode(
            op="rolling_mean",
            args=(field_node(),),
            window=20,
            center=True,
        )
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close"}, set())
        self.assertEqual(context.exception.code, FailureCode.LOOKAHEAD_DETECTED)

    def test_label_field_is_rejected(self) -> None:
        """验证标签字段不能进入因子表达式。"""
        node = field_node("label_o2o_5d")
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close", "label_o2o_5d"}, {"label_o2o_5d"})
        self.assertEqual(context.exception.code, FailureCode.LABEL_LEAKAGE_DETECTED)

    def test_unknown_operator_is_rejected(self) -> None:
        """验证未知算子被拒绝。"""
        node = FactorNode(op="sqrt", args=(field_node(),))
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close"}, set())
        self.assertEqual(context.exception.code, FailureCode.DSL_TYPE_ERROR)

    def test_window_outside_whitelist_is_rejected(self) -> None:
        """验证不在固定集合内的 rolling 窗口被拒绝。"""
        node = FactorNode(op="rolling_mean", args=(field_node(),), window=7)
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close"}, set())
        self.assertEqual(context.exception.code, FailureCode.DSL_TYPE_ERROR)

    def test_node_depth_and_lookback_limits_are_enforced(self) -> None:
        """验证节点数、深度和 lookback 上限都生效。"""
        with self.assertRaises(FactorMinerError):
            validate_ast(unary_chain(15), {"close"}, set())
        with self.assertRaises(FactorMinerError):
            validate_ast(unary_chain(5), {"close"}, set())
        too_far = FactorNode(op="delay", args=(field_node(),), period=131)
        with self.assertRaises(FactorMinerError):
            validate_ast(too_far, {"close"}, set())

    def test_commutative_children_share_canonical_hash(self) -> None:
        """验证 add 和 mul 的交换律规范化。"""
        left = FactorNode(op="field", field="close")
        right = FactorNode(op="field", field="volume")
        add_left = FactorNode(op="add", args=(left, right))
        add_right = FactorNode(op="add", args=(right, left))
        mul_left = FactorNode(op="mul", args=(left, right))
        mul_right = FactorNode(op="mul", args=(right, left))
        self.assertEqual(canonical_ast_hash(add_left), canonical_ast_hash(add_right))
        self.assertEqual(canonical_ast_hash(mul_left), canonical_ast_hash(mul_right))
        self.assertEqual(canonical_ast(add_left), canonical_ast(add_right))

    def test_subtraction_children_keep_order(self) -> None:
        """验证 sub 不执行交换律规范化。"""
        left = FactorNode(op="field", field="close")
        right = FactorNode(op="field", field="volume")
        first = FactorNode(op="sub", args=(left, right))
        second = FactorNode(op="sub", args=(right, left))
        self.assertNotEqual(canonical_ast_hash(first), canonical_ast_hash(second))

    def test_constants_only_expression_is_rejected(self) -> None:
        """验证只含常数的表达式不能作为因子。"""
        node = FactorNode(op="add", args=(FactorNode(op="const", value=1),))
        with self.assertRaises(FactorMinerError) as context:
            validate_ast(node, {"close"}, set())
        self.assertEqual(context.exception.code, FailureCode.DSL_TYPE_ERROR)

    def test_missing_field_and_invalid_arity_are_rejected(self) -> None:
        """验证未知字段和错误参数个数被拒绝。"""
        with self.assertRaises(FactorMinerError) as missing_context:
            validate_ast(field_node("amount"), {"close"}, set())
        self.assertEqual(missing_context.exception.code, FailureCode.FIELD_MISSING)
        invalid_arity = FactorNode(op="add", args=(field_node(),))
        with self.assertRaises(FactorMinerError) as arity_context:
            validate_ast(invalid_arity, {"close"}, set())
        self.assertEqual(arity_context.exception.code, FailureCode.DSL_TYPE_ERROR)


if __name__ == "__main__":
    unittest.main()
