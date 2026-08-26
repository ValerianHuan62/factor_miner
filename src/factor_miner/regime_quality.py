"""HMM 状态质量、Gaussian 距离和跨月 canonical 映射。"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy.optimize import linear_sum_assignment

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_features import TrainingStandardizer
from factor_miner.regime_model import GaussianHMMParameters
from factor_miner.regime_schema import RegimeQualityPolicy


@dataclass(frozen=True, slots=True)
class StateDiagnostic:
    """单个状态的占比、持续时间和警告。"""

    raw_state_id: int
    occupancy: float
    expected_duration: float
    core_state: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairwiseStateDiagnostic:
    """一对状态的发射距离和冗余结论。"""

    left_state_id: int
    right_state_id: int
    bhattacharyya_distance: float
    occupancy_difference: float
    duration_relative_difference: float
    transition_l1_distance: float
    redundant: bool


@dataclass(frozen=True, slots=True)
class RegimeQualityResult:
    """一个月度 HMM 的完整数值质量结论。"""

    passed: bool
    states: tuple[StateDiagnostic, ...]
    pairwise: tuple[PairwiseStateDiagnostic, ...]
    weighted_expected_duration: float
    warnings: tuple[str, ...]
    failure_codes: tuple[FailureCode, ...]


@dataclass(frozen=True, slots=True)
class RegimeStateProfile:
    """用于跨月匹配的 raw 坐标状态画像。"""

    parameters: GaussianHMMParameters
    occupancies: tuple[float, ...]
    durations: tuple[float, ...]
    canonical_state_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """状态画像的四组状态必须一一对应。"""

        state_count = self.parameters.start_probability.shape[0]
        if not (
            len(self.occupancies)
            == len(self.durations)
            == len(self.canonical_state_ids)
            == state_count
        ):
            raise ValueError("状态画像维度不一致")
        if len(set(self.canonical_state_ids)) != state_count:
            raise ValueError("canonical_state_ids 必须唯一")
        if (
            any(not math.isfinite(value) or value < 0 for value in self.occupancies)
            or not math.isclose(sum(self.occupancies), 1.0, abs_tol=1e-8)
            or any(not math.isfinite(value) or value <= 0 for value in self.durations)
        ):
            raise ValueError("状态画像占比或持续时间无效")


@dataclass(frozen=True, slots=True)
class StateMapping:
    """Hungarian 全局分配得到的 raw 到 canonical 映射。"""

    raw_state_ids: tuple[int, ...]
    raw_to_canonical: tuple[int, ...]
    assignment_costs: tuple[float, ...]
    total_cost: float


def _error(code: FailureCode, message: str) -> FactorMinerError:
    """构造稳定市场状态错误。"""

    return FactorMinerError(code, message)


def bhattacharyya_distance(
    left_mean: np.ndarray,
    left_covariance: np.ndarray,
    right_mean: np.ndarray,
    right_covariance: np.ndarray,
) -> float:
    """计算两个多元 Gaussian 的 Bhattacharyya distance。"""

    left_mean = np.asarray(left_mean, dtype=float)
    right_mean = np.asarray(right_mean, dtype=float)
    left_covariance = np.asarray(left_covariance, dtype=float)
    right_covariance = np.asarray(right_covariance, dtype=float)
    if (
        left_mean.ndim != 1
        or right_mean.shape != left_mean.shape
        or left_covariance.shape != (left_mean.size, left_mean.size)
        or right_covariance.shape != left_covariance.shape
        or not np.isfinite(left_mean).all()
        or not np.isfinite(right_mean).all()
        or not np.isfinite(left_covariance).all()
        or not np.isfinite(right_covariance).all()
    ):
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "Bhattacharyya 输入维度或有限性无效",
        )
    pooled = (left_covariance + right_covariance) / 2.0
    left_sign, left_logdet = np.linalg.slogdet(left_covariance)
    right_sign, right_logdet = np.linalg.slogdet(right_covariance)
    pooled_sign, pooled_logdet = np.linalg.slogdet(pooled)
    if min(left_sign, right_sign, pooled_sign) <= 0:
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "Bhattacharyya covariance 必须正定",
        )
    delta = left_mean - right_mean
    mean_term = 0.125 * float(delta @ np.linalg.solve(pooled, delta))
    covariance_term = 0.5 * (
        float(pooled_logdet)
        - 0.5 * (float(left_logdet) + float(right_logdet))
    )
    distance = mean_term + covariance_term
    if not math.isfinite(distance) or distance < -1e-12:
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "Bhattacharyya distance 非有限或为负",
        )
    return max(0.0, float(distance))


def _transition_feature(
    transition_matrix: np.ndarray,
    state_index: int,
) -> np.ndarray:
    """返回不依赖未知列映射的自转移和排序后外部转移。"""

    row = np.asarray(transition_matrix[state_index], dtype=float)
    non_self = np.delete(row, state_index)
    return np.concatenate(
        (
            np.array([row[state_index]], dtype=float),
            np.sort(non_self)[::-1],
        )
    )


def _duration_relative_difference(left: float, right: float) -> float:
    """返回以较大持续时间为分母的对称相对差。"""

    return abs(left - right) / max(left, right)


def evaluate_regime_quality(
    parameters: GaussianHMMParameters,
    filtered_probabilities: np.ndarray,
    policy: RegimeQualityPolicy,
) -> RegimeQualityResult:
    """执行收敛后的占比、持续时间、正定和冗余质量协议。"""

    probabilities = np.asarray(filtered_probabilities, dtype=float)
    state_count = parameters.start_probability.shape[0]
    if (
        probabilities.ndim != 2
        or probabilities.shape[1] != state_count
        or probabilities.shape[0] < 1
        or not np.isfinite(probabilities).all()
        or np.any(probabilities < 0)
        or not np.allclose(
            probabilities.sum(axis=1),
            1.0,
            rtol=0.0,
            atol=policy.probability_tolerance,
        )
    ):
        raise _error(
            FailureCode.REGIME_FILTER_INVALID,
            "训练 filtered probabilities 形状或归一化无效",
        )
    transition = parameters.transition_matrix
    if (
        transition.shape != (state_count, state_count)
        or not np.isfinite(transition).all()
        or np.any(transition < 0)
        or not np.allclose(
            transition.sum(axis=1),
            1.0,
            rtol=0.0,
            atol=policy.probability_tolerance,
        )
    ):
        raise _error(
            FailureCode.REGIME_TRANSITION_INVALID,
            "状态质量检查发现非法转移矩阵",
        )

    failure_codes: list[FailureCode] = []
    warnings: list[str] = []
    covariance_valid = True
    for state_index, covariance in enumerate(parameters.covariances):
        eigenvalues = np.linalg.eigvalsh(covariance)
        if (
            not np.isfinite(eigenvalues).all()
            or float(eigenvalues.min()) < policy.covariance_eigenvalue_hard_min
        ):
            covariance_valid = False
            if FailureCode.REGIME_COVARIANCE_INVALID not in failure_codes:
                failure_codes.append(FailureCode.REGIME_COVARIANCE_INVALID)
            warnings.append(f"状态 {state_index} covariance 未通过正定硬门槛")

    occupancies = probabilities.mean(axis=0)
    durations: list[float] = []
    states: list[StateDiagnostic] = []
    for state_index in range(state_count):
        self_transition = float(transition[state_index, state_index])
        duration = (
            math.inf
            if self_transition >= 1.0
            else 1.0 / (1.0 - self_transition)
        )
        durations.append(duration)
        state_warnings: list[str] = []
        occupancy = float(occupancies[state_index])
        if occupancy < policy.occupancy_hard_min:
            if FailureCode.REGIME_STATE_OCCUPANCY_TOO_LOW not in failure_codes:
                failure_codes.append(FailureCode.REGIME_STATE_OCCUPANCY_TOO_LOW)
        elif occupancy < policy.occupancy_warning_min:
            state_warnings.append("状态占比低于 8% 警告线")
        core_state = occupancy >= policy.occupancy_warning_min
        if not math.isfinite(duration):
            if FailureCode.REGIME_TRANSITION_INVALID not in failure_codes:
                failure_codes.append(FailureCode.REGIME_TRANSITION_INVALID)
        elif core_state and duration < policy.core_duration_hard_min:
            if FailureCode.REGIME_CORE_DURATION_TOO_SHORT not in failure_codes:
                failure_codes.append(FailureCode.REGIME_CORE_DURATION_TOO_SHORT)
        states.append(
            StateDiagnostic(
                raw_state_id=state_index,
                occupancy=occupancy,
                expected_duration=duration,
                core_state=core_state,
                warnings=tuple(state_warnings),
            )
        )
        warnings.extend(f"状态 {state_index}：{item}" for item in state_warnings)

    weighted_duration = float(np.dot(occupancies, np.asarray(durations)))
    if not math.isfinite(weighted_duration):
        if FailureCode.REGIME_TRANSITION_INVALID not in failure_codes:
            failure_codes.append(FailureCode.REGIME_TRANSITION_INVALID)
    elif weighted_duration < policy.weighted_duration_warning_min:
        warnings.append("占比加权平均持续时间低于 5 个交易日")

    pairwise: list[PairwiseStateDiagnostic] = []
    if covariance_valid and all(math.isfinite(value) for value in durations):
        for left in range(state_count):
            for right in range(left + 1, state_count):
                distance = bhattacharyya_distance(
                    parameters.means[left],
                    parameters.covariances[left],
                    parameters.means[right],
                    parameters.covariances[right],
                )
                occupancy_difference = abs(
                    float(occupancies[left] - occupancies[right])
                )
                duration_difference = _duration_relative_difference(
                    durations[left],
                    durations[right],
                )
                transition_distance = float(
                    np.abs(
                        _transition_feature(transition, left)
                        - _transition_feature(transition, right)
                    ).sum()
                )
                redundant = (
                    distance < policy.redundancy_distance_max
                    and occupancy_difference
                    < policy.redundancy_occupancy_difference_max
                    and duration_difference
                    < policy.redundancy_duration_relative_difference_max
                    and transition_distance < policy.redundancy_transition_l1_max
                )
                if distance < policy.similarity_warning_distance:
                    warnings.append(f"状态 {left} 与 {right} 的发射分布相似")
                if redundant and FailureCode.REGIME_STATES_REDUNDANT not in failure_codes:
                    failure_codes.append(FailureCode.REGIME_STATES_REDUNDANT)
                pairwise.append(
                    PairwiseStateDiagnostic(
                        left_state_id=left,
                        right_state_id=right,
                        bhattacharyya_distance=distance,
                        occupancy_difference=occupancy_difference,
                        duration_relative_difference=duration_difference,
                        transition_l1_distance=transition_distance,
                        redundant=redundant,
                    )
                )
    return RegimeQualityResult(
        passed=not failure_codes,
        states=tuple(states),
        pairwise=tuple(pairwise),
        weighted_expected_duration=weighted_duration,
        warnings=tuple(warnings),
        failure_codes=tuple(failure_codes),
    )


def destandardize_parameters(
    parameters: GaussianHMMParameters,
    standardizer: TrainingStandardizer,
) -> GaussianHMMParameters:
    """把月度 z-space Gaussian 参数还原到原始四维特征坐标。"""

    means = np.asarray(standardizer.means, dtype=float)
    scales = np.asarray(standardizer.scales, dtype=float)
    feature_count = parameters.means.shape[1]
    if (
        means.shape != (feature_count,)
        or scales.shape != (feature_count,)
        or not np.isfinite(means).all()
        or not np.isfinite(scales).all()
        or np.any(scales <= 0)
    ):
        raise _error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "还原 Gaussian 参数时 scaler 维度或数值无效",
        )
    scale_matrix = np.diag(scales)
    raw_means = means + parameters.means * scales
    raw_covariances = np.stack(
        [
            scale_matrix @ covariance @ scale_matrix
            for covariance in parameters.covariances
        ]
    )
    return GaussianHMMParameters(
        start_probability=parameters.start_probability,
        transition_matrix=parameters.transition_matrix,
        means=raw_means,
        covariances=raw_covariances,
        covariance_kind=parameters.covariance_kind,
    )


def build_state_cost_matrix(
    previous: RegimeStateProfile,
    current: RegimeStateProfile,
    policy: RegimeQualityPolicy,
) -> np.ndarray:
    """构造旧 canonical 状态到新 raw 状态的完整匹配成本矩阵。"""

    state_count = previous.parameters.start_probability.shape[0]
    if current.parameters.start_probability.shape[0] != state_count:
        raise _error(
            FailureCode.REGIME_MAPPING_INVALID,
            "正式 fixed K 要求跨月状态数相同",
        )
    weights = policy.mapping_weights
    caps = policy.mapping_caps
    costs = np.empty((state_count, state_count), dtype=float)
    for old_state in range(state_count):
        for new_state in range(state_count):
            emission = min(
                bhattacharyya_distance(
                    previous.parameters.means[old_state],
                    previous.parameters.covariances[old_state],
                    current.parameters.means[new_state],
                    current.parameters.covariances[new_state],
                )
                / caps.emission,
                1.0,
            )
            duration = min(
                abs(
                    math.log(
                        previous.durations[old_state]
                        / current.durations[new_state]
                    )
                )
                / caps.duration_log_ratio,
                1.0,
            )
            occupancy = min(
                abs(
                    previous.occupancies[old_state]
                    - current.occupancies[new_state]
                )
                / caps.occupancy,
                1.0,
            )
            transition = min(
                float(
                    np.abs(
                        _transition_feature(
                            previous.parameters.transition_matrix,
                            old_state,
                        )
                        - _transition_feature(
                            current.parameters.transition_matrix,
                            new_state,
                        )
                    ).sum()
                )
                / caps.transition_l1,
                1.0,
            )
            costs[old_state, new_state] = (
                weights.emission * emission
                + weights.duration * duration
                + weights.occupancy * occupancy
                + weights.transition * transition
            )
    if not np.isfinite(costs).all():
        raise _error(
            FailureCode.REGIME_MAPPING_INVALID,
            "状态匹配成本矩阵包含非有限值",
        )
    return costs


def solve_state_assignment(
    cost_matrix: np.ndarray,
    *,
    previous_canonical_ids: tuple[int, ...],
) -> StateMapping:
    """对完整方阵执行一次 Hungarian 全局最小成本分配。"""

    costs = np.asarray(cost_matrix, dtype=float)
    if (
        costs.ndim != 2
        or costs.shape[0] != costs.shape[1]
        or costs.shape[0] != len(previous_canonical_ids)
        or len(set(previous_canonical_ids)) != len(previous_canonical_ids)
        or not np.isfinite(costs).all()
    ):
        raise _error(
            FailureCode.REGIME_MAPPING_INVALID,
            "Hungarian 输入必须是有限方阵并匹配唯一 canonical IDs",
        )
    row_indices, column_indices = linear_sum_assignment(costs)
    state_count = costs.shape[0]
    raw_to_canonical = [-1] * state_count
    assignment_costs = [math.nan] * state_count
    for old_row, new_raw in zip(row_indices, column_indices, strict=True):
        raw_to_canonical[int(new_raw)] = previous_canonical_ids[int(old_row)]
        assignment_costs[int(new_raw)] = float(costs[old_row, new_raw])
    if any(value < 0 for value in raw_to_canonical) or any(
        not math.isfinite(value) for value in assignment_costs
    ):
        raise _error(
            FailureCode.REGIME_MAPPING_INVALID,
            "Hungarian 未形成完整一一映射",
        )
    return StateMapping(
        raw_state_ids=tuple(range(state_count)),
        raw_to_canonical=tuple(raw_to_canonical),
        assignment_costs=tuple(assignment_costs),
        total_cost=float(sum(assignment_costs)),
    )


def match_canonical_states(
    previous: RegimeStateProfile,
    current: RegimeStateProfile,
    policy: RegimeQualityPolicy,
) -> StateMapping:
    """构造完整成本矩阵并全局匹配新 raw state。"""

    costs = build_state_cost_matrix(previous, current, policy)
    return solve_state_assignment(
        costs,
        previous_canonical_ids=previous.canonical_state_ids,
    )
