"""Task6 三设计差异合同测试。"""

import unittest

from factor_miner.design_diversity import preflight_designs
from factor_miner.field_registry import FieldAvailabilityEntry, FieldAvailabilityRegistry
from factor_miner.research_evolution_schema import DesignDiversityPolicy
from factor_miner.schema import FactorNode


def _registry() -> FieldAvailabilityRegistry:
    """创建不含真实行情的合成字段注册表。"""

    return FieldAvailabilityRegistry(
        registry_id="registry-task6",
        data_release_id="release-task6",
        fields=(
            FieldAvailabilityEntry(
                field_id="close",
                public_alias="price_close",
                economic_type="price",
                unit_dimension="currency",
                panel_shape="asset_date_scalar",
                event_time="close_t",
                source_publish_time="close_t",
                vendor_available_time="close_t",
                revision_policy="none",
                point_in_time_guarantee=True,
                earliest_decision_time="after_close_t",
                eligible_for_factor=True,
            ),
        ),
    )


class CampaignDesignDiversityTest(unittest.TestCase):
    """验证窗口变化不能冒充三设计结构差异。"""

    def test_window_only_designs_fail_before_evaluation(self) -> None:
        expressions = tuple(
            FactorNode(
                op="rolling_mean",
                args=(FactorNode(op="field", field="close"),),
                window=window,
                center=False,
            )
            for window in (5, 10, 20)
        )

        result = preflight_designs(
            expressions,
            policy=DesignDiversityPolicy(),
            registry=_registry(),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_code, "DESIGN_DIVERSITY_FAILED")
        self.assertTrue(result.failures)

    def test_operator_structure_difference_passes(self) -> None:
        field = FactorNode(op="field", field="close")
        result = preflight_designs(
            (
                FactorNode(op="rolling_mean", args=(field,), window=5, center=False),
                FactorNode(op="rolling_std", args=(field,), window=10, center=False),
                FactorNode(op="delta", args=(field,), period=20),
            ),
            policy=DesignDiversityPolicy(),
            registry=_registry(),
        )

        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
