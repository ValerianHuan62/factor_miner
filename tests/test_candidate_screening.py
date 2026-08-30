"""固定候选筛选与稳健性报告的合成测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from factor_miner.candidate_screening import (
    CandidateScreeningInput,
    CandidateScreeningPolicy,
    PortfolioStressMetrics,
    UnavailableCandidateRecord,
    screen_candidates,
    stressed_portfolio_metrics,
)
from factor_miner.candidate_screening_artifacts import publish_screening_report


def _candidate(
    candidate_id: str,
    *,
    direction: str = "positive",
    confirmation_passed: bool = True,
    rank_ic_mean: float = 0.03,
    rank_ic_hac_t: float = 4.0,
    information_ratio: float = 0.8,
    sharpe: float = 1.0,
    recent_rank_ic_mean: float = 0.02,
    recent_information_ratio: float = 0.5,
    active_returns: tuple[float, ...] = (0.01, -0.01, 0.02, 0.0),
) -> CandidateScreeningInput:
    sign = 1.0 if direction == "positive" else -1.0
    dates = ("2024-01-09", "2024-01-16", "2024-01-23", "2024-01-30")
    benchmark = (0.001, -0.002, 0.003, 0.0)
    rows = tuple(
        {
            "entry_date": item,
            "exit_date": item,
            "target_long_gross_return": active + base + 0.0014,
            "target_long_turnover": 1.0,
            "target_long_cost": 0.0014,
            "target_long_net_return": active + base,
            "benchmark_return": base,
        }
        for item, active, base in zip(dates, active_returns, benchmark, strict=True)
    )
    return CandidateScreeningInput(
        candidate_id=candidate_id,
        business_id=candidate_id,
        source_run_id="run_" + "1" * 24,
        source_manifest_sha256="a" * 64,
        spec_sha256="b" * 64,
        formula_sha256=(candidate_id.encode().hex() + "0" * 64)[:64],
        hypothesis="合成候选假设",
        selected_direction=direction,
        hypothesis_relation="supported",
        confirmation_passed=confirmation_passed,
        confirmation_rank_ic_mean=sign * rank_ic_mean,
        confirmation_rank_ic_hac_t=sign * rank_ic_hac_t,
        confirmation_ic_mean=sign * rank_ic_mean,
        confirmation_ic_hac_t=sign * rank_ic_hac_t,
        confirmation_information_ratio=information_ratio,
        confirmation_sharpe=sharpe,
        confirmation_max_drawdown=0.15,
        confirmation_annualized_return=0.12,
        annual_rank_ic={"2024": sign * 0.03, "2025": sign * 0.02, "2026": sign * 0.01},
        horizon_rank_ic={1: sign * 0.01, 3: sign * 0.02, 5: sign * 0.03, 10: sign * 0.02, 20: sign * 0.01},
        recent_rank_ic_mean=sign * recent_rank_ic_mean,
        recent_information_ratio=recent_information_ratio,
        recent_sharpe=0.7,
        mean_turnover=1.0,
        portfolio_rows=rows,
        barra_status="not_available",
    )


class CandidateScreeningTest(unittest.TestCase):
    """筛选必须方向感知、成本感知并保持相关簇去重。"""

    def policy(self, *, target_min: int = 2, target_max: int = 3) -> CandidateScreeningPolicy:
        return CandidateScreeningPolicy(
            cutoff_at=datetime(2026, 8, 26, 6, 8, 40, tzinfo=timezone.utc),
            target_min=target_min,
            target_max=target_max,
            minimum_correlation_observations=3,
        )

    def test_policy_rejects_invalid_target_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "目标数量"):
            CandidateScreeningPolicy(
                cutoff_at=datetime.now(timezone.utc),
                target_min=30,
                target_max=20,
            )

    def test_negative_factor_uses_oriented_metrics(self) -> None:
        negative = _candidate("huan001", direction="negative")
        report = screen_candidates((negative,), self.policy(target_min=1, target_max=1))
        selected = report.selected[0]
        self.assertAlmostEqual(selected.oriented_rank_ic_mean, 0.03)
        self.assertAlmostEqual(selected.oriented_rank_ic_hac_t, 4.0)
        self.assertEqual(selected.status, "核心候选")

    def test_stress_cost_recalculates_net_performance(self) -> None:
        baseline = stressed_portfolio_metrics(
            _candidate("huan001").portfolio_rows,
            cost_bps=14,
            annualization_factor=52.0,
        )
        metrics = stressed_portfolio_metrics(
            _candidate("huan001").portfolio_rows,
            cost_bps=42,
            annualization_factor=52.0,
        )
        self.assertIsInstance(metrics, PortfolioStressMetrics)
        self.assertLess(metrics.sharpe, baseline.sharpe)
        self.assertLess(metrics.information_ratio, baseline.information_ratio)

    def test_hard_gate_rejects_failed_confirmation_and_recent_reversal(self) -> None:
        failed = _candidate("huan001", confirmation_passed=False)
        reversed_recent = _candidate("huan002", recent_rank_ic_mean=-0.01)
        report = screen_candidates(
            (failed, reversed_recent),
            self.policy(target_min=1, target_max=2),
        )
        self.assertFalse(report.selected)
        reasons = {item.business_id: item.failure_reasons for item in report.candidates}
        self.assertIn("确认期统计闸门未通过", reasons["huan001"])
        self.assertIn("最近期方向校正 RankIC 不为正", reasons["huan002"])

    def test_return_correlation_keeps_one_core_representative_per_cluster(self) -> None:
        first = _candidate("huan001", rank_ic_hac_t=5.0)
        duplicate = _candidate("huan002", rank_ic_hac_t=4.0)
        distinct = _candidate(
            "huan003",
            rank_ic_hac_t=3.0,
            active_returns=(0.03, 0.01, 0.02, 0.04),
        )
        report = screen_candidates(
            (first, duplicate, distinct),
            self.policy(target_min=2, target_max=3),
        )
        statuses = {item.business_id: item.status for item in report.candidates}
        self.assertEqual(statuses["huan001"], "核心候选")
        self.assertEqual(statuses["huan002"], "相关簇备选")
        self.assertEqual(statuses["huan003"], "核心候选")
        self.assertEqual(len(report.selected), 2)

    def test_v2_uses_frozen_extreme_spread_series_for_deduplication(self) -> None:
        first = _candidate("huan001").model_copy(
            update={
                "governance_spread_returns": {
                    "2024-01-09": 0.03,
                    "2024-01-16": -0.01,
                    "2024-01-23": 0.02,
                    "2024-01-30": 0.00,
                }
            }
        )
        second = _candidate("huan002").model_copy(
            update={
                "governance_spread_returns": {
                    "2024-01-09": 0.01,
                    "2024-01-16": 0.02,
                    "2024-01-23": -0.01,
                    "2024-01-30": 0.03,
                }
            }
        )
        policy = CandidateScreeningPolicy(
            version="candidate-screening-v2",
            correlation_series="extreme_spread_net_return",
            cutoff_at=datetime(2026, 8, 26, 6, 8, 40, tzinfo=timezone.utc),
            target_min=2,
            target_max=2,
            minimum_correlation_observations=3,
        )
        report = screen_candidates((first, second), policy)
        self.assertEqual([item.status for item in report.selected], ["核心候选", "核心候选"])
        self.assertEqual(len({item.correlation_cluster for item in report.selected}), 2)

    def test_publication_writes_immutable_chinese_report_and_manifest(self) -> None:
        report = screen_candidates(
            (_candidate("huan001"),),
            self.policy(target_min=1, target_max=1),
        )
        with tempfile.TemporaryDirectory() as directory:
            first = publish_screening_report(report, output_root=Path(directory))
            second = publish_screening_report(report, output_root=Path(directory))
            self.assertEqual(first, second)
            names = {path.name for path in first.report_root.iterdir()}
            self.assertEqual(
                names,
                {
                    "policy.json", "report.json", "manifest.json",
                    "候选筛选与稳健性报告.md", "候选明细.csv", "输入不可审计候选.csv",
                },
            )
            markdown = (first.report_root / "候选筛选与稳健性报告.md").read_text("utf-8")
            self.assertIn("# 候选筛选与稳健性报告", markdown)
            self.assertNotIn("认证 Alpha", markdown.split("研究边界", 1)[0])

    def test_unavailable_candidate_remains_in_frozen_universe(self) -> None:
        unavailable = UnavailableCandidateRecord(
            candidate_id="cand_missing",
            business_id="huan999",
            failure_reason="缺少完整不可变发布产物",
        )
        report = screen_candidates(
            (_candidate("huan001"),),
            self.policy(target_min=1, target_max=1),
            unavailable_candidates=(unavailable,),
        )
        self.assertEqual(report.candidate_count, 2)
        self.assertEqual(report.auditable_candidate_count, 1)
        self.assertEqual(report.unavailable_candidate_count, 1)
        self.assertFalse(report.input_complete)


if __name__ == "__main__":
    unittest.main()
