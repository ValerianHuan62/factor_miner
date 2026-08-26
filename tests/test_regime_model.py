import os
import unittest

import numpy as np

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_model import (
    GaussianHMMParameters,
    filter_state_probabilities,
    fit_gaussian_hmm,
    gaussian_log_emission,
)
from factor_miner.regime_schema import CovarianceKind


def hand_parameters() -> GaussianHMMParameters:
    """返回具有手算 filtered probability 的二状态参数。"""

    return GaussianHMMParameters(
        start_probability=np.array([0.6, 0.4]),
        transition_matrix=np.array([[0.8, 0.2], [0.3, 0.7]]),
        means=np.array([[-1.0], [1.0]]),
        covariances=np.array([[[0.25]], [[0.25]]]),
        covariance_kind=CovarianceKind.FULL,
    )


class RegimeModelTest(unittest.TestCase):
    """Gaussian HMM 参数拟合和项目自有过滤器测试。"""

    def test_forward_filter_matches_hand_calculation(self) -> None:
        """错误的平滑、Viterbi 或转移方向都会破坏手算概率。"""

        observations = np.array([[-1.0], [0.9]])
        result = filter_state_probabilities(observations, hand_parameters())
        np.testing.assert_allclose(
            result.probabilities,
            np.array(
                [
                    [0.9997764082525151, 0.0002235917474848822],
                    [0.0029753784725967958, 0.9970246215274031],
                ]
            ),
            rtol=0,
            atol=1e-14,
        )
        self.assertAlmostEqual(result.log_likelihood, -2.5880839878327753, places=14)

    def test_future_observation_does_not_change_past_filter(self) -> None:
        """追加未来观察不能改变已经形成的 filtered probability。"""

        historical = np.array([[-1.0], [0.9]])
        extended = np.array([[-1.0], [0.9], [-20.0]])
        left = filter_state_probabilities(historical, hand_parameters())
        right = filter_state_probabilities(extended, hand_parameters())
        np.testing.assert_array_equal(left.probabilities, right.probabilities[:2])

    def test_log_emission_matches_univariate_gaussian_literal(self) -> None:
        """Gaussian 发射密度必须包含 determinant 和二次型。"""

        emissions = gaussian_log_emission(np.array([[-1.0]]), hand_parameters())
        self.assertAlmostEqual(emissions[0, 0], -0.2257913526447274, places=14)
        self.assertAlmostEqual(emissions[0, 1], -8.225791352644727, places=14)

    def test_multiseed_fit_is_deterministic_and_normalizes_covariance_shape(self) -> None:
        """多 seed 只能选择最高训练似然的收敛解并统一 covariance 形状。"""

        rng = np.random.default_rng(20260729)
        values = np.vstack(
            [
                rng.normal(loc=(-1.0, 0.2), scale=(0.2, 0.3), size=(150, 2)),
                rng.normal(loc=(1.0, -0.2), scale=(0.3, 0.2), size=(150, 2)),
            ]
        )
        for covariance_kind in (CovarianceKind.DIAG, CovarianceKind.FULL):
            with self.subTest(covariance_kind=covariance_kind):
                first = fit_gaussian_hmm(
                    values,
                    state_count=2,
                    covariance_kind=covariance_kind,
                    seeds=(11, 23, 47),
                    n_iter=100,
                    tol=1e-4,
                    min_covar=1e-6,
                )
                second = fit_gaussian_hmm(
                    values,
                    state_count=2,
                    covariance_kind=covariance_kind,
                    seeds=(11, 23, 47),
                    n_iter=100,
                    tol=1e-4,
                    min_covar=1e-6,
                )
                self.assertEqual(first.seed, second.seed)
                self.assertEqual(
                    first.training_log_likelihood,
                    max(item.training_log_likelihood for item in first.seed_diagnostics),
                )
                self.assertEqual(first.parameters.covariances.shape, (2, 2, 2))
                np.testing.assert_array_equal(
                    first.parameters.transition_matrix,
                    second.parameters.transition_matrix,
                )
                self.assertTrue(all(item.converged for item in first.seed_diagnostics))

    def test_all_seed_failures_raise_stable_error(self) -> None:
        """没有任何合法初始解时不能返回伪造模型。"""

        with self.assertRaises(FactorMinerError) as raised:
            fit_gaussian_hmm(
                np.array([[0.0, 1.0]]),
                state_count=2,
                covariance_kind=CovarianceKind.DIAG,
                seeds=(11, 23),
                n_iter=10,
                tol=1e-4,
                min_covar=1e-6,
            )
        self.assertEqual(raised.exception.code, FailureCode.REGIME_FIT_NOT_CONVERGED)

    def test_invalid_transition_or_probability_fails_closed(self) -> None:
        """非随机转移矩阵和非有限输入必须使用稳定错误失败。"""

        invalid = GaussianHMMParameters(
            start_probability=np.array([0.6, 0.4]),
            transition_matrix=np.array([[2.0, 0.2], [0.3, 0.7]]),
            means=np.array([[-1.0], [1.0]]),
            covariances=np.array([[[0.25]], [[0.25]]]),
            covariance_kind=CovarianceKind.FULL,
        )
        with self.assertRaises(FactorMinerError) as raised:
            filter_state_probabilities(np.array([[-1.0]]), invalid)
        self.assertEqual(raised.exception.code, FailureCode.REGIME_TRANSITION_INVALID)

        with self.assertRaises(FactorMinerError) as raised:
            filter_state_probabilities(np.array([[np.nan]]), hand_parameters())
        self.assertEqual(raised.exception.code, FailureCode.REGIME_FILTER_INVALID)


if __name__ == "__main__":
    unittest.main()
