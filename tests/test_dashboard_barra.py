"""Dashboard Barra 嵌套发布结构适配测试。"""

import unittest


class DashboardBarraTest(unittest.TestCase):
    def test_published_wrapper_exposes_full_risk_tables(self) -> None:
        """页面必须读取 attribution 子对象，不能把 available 当成风险状态。"""

        from dashboard.barra_view import normalize_barra_view

        payload = normalize_barra_view(
            {
                "status": "available",
                "missing_inputs": [],
                "attribution": {
                    "risk_decomposition_status": "complete",
                    "exposure_summary": [{"portfolio": "Q10", "Size": 0.2}],
                    "attribution": [{"factor": "Size", "contribution": 0.01}],
                    "risk_decomposition": [
                        {
                            "portfolio": "Q10",
                            "factor_variance": 0.02,
                            "specific_variance": 0.01,
                            "total_variance": 0.03,
                            "volatility": 0.1732,
                        }
                    ],
                },
            }
        )

        self.assertEqual(payload["status"], "complete")
        self.assertEqual(len(payload["exposure_summary"]), 1)
        self.assertEqual(len(payload["attribution"]), 1)
        self.assertEqual(len(payload["risk_decomposition"]), 1)

    def test_not_available_preserves_missing_inputs(self) -> None:
        from dashboard.barra_view import normalize_barra_view

        payload = normalize_barra_view(
            {"status": "not_available", "missing_inputs": ["covariance"]}
        )

        self.assertEqual(payload["status"], "not_available")
        self.assertEqual(payload["missing_inputs"], ["covariance"])

    def test_page_limits_full_history_to_latest_signal_date(self) -> None:
        """来源 Pilot 的完整历史不得整体发送给浏览器表格。"""

        from dashboard.barra_view import normalize_barra_view

        rows = [
            {"signal_date": "2026-01-01", "portfolio": "Q1", "Size": 0.1},
            {"signal_date": "2026-01-08", "portfolio": "Q1", "Size": 0.2},
        ]
        payload = normalize_barra_view({
            "status": "available",
            "attribution": {
                "risk_decomposition_status": "complete",
                "exposure_summary": rows,
                "attribution": rows,
                "risk_decomposition": rows,
            },
        })

        self.assertEqual(payload["exposure_summary"], [rows[1]])
        self.assertEqual(payload["attribution"], [rows[1]])
        self.assertEqual(payload["risk_decomposition"], [rows[1]])


if __name__ == "__main__":
    unittest.main()
