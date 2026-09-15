"""单因子回测演示合同测试。"""

from pathlib import Path
import unittest

from dashboard.demo_backtest import (
    DEMO_FACTOR_ID,
    FROZEN_COST_BPS,
    FROZEN_HAC_MAX_LAGS,
    demo_barra_rows,
    demo_daily_rows,
    demo_ic_rows,
    demo_ic_summary,
)


class DemoBacktestTest(unittest.TestCase):
    """演示只包含一个固定候选，不读取或批量计算历史目录。"""

    def test_demo_has_reproducible_daily_ic_and_barra_shapes(self) -> None:
        daily = demo_daily_rows()
        self.assertEqual(len(daily), 90)
        self.assertEqual(len(demo_ic_rows()), len(daily))
        self.assertAlmostEqual(
            daily[0]["target_long_cost"],
            daily[0]["target_long_turnover"] * FROZEN_COST_BPS / 10_000,
        )
        self.assertEqual(set(demo_barra_rows()), {"exposure", "attribution", "risk"})

    def test_demo_uses_frozen_hac_instead_of_plain_sample_t(self) -> None:
        summary = demo_ic_summary()
        self.assertIn("rank_ic_hac_t", summary)
        self.assertNotIn("rank_ic_t", summary)
        self.assertEqual(FROZEN_HAC_MAX_LAGS, 5)

    def test_backtest_page_is_single_demo_not_367_factor_recalculation(self) -> None:
        page = Path("dashboard/pages/3_分组回测.py").read_text("utf-8")
        self.assertIn("DEMO_FACTOR_ID", page)
        self.assertNotIn("PostgresDashboardStore", page)
        self.assertNotIn("load_factor_metrics_detail", page)
        self.assertIn("Calmar", page)
        self.assertNotIn('["交互回测", "IC 诊断", "Barra 归因"', page)
        self.assertEqual(DEMO_FACTOR_ID, "demo_momentum_20d")

    def test_us_page_routes_to_aggregated_real_backtest_catalog(self) -> None:
        page = Path("dashboard/pages/3_分组回测.py").read_text("utf-8")
        self.assertIn('market_id == "us_equity"', page)
        self.assertIn("load_real_backtest_catalog", page)
        self.assertIn("factor_options", page)
        self.assertIn("开发期真实数据 Smoke", page)


if __name__ == "__main__":
    unittest.main()
