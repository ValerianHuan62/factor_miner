import unittest

import numpy as np

from factor_miner.errors import FailureCode
from factor_miner.regime_features import FEATURE_COLUMNS, TrainingStandardizer
from factor_miner.regime_model import GaussianHMMParameters
from factor_miner.regime_quality import (
    RegimeStateProfile,
    bhattacharyya_distance,
    build_state_cost_matrix,
    destandardize_parameters,
    evaluate_regime_quality,
    match_canonical_states,
    solve_state_assignment,
)
from factor_miner.regime_schema import CovarianceKind, RegimeQualityPolicy


def parameters_with_covariances(
    covariances: np.ndarray,
    *,
    means: np.ndarray | None = None,
    transition: np.ndarray | None = None,
) -> GaussianHMMParameters:
    """构造两状态四维参数。"""

    return GaussianHMMParameters(
        start_probability=np.array([0.5, 0.5]),
        transition_matrix=(
            np.array([[0.9, 0.1], [0.1, 0.9]])
            if transition is None
            else transition
        ),
        means=np.zeros((2, 4)) if means is None else means,
        covariances=covariances,
        covariance_kind=CovarianceKind.FULL,
    )


class RegimeQualityTest(unittest.TestCase):
    """状态质量、分布距离和全局映射测试。"""

    def test_equal_returns_different_volatility_are_not_redundant(self) -> None:
        """相同均值但 covariance 明显不同的状态不能被当作噪声合并。"""

        covariances = np.array(
            [
                np.eye(4),
                np.diag([1.0, 4.0, 1.0, 1.0]),
            ]
        )
        parameters = parameters_with_covariances(covariances)
        probabilities = np.tile(np.array([[0.5, 0.5]]), (100, 1))
        result = evaluate_regime_quality(
            parameters,
            probabilities,
            RegimeQualityPolicy(),
        )
        self.assertTrue(result.passed)
        self.assertGreater(result.pairwise[0].bhattacharyya_distance, 0.05)
        self.assertNotIn(
            FailureCode.REGIME_STATES_REDUNDANT,
            result.failure_codes,
        )

    def test_states_redundant_only_when_all_dimensions_are_close(self) -> None:
        """发射、占比、持续时间和转移均相近才触发冗余硬拒绝。"""

        parameters = parameters_with_covariances(
            np.array([np.eye(4), np.eye(4) * 1.001]),
            means=np.array([[0.0, 0.0, 0.0, 0.0], [0.001, 0.0, 0.0, 0.0]]),
        )
        probabilities = np.tile(np.array([[0.5, 0.5]]), (100, 1))
        result = evaluate_regime_quality(
            parameters,
            probabilities,
            RegimeQualityPolicy(),
        )
        self.assertFalse(result.passed)
        self.assertIn(
            FailureCode.REGIME_STATES_REDUNDANT,
            result.failure_codes,
        )

    def test_occupancy_and_core_duration_numeric_rules_are_enforced(self) -> None:
        """低占比和短核心状态必须分别触发已冻结硬拒绝码。"""

        low_occupancy = np.tile(np.array([[0.99, 0.01]]), (100, 1))
        result = evaluate_regime_quality(
            parameters_with_covariances(
                np.array([np.eye(4), np.eye(4) * 2.0])
            ),
            low_occupancy,
            RegimeQualityPolicy(),
        )
        self.assertIn(
            FailureCode.REGIME_STATE_OCCUPANCY_TOO_LOW,
            result.failure_codes,
        )

        short_transition = np.array([[0.5, 0.5], [0.5, 0.5]])
        result = evaluate_regime_quality(
            parameters_with_covariances(
                np.array([np.eye(4), np.eye(4) * 2.0]),
                transition=short_transition,
            ),
            np.tile(np.array([[0.5, 0.5]]), (100, 1)),
            RegimeQualityPolicy(),
        )
        self.assertIn(
            FailureCode.REGIME_CORE_DURATION_TOO_SHORT,
            result.failure_codes,
        )

    def test_bhattacharyya_distance_uses_covariance_term(self) -> None:
        """均值完全相同时 covariance 差异仍必须产生正距离。"""

        distance = bhattacharyya_distance(
            np.zeros(2),
            np.eye(2),
            np.zeros(2),
            np.diag([1.0, 4.0]),
        )
        self.assertAlmostEqual(distance, 0.11157177565710488, places=14)

    def test_destandardization_restores_common_raw_coordinates(self) -> None:
        """不同训练 scaler 下的同一 raw Gaussian 必须还原为同一参数。"""

        raw_mean = np.array([10.0, 20.0, 30.0, 40.0])
        raw_covariance = np.diag([4.0, 9.0, 16.0, 25.0])
        first_scaler = TrainingStandardizer(
            feature_columns=FEATURE_COLUMNS,
            means=(0.0, 0.0, 0.0, 0.0),
            scales=(2.0, 3.0, 4.0, 5.0),
        )
        second_scaler = TrainingStandardizer(
            feature_columns=FEATURE_COLUMNS,
            means=(8.0, 17.0, 26.0, 35.0),
            scales=(1.0, 1.0, 1.0, 1.0),
        )
        first = GaussianHMMParameters(
            start_probability=np.array([1.0]),
            transition_matrix=np.array([[1.0]]),
            means=np.array([raw_mean / np.array(first_scaler.scales)]),
            covariances=np.array(
                [
                    np.diag(1.0 / np.array(first_scaler.scales))
                    @ raw_covariance
                    @ np.diag(1.0 / np.array(first_scaler.scales))
                ]
            ),
            covariance_kind=CovarianceKind.FULL,
        )
        second = GaussianHMMParameters(
            start_probability=np.array([1.0]),
            transition_matrix=np.array([[1.0]]),
            means=np.array([raw_mean - np.array(second_scaler.means)]),
            covariances=np.array([raw_covariance]),
            covariance_kind=CovarianceKind.FULL,
        )
        first_raw = destandardize_parameters(first, first_scaler)
        second_raw = destandardize_parameters(second, second_scaler)
        np.testing.assert_allclose(first_raw.means, second_raw.means, atol=1e-14)
        np.testing.assert_allclose(
            first_raw.covariances,
            second_raw.covariances,
            atol=1e-14,
        )

    def test_hungarian_assignment_beats_row_greedy(self) -> None:
        """全局最优分配必须处理局部贪心会冲突的成本矩阵。"""

        mapping = solve_state_assignment(
            np.array(
                [
                    [1.0, 2.0, 3.0],
                    [2.0, 100.0, 4.0],
                    [3.0, 4.0, 100.0],
                ]
            ),
            previous_canonical_ids=(10, 11, 12),
        )
        self.assertEqual(mapping.raw_to_canonical, (11, 12, 10))
        self.assertEqual(mapping.total_cost, 9.0)

    def test_cost_matrix_and_mapping_keep_raw_and_canonical_identity(self) -> None:
        """真实状态画像生成完整成本矩阵并保留两类身份。"""

        previous_parameters = parameters_with_covariances(
            np.array([np.eye(4), np.eye(4) * 3.0]),
            means=np.array([[-1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]),
        )
        current_parameters = parameters_with_covariances(
            np.array([np.eye(4) * 3.0, np.eye(4)]),
            means=np.array([[1.0, 0.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0]]),
        )
        previous = RegimeStateProfile(
            parameters=previous_parameters,
            occupancies=(0.4, 0.6),
            durations=(10.0, 10.0),
            canonical_state_ids=(4, 7),
        )
        current = RegimeStateProfile(
            parameters=current_parameters,
            occupancies=(0.6, 0.4),
            durations=(10.0, 10.0),
            canonical_state_ids=(0, 1),
        )
        costs = build_state_cost_matrix(previous, current, RegimeQualityPolicy())
        self.assertEqual(costs.shape, (2, 2))
        mapping = match_canonical_states(previous, current, RegimeQualityPolicy())
        self.assertEqual(mapping.raw_to_canonical, (7, 4))
        self.assertEqual(mapping.raw_state_ids, (0, 1))


if __name__ == "__main__":
    unittest.main()
