"""V0.5 DSL 单位、轴与 availability 语义测试。"""

import unittest

from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.schema import FactorNode


def semantic_registry() -> FieldAvailabilityRegistry:
    """构造价格与成交额两种不相容单位。"""

    common = {
        "panel_shape": "asset_date_scalar",
        "event_time": "close_t",
        "source_publish_time": "close_t",
        "vendor_available_time": "after_close_t",
        "revision_policy": "immutable_daily",
        "point_in_time_guarantee": True,
        "earliest_decision_time": "after_close_t",
        "eligible_for_factor": True,
    }
    return FieldAvailabilityRegistry(
        registry_id="field-registry-semantic-v1",
        data_release_id="release-semantic-v1",
        fields=(
            FieldAvailabilityEntry(
                field_id="close",
                public_alias="price_close",
                economic_type="market_price",
                unit_dimension="price",
                **common,
            ),
            FieldAvailabilityEntry(
                field_id="amount",
                public_alias="traded_value",
                economic_type="trading_activity",
                unit_dimension="currency_amount",
                **common,
            ),
        ),
    )


class DslSemanticTest(unittest.TestCase):
    """可执行但单位错误的表达式必须在编译前失败。"""

    def test_partial_correlation_and_new_temporal_units(self) -> None:
        price = FactorNode(op="field", field="price_close")
        amount = FactorNode(op="field", field="traded_value")
        lag = FactorNode(op="calendar_delay", args=(price,), period=1)
        self.assertEqual(analyse_semantic_type(lag, semantic_registry()).unit_dimension, "price")
        for expression in [FactorNode(op="sign", args=(price,)),
                           FactorNode(op="rolling_partial_corr", args=(price, amount, lag), window=20)]:
            analysis = analyse_semantic_type(expression, semantic_registry())
            self.assertEqual(analysis.unit_dimension, "dimensionless")
            self.assertEqual(analysis.earliest_decision_time, "after_close_t")

    def test_add_rejects_incompatible_units(self) -> None:
        node = FactorNode(
            op="add",
            args=(
                FactorNode(op="field", field="price_close"),
                FactorNode(op="field", field="traded_value"),
            ),
        )
        with self.assertRaises(FactorMinerError) as context:
            analyse_semantic_type(node, semantic_registry())
        self.assertEqual(context.exception.code, FailureCode.DSL_TYPE_ERROR)

    def test_price_change_divided_by_lagged_price_is_dimensionless(self) -> None:
        price = FactorNode(op="field", field="price_close")
        node = FactorNode(
            op="div",
            args=(
                FactorNode(op="delta", args=(price,), period=20),
                FactorNode(op="delay", args=(price,), period=20),
            ),
        )
        analysis = analyse_semantic_type(node, semantic_registry())
        self.assertEqual(analysis.unit_dimension, "dimensionless")
        self.assertEqual(analysis.axis_type, "single_asset_time_series")
        self.assertEqual(analysis.earliest_decision_time, "after_close_t")


if __name__ == "__main__":
    unittest.main()
