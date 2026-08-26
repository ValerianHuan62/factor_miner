"""Gaussian HMM 参数拟合和只使用历史观察的 forward filter。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
import math

from hmmlearn.hmm import GaussianHMM
import numpy as np
from scipy.special import logsumexp

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_schema import CovarianceKind


@dataclass(frozen=True, slots=True)
class GaussianHMMParameters:
    """从第三方拟合器复制出来的项目自有 HMM 参数。"""

    start_probability: np.ndarray
    transition_matrix: np.ndarray
    means: np.ndarray
    covariances: np.ndarray
    covariance_kind: CovarianceKind

    def __post_init__(self) -> None:
        """复制数组并禁止调用方在登记后原地改写参数。"""

        for name in (
            "start_probability",
            "transition_matrix",
            "means",
            "covariances",
        ):
            copied = np.array(getattr(self, name), dtype=float, copy=True)
            copied.setflags(write=False)
            object.__setattr__(self, name, copied)
        object.__setattr__(self, "covariance_kind", CovarianceKind(self.covariance_kind))


@dataclass(frozen=True, slots=True)
class SeedFitDiagnostic:
    """一个固定 seed 的成功拟合诊断。"""

    seed: int
    converged: bool
    iterations: int
    training_log_likelihood: float
    bic: float


@dataclass(frozen=True, slots=True)
class MonthlyHMMFit:
    """多 seed 中训练似然最高的合格月度模型。"""

    parameters: GaussianHMMParameters
    seed: int
    converged: bool
    iterations: int
    training_log_likelihood: float
    bic: float
    seed_diagnostics: tuple[SeedFitDiagnostic, ...]
    hmmlearn_version: str


@dataclass(frozen=True, slots=True)
class FilteredStateSeries:
    """逐观察过滤概率和联合对数似然。"""

    probabilities: np.ndarray
    log_likelihood: float
    log_scales: tuple[float, ...]

    def __post_init__(self) -> None:
        """冻结过滤概率，防止产出后被调用方改写。"""

        copied = np.array(self.probabilities, dtype=float, copy=True)
        copied.setflags(write=False)
        object.__setattr__(self, "probabilities", copied)


def _error(code: FailureCode, message: str) -> FactorMinerError:
    """构造稳定市场状态错误。"""

    return FactorMinerError(code, message)


def _validate_probability_vector(
    values: np.ndarray,
    *,
    name: str,
    tolerance: float = 1e-8,
) -> None:
    """验证非负有限且和为一的概率向量。"""

    if (
        values.ndim != 1
        or not np.isfinite(values).all()
        or np.any(values < 0)
        or not math.isclose(
            float(values.sum()),
            1.0,
            rel_tol=0.0,
            abs_tol=tolerance,
        )
    ):
        raise _error(FailureCode.REGIME_TRANSITION_INVALID, f"{name} 不是合法概率向量")


def _validate_parameters(parameters: GaussianHMMParameters) -> tuple[int, int]:
    """验证 HMM 参数形状、随机矩阵和 covariance 正定性。"""

    start = parameters.start_probability
    transition = parameters.transition_matrix
    means = parameters.means
    covariances = parameters.covariances
    _validate_probability_vector(start, name="start_probability")
    state_count = start.shape[0]
    if transition.shape != (state_count, state_count):
        raise _error(
            FailureCode.REGIME_TRANSITION_INVALID,
            "transition_matrix 形状与状态数不一致",
        )
    for row_index, row in enumerate(transition):
        _validate_probability_vector(row, name=f"transition_matrix[{row_index}]")
    if means.ndim != 2 or means.shape[0] != state_count:
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "means 形状与状态数不一致",
        )
    feature_count = means.shape[1]
    if covariances.shape != (state_count, feature_count, feature_count):
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "covariances 必须规范化为 K×D×D",
        )
    if not np.isfinite(means).all() or not np.isfinite(covariances).all():
        raise _error(
            FailureCode.REGIME_COVARIANCE_INVALID,
            "Gaussian 参数包含非有限值",
        )
    for state_index, covariance in enumerate(covariances):
        if not np.allclose(covariance, covariance.T, rtol=0.0, atol=1e-12):
            raise _error(
                FailureCode.REGIME_COVARIANCE_INVALID,
                f"状态 {state_index} covariance 不对称",
            )
        eigenvalues = np.linalg.eigvalsh(covariance)
        if not np.isfinite(eigenvalues).all() or np.any(eigenvalues <= 0):
            raise _error(
                FailureCode.REGIME_COVARIANCE_INVALID,
                f"状态 {state_index} covariance 非正定",
            )
    return state_count, feature_count


def gaussian_log_emission(
    values: np.ndarray,
    parameters: GaussianHMMParameters,
) -> np.ndarray:
    """计算每个观察在各 Gaussian 状态下的 log density。"""

    observations = np.asarray(values, dtype=float)
    state_count, feature_count = _validate_parameters(parameters)
    if (
        observations.ndim != 2
        or observations.shape[1] != feature_count
        or not np.isfinite(observations).all()
    ):
        raise _error(
            FailureCode.REGIME_FILTER_INVALID,
            "HMM 观察必须是维度匹配的有限二维矩阵",
        )
    result = np.empty((observations.shape[0], state_count), dtype=float)
    constant = feature_count * math.log(2.0 * math.pi)
    for state_index in range(state_count):
        covariance = parameters.covariances[state_index]
        sign, log_determinant = np.linalg.slogdet(covariance)
        if sign <= 0 or not math.isfinite(float(log_determinant)):
            raise _error(
                FailureCode.REGIME_COVARIANCE_INVALID,
                f"状态 {state_index} covariance determinant 无效",
            )
        delta = observations - parameters.means[state_index]
        solved = np.linalg.solve(covariance, delta.T).T
        quadratic = np.sum(delta * solved, axis=1)
        result[:, state_index] = -0.5 * (
            constant + float(log_determinant) + quadratic
        )
    if not np.isfinite(result).all():
        raise _error(FailureCode.REGIME_FILTER_INVALID, "Gaussian log emission 非有限")
    return result


def filter_state_probabilities(
    values: np.ndarray,
    parameters: GaussianHMMParameters,
    initial_probability: np.ndarray | None = None,
) -> FilteredStateSeries:
    """逐日计算 ``P(S_t | X_1,...,X_t)``，不执行后向平滑。"""

    log_emissions = gaussian_log_emission(values, parameters)
    prior = (
        np.array(parameters.start_probability, dtype=float, copy=True)
        if initial_probability is None
        else np.asarray(initial_probability, dtype=float).copy()
    )
    _validate_probability_vector(prior, name="initial_probability")
    state_count = parameters.start_probability.shape[0]
    if prior.shape != (state_count,):
        raise _error(
            FailureCode.REGIME_TRANSITION_INVALID,
            "initial_probability 状态数不一致",
        )
    probabilities = np.empty_like(log_emissions)
    log_scales: list[float] = []
    for index, log_emission in enumerate(log_emissions):
        if np.any(prior <= 0):
            log_prior = np.full(state_count, -np.inf, dtype=float)
            positive = prior > 0
            log_prior[positive] = np.log(prior[positive])
        else:
            log_prior = np.log(prior)
        log_unnormalized = log_prior + log_emission
        log_scale = float(logsumexp(log_unnormalized))
        if not math.isfinite(log_scale):
            raise _error(
                FailureCode.REGIME_FILTER_INVALID,
                f"第 {index} 个观察无法归一化 filtered probability",
            )
        posterior = np.exp(log_unnormalized - log_scale)
        if (
            not np.isfinite(posterior).all()
            or np.any(posterior < 0)
            or not math.isclose(
                float(posterior.sum()),
                1.0,
                rel_tol=0.0,
                abs_tol=1e-8,
            )
        ):
            raise _error(
                FailureCode.REGIME_FILTER_INVALID,
                f"第 {index} 个 filtered probability 非法",
            )
        probabilities[index] = posterior
        log_scales.append(log_scale)
        prior = posterior @ parameters.transition_matrix
    return FilteredStateSeries(
        probabilities=probabilities,
        log_likelihood=float(sum(log_scales)),
        log_scales=tuple(log_scales),
    )


def _normalized_covariances(
    model: GaussianHMM,
    state_count: int,
    feature_count: int,
) -> np.ndarray:
    """将 hmmlearn covariance 统一复制为 K×D×D。"""

    covariances = np.asarray(model.covars_, dtype=float)
    if covariances.shape == (state_count, feature_count):
        return np.stack([np.diag(row) for row in covariances])
    if covariances.shape == (state_count, feature_count, feature_count):
        return covariances.copy()
    raise ValueError(f"未知 hmmlearn covariance 形状：{covariances.shape}")


def fit_gaussian_hmm(
    values: np.ndarray,
    *,
    state_count: int,
    covariance_kind: CovarianceKind,
    seeds: tuple[int, ...],
    n_iter: int,
    tol: float,
    min_covar: float,
) -> MonthlyHMMFit:
    """对固定配置运行多个 seed，并保留训练似然最高的合格解。"""

    observations = np.asarray(values, dtype=float)
    if (
        observations.ndim != 2
        or observations.shape[0] < state_count
        or observations.shape[1] < 1
        or not np.isfinite(observations).all()
        or not seeds
    ):
        raise _error(
            FailureCode.REGIME_FIT_NOT_CONVERGED,
            "HMM 拟合输入、状态数或 seeds 无效",
        )
    covariance_kind = CovarianceKind(covariance_kind)
    successful: list[
        tuple[SeedFitDiagnostic, GaussianHMMParameters]
    ] = []
    failures: list[str] = []
    for seed in seeds:
        try:
            model = GaussianHMM(
                n_components=state_count,
                covariance_type=covariance_kind.value,
                min_covar=min_covar,
                n_iter=n_iter,
                tol=tol,
                random_state=seed,
                implementation="log",
            )
            model.fit(observations)
            converged = bool(model.monitor_.converged)
            if not converged:
                failures.append(f"seed={seed}: 未收敛")
                continue
            parameters = GaussianHMMParameters(
                start_probability=np.asarray(model.startprob_, dtype=float),
                transition_matrix=np.asarray(model.transmat_, dtype=float),
                means=np.asarray(model.means_, dtype=float),
                covariances=_normalized_covariances(
                    model,
                    state_count,
                    observations.shape[1],
                ),
                covariance_kind=covariance_kind,
            )
            _validate_parameters(parameters)
            likelihood = float(model.score(observations))
            bic = float(model.bic(observations))
            if not math.isfinite(likelihood) or not math.isfinite(bic):
                raise ValueError("训练 likelihood 或 BIC 非有限")
            diagnostic = SeedFitDiagnostic(
                seed=seed,
                converged=True,
                iterations=int(model.monitor_.iter),
                training_log_likelihood=likelihood,
                bic=bic,
            )
            successful.append((diagnostic, parameters))
        except Exception as error:
            failures.append(f"seed={seed}: {type(error).__name__}: {error}")
    if not successful:
        detail = "；".join(failures[:5])
        raise _error(
            FailureCode.REGIME_FIT_NOT_CONVERGED,
            f"全部 HMM seed 失败：{detail}",
        )
    successful.sort(
        key=lambda item: (-item[0].training_log_likelihood, item[0].seed)
    )
    selected_diagnostic, selected_parameters = successful[0]
    return MonthlyHMMFit(
        parameters=selected_parameters,
        seed=selected_diagnostic.seed,
        converged=True,
        iterations=selected_diagnostic.iterations,
        training_log_likelihood=selected_diagnostic.training_log_likelihood,
        bic=selected_diagnostic.bic,
        seed_diagnostics=tuple(item[0] for item in successful),
        hmmlearn_version=version("hmmlearn"),
    )


def score_filtered_log_likelihood(
    values: np.ndarray,
    parameters: GaussianHMMParameters,
    initial_probability: np.ndarray | None = None,
) -> float:
    """返回项目 forward filter 对观察序列给出的联合 log likelihood。"""

    return filter_state_probabilities(
        values,
        parameters,
        initial_probability=initial_probability,
    ).log_likelihood
