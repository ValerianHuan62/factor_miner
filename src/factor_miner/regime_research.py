"""固定配置的月度走步 HMM 研究、比较和条件诊断。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from statistics import median
from typing import Literal, Mapping

import numpy as np
import polars as pl

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_features import (
    FEATURE_COLUMNS,
    TrainingStandardizer,
    fit_training_standardizer,
    transform_market_features,
)
from factor_miner.regime_model import (
    GaussianHMMParameters,
    MonthlyHMMFit,
    filter_state_probabilities,
    fit_gaussian_hmm,
)
from factor_miner.regime_quality import (
    RegimeQualityResult,
    RegimeStateProfile,
    StateMapping,
    destandardize_parameters,
    evaluate_regime_quality,
    match_canonical_states,
)
from factor_miner.regime_schema import (
    CovarianceKind,
    RegimeResearchSpec,
    ReturnAggregation,
    TrainingWindow,
    regime_research_id,
)


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """一个月度推断区间及其严格历史训练日期。"""

    model_month: str
    training_dates: tuple[date, ...]
    inference_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class RegimeCandidateConfig:
    """研究空间中的一个固定 HMM 配置。"""

    candidate_id: str
    state_count: int
    return_aggregation: ReturnAggregation
    training_window: TrainingWindow
    covariance_kind: CovarianceKind


@dataclass(frozen=True, slots=True)
class MonthlyCandidateResult:
    """一个配置在一个样本外月份的结果或显式失败。"""

    candidate_id: str
    model_month: str
    status: Literal["ok", "unavailable"]
    training_start: date
    training_end: date
    inference_start: date
    inference_end: date
    failure_code: str | None
    failure_message: str | None
    fit: MonthlyHMMFit | None
    standardizer: TrainingStandardizer | None
    quality: RegimeQualityResult | None
    mapping: StateMapping | None
    inference_dates: tuple[date, ...]
    raw_probabilities: np.ndarray | None
    canonical_probabilities: np.ndarray | None
    oos_log_likelihood_per_observation: float | None
    minimum_bhattacharyya: float | None

    def __post_init__(self) -> None:
        """冻结月度概率数组。"""

        for name in ("raw_probabilities", "canonical_probabilities"):
            values = getattr(self, name)
            if values is not None:
                copied = np.array(values, dtype=float, copy=True)
                copied.setflags(write=False)
                object.__setattr__(self, name, copied)


@dataclass(frozen=True, slots=True)
class RegimeCandidateSummary:
    """一个固定配置跨月聚合后的研究比较指标。"""

    candidate_id: str
    return_aggregation: ReturnAggregation
    successful_months: int
    total_months: int
    successful_month_ratio: float
    median_oos_log_likelihood: float | None
    median_mapping_cost: float | None
    median_min_bhattacharyya: float | None
    median_bic: float | None
    eligible: bool


@dataclass(frozen=True, slots=True)
class StateConditionalDiagnostic:
    """一个 canonical 状态下的概率加权逐日指标摘要。"""

    canonical_state_id: int
    weighted_mean: float
    weighted_std: float
    probability_weight_sum: float
    argmax_dates: int


@dataclass(frozen=True, slots=True)
class RegimeConditionalSummary:
    """一个逐日外部指标的完整状态条件摘要。"""

    availability: Literal["close_signal_t", "realized_during_t"]
    valid_dates: int
    states: tuple[StateConditionalDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class RegimeResearchReport:
    """V0.3 研究阶段的配置、逐月结果和分轨推荐。"""

    regime_research_id: str
    configurations: tuple[RegimeCandidateConfig, ...]
    candidates: tuple[RegimeCandidateSummary, ...]
    monthly_results: tuple[MonthlyCandidateResult, ...]
    recommendations: Mapping[ReturnAggregation, str | None]


def regime_research_report_payload(
    report: RegimeResearchReport,
) -> dict[str, object]:
    """生成供 CLI 登记和人工审批使用的 JSON 安全研究摘要。

    逐月模型的完整参数属于后续固定快照；研究报告这里只保存模型选择所需
    的配置、聚合指标和月度成功/失败诊断，避免把逐日概率重复嵌入 JSON。
    """

    return {
        "report_version": "1",
        "regime_research_id": report.regime_research_id,
        "configurations": [
            {
                "candidate_id": item.candidate_id,
                "state_count": item.state_count,
                "return_aggregation": item.return_aggregation.value,
                "training_window": item.training_window.value,
                "covariance_kind": item.covariance_kind.value,
            }
            for item in report.configurations
        ],
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "return_aggregation": item.return_aggregation.value,
                "successful_months": item.successful_months,
                "total_months": item.total_months,
                "successful_month_ratio": item.successful_month_ratio,
                "median_oos_log_likelihood": item.median_oos_log_likelihood,
                "median_mapping_cost": item.median_mapping_cost,
                "median_min_bhattacharyya": item.median_min_bhattacharyya,
                "median_bic": item.median_bic,
                "eligible": item.eligible,
            }
            for item in report.candidates
        ],
        "monthly_diagnostics": [
            {
                "candidate_id": item.candidate_id,
                "model_month": item.model_month,
                "status": item.status,
                "training_start": item.training_start.isoformat(),
                "training_end": item.training_end.isoformat(),
                "inference_start": item.inference_start.isoformat(),
                "inference_end": item.inference_end.isoformat(),
                "failure_code": item.failure_code,
                "failure_message": item.failure_message,
                "seed": item.fit.seed if item.fit is not None else None,
                "converged": (
                    item.fit.converged if item.fit is not None else None
                ),
                "bic": item.fit.bic if item.fit is not None else None,
                "oos_log_likelihood_per_observation": (
                    item.oos_log_likelihood_per_observation
                ),
                "minimum_bhattacharyya": item.minimum_bhattacharyya,
                "mapping_cost": (
                    item.mapping.total_cost if item.mapping is not None else None
                ),
                "quality_passed": (
                    item.quality.passed if item.quality is not None else None
                ),
            }
            for item in report.monthly_results
        ],
        "recommendations": {
            key.value: value for key, value in report.recommendations.items()
        },
    }


def regime_research_report_from_payload(
    payload: Mapping[str, object],
) -> RegimeResearchReport:
    """从正式 JSON 摘要恢复部署校验所需的研究报告部分。"""

    if payload.get("report_version") != "1":
        raise ValueError("未知市场状态研究报告版本")
    configurations = tuple(
        RegimeCandidateConfig(
            candidate_id=str(item["candidate_id"]),
            state_count=int(item["state_count"]),
            return_aggregation=ReturnAggregation(str(item["return_aggregation"])),
            training_window=TrainingWindow(str(item["training_window"])),
            covariance_kind=CovarianceKind(str(item["covariance_kind"])),
        )
        for item in payload["configurations"]  # type: ignore[index,union-attr]
    )
    candidates = tuple(
        RegimeCandidateSummary(
            candidate_id=str(item["candidate_id"]),
            return_aggregation=ReturnAggregation(str(item["return_aggregation"])),
            successful_months=int(item["successful_months"]),
            total_months=int(item["total_months"]),
            successful_month_ratio=float(item["successful_month_ratio"]),
            median_oos_log_likelihood=(
                None
                if item["median_oos_log_likelihood"] is None
                else float(item["median_oos_log_likelihood"])
            ),
            median_mapping_cost=(
                None
                if item["median_mapping_cost"] is None
                else float(item["median_mapping_cost"])
            ),
            median_min_bhattacharyya=(
                None
                if item["median_min_bhattacharyya"] is None
                else float(item["median_min_bhattacharyya"])
            ),
            median_bic=(
                None
                if item["median_bic"] is None
                else float(item["median_bic"])
            ),
            eligible=bool(item["eligible"]),
        )
        for item in payload["candidates"]  # type: ignore[index,union-attr]
    )
    recommendations_payload = payload.get("recommendations")
    if not isinstance(recommendations_payload, Mapping):
        raise ValueError("市场状态研究报告缺少 recommendations")
    return RegimeResearchReport(
        regime_research_id=str(payload["regime_research_id"]),
        configurations=configurations,
        candidates=candidates,
        monthly_results=(),
        recommendations={
            ReturnAggregation(str(key)): (
                None if value is None else str(value)
            )
            for key, value in recommendations_payload.items()
        },
    )


def _candidate_id(
    spec: RegimeResearchSpec,
    *,
    state_count: int,
    aggregation: ReturnAggregation,
    window: TrainingWindow,
    covariance: CovarianceKind,
) -> str:
    """从研究身份和固定配置派生候选 ID。"""

    payload = {
        "regime_research_id": regime_research_id(spec),
        "state_count": state_count,
        "return_aggregation": aggregation.value,
        "training_window": window.value,
        "covariance_kind": covariance.value,
    }
    return f"regcand_{sha256_json(payload)[:24]}"


def enumerate_regime_candidates(
    spec: RegimeResearchSpec,
) -> tuple[RegimeCandidateConfig, ...]:
    """按冻结搜索轴的顺序生成全部固定配置。"""

    candidates: list[RegimeCandidateConfig] = []
    for state_count in spec.candidate_state_counts:
        for aggregation in spec.return_aggregations:
            for window in spec.training_windows:
                for covariance in spec.covariance_kinds:
                    candidates.append(
                        RegimeCandidateConfig(
                            candidate_id=_candidate_id(
                                spec,
                                state_count=state_count,
                                aggregation=aggregation,
                                window=window,
                                covariance=covariance,
                            ),
                            state_count=state_count,
                            return_aggregation=aggregation,
                            training_window=window,
                            covariance_kind=covariance,
                        )
                    )
    return tuple(candidates)


def walk_forward_months(
    dates: tuple[date, ...],
    spec: RegimeResearchSpec,
    training_window: TrainingWindow,
) -> tuple[WalkForwardFold, ...]:
    """按月构造严格历史训练集和当月样本外推断集。"""

    ordered = tuple(
        value
        for value in sorted(set(dates))
        if spec.research_start <= value <= spec.research_end
    )
    if not ordered:
        return ()
    grouped: dict[tuple[int, int], list[date]] = {}
    for value in ordered:
        grouped.setdefault((value.year, value.month), []).append(value)
    required = 2016 if training_window is TrainingWindow.ROLLING_8Y else 1260
    folds: list[WalkForwardFold] = []
    for key in sorted(grouped):
        inference_dates = tuple(grouped[key])
        history = tuple(value for value in ordered if value < inference_dates[0])
        if len(history) < required:
            continue
        if training_window is TrainingWindow.ROLLING_5Y:
            training_dates = history[-1260:]
        elif training_window is TrainingWindow.ROLLING_8Y:
            training_dates = history[-2016:]
        else:
            training_dates = history
        folds.append(
            WalkForwardFold(
                model_month=f"{key[0]:04d}-{key[1]:02d}",
                training_dates=training_dates,
                inference_dates=inference_dates,
            )
        )
    return tuple(folds)


def _initial_mapping(raw_parameters: GaussianHMMParameters) -> StateMapping:
    """按条件收益、条件波动和 raw ID 建立首月 canonical 编号。"""

    parameters = raw_parameters
    state_count = parameters.means.shape[0]
    ordered_raw = sorted(
        range(state_count),
        key=lambda raw: (
            float(parameters.means[raw, 0]),
            -float(parameters.means[raw, 1]),
            raw,
        ),
    )
    raw_to_canonical = [-1] * state_count
    for canonical, raw in enumerate(ordered_raw):
        raw_to_canonical[raw] = canonical
    return StateMapping(
        raw_state_ids=tuple(range(state_count)),
        raw_to_canonical=tuple(raw_to_canonical),
        assignment_costs=tuple(0.0 for _ in range(state_count)),
        total_cost=0.0,
    )


def _state_profile(
    parameters: GaussianHMMParameters,
    quality: RegimeQualityResult,
    mapping: StateMapping,
) -> RegimeStateProfile:
    """把质量诊断和 canonical 映射组合成跨月画像。"""

    return RegimeStateProfile(
        parameters=parameters,
        occupancies=tuple(item.occupancy for item in quality.states),
        durations=tuple(item.expected_duration for item in quality.states),
        canonical_state_ids=mapping.raw_to_canonical,
    )


def _canonical_probabilities(
    raw_probabilities: np.ndarray,
    mapping: StateMapping,
) -> np.ndarray:
    """按 raw→canonical 映射重排逐日概率列。"""

    result = np.empty_like(raw_probabilities)
    for raw_state, canonical_state in enumerate(mapping.raw_to_canonical):
        result[:, canonical_state] = raw_probabilities[:, raw_state]
    return result


def _failed_month(
    candidate: RegimeCandidateConfig,
    fold: WalkForwardFold,
    error: FactorMinerError,
) -> MonthlyCandidateResult:
    """把可预期领域失败保存为不可用月份。"""

    return MonthlyCandidateResult(
        candidate_id=candidate.candidate_id,
        model_month=fold.model_month,
        status="unavailable",
        training_start=fold.training_dates[0],
        training_end=fold.training_dates[-1],
        inference_start=fold.inference_dates[0],
        inference_end=fold.inference_dates[-1],
        failure_code=error.code.value,
        failure_message=error.message,
        fit=None,
        standardizer=None,
        quality=None,
        mapping=None,
        inference_dates=fold.inference_dates,
        raw_probabilities=None,
        canonical_probabilities=None,
        oos_log_likelihood_per_observation=None,
        minimum_bhattacharyya=None,
    )


def _run_candidate(
    features: pl.DataFrame,
    spec: RegimeResearchSpec,
    candidate: RegimeCandidateConfig,
) -> tuple[MonthlyCandidateResult, ...]:
    """运行一个固定配置的全部月度走步 fold。"""

    valid = (
        features.filter(
            pl.col("feature_valid")
            & pl.col("date").is_between(
                spec.research_start,
                spec.research_end,
                closed="both",
            )
        )
        .select(["date", *FEATURE_COLUMNS])
        .sort("date")
    )
    dates = tuple(valid.get_column("date").to_list())
    folds = walk_forward_months(dates, spec, candidate.training_window)
    results: list[MonthlyCandidateResult] = []
    previous_profile: RegimeStateProfile | None = None
    previous_last_canonical: np.ndarray | None = None
    previous_fold_was_successful = False
    for fold in folds:
        try:
            training = valid.filter(pl.col("date").is_in(fold.training_dates))
            inference = valid.filter(pl.col("date").is_in(fold.inference_dates))
            if training.height != len(fold.training_dates) or inference.height != len(
                fold.inference_dates
            ):
                raise FactorMinerError(
                    FailureCode.REGIME_FEATURE_INCOMPLETE,
                    f"{fold.model_month} 训练或推断日期不完整",
                )
            standardizer = fit_training_standardizer(training)
            training_values = transform_market_features(training, standardizer)
            inference_values = transform_market_features(inference, standardizer)
            fit = fit_gaussian_hmm(
                training_values,
                state_count=candidate.state_count,
                covariance_kind=candidate.covariance_kind,
                seeds=spec.seeds,
                n_iter=spec.n_iter,
                tol=float(spec.tol),
                min_covar=float(spec.min_covar),
            )
            training_filtered = filter_state_probabilities(
                training_values,
                fit.parameters,
            )
            quality = evaluate_regime_quality(
                fit.parameters,
                training_filtered.probabilities,
                spec.quality_policy,
            )
            if not quality.passed:
                raise FactorMinerError(
                    quality.failure_codes[0],
                    "月度 HMM 未通过质量硬门槛："
                    + ",".join(code.value for code in quality.failure_codes),
                )
            raw_parameters = destandardize_parameters(
                fit.parameters,
                standardizer,
            )
            current_unmapped = RegimeStateProfile(
                parameters=raw_parameters,
                occupancies=tuple(item.occupancy for item in quality.states),
                durations=tuple(item.expected_duration for item in quality.states),
                canonical_state_ids=tuple(range(candidate.state_count)),
            )
            mapping = (
                _initial_mapping(raw_parameters)
                if previous_profile is None
                else match_canonical_states(
                    previous_profile,
                    current_unmapped,
                    spec.quality_policy,
                )
            )
            if previous_fold_was_successful and previous_last_canonical is not None:
                previous_in_current_raw = np.asarray(
                    [
                        previous_last_canonical[canonical]
                        for canonical in mapping.raw_to_canonical
                    ],
                    dtype=float,
                )
                inference_prior = (
                    previous_in_current_raw @ fit.parameters.transition_matrix
                )
            else:
                inference_prior = (
                    training_filtered.probabilities[-1]
                    @ fit.parameters.transition_matrix
                )
            inference_filtered = filter_state_probabilities(
                inference_values,
                fit.parameters,
                initial_probability=inference_prior,
            )
            canonical = _canonical_probabilities(
                inference_filtered.probabilities,
                mapping,
            )
            minimum_distance = min(
                item.bhattacharyya_distance for item in quality.pairwise
            )
            result = MonthlyCandidateResult(
                candidate_id=candidate.candidate_id,
                model_month=fold.model_month,
                status="ok",
                training_start=fold.training_dates[0],
                training_end=fold.training_dates[-1],
                inference_start=fold.inference_dates[0],
                inference_end=fold.inference_dates[-1],
                failure_code=None,
                failure_message=None,
                fit=fit,
                standardizer=standardizer,
                quality=quality,
                mapping=mapping,
                inference_dates=fold.inference_dates,
                raw_probabilities=inference_filtered.probabilities,
                canonical_probabilities=canonical,
                oos_log_likelihood_per_observation=(
                    inference_filtered.log_likelihood / inference.height
                ),
                minimum_bhattacharyya=minimum_distance,
            )
            results.append(result)
            previous_profile = _state_profile(raw_parameters, quality, mapping)
            previous_last_canonical = canonical[-1].copy()
            previous_fold_was_successful = True
        except FactorMinerError as error:
            results.append(_failed_month(candidate, fold, error))
            previous_fold_was_successful = False
            previous_last_canonical = None
    return tuple(results)


def run_fixed_regime_candidate(
    features: pl.DataFrame,
    spec: RegimeResearchSpec,
    candidate: RegimeCandidateConfig,
) -> tuple[MonthlyCandidateResult, ...]:
    """按单一冻结配置运行月度走步，不执行状态数或模型选择。"""

    return _run_candidate(features, spec, candidate)


def _summarize_candidate(
    candidate: RegimeCandidateConfig,
    monthly: tuple[MonthlyCandidateResult, ...],
    spec: RegimeResearchSpec,
) -> RegimeCandidateSummary:
    """聚合一个配置的样本外和稳定性指标。"""

    successful = tuple(item for item in monthly if item.status == "ok")
    total = len(monthly)
    ratio = len(successful) / total if total else 0.0
    eligible = (
        len(successful) >= spec.min_oos_months
        and ratio >= spec.min_successful_month_ratio
    )
    if successful:
        oos = median(
            item.oos_log_likelihood_per_observation
            for item in successful
            if item.oos_log_likelihood_per_observation is not None
        )
        mapping = median(
            item.mapping.total_cost
            for item in successful
            if item.mapping is not None
        )
        distance = median(
            item.minimum_bhattacharyya
            for item in successful
            if item.minimum_bhattacharyya is not None
        )
        bic = median(item.fit.bic for item in successful if item.fit is not None)
    else:
        oos = None
        mapping = None
        distance = None
        bic = None
    return RegimeCandidateSummary(
        candidate_id=candidate.candidate_id,
        return_aggregation=candidate.return_aggregation,
        successful_months=len(successful),
        total_months=total,
        successful_month_ratio=ratio,
        median_oos_log_likelihood=float(oos) if oos is not None else None,
        median_mapping_cost=float(mapping) if mapping is not None else None,
        median_min_bhattacharyya=(
            float(distance) if distance is not None else None
        ),
        median_bic=float(bic) if bic is not None else None,
        eligible=eligible,
    )


def recommend_candidates(
    summaries: tuple[RegimeCandidateSummary, ...],
) -> dict[ReturnAggregation, str | None]:
    """分别在 median 和 equal-weight 轨道内按冻结优先级推荐。"""

    recommendations: dict[ReturnAggregation, str | None] = {}
    for aggregation in ReturnAggregation:
        eligible = [
            item
            for item in summaries
            if item.return_aggregation is aggregation
            and item.eligible
            and item.median_oos_log_likelihood is not None
            and item.median_mapping_cost is not None
            and item.median_min_bhattacharyya is not None
            and item.median_bic is not None
        ]
        eligible.sort(
            key=lambda item: (
                -item.median_oos_log_likelihood,
                item.median_mapping_cost,
                -item.median_min_bhattacharyya,
                item.median_bic,
                item.candidate_id,
            )
        )
        recommendations[aggregation] = (
            eligible[0].candidate_id if eligible else None
        )
    return recommendations


def run_regime_research(
    features_by_aggregation: Mapping[ReturnAggregation, pl.DataFrame],
    spec: RegimeResearchSpec,
) -> RegimeResearchReport:
    """运行冻结研究空间并输出两个收益轨道的独立推荐。"""

    configurations = enumerate_regime_candidates(spec)
    monthly_results: list[MonthlyCandidateResult] = []
    summaries: list[RegimeCandidateSummary] = []
    for candidate in configurations:
        features = features_by_aggregation.get(candidate.return_aggregation)
        if features is None:
            raise FactorMinerError(
                FailureCode.REGIME_FEATURE_INCOMPLETE,
                f"缺少 {candidate.return_aggregation.value} 市场特征",
            )
        monthly = _run_candidate(features, spec, candidate)
        monthly_results.extend(monthly)
        summaries.append(_summarize_candidate(candidate, monthly, spec))
    frozen_summaries = tuple(summaries)
    return RegimeResearchReport(
        regime_research_id=regime_research_id(spec),
        configurations=configurations,
        candidates=frozen_summaries,
        monthly_results=tuple(monthly_results),
        recommendations=recommend_candidates(frozen_summaries),
    )


def summarize_daily_diagnostics_by_regime(
    filtered: pl.DataFrame,
    diagnostic_series: pl.DataFrame,
    *,
    availability: Literal["close_signal_t", "realized_during_t"],
) -> RegimeConditionalSummary:
    """按信息可得时点对齐并计算 canonical 状态概率加权摘要。"""

    required_filtered = {
        "observation_date",
        "earliest_use_date",
        "canonical_state_probabilities",
    }
    if required_filtered - set(filtered.columns) or {"date", "value"} - set(
        diagnostic_series.columns
    ):
        raise FactorMinerError(
            FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
            "状态概率或逐日诊断缺少标准列",
        )
    join_column = (
        "observation_date"
        if availability == "close_signal_t"
        else "earliest_use_date"
    )
    joined = filtered.join(
        diagnostic_series,
        left_on=join_column,
        right_on="date",
        how="inner",
        validate="m:1",
    ).filter(pl.col("value").is_finite())
    if joined.is_empty():
        raise FactorMinerError(
            FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
            "状态与逐日诊断没有有效时点交集",
        )
    probability_rows = joined.get_column(
        "canonical_state_probabilities"
    ).to_list()
    state_count = len(probability_rows[0])
    if state_count < 1 or any(len(row) != state_count for row in probability_rows):
        raise FactorMinerError(
            FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
            "canonical 状态概率维度不一致",
        )
    probabilities = np.asarray(probability_rows, dtype=float)
    values = joined.get_column("value").to_numpy().astype(float, copy=False)
    if (
        not np.isfinite(probabilities).all()
        or np.any(probabilities < 0)
        or not np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-8, rtol=0.0)
    ):
        raise FactorMinerError(
            FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
            "canonical 状态概率非法",
        )
    argmax = probabilities.argmax(axis=1)
    states: list[StateConditionalDiagnostic] = []
    for state in range(state_count):
        weights = probabilities[:, state]
        weight_sum = float(weights.sum())
        if weight_sum <= 0:
            weighted_mean = math.nan
            weighted_std = math.nan
        else:
            weighted_mean = float(np.dot(weights, values) / weight_sum)
            weighted_variance = float(
                np.dot(weights, (values - weighted_mean) ** 2) / weight_sum
            )
            weighted_std = math.sqrt(max(0.0, weighted_variance))
        states.append(
            StateConditionalDiagnostic(
                canonical_state_id=state,
                weighted_mean=weighted_mean,
                weighted_std=weighted_std,
                probability_weight_sum=weight_sum,
                argmax_dates=int(np.sum(argmax == state)),
            )
        )
    return RegimeConditionalSummary(
        availability=availability,
        valid_dates=joined.height,
        states=tuple(states),
    )
