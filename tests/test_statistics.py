"""HAC 与 Bonferroni 统计测试。"""

import math
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.statistics import bonferroni_adjust, hac_mean_test
from tests.helpers import valid_campaign


class StatisticsTest(unittest.TestCase):
    """验证冻结的 HAC 参数和 hypothesis family。"""

    def test_bonferroni_matches_frozen_family_size(self) -> None:
        """Bonferroni 必须使用显式 family size。"""
        self.assertEqual(bonferroni_adjust(0.001, 100), 0.1)
        self.assertEqual(bonferroni_adjust(0.02, 100), 1.0)

    def test_hac_reports_two_sided_inference_and_adjusted_p(self) -> None:
        """HAC 结果应报告全部字段和按 campaign budget 调整的 p 值。"""
        campaign = valid_campaign().model_copy(
            update={"max_hypotheses": 5, "min_valid_dates": 8, "hac_max_lags": 2}
        )
        result = hac_mean_test(
            (0.20, 0.21, 0.19, 0.18, 0.22, 0.20, 0.21, 0.19),
            campaign,
        )
        self.assertEqual(result.nobs, 8)
        self.assertEqual(result.maxlags, 2)
        self.assertGreater(result.mean, 0.0)
        self.assertGreater(result.standard_error, 0.0)
        self.assertTrue(math.isfinite(result.t_value))
        self.assertGreaterEqual(result.raw_p_value, 0.0)
        self.assertLessEqual(result.raw_p_value, 1.0)
        self.assertEqual(
            result.bonferroni_p_value,
            bonferroni_adjust(result.raw_p_value, campaign.max_hypotheses),
        )
        self.assertLess(result.conf_low, result.conf_high)

    def test_insufficient_valid_dates_blocks_inference(self) -> None:
        """有效日期不足时不得运行统计推断。"""
        campaign = valid_campaign().model_copy(update={"min_valid_dates": 5})
        with self.assertRaisesRegex(FactorMinerError, "INSUFFICIENT_VALID_DATES"):
            hac_mean_test((0.1, 0.2, 0.3, 0.4), campaign)

    def test_non_finite_rank_ic_is_rejected(self) -> None:
        """统计输入不能包含 NaN 或无穷值。"""
        campaign = valid_campaign().model_copy(update={"min_valid_dates": 2})
        with self.assertRaisesRegex(FactorMinerError, "STAT_FAMILY_NOT_FROZEN"):
            hac_mean_test((0.1, float("nan")), campaign)

    def test_invalid_family_budget_is_rejected_even_if_model_is_constructed(self) -> None:
        """即使绕过 schema 构造，也不能使用未冻结的 family budget。"""
        campaign = valid_campaign().model_construct(max_hypotheses=0)
        with self.assertRaisesRegex(FactorMinerError, "STAT_FAMILY_NOT_FROZEN"):
            hac_mean_test((0.1, 0.2), campaign)

    def test_bonferroni_rejects_invalid_arguments(self) -> None:
        """Bonferroni 参数必须是有限 p 值和正整数 family。"""
        for p_value, family_size in ((-0.1, 2), (1.1, 2), (0.1, 0), (float("nan"), 2)):
            with self.subTest(p_value=p_value, family_size=family_size):
                with self.assertRaises(ValueError):
                    bonferroni_adjust(p_value, family_size)


if __name__ == "__main__":
    unittest.main()
