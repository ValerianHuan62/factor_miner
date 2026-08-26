"""V0.5 字段公开别名与 point-in-time 可用性测试。"""

import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
    intraday_daily_field_entries,
    resolve_public_field_aliases,
)
from factor_miner.schema import FactorNode


def registry() -> FieldAvailabilityRegistry:
    """构造含合法价格和非法后复权字段的合成注册表。"""

    return FieldAvailabilityRegistry(
        registry_id="field-registry-synthetic-v1",
        data_release_id="release-synthetic-v1",
        fields=(
            FieldAvailabilityEntry(
                field_id="close",
                public_alias="price_close",
                economic_type="market_price",
                unit_dimension="price",
                panel_shape="asset_date_scalar",
                event_time="close_t",
                source_publish_time="close_t",
                vendor_available_time="after_close_t",
                revision_policy="immutable_daily_bar",
                point_in_time_guarantee=True,
                earliest_decision_time="after_close_t",
                eligible_for_factor=True,
            ),
            FieldAvailabilityEntry(
                field_id="back_adjusted_close",
                public_alias="adjusted_price_history",
                economic_type="adjusted_price",
                unit_dimension="price",
                panel_shape="asset_date_scalar",
                event_time="close_t",
                source_publish_time="future_corporate_action",
                vendor_available_time="after_close_t",
                revision_policy="historically_rewritten",
                point_in_time_guarantee=False,
                earliest_decision_time="after_close_t",
                eligible_for_factor=False,
            ),
        ),
    )


class FieldRegistryTest(unittest.TestCase):
    """公开别名解析不得绕过 point-in-time 和准入边界。"""

    def test_alias_resolves_to_local_field_without_model_override(self) -> None:
        node = FactorNode(op="field", field="price_close")
        resolved = resolve_public_field_aliases(node, registry())
        self.assertEqual(resolved.field, "close")

    def test_non_point_in_time_field_is_rejected(self) -> None:
        node = FactorNode(op="field", field="adjusted_price_history")
        with self.assertRaises(FactorMinerError) as context:
            resolve_public_field_aliases(node, registry())
        self.assertEqual(context.exception.code, FailureCode.FIELD_MISSING)

    def test_intraday_aggregates_are_available_only_after_close(self) -> None:
        """分钟聚合是日级字段，不能宣称盘中可交易。"""

        entries = intraday_daily_field_entries()
        self.assertEqual(len(entries), 4)
        for entry in entries:
            self.assertTrue(entry.field_id.startswith("intraday_"))
            self.assertEqual(entry.panel_shape, "asset_date_scalar")
            self.assertEqual(entry.event_time, "close_t")
            self.assertEqual(entry.earliest_decision_time, "after_close_t")
            self.assertTrue(entry.point_in_time_guarantee)


if __name__ == "__main__":
    unittest.main()
