"""因子探索页的筛选与导出合同测试。"""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
import unittest

from dashboard.factor_explorer import (
    factor_rows_csv,
    filter_factor_rows,
    sort_factor_rows,
    text_values,
)


ROWS = [
    {
        "factor_id": "huan001",
        "hypothesis": "成交量冲击后存在反转",
        "mechanism": "流动性补偿",
        "formula": "neg(ts_mean(ret, 5))",
        "calculation": "五日收益取反",
        "discovered_direction": "正向",
        "direction_relation": "与假设一致",
        "status": "通过当前协议验证的候选因子",
        "valid_dates": 300,
        "rank_ic_hac_t": 2.4,
        "rank_ic_mean": 0.02,
        "annualized_return": 0.08,
        "max_drawdown": 0.05,
        "sharpe": 1.1,
    },
    {
        "factor_id": "huan002",
        "hypothesis": "短期趋势延续",
        "mechanism": "信息扩散",
        "formula": "ts_mean(ret, 20)",
        "calculation": "二十日收益均值",
        "discovered_direction": "负向",
        "direction_relation": "与假设相反",
        "status": "未通过确认",
        "valid_dates": 120,
        "rank_ic_hac_t": -1.2,
        "rank_ic_mean": -0.01,
        "annualized_return": -0.03,
        "max_drawdown": 0.12,
        "sharpe": -0.4,
    },
]


class FactorExplorerTest(unittest.TestCase):
    """交互只能筛选既有结果，不能改变研究指标。"""

    def test_search_and_numeric_filters_preserve_source_values(self) -> None:
        selected = filter_factor_rows(
            ROWS,
            query="流动性",
            directions=["正向"],
            minimum_valid_dates=250,
            minimum_abs_hac_t=2.0,
        )

        self.assertEqual(selected, [ROWS[0]])
        self.assertEqual(selected[0]["rank_ic_mean"], 0.02)

    def test_filter_uses_absolute_hac_threshold(self) -> None:
        selected = filter_factor_rows(ROWS, minimum_abs_hac_t=1.5)

        self.assertEqual([row["factor_id"] for row in selected], ["huan001"])

    def test_sort_places_missing_values_last(self) -> None:
        rows = [*ROWS, {**ROWS[0], "factor_id": "huan003", "sharpe": None}]

        sorted_rows = sort_factor_rows(rows, field="sharpe", descending=True)

        self.assertEqual([row["factor_id"] for row in sorted_rows], ["huan001", "huan002", "huan003"])

    def test_text_values_and_csv_export_are_stable(self) -> None:
        self.assertEqual(text_values(ROWS, "discovered_direction"), ["正向", "负向"])
        payload = factor_rows_csv(ROWS).decode("utf-8-sig")
        exported = list(csv.DictReader(StringIO(payload)))
        self.assertEqual([row["factor_id"] for row in exported], ["huan001", "huan002"])

    def test_results_navigation_offers_shared_favor_workflow(self) -> None:
        app = Path("dashboard/app.py").read_text("utf-8")
        self.assertIn('st.Page(render_results, title="看结果"', app)
        self.assertIn('"FaVOR 联合策略"', Path("dashboard/results.py").read_text("utf-8"))

    def test_side_by_side_review_uses_requested_metric_order(self) -> None:
        """并排卡片依次展示 IC、RankIC、t、年化和 Sharpe。"""

        page = Path("dashboard/pages/8_因子探索.py").read_text("utf-8")
        labels = (
            'st.metric("IC 均值"',
            'st.metric("RankIC 均值"',
            'st.metric("RankIC HAC t"',
            'st.metric("年化收益"',
            'st.metric("Sharpe"',
        )
        positions = [page.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('number(row["ic_mean"], 4)', page)
        self.assertIn('number(row["rank_ic_mean"], 4)', page)
        self.assertNotIn('pct(row["ic_mean"])', page)



if __name__ == "__main__":
    unittest.main()
