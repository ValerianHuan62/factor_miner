from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.incremental import (
    DailyOrthogonalization,
    OrthogonalizationSummary,
    OrthogonalizedFactor,
    evaluate_incremental_information,
    orthogonalize_candidate,
)
from factor_miner.schema import ExpectedSign
from tests.helpers import valid_campaign, valid_incremental_policy


REFERENCE_PERMUTATIONS: tuple[tuple[int, ...], ...] = (
    (1, 4, 2, 7, 3, 5, 6),
    (5, 2, 6, 1, 7, 4, 3),
    (5, 4, 7, 2, 1, 3, 6),
    (3, 4, 7, 6, 1, 2, 5),
)


def orthogonalization_fixture(
    *,
    days: int = 3,
) -> tuple[pl.DataFrame, dict[str, pl.DataFrame], pl.DataFrame]:
    """构造逐对相关不高但联合空间精确包含候选的截面。"""

    candidate_rows: list[dict[str, object]] = []
    eligibility_rows: list[dict[str, object]] = []
    reference_rows: list[list[dict[str, object]]] = [
        [] for _ in REFERENCE_PERMUTATIONS
    ]
    for day_index in range(days):
        current = date(2020, 1, 1) + timedelta(days=day_index)
        for asset_index in range(7):
            asset = f"A{asset_index + 1:02d}"
            candidate_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "raw_factor": float(asset_index + 1),
                }
            )
            eligibility_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "valid_for_factor_rank": True,
                }
            )
            for reference_index, permutation in enumerate(REFERENCE_PERMUTATIONS):
                reference_rows[reference_index].append(
                    {
                        "date": current,
                        "asset": asset,
                        "raw_factor": float(permutation[asset_index]),
                    }
                )
    references = {
        f"reference_{index + 1}": pl.DataFrame(rows)
        for index, rows in enumerate(reference_rows)
    }
    return (
        pl.DataFrame(candidate_rows),
        references,
        pl.DataFrame(eligibility_rows),
    )


def orthogonalization_policy(**updates):
    """返回适用于七只股票小截面的冻结正交化政策。"""

    base = valid_incremental_policy().orthogonalization
    return base.model_copy(
        update={
            "min_reference_coverage": 1.0,
            "min_cross_sectional_excess_names": 2,
            "max_condition_number": 1_000.0,
            **updates,
        }
    )


def incremental_evaluation_fixture(
    *,
    days: int = 70,
) -> tuple[OrthogonalizedFactor, pl.DataFrame]:
    """构造具有稳定但非完全残差预测关系的日期截面。"""

    residual_rows: list[dict[str, object]] = []
    outcome_rows: list[dict[str, object]] = []
    daily: list[DailyOrthogonalization] = []
    for day_index in range(days):
        current = date(2020, 1, 1) + timedelta(days=day_index)
        for asset_index in range(31):
            asset = f"A{asset_index + 1:03d}"
            residual = float((asset_index * 7 + day_index * 3) % 31)
            noise = float(((asset_index * 11 + day_index * 5) % 7) - 3)
            residual_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "orthogonal_residual": residual,
                }
            )
            outcome_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "valid_for_factor_rank": True,
                    "label_o2o_5d": residual + noise * 0.35,
                }
            )
        daily.append(
            DailyOrthogonalization(
                date=current,
                total_eligible_count=31,
                complete_case_count=31,
                reference_count=3,
                reference_coverage=1.0,
                design_rank=4,
                condition_number=2.0,
                r_squared=0.6,
                residual_variance_ratio=0.4,
            )
        )
    summary = OrthogonalizationSummary(
        basis_ids=("reference_value", "reference_quality", "reference_momentum"),
        daily=tuple(daily),
        valid_dates=days,
        median_reference_coverage=1.0,
        median_r_squared=0.6,
        median_residual_variance_ratio=0.4,
    )
    return (
        OrthogonalizedFactor(
            frame=pl.DataFrame(residual_rows),
            summary=summary,
        ),
        pl.DataFrame(outcome_rows),
    )


class IncrementalInformationTest(unittest.TestCase):
    """验证 V0.2 联合正交化和增量信息评价。"""

    def test_joint_linear_redundancy_has_zero_residual(self) -> None:
        """多个低相关参考联合解释候选时残差必须接近零。"""

        candidate, references, eligibility = orthogonalization_fixture()
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 1, 3),
                "min_names_per_date": 2,
            }
        )
        result = orthogonalize_candidate(
            candidate,
            references,
            eligibility,
            campaign,
            orthogonalization_policy(),
        )
        maximum_residual = result.frame.select(
            pl.col("orthogonal_residual").abs().max()
        ).item()
        self.assertLess(maximum_residual, 1e-10)
        self.assertAlmostEqual(result.summary.median_r_squared, 1.0, places=12)
        self.assertLess(
            result.summary.median_residual_variance_ratio,
            1e-12,
        )
        self.assertEqual(
            result.summary.basis_ids,
            tuple(references),
        )

    def test_reference_complete_case_coverage_is_enforced(self) -> None:
        """参考缺失造成完整交集覆盖率不足时必须硬失败。"""

        candidate, references, eligibility = orthogonalization_fixture(days=1)
        first_name = next(iter(references))
        references[first_name] = references[first_name].with_columns(
            pl.when(pl.col("asset") == "A01")
            .then(None)
            .otherwise(pl.col("raw_factor"))
            .alias("raw_factor")
        )
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 1, 1),
                "min_names_per_date": 2,
            }
        )
        with self.assertRaises(FactorMinerError) as context:
            orthogonalize_candidate(
                candidate,
                references,
                eligibility,
                campaign,
                orthogonalization_policy(min_reference_coverage=0.9),
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.ORTHOGONALIZATION_COVERAGE_LOW,
        )

    def test_rank_deficient_reference_basis_is_rejected(self) -> None:
        """重复参考列导致设计矩阵秩亏时不得使用伪逆静默继续。"""

        candidate, references, eligibility = orthogonalization_fixture(days=1)
        names = tuple(references)
        references[names[1]] = references[names[0]]
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 1, 1),
                "min_names_per_date": 2,
            }
        )
        with self.assertRaises(FactorMinerError) as context:
            orthogonalize_candidate(
                candidate,
                references,
                eligibility,
                campaign,
                orthogonalization_policy(),
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.ORTHOGONALIZATION_RANK_DEFICIENT,
        )

    def test_condition_number_above_frozen_limit_is_rejected(self) -> None:
        """满秩但条件数超过冻结阈值时必须报告数值不稳定。"""

        candidate, references, eligibility = orthogonalization_fixture(days=1)
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 1, 1),
                "min_names_per_date": 2,
            }
        )
        with self.assertRaises(FactorMinerError) as context:
            orthogonalize_candidate(
                candidate,
                references,
                eligibility,
                campaign,
                orthogonalization_policy(max_condition_number=2.0),
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.ORTHOGONALIZATION_NUMERIC_UNSTABLE,
        )

    def test_orthogonalization_is_deterministic_and_has_no_outcome_input(self) -> None:
        """相同因子输入重复运行必须得到相同残差且接口不接收标签。"""

        candidate, references, eligibility = orthogonalization_fixture()
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 1, 3),
                "min_names_per_date": 2,
            }
        )
        first = orthogonalize_candidate(
            candidate,
            references,
            eligibility,
            campaign,
            orthogonalization_policy(),
        )
        second = orthogonalize_candidate(
            candidate,
            references,
            eligibility,
            campaign,
            orthogonalization_policy(),
        )
        self.assertTrue(first.frame.equals(second.frame))
        self.assertEqual(first.summary, second.summary)
        self.assertNotIn("label_o2o_5d", first.frame.columns)

    def test_independent_component_passes_residual_inference(self) -> None:
        """独立残差与未来收益稳定同向时必须通过完整增量闸门。"""

        orthogonalized, outcomes = incremental_evaluation_fixture()
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 3, 10),
                "max_hypotheses": 1,
                "min_valid_dates": 60,
                "min_names_per_date": 20,
                "hac_max_lags": 5,
            }
        )
        result = evaluate_incremental_information(
            orthogonalized,
            outcomes,
            campaign,
            ExpectedSign.POSITIVE,
            orthogonalization_policy(
                min_median_residual_variance_ratio=0.05,
                min_abs_mean_residual_rank_ic=0.01,
            ),
        )
        self.assertGreater(result.residual_evaluation.mean_rank_ic, 0.9)
        self.assertLessEqual(
            result.residual_inference.bonferroni_p_value,
            campaign.alpha,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.failure_reasons, ())

    def test_wrong_residual_direction_returns_failed_result(self) -> None:
        """残差 IC 与事前方向相反时保留统计结果但不得通过。"""

        orthogonalized, outcomes = incremental_evaluation_fixture()
        outcomes = outcomes.with_columns(
            (-pl.col("label_o2o_5d")).alias("label_o2o_5d")
        )
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 3, 10),
                "max_hypotheses": 1,
                "min_valid_dates": 60,
                "min_names_per_date": 20,
            }
        )
        result = evaluate_incremental_information(
            orthogonalized,
            outcomes,
            campaign,
            ExpectedSign.POSITIVE,
            orthogonalization_policy(),
        )
        self.assertFalse(result.direction_passed)
        self.assertFalse(result.passed)
        self.assertIn("残差 RankIC 方向与事前假设不一致", result.failure_reasons)

    def test_low_independent_variance_returns_failed_result(self) -> None:
        """独立方差低于冻结门槛时即使残差 IC 强也不得通过。"""

        orthogonalized, outcomes = incremental_evaluation_fixture()
        low_variance = OrthogonalizedFactor(
            frame=orthogonalized.frame,
            summary=orthogonalized.summary.model_copy(
                update={"median_residual_variance_ratio": 0.001}
            ),
        )
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 3, 10),
                "max_hypotheses": 1,
                "min_valid_dates": 60,
                "min_names_per_date": 20,
            }
        )
        result = evaluate_incremental_information(
            low_variance,
            outcomes,
            campaign,
            ExpectedSign.POSITIVE,
            orthogonalization_policy(
                min_median_residual_variance_ratio=0.05,
            ),
        )
        self.assertFalse(result.variance_passed)
        self.assertFalse(result.passed)
        self.assertIn("残差独立方差低于冻结门槛", result.failure_reasons)

    def test_residual_effect_size_threshold_is_enforced(self) -> None:
        """残差平均 RankIC 未达到冻结效果量时不得通过。"""

        orthogonalized, outcomes = incremental_evaluation_fixture()
        campaign = valid_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 1),
                "visible_end": date(2020, 3, 10),
                "max_hypotheses": 1,
                "min_valid_dates": 60,
                "min_names_per_date": 20,
            }
        )
        result = evaluate_incremental_information(
            orthogonalized,
            outcomes,
            campaign,
            ExpectedSign.POSITIVE,
            orthogonalization_policy(
                min_abs_mean_residual_rank_ic=0.999,
            ),
        )
        self.assertFalse(result.effect_size_passed)
        self.assertFalse(result.passed)
        self.assertIn("残差 RankIC 效果量低于冻结门槛", result.failure_reasons)


if __name__ == "__main__":
    unittest.main()
