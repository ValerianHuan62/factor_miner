"""市场状态研究结果进入固定部署前的治理校验。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from factor_miner.regime_data import RegimeDataRequest, RegimeDataSource
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_features import build_market_features
from factor_miner.regime_research import (
    RegimeCandidateConfig,
    RegimeResearchReport,
    run_fixed_regime_candidate,
)
from factor_miner.regime_schema import (
    RegisteredRegimeDeployment,
    RegisteredRegimeResearch,
    RegimeResearchSpec,
)
from factor_miner.regime_snapshot import RegimeBuildResult, publish_regime_snapshot


def _manifest_mismatch(message: str) -> FactorMinerError:
    """构造稳定的研究—部署清单不匹配错误。"""

    return FactorMinerError(
        FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
        message,
    )


def validate_regime_deployment(
    research: RegisteredRegimeResearch,
    report: RegimeResearchReport,
    deployment: RegisteredRegimeDeployment,
) -> RegisteredRegimeDeployment:
    """确认部署完整复现一个通过研究门槛的固定候选。

    本函数不根据结果自动批准模型，只验证人工选择的部署没有改变研究时
    已冻结的状态数、特征、窗口、拟合参数或质量政策。
    """

    spec = research.spec
    deployed = deployment.spec
    if (
        report.regime_research_id != research.regime_research_id
        or deployed.regime_research_id != research.regime_research_id
    ):
        raise _manifest_mismatch("研究报告、研究登记和部署引用的研究 ID 不一致")

    configurations = {
        item.candidate_id: item for item in report.configurations
    }
    summaries = {item.candidate_id: item for item in report.candidates}
    candidate = configurations.get(deployed.research_candidate_id)
    summary = summaries.get(deployed.research_candidate_id)
    if candidate is None or summary is None:
        raise _manifest_mismatch("正式部署引用了研究报告中不存在的候选")
    if not summary.eligible:
        raise _manifest_mismatch("正式部署引用的候选未通过研究阶段可用性门槛")

    candidate_fields = (
        deployed.state_count == candidate.state_count,
        deployed.return_aggregation == candidate.return_aggregation,
        deployed.training_window == candidate.training_window,
        deployed.covariance_kind == candidate.covariance_kind,
    )
    frozen_fields = (
        deployed.seeds == spec.seeds,
        deployed.n_iter == spec.n_iter,
        deployed.tol == spec.tol,
        deployed.min_covar == spec.min_covar,
        deployed.min_daily_assets == spec.min_daily_assets,
        deployed.feature_policy == spec.feature_policy,
        deployed.quality_policy == spec.quality_policy,
    )
    if not all((*candidate_fields, *frozen_fields)):
        raise _manifest_mismatch("正式部署配置与已完成研究的冻结配置不一致")
    if deployed.visible_cutoff > spec.research_end:
        raise _manifest_mismatch("部署可见截止日超过研究报告截止日")
    return deployment


def _validated_cutoff(value: str, *, name: str, required: date) -> None:
    """验证 provenance 截止日覆盖正式构建区间。"""

    try:
        cutoff = date.fromisoformat(value)
    except ValueError as error:
        raise _manifest_mismatch(f"{name} 不是 ISO 日期") from error
    if cutoff < required:
        raise _manifest_mismatch(f"{name} 早于正式构建截止日")


def build_regime_snapshot(
    source: RegimeDataSource,
    registered_deployment: RegisteredRegimeDeployment,
    start: date,
    end: date,
    artifact_root: Path,
) -> RegimeBuildResult:
    """从 input-only 面板按固定 K 构建并发布正式状态快照。

    ``start`` 需要覆盖所选训练窗口的历史；函数不会读取标签，也不会在
    月度失败时改选 K、协方差形式或训练窗口。
    """

    if end < start:
        raise ValueError("状态快照 end 不能早于 start")
    provenance = source.inspect_inputs()
    _validated_cutoff(provenance.market_cutoff, name="market_cutoff", required=end)
    _validated_cutoff(
        provenance.state_table_cutoff,
        name="state_table_cutoff",
        required=end,
    )
    panel = source.scan_inputs(RegimeDataRequest(start=start, end=end)).collect()
    deployed = registered_deployment.spec
    features = build_market_features(
        panel,
        deployed.feature_policy,
        deployed.return_aggregation,
        min_daily_assets=deployed.min_daily_assets,
    ).frame
    run_spec = RegimeResearchSpec(
        candidate_state_counts=(deployed.state_count,),
        return_aggregations=(deployed.return_aggregation,),
        training_windows=(deployed.training_window,),
        covariance_kinds=(deployed.covariance_kind,),
        seeds=deployed.seeds,
        n_iter=deployed.n_iter,
        tol=deployed.tol,
        min_covar=deployed.min_covar,
        min_daily_assets=deployed.min_daily_assets,
        min_oos_months=1,
        min_successful_month_ratio=1.0,
        feature_policy=deployed.feature_policy,
        quality_policy=deployed.quality_policy,
        research_start=start,
        research_end=end,
        created_at=deployed.created_at,
    )
    candidate = RegimeCandidateConfig(
        candidate_id=deployed.research_candidate_id,
        state_count=deployed.state_count,
        return_aggregation=deployed.return_aggregation,
        training_window=deployed.training_window,
        covariance_kind=deployed.covariance_kind,
    )
    monthly = run_fixed_regime_candidate(features, run_spec, candidate)
    return publish_regime_snapshot(
        artifact_root=artifact_root,
        deployment=registered_deployment,
        provenance=provenance,
        monthly_results=monthly,
        market_features=features,
        annotations=(),
    )
