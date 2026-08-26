from datetime import date, timedelta
import unittest

import numpy as np
import polars as pl

from factor_miner.regime_features import FEATURE_COLUMNS
from factor_miner.regime_research import (
    RegimeCandidateSummary,
    enumerate_regime_candidates,
    recommend_candidates,
    run_regime_research,
    summarize_daily_diagnostics_by_regime,
    walk_forward_months,
)
from factor_miner.regime_schema import (
    CovarianceKind,
    RegimeResearchSpec,
    ReturnAggregation,
    TrainingWindow,
)
from tests.test_regime_schema import valid_research_spec


def sequential_dates(count: int) -> tuple[date, ...]:
    """把连续日期当作程序生成的合成交易日。"""

    start = date(2015, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def reduced_research_spec(end: date) -> RegimeResearchSpec:
    """返回只运行一个配置的快速合成研究合同。"""

    payload = valid_research_spec().model_dump(mode="json")
    payload.update(
        {
            "candidate_state_counts": [2],
            "return_aggregations": ["median"],
            "training_windows": ["expanding"],
            "covariance_kinds": ["diag"],
            "seeds": [11],
            "n_iter": 100,
            "min_oos_months": 1,
            "min_successful_month_ratio": 0.5,
            "research_start": "2015-01-01",
            "research_end": end.isoformat(),
        }
    )
    return RegimeResearchSpec.model_validate(payload)


def synthetic_feature_frame(count: int = 1320) -> pl.DataFrame:
    """生成具有可识别持续状态的四维市场特征。"""

    rng = np.random.default_rng(20260729)
    dates = sequential_dates(count)
    values: list[np.ndarray] = []
    for index in range(count):
        state = (index // 80) % 2
        center = (
            np.array([-0.02, 0.035, 0.25, 0.30])
            if state == 0
            else np.array([0.02, 0.012, -0.10, 0.70])
        )
        values.append(center + rng.normal(0.0, [0.003, 0.002, 0.02, 0.02]))
    matrix = np.vstack(values)
    return pl.DataFrame(
        {
            "date": dates,
            **{
                column: matrix[:, index]
                for index, column in enumerate(FEATURE_COLUMNS)
            },
            "feature_valid": [True] * count,
        }
    )


class RegimeResearchTest(unittest.TestCase):
    """走步式切分、候选选择和条件诊断测试。"""

    def test_monthly_folds_never_read_inference_month(self) -> None:
        """训练截止日必须严格早于推断月且窗口长度不能漂移。"""

        dates = sequential_dates(2300)
        spec = valid_research_spec().model_copy(
            update={
                "research_start": dates[0],
                "research_end": dates[-1],
            }
        )
        expanding = walk_forward_months(dates, spec, TrainingWindow.EXPANDING)
        rolling_5y = walk_forward_months(dates, spec, TrainingWindow.ROLLING_5Y)
        rolling_8y = walk_forward_months(dates, spec, TrainingWindow.ROLLING_8Y)
        self.assertTrue(expanding)
        self.assertTrue(rolling_5y)
        self.assertTrue(rolling_8y)
        for folds in (expanding, rolling_5y, rolling_8y):
            for fold in folds:
                self.assertLess(fold.training_dates[-1], fold.inference_dates[0])
                self.assertTrue(
                    all(
                        (value.year, value.month)
                        == (
                            fold.inference_dates[0].year,
                            fold.inference_dates[0].month,
                        )
                        for value in fold.inference_dates
                    )
                )
        self.assertEqual(len(rolling_5y[-1].training_dates), 1260)
        self.assertEqual(len(rolling_8y[-1].training_dates), 2016)
        self.assertGreater(
            len(expanding[-1].training_dates),
            len(rolling_8y[-1].training_dates),
        )

    def test_full_spec_enumerates_36_fixed_configurations(self) -> None:
        """每个候选内部的 K、收益、窗口和 covariance 必须固定。"""

        candidates = enumerate_regime_candidates(valid_research_spec())
        self.assertEqual(len(candidates), 36)
        self.assertEqual(len({item.candidate_id for item in candidates}), 36)
        self.assertTrue(all(isinstance(item.state_count, int) for item in candidates))

    def test_candidate_ranking_is_lexicographic_with_separate_return_tracks(self) -> None:
        """不得用不同收益定义的 likelihood 选出一个跨轨道冠军。"""

        summaries = (
            RegimeCandidateSummary(
                candidate_id="regcand_" + "1" * 24,
                return_aggregation=ReturnAggregation.MEDIAN,
                successful_months=24,
                total_months=24,
                successful_month_ratio=1.0,
                median_oos_log_likelihood=-4.0,
                median_mapping_cost=0.2,
                median_min_bhattacharyya=0.5,
                median_bic=100.0,
                eligible=True,
            ),
            RegimeCandidateSummary(
                candidate_id="regcand_" + "2" * 24,
                return_aggregation=ReturnAggregation.MEDIAN,
                successful_months=24,
                total_months=24,
                successful_month_ratio=1.0,
                median_oos_log_likelihood=-3.0,
                median_mapping_cost=0.9,
                median_min_bhattacharyya=0.1,
                median_bic=200.0,
                eligible=True,
            ),
            RegimeCandidateSummary(
                candidate_id="regcand_" + "3" * 24,
                return_aggregation=ReturnAggregation.EQUAL_WEIGHT,
                successful_months=24,
                total_months=24,
                successful_month_ratio=1.0,
                median_oos_log_likelihood=20.0,
                median_mapping_cost=0.1,
                median_min_bhattacharyya=1.0,
                median_bic=50.0,
                eligible=True,
            ),
            RegimeCandidateSummary(
                candidate_id="regcand_" + "4" * 24,
                return_aggregation=ReturnAggregation.MEDIAN,
                successful_months=23,
                total_months=24,
                successful_month_ratio=23 / 24,
                median_oos_log_likelihood=100.0,
                median_mapping_cost=0.0,
                median_min_bhattacharyya=10.0,
                median_bic=1.0,
                eligible=False,
            ),
        )
        recommendations = recommend_candidates(summaries)
        self.assertEqual(
            recommendations[ReturnAggregation.MEDIAN],
            "regcand_" + "2" * 24,
        )
        self.assertEqual(
            recommendations[ReturnAggregation.EQUAL_WEIGHT],
            "regcand_" + "3" * 24,
        )

    def test_probability_weighted_diagnostics_obey_availability(self) -> None:
        """收盘信号用同日状态，盘中已实现指标只能用上一可用状态。"""

        filtered = pl.DataFrame(
            {
                "observation_date": [
                    date(2026, 1, 5),
                    date(2026, 1, 6),
                ],
                "earliest_use_date": [
                    date(2026, 1, 6),
                    date(2026, 1, 7),
                ],
                "canonical_state_probabilities": [[0.8, 0.2], [0.3, 0.7]],
            }
        )
        diagnostics = pl.DataFrame(
            {
                "date": [date(2026, 1, 5), date(2026, 1, 6)],
                "value": [1.0, 3.0],
            }
        )
        close_signal = summarize_daily_diagnostics_by_regime(
            filtered,
            diagnostics,
            availability="close_signal_t",
        )
        realized = summarize_daily_diagnostics_by_regime(
            filtered,
            diagnostics,
            availability="realized_during_t",
        )
        self.assertAlmostEqual(close_signal.states[0].weighted_mean, 1.5454545454545454)
        self.assertAlmostEqual(close_signal.states[1].weighted_mean, 2.5555555555555554)
        self.assertAlmostEqual(realized.states[0].weighted_mean, 3.0)
        self.assertAlmostEqual(realized.states[1].weighted_mean, 3.0)
        self.assertEqual(realized.valid_dates, 1)

    def test_reduced_walk_forward_research_returns_a_track_recommendation(self) -> None:
        """最小真实拟合研究必须完成月度过滤、质量和轨道内推荐。"""

        features = synthetic_feature_frame()
        spec = reduced_research_spec(features.get_column("date")[-1])
        report = run_regime_research(
            {ReturnAggregation.MEDIAN: features},
            spec,
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertGreaterEqual(report.candidates[0].successful_months, 1)
        self.assertEqual(
            report.recommendations[ReturnAggregation.MEDIAN],
            report.candidates[0].candidate_id,
        )
        for monthly in report.monthly_results:
            if monthly.status == "ok":
                self.assertLess(monthly.training_end, monthly.inference_start)
                np.testing.assert_allclose(
                    monthly.canonical_probabilities.sum(axis=1),
                    1.0,
                    rtol=0,
                    atol=1e-8,
                )

    def test_unavailable_candidate_uses_json_safe_empty_metrics(self) -> None:
        """训练历史不足不能用 Infinity 冒充可序列化研究指标。"""

        features = synthetic_feature_frame(count=100)
        spec = reduced_research_spec(features.get_column("date")[-1])
        report = run_regime_research(
            {ReturnAggregation.MEDIAN: features},
            spec,
        )
        summary = report.candidates[0]
        self.assertFalse(summary.eligible)
        self.assertIsNone(summary.median_oos_log_likelihood)
        self.assertIsNone(summary.median_mapping_cost)
        self.assertIsNone(summary.median_min_bhattacharyya)
        self.assertIsNone(summary.median_bic)
        self.assertIsNone(report.recommendations[ReturnAggregation.MEDIAN])


if __name__ == "__main__":
    unittest.main()
