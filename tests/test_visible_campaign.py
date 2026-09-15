"""开发期 Smoke 候选的 Dashboard 转换测试。"""

from pathlib import Path
import json
import tempfile
import unittest

from dashboard.visible_campaign import load_visible_campaign_rows


class VisibleCampaignTest(unittest.TestCase):
    """只投影实际存在的 IC/HAC，不伪造组合回测。"""

    def test_smoke_candidate_keeps_portfolio_metrics_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run"
            candidate_id = "cand_test"
            candidate_root = run_root / "candidates" / candidate_id
            state_root = root / "state"
            candidate_root.mkdir(parents=True)
            state_root.mkdir()
            (run_root / "run_manifest.json").write_text(
                json.dumps({"run_id": "run_123456789012345678901234", "statuses": {candidate_id: "visible_passed"}}),
                encoding="utf-8",
            )
            (state_root / f"{candidate_id}.json").write_text(
                json.dumps({
                    "candidate_id": candidate_id,
                    "spec": {
                        "hypothesis": {"claim": "低波动股票后续收益更稳健", "mechanism": "风险偏好约束", "expected_sign": "positive"},
                        "expression": {"op": "neg", "args": [{"field": "close"}]},
                    },
                }),
                encoding="utf-8",
            )
            (candidate_root / "evaluation.json").write_text(
                json.dumps({"mean_rank_ic": 0.02, "std_rank_ic": 0.1, "icir": 0.2, "valid_dates": 100, "median_coverage": 0.9}),
                encoding="utf-8",
            )
            (candidate_root / "inference.json").write_text(
                json.dumps({"t_value": 2.1, "raw_p_value": 0.03, "bonferroni_p_value": 0.09}),
                encoding="utf-8",
            )
            (candidate_root / "candidate_package.json").write_text(
                json.dumps({"candidate_id": candidate_id, "status": "visible_passed"}),
                encoding="utf-8",
            )
            preregistration = root / "pre.json"
            preregistration.write_text(
                json.dumps({"candidates": [{"candidate_id": candidate_id, "name": "us_low_volatility"}]}),
                encoding="utf-8",
            )

            run_id, rows = load_visible_campaign_rows(
                run_root=run_root,
                candidate_state_root=state_root,
                pre_registration_path=preregistration,
                horizon_days=5,
            )

        self.assertEqual(run_id, "run_123456789012345678901234")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank_ic_mean"], 0.02)
        self.assertIn("未执行组合回测", rows[0]["evaluation_scope"])
        self.assertNotIn("annualized_return", rows[0])
        self.assertNotIn("sharpe", rows[0])


if __name__ == "__main__":
    unittest.main()
