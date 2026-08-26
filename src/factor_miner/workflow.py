"""候选登记、计算、评价和终态账本编排。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import polars as pl
from pydantic import BaseModel, ConfigDict

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.barra_attribution import calculate_barra_attribution
from factor_miner.barra_schema import (
    BarraEvaluationPolicy,
    BarraInputIdentity,
    barra_policy_id,
)
from factor_miner.compiler import CompiledFactorPlan, compile_candidate
from factor_miner.compute import (
    FactorArtifact,
    compute_raw_factor,
    compute_trusted_raw_factor,
)
from factor_miner.data_source import (
    DataProvenance,
    DataRequest,
    DataSource,
    FactorInputRequest,
    FactorInputSource,
    InputProvenance,
    MARKET_COLUMNS,
    OutcomeRequest,
    OutcomeSource,
    ReferenceFactorSource,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.evaluation import EvaluationMetrics, evaluate_rank_ic
from factor_miner.ledger import (
    EventType,
    JsonlLedger,
    TrialEvent,
    recover_interrupted_runs,
)
from factor_miner.ic_diagnostics import evaluate_ic_horizons
from factor_miner.lookahead import LookaheadProbeResult, run_lookahead_probes
from factor_miner.incremental import (
    IncrementalInformationResult,
    OrthogonalizedFactor,
    evaluate_incremental_information,
    orthogonalize_candidate,
)
from factor_miner.redundancy import (
    OutputRedundancy,
    check_output_redundancy,
    check_structural_redundancy,
)
from factor_miner.schema import (
    CampaignSpec,
    EvaluationPolicySpec,
    IncrementalEvaluationPolicySpec,
    RegisteredCandidate,
    RegisteredReferenceFactorLibrary,
    RegisteredResearchFamily,
    RegisteredTrustedCandidate,
    TrustedVisibleCampaignSpec,
    campaign_id,
    evaluation_policy_id,
    trusted_campaign_id,
    validate_incremental_policy_library,
    validate_trusted_campaign,
)
from factor_miner.llm_schema import RegisteredLLMDiscoveryResearchFamily
from factor_miner.llm_seal import (
    RegisteredGenerationSeal,
    VerifiedDiscoveryProjection,
    authorize_evaluation_open,
)
from factor_miner.portfolio_artifacts import (
    publish_run_artifacts,
    verify_published_run,
)
from factor_miner.portfolio_evaluation import run_portfolio_backtest
from factor_miner.portfolio_schema import (
    PortfolioEvaluationPolicy,
    portfolio_policy_id,
)
from factor_miner.portfolio_statistics import calculate_portfolio_metrics
from factor_miner.trading_schedule import RebalanceWindow
from factor_miner.statistics import HacInference, hac_mean_test


class CandidateTerminalStatus(StrEnum):
    """候选在一次运行中的明确终态。"""

    SMOKE_PASSED = "smoke_passed"
    VISIBLE_PASSED = "visible_passed"
    VISIBLE_FAILED = "visible_failed"
    COMPILE_FAILED = "compile_failed"
    COMPUTE_FAILED = "compute_failed"
    EVALUATION_FAILED = "evaluation_failed"
    REDUNDANCY_FAILED = "redundancy_failed"
    INCREMENTAL_FAILED = "incremental_failed"
    INTERRUPTED = "interrupted"


class RunResult(BaseModel):
    """一次 smoke 或 visible 运行的可审计结果摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    campaign_id: str
    statuses: dict[str, CandidateTerminalStatus]
    artifact_refs: dict[str, tuple[str, ...]]
    artifact_hashes: dict[str, tuple[str, ...]]
    metrics: dict[str, dict[str, Any]]
    visible_only: bool
    run_manifest_path: str
    run_manifest_sha256: str
    sealed_oos_used: bool = False
    production_eligible: bool = False


@dataclass(frozen=True, slots=True)
class PortfolioSources:
    """服务器侧组合、IC 和 Barra 数据端口的显式输入集合。"""

    candidates: Mapping[str, RegisteredTrustedCandidate]
    generation_family: RegisteredLLMDiscoveryResearchFamily
    generation_projection: VerifiedDiscoveryProjection
    generation_seal: RegisteredGenerationSeal
    portfolio_panels: Mapping[str, pl.LazyFrame]
    ic_panels: Mapping[str, pl.LazyFrame]
    benchmark_returns: pl.LazyFrame
    calendar: pl.DataFrame
    calendar_version: str
    calendar_sha256: str
    schedule: tuple[RebalanceWindow, ...]
    ic_policy: EvaluationPolicySpec
    barra_benchmark_weights: pl.LazyFrame
    barra_exposures: pl.LazyFrame
    barra_factor_returns: pl.LazyFrame
    barra_weights_by_candidate: Mapping[str, pl.LazyFrame]
    barra_identity: BarraInputIdentity


class PublishedRun(BaseModel):
    """原子发布完成后的最终运行目录和清单身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_root: Path
    manifest_path: Path
    manifest_sha256: str
    file_hashes: dict[str, str]


class StagedRunPublisher:
    """在独立暂存目录写完、校验并原子发布一次运行。"""

    def __init__(self, artifact_root: Path, run_id: str) -> None:
        if not run_id.strip() or "/" in run_id:
            raise ValueError("run_id 必须是非空单级名称")
        root = artifact_root.expanduser().resolve(strict=False)
        self.run_id = run_id
        self.staging_root = root / "artifacts" / ".staging" / run_id
        self.final_root = root / "artifacts" / "runs" / run_id
        if self.staging_root.exists() or self.final_root.exists():
            raise FactorMinerError(
                FailureCode.LEDGER_CORRUPT,
                f"运行目录已经存在：{run_id}",
            )
        self.staging_root.mkdir(parents=True)

    def path(self, relative: str) -> Path:
        """返回暂存目录内的安全文件路径并创建父目录。"""

        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.name:
            raise ValueError("运行产物路径必须是安全相对文件路径")
        path = self.staging_root / candidate
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative: str, payload: Any) -> Path:
        """以规范 JSON、换行和 fsync 写入一个暂存文件。"""

        path = self.path(relative)
        if path.exists():
            raise FactorMinerError(FailureCode.LEDGER_CORRUPT, f"产物禁止覆盖：{path}")
        data = canonical_json_bytes(payload) + b"\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return path

    def publish(
        self,
        manifest_base: Mapping[str, Any],
        *,
        before_rename: Callable[[], None] | None = None,
    ) -> PublishedRun:
        """哈希全部文件、写清单并原子重命名为最终目录。"""

        if not self.staging_root.is_dir() or self.final_root.exists():
            raise FactorMinerError(FailureCode.LEDGER_CORRUPT, "暂存或最终目录状态非法")
        files = {
            path.relative_to(self.staging_root).as_posix(): _file_sha256(path)
            for path in sorted(self.staging_root.rglob("*"))
            if path.is_file() and path.name != "run_manifest.json"
        }
        manifest = {**dict(manifest_base), "files": files}
        manifest_path = self.write_json("run_manifest.json", manifest)
        for path in sorted(self.staging_root.rglob("*")):
            if path.is_file():
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        _fsync_directories(self.staging_root)
        if before_rename is not None:
            before_rename()
        self.final_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.staging_root, self.final_root)
        _fsync_directory(self.final_root.parent)
        final_manifest = self.final_root / "run_manifest.json"
        return PublishedRun(
            final_root=self.final_root,
            manifest_path=final_manifest,
            manifest_sha256=_file_sha256(final_manifest),
            file_hashes=files,
        )


def _file_sha256(path: Path) -> str:
    """计算文件完整 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """将单个目录项同步到磁盘。"""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directories(root: Path) -> None:
    """由深到浅同步暂存目录树。"""

    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def validate_trusted_plan_before_outcomes(
    plan: CompiledFactorPlan,
    source: FactorInputSource,
    request: FactorInputRequest,
    *,
    checkpoints: tuple[date, ...],
    seed: int,
) -> tuple[InputProvenance, LookaheadProbeResult]:
    """只通过因子输入端口完成合同与动态未来依赖检查。"""

    provenance = source.inspect_inputs()
    input_frame = source.scan_inputs(request).collect()
    probe = run_lookahead_probes(plan, input_frame, checkpoints, seed)
    return provenance, probe


def prepare_trusted_campaign_plans(
    campaign: TrustedVisibleCampaignSpec,
    candidates: Mapping[str, RegisteredTrustedCandidate],
    policy: EvaluationPolicySpec | IncrementalEvaluationPolicySpec,
    family: RegisteredResearchFamily,
    reference_plans: Mapping[str, CompiledFactorPlan],
) -> dict[str, CompiledFactorPlan]:
    """按冻结顺序编译可信候选并在读取输入前强制结构去重。"""

    try:
        validate_trusted_campaign(campaign, family, policy, candidates)
    except ValueError as error:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, str(error)) from error
    expected_references = policy.reference_factor_ids
    if tuple(reference_plans) != expected_references:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            "结构冗余参考元数据必须完整且保持冻结顺序："
            f"期望 {expected_references}，实际 {tuple(reference_plans)}",
        )
    frozen_references: list[CompiledFactorPlan] = []
    for reference_id in expected_references:
        plan = reference_plans[reference_id]
        if plan.candidate_id != reference_id:
            raise FactorMinerError(
                FailureCode.DATA_RELEASE_MISMATCH,
                f"参考编译元数据 ID 与清单键不一致：{reference_id}",
            )
        frozen_references.append(plan)

    compiled: dict[str, CompiledFactorPlan] = {}
    prior_plans: list[CompiledFactorPlan] = []
    for candidate_id in campaign.candidate_ids:
        plan = compile_candidate(candidates[candidate_id], MARKET_COLUMNS)
        result = check_structural_redundancy(
            plan, (*frozen_references, *prior_plans)
        )
        if not result.passed:
            raise FactorMinerError(
                FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                f"候选 {candidate_id} 与冻结参考或批次内先前候选结构重复",
            )
        compiled[candidate_id] = plan
        prior_plans.append(plan)
    return compiled


def resolved_trusted_evaluation_campaign(
    campaign: TrustedVisibleCampaignSpec,
    policy: EvaluationPolicySpec,
    family: RegisteredResearchFamily,
) -> CampaignSpec:
    """将不可覆盖的可信对象解析为旧评价器所需的只读参数视图。"""

    return CampaignSpec(
        visible_start=campaign.visible_start,
        visible_end=campaign.visible_end,
        candidate_ids=campaign.candidate_ids,
        max_hypotheses=family.spec.global_hypothesis_budget,
        alpha=policy.alpha,
        label_column=policy.label_column,
        rank_mask_column=policy.rank_mask_column,
        hac_max_lags=policy.hac_max_lags,
        min_valid_dates=policy.min_valid_dates,
        min_names_per_date=policy.min_names_per_date,
        min_median_coverage=policy.min_median_coverage,
        max_abs_output_correlation=policy.max_abs_output_correlation,
        reference_factor_columns=policy.reference_factor_ids,
    )


def run_mandatory_output_redundancy(
    candidate_frame: pl.DataFrame,
    campaign: TrustedVisibleCampaignSpec,
    policy: EvaluationPolicySpec,
    family: RegisteredResearchFamily,
    reference_source: ReferenceFactorSource,
    *,
    prior_candidate_frames: Mapping[str, pl.DataFrame],
    candidate_id: str | None = None,
) -> OutputRedundancy:
    """加载完整冻结参考池与先前候选，执行不可跳过的输出冗余检查。"""

    provenance = reference_source.inspect_references(policy.reference_manifest_id)
    if provenance.manifest_id != policy.reference_manifest_id:
        raise FactorMinerError(
            FailureCode.DATA_RELEASE_MISMATCH,
            "参考因子清单 ID 与冻结政策不一致",
        )
    if provenance.factor_ids != policy.reference_factor_ids:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            "参考因子集合不完整或顺序与冻结政策不一致",
        )
    reference_frames: dict[str, pl.DataFrame] = {}
    for reference_id in policy.reference_factor_ids:
        reference_frames[reference_id] = reference_source.scan_reference(
            reference_id, campaign.visible_start, campaign.visible_end
        ).collect()
    for prior_id, frame in prior_candidate_frames.items():
        reference_frames[f"campaign:{prior_id}"] = frame
    if len(reference_frames) != len(policy.reference_factor_ids) + len(
        prior_candidate_frames
    ):
        raise FactorMinerError(FailureCode.FIELD_MISSING, "输出冗余比较集合不完整")
    return check_output_redundancy(
        candidate_frame,
        reference_frames,
        resolved_trusted_evaluation_campaign(campaign, policy, family),
        candidate_column="raw_factor",
        candidate_id=candidate_id,
    )


def run_trusted_visible_campaign(
    campaign: TrustedVisibleCampaignSpec,
    candidates: Mapping[str, RegisteredTrustedCandidate],
    policy: EvaluationPolicySpec,
    family: RegisteredResearchFamily,
    input_source: FactorInputSource,
    outcome_source: OutcomeSource,
    reference_source: ReferenceFactorSource,
    reference_plans: Mapping[str, CompiledFactorPlan],
    artifact_root: Path,
    *,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str,
    probe_seed: int = 17,
) -> RunResult:
    """执行完整 V0.1 可信可见验证，并在发布后追加终态事件。"""

    root = artifact_root.expanduser().resolve(strict=False)
    recover_interrupted_runs(root)
    try:
        validate_trusted_campaign(campaign, family, policy, candidates)
    except ValueError as error:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, str(error)) from error
    if not code_hash.strip() or not config_hash.strip() or not uv_lock_hash.strip():
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "运行身份哈希不能为空")

    ledger = JsonlLedger(root)
    campaign_identifier = trusted_campaign_id(campaign)
    ledger.register_evaluation_policy(policy)
    ledger.register_research_family(family)
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        ledger.register_candidate(candidate)
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                event_type=EventType.CANDIDATE_REGISTERED,
                status="registered",
                spec_hash=candidate.spec_hash,
                code_hash=code_hash,
                config_hash=config_hash,
            )
        )
    ledger.register_campaign(campaign)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            event_type=EventType.CAMPAIGN_REGISTERED,
            status="registered",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    run_id = f"run_{uuid4().hex}"
    publisher = StagedRunPublisher(root, run_id)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            status="started",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )

    plans = prepare_trusted_campaign_plans(
        campaign, candidates, policy, family, reference_plans
    )
    request = FactorInputRequest(
        start=campaign.visible_start,
        end=campaign.visible_end,
        required_fields=tuple(
            sorted({field for plan in plans.values() for field in plan.required_fields})
        ),
        warmup_observations=max(plan.required_lookback for plan in plans.values()),
    )
    input_provenance = input_source.inspect_inputs()
    input_frame = input_source.scan_inputs(request).collect()
    checkpoints = _trusted_probe_checkpoints(input_frame, campaign)
    artifacts: dict[str, FactorArtifact] = {}
    probes: dict[str, LookaheadProbeResult] = {}
    raw_frames: dict[str, pl.DataFrame] = {}
    for candidate_id, plan in plans.items():
        probe = run_lookahead_probes(plan, input_frame, checkpoints, probe_seed)
        probes[candidate_id] = probe
        base = f"candidates/{candidate_id}"
        publisher.write_json(f"{base}/execution_plan.json", plan.model_dump(mode="json"))
        publisher.write_json(f"{base}/lookahead_probe.json", probe.model_dump(mode="json"))
        artifact = compute_trusted_raw_factor(
            plan,
            input_source,
            request,
            publisher.path(f"{base}/raw_factor.parquet"),
        )
        artifacts[candidate_id] = artifact
        raw_frames[candidate_id] = pl.read_parquet(artifact.artifact_path).select(
            ["date", "asset", "raw_factor"]
        )
        publisher.write_json(f"{base}/quality.json", artifact.quality.model_dump(mode="json"))

    outcome_provenance = outcome_source.inspect_outcomes()
    outcome_frame = outcome_source.scan_outcomes(
        OutcomeRequest(campaign.visible_start, campaign.visible_end)
    ).collect()
    reference_provenance = reference_source.inspect_references(
        policy.reference_manifest_id
    )
    resolved_campaign = resolved_trusted_evaluation_campaign(campaign, policy, family)
    statuses: dict[str, CandidateTerminalStatus] = {}
    metrics: dict[str, dict[str, Any]] = {}
    terminal_codes: dict[str, FailureCode | None] = {}
    prior_frames: dict[str, pl.DataFrame] = {}
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        artifact = artifacts[candidate_id]
        base = f"candidates/{candidate_id}"
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=EventType.OUTCOME_EXPOSED,
                status="outcome_exposed",
                outcome_exposed=True,
                data_hash=artifact.artifact_sha256,
            )
        )
        try:
            evaluation_frame = (
                input_frame.filter(
                    pl.col("date").is_between(
                        campaign.visible_start, campaign.visible_end, closed="both"
                    )
                )
                .select(["date", "asset", policy.rank_mask_column])
                .join(outcome_frame, on=["date", "asset"], how="inner", validate="1:1")
                .join(raw_frames[candidate_id], on=["date", "asset"], how="inner", validate="1:1")
            )
            evaluation = evaluate_rank_ic(evaluation_frame, resolved_campaign)
            if evaluation.valid_dates < policy.min_valid_dates:
                raise FactorMinerError(
                    FailureCode.INSUFFICIENT_VALID_DATES,
                    "有效 RankIC 日期不足",
                )
            if evaluation.median_coverage < policy.min_median_coverage:
                raise FactorMinerError(
                    FailureCode.FACTOR_COVERAGE_TOO_LOW,
                    "RankIC 覆盖率低于冻结政策",
                )
            inference = hac_mean_test(evaluation.rank_ic_values, resolved_campaign)
            redundancy = run_mandatory_output_redundancy(
                raw_frames[candidate_id],
                campaign,
                policy,
                family,
                reference_source,
                prior_candidate_frames=prior_frames,
                candidate_id=candidate_id,
            )
            publisher.write_json(
                f"{base}/evaluation.json", evaluation.model_dump(mode="json")
            )
            publisher.write_json(
                f"{base}/inference.json", inference.model_dump(mode="json")
            )
            publisher.write_json(
                f"{base}/redundancy.json", redundancy.model_dump(mode="json")
            )
            metrics[candidate_id] = {
                "evaluation": evaluation.model_dump(mode="json"),
                "inference": inference.model_dump(mode="json"),
                "redundancy": redundancy.model_dump(mode="json"),
            }
            ledger.append_event(
                TrialEvent(
                    candidate_id=candidate_id,
                    campaign_id=campaign_identifier,
                    run_id=run_id,
                    event_type=EventType.EVALUATION_COMPLETED,
                    status="evaluation_completed",
                    outcome_exposed=True,
                    data_hash=artifact.artifact_sha256,
                )
            )
            direction = candidate.spec.hypothesis.expected_sign.value
            direction_pass = inference.mean > 0 if direction == "positive" else inference.mean < 0
            statistical_pass = (
                direction_pass
                and inference.bonferroni_p_value <= policy.alpha
                and abs(inference.mean) >= policy.min_abs_mean_rank_ic
            )
            if not redundancy.passed:
                status = CandidateTerminalStatus.REDUNDANCY_FAILED
                code = FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
            elif not statistical_pass:
                status = CandidateTerminalStatus.VISIBLE_FAILED
                code = None
            else:
                status = CandidateTerminalStatus.VISIBLE_PASSED
                code = None
        except FactorMinerError as error:
            publisher.write_json(f"{base}/evaluation.json", {"error": str(error)})
            publisher.write_json(f"{base}/inference.json", {"error": str(error)})
            publisher.write_json(
                f"{base}/redundancy.json",
                {"passed": False, "error": "评价失败后不允许通过"},
            )
            metrics[candidate_id] = {"error": str(error)}
            status = CandidateTerminalStatus.EVALUATION_FAILED
            code = error.code
        statuses[candidate_id] = status
        terminal_codes[candidate_id] = code
        publisher.write_json(
            f"{base}/candidate_package.json",
            {
                "candidate_id": candidate_id,
                "status": status.value,
                "visible_only": True,
                "sealed_oos_used": False,
                "production_eligible": False,
                "mechanism_status": "mechanism_unverified",
            },
        )
        prior_frames[candidate_id] = raw_frames[candidate_id]

    published = publisher.publish(
        {
            "run_id": run_id,
            "campaign_id": campaign_identifier,
            "campaign_spec_hash": sha256_json(campaign.model_dump(mode="json")),
            "evaluation_policy_id": campaign.evaluation_policy_id,
            "research_family_id": campaign.research_family_id,
            "code_commit": code_hash,
            "config_hash": config_hash,
            "uv_lock_sha256": uv_lock_hash,
            "input_provenance": asdict(input_provenance),
            "outcome_provenance": asdict(outcome_provenance),
            "reference_provenance": asdict(reference_provenance),
            "statuses": {key: value.value for key, value in statuses.items()},
        }
    )
    artifact_refs: dict[str, tuple[str, ...]] = {}
    artifact_hashes: dict[str, tuple[str, ...]] = {}
    for candidate_id, status in statuses.items():
        prefix = f"candidates/{candidate_id}/"
        relatives = tuple(
            relative for relative in published.file_hashes if relative.startswith(prefix)
        )
        refs = tuple(str(published.final_root / relative) for relative in relatives)
        hashes = tuple(published.file_hashes[relative] for relative in relatives)
        artifact_refs[candidate_id] = refs
        artifact_hashes[candidate_id] = hashes
        event_type = {
            CandidateTerminalStatus.VISIBLE_PASSED: EventType.VISIBLE_PASSED,
            CandidateTerminalStatus.VISIBLE_FAILED: EventType.VISIBLE_FAILED,
            CandidateTerminalStatus.REDUNDANCY_FAILED: EventType.REDUNDANCY_FAILED,
            CandidateTerminalStatus.EVALUATION_FAILED: EventType.EVALUATION_FAILED,
        }[status]
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=event_type,
                status=status.value,
                outcome_exposed=True,
                failure_code=terminal_codes[candidate_id],
                data_hash=artifacts[candidate_id].artifact_sha256,
                artifact_refs=refs,
            )
        )
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_COMPLETED,
            status="completed",
            outcome_exposed=True,
            artifact_refs=(str(published.manifest_path),),
        )
    )
    return RunResult(
        run_id=run_id,
        campaign_id=campaign_identifier,
        statuses=statuses,
        artifact_refs=artifact_refs,
        artifact_hashes=artifact_hashes,
        metrics=metrics,
        visible_only=True,
        run_manifest_path=str(published.manifest_path),
        run_manifest_sha256=published.manifest_sha256,
        sealed_oos_used=False,
        production_eligible=False,
    )


def run_incremental_visible_campaign(
    campaign: TrustedVisibleCampaignSpec,
    candidates: Mapping[str, RegisteredTrustedCandidate],
    policy: IncrementalEvaluationPolicySpec,
    family: RegisteredResearchFamily,
    reference_library: RegisteredReferenceFactorLibrary,
    input_source: FactorInputSource,
    outcome_source: OutcomeSource,
    reference_source: ReferenceFactorSource,
    reference_plans: Mapping[str, CompiledFactorPlan],
    artifact_root: Path,
    *,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str,
    probe_seed: int = 17,
) -> RunResult:
    """执行 V0.2 可见增量信息评价，并保持 V0.1 历史语义不变。"""

    try:
        validate_incremental_policy_library(policy, reference_library)
    except ValueError as error:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            str(error),
        ) from error
    try:
        validate_trusted_campaign(campaign, family, policy, candidates)
    except ValueError as error:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, str(error)) from error
    if not code_hash.strip() or not config_hash.strip() or not uv_lock_hash.strip():
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "运行身份哈希不能为空")

    root = artifact_root.expanduser().resolve(strict=False)
    recover_interrupted_runs(root)
    ledger = JsonlLedger(root)
    campaign_identifier = trusted_campaign_id(campaign)
    ledger.register_reference_factor_library(reference_library)
    ledger.register_evaluation_policy(policy)
    ledger.register_research_family(family)
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        ledger.register_candidate(candidate)
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                event_type=EventType.CANDIDATE_REGISTERED,
                status="registered",
                spec_hash=candidate.spec_hash,
                code_hash=code_hash,
                config_hash=config_hash,
            )
        )
    ledger.register_campaign(campaign)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            event_type=EventType.CAMPAIGN_REGISTERED,
            status="registered",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    run_id = f"run_{uuid4().hex}"
    publisher = StagedRunPublisher(root, run_id)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            status="started",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )

    plans = prepare_trusted_campaign_plans(
        campaign,
        candidates,
        policy,
        family,
        reference_plans,
    )
    request = FactorInputRequest(
        start=campaign.visible_start,
        end=campaign.visible_end,
        required_fields=tuple(
            sorted(
                {
                    field
                    for plan in plans.values()
                    for field in plan.required_fields
                }
            )
        ),
        warmup_observations=max(
            plan.required_lookback for plan in plans.values()
        ),
    )
    input_provenance = input_source.inspect_inputs()
    input_frame = input_source.scan_inputs(request).collect()
    checkpoints = _trusted_probe_checkpoints(input_frame, campaign)
    artifacts: dict[str, FactorArtifact] = {}
    raw_frames: dict[str, pl.DataFrame] = {}
    for candidate_id, plan in plans.items():
        base = f"candidates/{candidate_id}"
        probe = run_lookahead_probes(plan, input_frame, checkpoints, probe_seed)
        publisher.write_json(
            f"{base}/execution_plan.json",
            plan.model_dump(mode="json"),
        )
        publisher.write_json(
            f"{base}/lookahead_probe.json",
            probe.model_dump(mode="json"),
        )
        artifact = compute_trusted_raw_factor(
            plan,
            input_source,
            request,
            publisher.path(f"{base}/raw_factor.parquet"),
        )
        artifacts[candidate_id] = artifact
        raw_frames[candidate_id] = pl.read_parquet(
            artifact.artifact_path
        ).select(["date", "asset", "raw_factor"])
        publisher.write_json(
            f"{base}/quality.json",
            artifact.quality.model_dump(mode="json"),
        )

    reference_provenance = reference_source.inspect_references(
        reference_library.spec.reference_manifest_id
    )
    if (
        reference_provenance.manifest_id
        != reference_library.spec.reference_manifest_id
        or reference_provenance.manifest_sha256
        != reference_library.spec.reference_manifest_sha256
        or reference_provenance.factor_ids
        != reference_library.spec.reference_factor_ids
    ):
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "参考数据端口与冻结参考因子库不一致",
        )
    frozen_reference_frames = {
        reference_id: reference_source.scan_reference(
            reference_id,
            campaign.visible_start,
            campaign.visible_end,
        ).collect()
        for reference_id in reference_library.spec.reference_factor_ids
    }
    if tuple(frozen_reference_frames) != reference_library.spec.reference_factor_ids:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "参考因子值集合不完整或顺序不一致",
        )
    resolved_campaign = resolved_trusted_evaluation_campaign(
        campaign,
        policy,
        family,
    )
    eligibility_frame = input_frame.select(
        ["date", "asset", policy.rank_mask_column]
    )
    orthogonalized: dict[str, OrthogonalizedFactor] = {}
    redundancies: dict[str, OutputRedundancy] = {}
    prior_frames: dict[str, pl.DataFrame] = {}
    for candidate_id in campaign.candidate_ids:
        basis_frames = {
            **frozen_reference_frames,
            **{
                f"campaign:{prior_id}": frame
                for prior_id, frame in prior_frames.items()
            },
        }
        redundancies[candidate_id] = check_output_redundancy(
            raw_frames[candidate_id],
            basis_frames,
            resolved_campaign,
            candidate_column="raw_factor",
            candidate_id=candidate_id,
        )
        orthogonalized[candidate_id] = orthogonalize_candidate(
            raw_frames[candidate_id],
            basis_frames,
            eligibility_frame,
            resolved_campaign,
            policy.orthogonalization,
        )
        prior_frames[candidate_id] = raw_frames[candidate_id]

    outcome_provenance = outcome_source.inspect_outcomes()
    outcome_frame = outcome_source.scan_outcomes(
        OutcomeRequest(campaign.visible_start, campaign.visible_end)
    ).collect()
    statuses: dict[str, CandidateTerminalStatus] = {}
    metrics: dict[str, dict[str, Any]] = {}
    terminal_codes: dict[str, FailureCode | None] = {}
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        artifact = artifacts[candidate_id]
        base = f"candidates/{candidate_id}"
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=EventType.OUTCOME_EXPOSED,
                status="outcome_exposed",
                outcome_exposed=True,
                data_hash=artifact.artifact_sha256,
            )
        )
        try:
            evaluation_frame = (
                input_frame.filter(
                    pl.col("date").is_between(
                        campaign.visible_start,
                        campaign.visible_end,
                        closed="both",
                    )
                )
                .select(["date", "asset", policy.rank_mask_column])
                .join(
                    outcome_frame,
                    on=["date", "asset"],
                    how="inner",
                    validate="1:1",
                )
                .join(
                    raw_frames[candidate_id],
                    on=["date", "asset"],
                    how="inner",
                    validate="1:1",
                )
            )
            evaluation = evaluate_rank_ic(
                evaluation_frame,
                resolved_campaign,
            )
            if evaluation.valid_dates < policy.min_valid_dates:
                raise FactorMinerError(
                    FailureCode.INSUFFICIENT_VALID_DATES,
                    "有效原始 RankIC 日期不足",
                )
            if evaluation.median_coverage < policy.min_median_coverage:
                raise FactorMinerError(
                    FailureCode.FACTOR_COVERAGE_TOO_LOW,
                    "原始 RankIC 覆盖率低于冻结政策",
                )
            inference = hac_mean_test(
                evaluation.rank_ic_values,
                resolved_campaign,
            )
            incremental = evaluate_incremental_information(
                orthogonalized[candidate_id],
                evaluation_frame.select(
                    [
                        "date",
                        "asset",
                        policy.rank_mask_column,
                        policy.label_column,
                    ]
                ),
                resolved_campaign,
                candidate.spec.hypothesis.expected_sign,
                policy.orthogonalization,
            )
            redundancy = redundancies[candidate_id]
            publisher.write_json(
                f"{base}/evaluation.json",
                evaluation.model_dump(mode="json"),
            )
            publisher.write_json(
                f"{base}/inference.json",
                inference.model_dump(mode="json"),
            )
            publisher.write_json(
                f"{base}/redundancy.json",
                redundancy.model_dump(mode="json"),
            )
            publisher.write_json(
                f"{base}/incremental_information.json",
                incremental.model_dump(mode="json"),
            )
            metrics[candidate_id] = {
                "evaluation": evaluation.model_dump(mode="json"),
                "inference": inference.model_dump(mode="json"),
                "redundancy": redundancy.model_dump(mode="json"),
                "incremental_information": incremental.model_dump(mode="json"),
            }
            ledger.append_event(
                TrialEvent(
                    candidate_id=candidate_id,
                    campaign_id=campaign_identifier,
                    run_id=run_id,
                    event_type=EventType.EVALUATION_COMPLETED,
                    status="evaluation_completed",
                    outcome_exposed=True,
                    data_hash=artifact.artifact_sha256,
                )
            )
            direction = candidate.spec.hypothesis.expected_sign.value
            raw_direction_passed = (
                inference.mean > 0
                if direction == "positive"
                else inference.mean < 0
            )
            raw_statistics_passed = (
                raw_direction_passed
                and inference.bonferroni_p_value <= policy.alpha
                and abs(inference.mean) >= policy.min_abs_mean_rank_ic
            )
            if not redundancy.passed:
                status = CandidateTerminalStatus.REDUNDANCY_FAILED
                code = FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
            elif not raw_statistics_passed:
                status = CandidateTerminalStatus.VISIBLE_FAILED
                code = None
            elif not incremental.passed:
                status = CandidateTerminalStatus.INCREMENTAL_FAILED
                code = FailureCode.INCREMENTAL_INFORMATION_INSUFFICIENT
            else:
                status = CandidateTerminalStatus.VISIBLE_PASSED
                code = None
        except FactorMinerError as error:
            publisher.write_json(
                f"{base}/evaluation.json",
                {"error": str(error)},
            )
            publisher.write_json(
                f"{base}/inference.json",
                {"error": str(error)},
            )
            publisher.write_json(
                f"{base}/redundancy.json",
                {"passed": False, "error": "评价失败后不允许通过"},
            )
            publisher.write_json(
                f"{base}/incremental_information.json",
                {"passed": False, "error": "评价失败后不允许通过"},
            )
            metrics[candidate_id] = {"error": str(error)}
            status = CandidateTerminalStatus.EVALUATION_FAILED
            code = error.code
        statuses[candidate_id] = status
        terminal_codes[candidate_id] = code
        incremental_result = metrics[candidate_id].get(
            "incremental_information",
            {},
        )
        publisher.write_json(
            f"{base}/candidate_package.json",
            {
                "candidate_id": candidate_id,
                "status": status.value,
                "visible_only": True,
                "visible_incremental_only": True,
                "incremental_information_evaluated": (
                    "incremental_information" in metrics[candidate_id]
                ),
                "incremental_information_passed": incremental_result.get(
                    "passed",
                    False,
                ),
                "reference_factor_library_id": (
                    reference_library.reference_factor_library_id
                ),
                "sealed_oos_used": False,
                "production_eligible": False,
                "mechanism_status": "mechanism_unverified",
            },
        )

    published = publisher.publish(
        {
            "run_id": run_id,
            "campaign_id": campaign_identifier,
            "campaign_spec_hash": sha256_json(campaign.model_dump(mode="json")),
            "evaluation_policy_id": campaign.evaluation_policy_id,
            "research_family_id": campaign.research_family_id,
            "reference_factor_library_id": (
                reference_library.reference_factor_library_id
            ),
            "reference_factor_library_spec_hash": reference_library.spec_hash,
            "code_commit": code_hash,
            "config_hash": config_hash,
            "uv_lock_sha256": uv_lock_hash,
            "input_provenance": asdict(input_provenance),
            "outcome_provenance": asdict(outcome_provenance),
            "reference_provenance": asdict(reference_provenance),
            "statuses": {
                key: value.value for key, value in statuses.items()
            },
        }
    )
    artifact_refs: dict[str, tuple[str, ...]] = {}
    artifact_hashes: dict[str, tuple[str, ...]] = {}
    for candidate_id, status in statuses.items():
        prefix = f"candidates/{candidate_id}/"
        relatives = tuple(
            relative
            for relative in published.file_hashes
            if relative.startswith(prefix)
        )
        refs = tuple(
            str(published.final_root / relative) for relative in relatives
        )
        hashes = tuple(
            published.file_hashes[relative] for relative in relatives
        )
        artifact_refs[candidate_id] = refs
        artifact_hashes[candidate_id] = hashes
        event_type = {
            CandidateTerminalStatus.VISIBLE_PASSED: EventType.VISIBLE_PASSED,
            CandidateTerminalStatus.VISIBLE_FAILED: EventType.VISIBLE_FAILED,
            CandidateTerminalStatus.REDUNDANCY_FAILED: EventType.REDUNDANCY_FAILED,
            CandidateTerminalStatus.INCREMENTAL_FAILED: EventType.INCREMENTAL_FAILED,
            CandidateTerminalStatus.EVALUATION_FAILED: EventType.EVALUATION_FAILED,
        }[status]
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=event_type,
                status=status.value,
                outcome_exposed=True,
                failure_code=terminal_codes[candidate_id],
                data_hash=artifacts[candidate_id].artifact_sha256,
                artifact_refs=refs,
            )
        )
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_COMPLETED,
            status="completed",
            outcome_exposed=True,
            artifact_refs=(str(published.manifest_path),),
        )
    )
    return RunResult(
        run_id=run_id,
        campaign_id=campaign_identifier,
        statuses=statuses,
        artifact_refs=artifact_refs,
        artifact_hashes=artifact_hashes,
        metrics=metrics,
        visible_only=True,
        run_manifest_path=str(published.manifest_path),
        run_manifest_sha256=published.manifest_sha256,
        sealed_oos_used=False,
        production_eligible=False,
    )


def run_trusted_smoke_campaign(
    campaign: TrustedVisibleCampaignSpec,
    candidates: Mapping[str, RegisteredTrustedCandidate],
    policy: EvaluationPolicySpec | IncrementalEvaluationPolicySpec,
    family: RegisteredResearchFamily,
    input_source: FactorInputSource,
    reference_plans: Mapping[str, CompiledFactorPlan],
    artifact_root: Path,
    *,
    reference_library: RegisteredReferenceFactorLibrary | None = None,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str,
    probe_seed: int = 17,
) -> RunResult:
    """执行不构造结果端口的 V0.1/V0.2 有界输入冒烟运行。"""

    if isinstance(policy, IncrementalEvaluationPolicySpec):
        if reference_library is None:
            raise FactorMinerError(
                FailureCode.REFERENCE_LIBRARY_MISMATCH,
                "V0.2 smoke 缺少冻结参考因子库",
            )
        try:
            validate_incremental_policy_library(policy, reference_library)
        except ValueError as error:
            raise FactorMinerError(
                FailureCode.REFERENCE_LIBRARY_MISMATCH,
                str(error),
            ) from error
    elif reference_library is not None:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "V0.1 smoke 不接受 V0.2 参考因子库",
        )
    root = artifact_root.expanduser().resolve(strict=False)
    recover_interrupted_runs(root)
    try:
        validate_trusted_campaign(campaign, family, policy, candidates)
    except ValueError as error:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, str(error)) from error
    ledger = JsonlLedger(root)
    campaign_identifier = trusted_campaign_id(campaign)
    ledger.register_evaluation_policy(policy)
    if reference_library is not None:
        ledger.register_reference_factor_library(reference_library)
    ledger.register_research_family(family)
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        ledger.register_candidate(candidate)
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                event_type=EventType.CANDIDATE_REGISTERED,
                status="registered",
                spec_hash=candidate.spec_hash,
                code_hash=code_hash,
                config_hash=config_hash,
            )
        )
    ledger.register_campaign(campaign)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            event_type=EventType.CAMPAIGN_REGISTERED,
            status="registered",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    run_id = f"run_{uuid4().hex}"
    publisher = StagedRunPublisher(root, run_id)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            status="started",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    plans = prepare_trusted_campaign_plans(
        campaign, candidates, policy, family, reference_plans
    )
    request = FactorInputRequest(
        start=campaign.visible_start,
        end=campaign.visible_end,
        required_fields=tuple(
            sorted({field for plan in plans.values() for field in plan.required_fields})
        ),
        warmup_observations=max(plan.required_lookback for plan in plans.values()),
    )
    input_provenance = input_source.inspect_inputs()
    input_frame = input_source.scan_inputs(request).collect()
    checkpoints = _trusted_probe_checkpoints(input_frame, campaign)
    artifacts: dict[str, FactorArtifact] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for candidate_id, plan in plans.items():
        base = f"candidates/{candidate_id}"
        probe = run_lookahead_probes(plan, input_frame, checkpoints, probe_seed)
        publisher.write_json(f"{base}/execution_plan.json", plan.model_dump(mode="json"))
        publisher.write_json(f"{base}/lookahead_probe.json", probe.model_dump(mode="json"))
        first = compute_trusted_raw_factor(
            plan,
            input_source,
            request,
            publisher.path(f"{base}/raw_factor.parquet"),
        )
        replay_path = publisher.path(f"{base}/raw_factor.replay.parquet")
        second = compute_trusted_raw_factor(plan, input_source, request, replay_path)
        if first.artifact_sha256 != second.artifact_sha256:
            raise FactorMinerError(
                FailureCode.FACTOR_DETERMINISM_FAILED,
                "冒烟运行两次原始因子哈希不一致",
            )
        replay_path.unlink()
        artifacts[candidate_id] = first
        publisher.write_json(f"{base}/quality.json", first.quality.model_dump(mode="json"))
        publisher.write_json(f"{base}/evaluation.json", {"not_applicable": "smoke_input_only"})
        publisher.write_json(f"{base}/inference.json", {"not_applicable": "smoke_input_only"})
        publisher.write_json(
            f"{base}/redundancy.json",
            {"structural_passed": True, "output_not_applicable": "smoke_input_only"},
        )
        if reference_library is not None:
            publisher.write_json(
                f"{base}/incremental_information.json",
                {"not_applicable": "smoke_input_only"},
            )
        publisher.write_json(
            f"{base}/candidate_package.json",
            {
                "candidate_id": candidate_id,
                "status": CandidateTerminalStatus.SMOKE_PASSED.value,
                "visible_only": False,
                "sealed_oos_used": False,
                "production_eligible": False,
                "mechanism_status": "mechanism_unverified",
            },
        )
        metrics[candidate_id] = {
            "quality": first.quality.model_dump(mode="json"),
            "lookahead_probe": probe.model_dump(mode="json"),
        }
    statuses = {
        candidate_id: CandidateTerminalStatus.SMOKE_PASSED
        for candidate_id in campaign.candidate_ids
    }
    published = publisher.publish(
        {
            "run_id": run_id,
            "campaign_id": campaign_identifier,
            "campaign_spec_hash": sha256_json(campaign.model_dump(mode="json")),
            "evaluation_policy_id": campaign.evaluation_policy_id,
            "research_family_id": campaign.research_family_id,
            "reference_factor_library_id": (
                reference_library.reference_factor_library_id
                if reference_library is not None
                else None
            ),
            "code_commit": code_hash,
            "config_hash": config_hash,
            "uv_lock_sha256": uv_lock_hash,
            "input_provenance": asdict(input_provenance),
            "outcomes_opened": False,
            "statuses": {key: value.value for key, value in statuses.items()},
        }
    )
    artifact_refs: dict[str, tuple[str, ...]] = {}
    artifact_hashes: dict[str, tuple[str, ...]] = {}
    for candidate_id in campaign.candidate_ids:
        prefix = f"candidates/{candidate_id}/"
        relatives = tuple(
            relative for relative in published.file_hashes if relative.startswith(prefix)
        )
        refs = tuple(str(published.final_root / relative) for relative in relatives)
        hashes = tuple(published.file_hashes[relative] for relative in relatives)
        artifact_refs[candidate_id] = refs
        artifact_hashes[candidate_id] = hashes
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=EventType.SMOKE_PASSED,
                status="smoke_passed",
                outcome_exposed=False,
                data_hash=artifacts[candidate_id].artifact_sha256,
                artifact_refs=refs,
            )
        )
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_COMPLETED,
            status="completed",
            outcome_exposed=False,
            artifact_refs=(str(published.manifest_path),),
        )
    )
    return RunResult(
        run_id=run_id,
        campaign_id=campaign_identifier,
        statuses=statuses,
        artifact_refs=artifact_refs,
        artifact_hashes=artifact_hashes,
        metrics=metrics,
        visible_only=False,
        run_manifest_path=str(published.manifest_path),
        run_manifest_sha256=published.manifest_sha256,
        sealed_oos_used=False,
        production_eligible=False,
    )


def _trusted_probe_checkpoints(
    input_frame: pl.DataFrame,
    campaign: TrustedVisibleCampaignSpec,
) -> tuple[date, ...]:
    """从冻结可见区间确定两个保留未来行的检查点。"""

    dates = (
        input_frame.filter(
            pl.col("date").is_between(
                campaign.visible_start, campaign.visible_end, closed="both"
            )
        )
        .get_column("date")
        .unique()
        .sort()
        .to_list()
    )
    if len(dates) < 3:
        raise FactorMinerError(
            FailureCode.FACTOR_COVERAGE_TOO_LOW,
            "动态未来依赖探针至少需要三个可见日期",
        )
    indexes = sorted({max(0, len(dates) // 3), max(0, (2 * len(dates)) // 3)})
    return tuple(dates[min(index, len(dates) - 2)] for index in indexes)


def run_smoke_campaign(
    campaign: CampaignSpec,
    candidates: Mapping[str, RegisteredCandidate],
    source: DataSource,
    artifact_root: Path,
    *,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str = "",
) -> RunResult:
    """运行不做统计推断的确定性 smoke campaign。"""

    context = _start_run(campaign, candidates, artifact_root, code_hash, config_hash)
    data_provenance = source.inspect()
    request = _request_for(campaign, candidates)
    statuses: dict[str, CandidateTerminalStatus] = {}
    artifact_refs: dict[str, tuple[str, ...]] = {}
    artifact_hashes: dict[str, tuple[str, ...]] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        try:
            plan = _compile(candidate)
        except FactorMinerError as error:
            _failure(
                context.ledger,
                context.run_id,
                context.campaign_id,
                candidate,
                EventType.COMPILE_FAILED,
                error,
            )
            statuses[candidate_id] = CandidateTerminalStatus.COMPILE_FAILED
            continue
        try:
            first, second = _compute_twice(
                plan,
                source,
                request,
                context.run_directory,
                candidate_id,
            )
            if first.artifact_sha256 != second.artifact_sha256:
                raise FactorMinerError(
                    FailureCode.FACTOR_DETERMINISM_FAILED,
                    "相同 smoke 输入的两次 raw factor 内容哈希不一致",
                )
        except FactorMinerError as error:
            _failure(
                context.ledger,
                context.run_id,
                context.campaign_id,
                candidate,
                EventType.COMPUTE_FAILED,
                error,
            )
            statuses[candidate_id] = CandidateTerminalStatus.COMPUTE_FAILED
            continue
        refs = (str(first.artifact_path), str(second.artifact_path))
        hashes = (first.artifact_sha256, second.artifact_sha256)
        _outcome_exposed(
            context.ledger,
            context.run_id,
            context.campaign_id,
            candidate,
            refs,
            hashes[0],
        )
        context.ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=context.campaign_id,
                run_id=context.run_id,
                event_type=EventType.EVALUATION_COMPLETED,
                status="smoke_deterministic",
                outcome_exposed=True,
                data_hash=hashes[0],
                artifact_refs=refs,
            )
        )
        statuses[candidate_id] = CandidateTerminalStatus.SMOKE_PASSED
        artifact_refs[candidate_id] = refs
        artifact_hashes[candidate_id] = hashes
        metrics[candidate_id] = _quality_metrics(first)
    return _run_result(
        context,
        statuses,
        artifact_refs,
        artifact_hashes,
        metrics,
        visible_only=False,
        campaign=campaign,
        data_provenance=data_provenance,
        code_hash=code_hash,
        config_hash=config_hash,
        uv_lock_hash=uv_lock_hash,
    )


def run_visible_campaign(
    campaign: CampaignSpec,
    candidates: Mapping[str, RegisteredCandidate],
    source: DataSource,
    artifact_root: Path,
    *,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str = "",
    reference_frames: Mapping[str, pl.DataFrame] | None = None,
) -> RunResult:
    """运行带 RankIC、HAC、Bonferroni 和冗余闸门的 visible campaign。"""

    expected_reference_names = campaign.reference_factor_columns
    actual_reference_names = tuple(reference_frames) if reference_frames is not None else ()
    if actual_reference_names != expected_reference_names:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            "可见验证必须提供完整且顺序一致的冻结参考池："
            f"期望 {expected_reference_names}，实际 {actual_reference_names}",
        )

    context = _start_run(campaign, candidates, artifact_root, code_hash, config_hash)
    data_provenance = source.inspect()
    request = _request_for(campaign, candidates)
    statuses: dict[str, CandidateTerminalStatus] = {}
    artifact_refs: dict[str, tuple[str, ...]] = {}
    artifact_hashes: dict[str, tuple[str, ...]] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        try:
            plan = _compile(candidate)
        except FactorMinerError as error:
            _failure(
                context.ledger,
                context.run_id,
                context.campaign_id,
                candidate,
                EventType.COMPILE_FAILED,
                error,
            )
            statuses[candidate_id] = CandidateTerminalStatus.COMPILE_FAILED
            continue
        try:
            artifact = compute_raw_factor(
                plan,
                source,
                request,
                context.run_directory / f"{candidate_id}.raw.parquet",
            )
        except FactorMinerError as error:
            _failure(
                context.ledger,
                context.run_id,
                context.campaign_id,
                candidate,
                EventType.COMPUTE_FAILED,
                error,
                outcome_exposed=True,
            )
            statuses[candidate_id] = CandidateTerminalStatus.COMPUTE_FAILED
            continue
        refs = (str(artifact.artifact_path),)
        artifact_refs[candidate_id] = refs
        artifact_hashes[candidate_id] = (artifact.artifact_sha256,)
        _outcome_exposed(
            context.ledger,
            context.run_id,
            context.campaign_id,
            candidate,
            refs,
            artifact.artifact_sha256,
        )
        evaluation: EvaluationMetrics | None = None
        try:
            evaluation_frame = _evaluation_frame(source, request, artifact)
            evaluation = evaluate_rank_ic(evaluation_frame, campaign)
            if evaluation.valid_dates < campaign.min_valid_dates:
                raise FactorMinerError(
                    FailureCode.INSUFFICIENT_VALID_DATES,
                    f"有效 RankIC 日期不足：{evaluation.valid_dates} < {campaign.min_valid_dates}",
                )
            if evaluation.median_coverage < campaign.min_median_coverage:
                raise FactorMinerError(
                    FailureCode.FACTOR_COVERAGE_TOO_LOW,
                    "RankIC median coverage 低于 campaign 阈值",
                )
            inference = hac_mean_test(evaluation.rank_ic_values, campaign)
        except FactorMinerError as error:
            _failure(
                context.ledger,
                context.run_id,
                context.campaign_id,
                candidate,
                EventType.EVALUATION_FAILED,
                error,
                outcome_exposed=True,
                artifact_refs=refs,
            )
            statuses[candidate_id] = CandidateTerminalStatus.EVALUATION_FAILED
            metrics[candidate_id] = _evaluation_metrics(evaluation, None)
            continue
        context.ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=context.campaign_id,
                run_id=context.run_id,
                event_type=EventType.EVALUATION_COMPLETED,
                status="evaluation_completed",
                outcome_exposed=True,
                data_hash=artifact.artifact_sha256,
                artifact_refs=refs,
            )
        )
        metrics[candidate_id] = _evaluation_metrics(evaluation, inference)
        if not _visible_statistics_pass(candidate, inference, campaign):
            context.ledger.append_event(
                TrialEvent(
                    candidate_id=candidate_id,
                    campaign_id=context.campaign_id,
                    run_id=context.run_id,
                    event_type=EventType.VISIBLE_FAILED,
                    status="visible_failed",
                    outcome_exposed=True,
                    data_hash=artifact.artifact_sha256,
                    artifact_refs=refs,
                )
            )
            statuses[candidate_id] = CandidateTerminalStatus.VISIBLE_FAILED
            continue
        if reference_frames is not None:
            redundancy = check_output_redundancy(
                pl.read_parquet(artifact.artifact_path),
                reference_frames,
                campaign,
                candidate_column="raw_factor",
                candidate_id=candidate_id,
            )
            if not redundancy.passed:
                _failure(
                    context.ledger,
                    context.run_id,
                    context.campaign_id,
                    candidate,
                    EventType.REDUNDANCY_FAILED,
                    FactorMinerError(
                        FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                        "候选未通过输出冗余闸门",
                    ),
                    outcome_exposed=True,
                    artifact_refs=refs,
                )
                statuses[candidate_id] = CandidateTerminalStatus.REDUNDANCY_FAILED
                continue
        context.ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=context.campaign_id,
                run_id=context.run_id,
                event_type=EventType.VISIBLE_PASSED,
                status="visible_passed",
                outcome_exposed=True,
                data_hash=artifact.artifact_sha256,
                artifact_refs=refs,
            )
        )
        statuses[candidate_id] = CandidateTerminalStatus.VISIBLE_PASSED
    return _run_result(
        context,
        statuses,
        artifact_refs,
        artifact_hashes,
        metrics,
        visible_only=True,
        campaign=campaign,
        data_provenance=data_provenance,
        code_hash=code_hash,
        config_hash=config_hash,
        uv_lock_hash=uv_lock_hash,
    )


class _RunContext:
    """工作流内部的运行上下文。"""

    def __init__(
        self,
        ledger: JsonlLedger,
        run_id: str,
        campaign_id_value: str,
        run_directory: Path,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.campaign_id = campaign_id_value
        self.run_directory = run_directory


def _start_run(
    campaign: CampaignSpec,
    candidates: Mapping[str, RegisteredCandidate],
    artifact_root: Path,
    code_hash: str,
    config_hash: str,
) -> _RunContext:
    """登记候选和 campaign，再启动一次运行。"""

    if not code_hash.strip() or not config_hash.strip():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "运行必须显式提供 code_hash 和 config_hash",
        )
    missing = [candidate_id for candidate_id in campaign.candidate_ids if candidate_id not in candidates]
    if missing:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"campaign 缺少已登记候选：{missing}",
        )
    ledger = JsonlLedger(artifact_root)
    campaign_identifier = campaign_id(campaign)
    for candidate_id in campaign.candidate_ids:
        candidate = candidates[candidate_id]
        ledger.register_candidate(candidate)
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                event_type=EventType.CANDIDATE_REGISTERED,
                status="registered",
                spec_hash=candidate.spec_hash,
                code_hash=code_hash,
                config_hash=config_hash,
            )
        )
    ledger.register_campaign(campaign)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            event_type=EventType.CAMPAIGN_REGISTERED,
            status="registered",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    run_id = f"run_{uuid4().hex}"
    run_directory = Path(artifact_root).expanduser().resolve(strict=False) / "artifacts" / "runs" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            status="started",
            code_hash=code_hash,
            config_hash=config_hash,
        )
    )
    return _RunContext(ledger, run_id, campaign_identifier, run_directory)


def _request_for(
    campaign: CampaignSpec,
    candidates: Mapping[str, RegisteredCandidate],
) -> DataRequest:
    """从冻结候选集合派生唯一数据请求。"""

    fields = sorted(
        {
            field
            for candidate_id in campaign.candidate_ids
            for field in candidates[candidate_id].spec.required_fields
        }
    )
    warmup = max(
        candidates[candidate_id].spec.max_lookback
        for candidate_id in campaign.candidate_ids
    )
    return DataRequest(
        campaign.visible_start,
        campaign.visible_end,
        tuple(fields),
        warmup_days=warmup,
    )


def _compile(candidate: RegisteredCandidate) -> CompiledFactorPlan:
    """使用公司 A 股标准行情字段编译候选。"""

    return compile_candidate(candidate, set(MARKET_COLUMNS))


def _compute_twice(
    plan: CompiledFactorPlan,
    source: DataSource,
    request: DataRequest,
    run_directory: Path,
    candidate_id: str,
) -> tuple[FactorArtifact, FactorArtifact]:
    """在同一运行中执行两次 raw factor 计算。"""

    first = compute_raw_factor(
        plan,
        source,
        request,
        run_directory / f"{candidate_id}.first.raw.parquet",
    )
    second = compute_raw_factor(
        plan,
        source,
        request,
        run_directory / f"{candidate_id}.second.raw.parquet",
    )
    return first, second


def _evaluation_frame(
    source: DataSource,
    request: DataRequest,
    artifact: FactorArtifact,
) -> pl.DataFrame:
    """将 source 的 rank mask/label 与 raw factor 按主键合并。"""

    source_frame = source.scan(request).collect()
    raw_frame = pl.read_parquet(artifact.artifact_path).select(
        ["date", "asset", "raw_factor"]
    )
    return source_frame.join(raw_frame, on=["date", "asset"], how="inner", validate="1:1")


def _visible_statistics_pass(
    candidate: RegisteredCandidate,
    inference: HacInference,
    campaign: CampaignSpec,
) -> bool:
    """执行 frozen expected sign、双侧 Bonferroni 和 alpha 闸门。"""

    expected_sign = candidate.spec.hypothesis.expected_sign.value
    direction_pass = (
        inference.mean > 0 if expected_sign == "positive" else inference.mean < 0
    )
    return direction_pass and inference.bonferroni_p_value <= campaign.alpha


def _outcome_exposed(
    ledger: JsonlLedger,
    run_id: str,
    campaign_identifier: str,
    candidate: RegisteredCandidate,
    artifact_refs: tuple[str, ...],
    data_hash: str,
) -> None:
    """追加结果暴露事件，标记后续评价可读取结果。"""

    ledger.append_event(
        TrialEvent(
            candidate_id=candidate.candidate_id,
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.OUTCOME_EXPOSED,
            status="outcome_exposed",
            outcome_exposed=True,
            data_hash=data_hash,
            artifact_refs=artifact_refs,
        )
    )


def _failure(
    ledger: JsonlLedger,
    run_id: str,
    campaign_identifier: str,
    candidate: RegisteredCandidate,
    event_type: EventType,
    error: FactorMinerError,
    *,
    outcome_exposed: bool = False,
    artifact_refs: tuple[str, ...] = (),
) -> None:
    """追加带稳定错误码的失败终态事件。"""

    ledger.append_event(
        TrialEvent(
            candidate_id=candidate.candidate_id,
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=event_type,
            status=event_type.value,
            outcome_exposed=outcome_exposed,
            failure_code=error.code,
            artifact_refs=artifact_refs,
        )
    )


def _quality_metrics(artifact: FactorArtifact) -> dict[str, Any]:
    """将 smoke 质量摘要转换为 CLI 可输出字典。"""

    return artifact.quality.model_dump(mode="json")


def _evaluation_metrics(
    evaluation: EvaluationMetrics | None,
    inference: HacInference | None,
) -> dict[str, Any]:
    """将评价和统计摘要转换为 CLI 可输出字典。"""

    payload: dict[str, Any] = {}
    if evaluation is not None:
        payload["evaluation"] = evaluation.model_dump(mode="json")
        payload["rank_ic_values"] = evaluation.rank_ic_values
    if inference is not None:
        payload["inference"] = inference.model_dump(mode="json")
    return payload


def _run_result(
    context: _RunContext,
    statuses: dict[str, CandidateTerminalStatus],
    artifact_refs: dict[str, tuple[str, ...]],
    artifact_hashes: dict[str, tuple[str, ...]],
    metrics: dict[str, dict[str, Any]],
    *,
    visible_only: bool,
    campaign: CampaignSpec,
    data_provenance: DataProvenance,
    code_hash: str,
    config_hash: str,
    uv_lock_hash: str,
) -> RunResult:
    """构造不含 sealed OOS 和生产资格的终态摘要。"""

    manifest = {
        "run_id": context.run_id,
        "campaign_id": context.campaign_id,
        "campaign_spec_hash": sha256_json(campaign.model_dump(mode="json")),
        "code_commit": code_hash,
        "config_hash": config_hash,
        "uv_lock_sha256": uv_lock_hash,
        "data_provenance": asdict(data_provenance),
        "statuses": {candidate_id: status.value for candidate_id, status in statuses.items()},
    }
    manifest_path = context.run_directory / "run_manifest.json"
    manifest_hash = _write_run_manifest(manifest_path, manifest)

    return RunResult(
        run_id=context.run_id,
        campaign_id=context.campaign_id,
        statuses=statuses,
        artifact_refs=artifact_refs,
        artifact_hashes=artifact_hashes,
        metrics=metrics,
        visible_only=visible_only,
        run_manifest_path=str(manifest_path),
        run_manifest_sha256=manifest_hash,
        sealed_oos_used=False,
        production_eligible=False,
    )


def _write_run_manifest(path: Path, manifest: dict[str, Any]) -> str:
    """以不可覆盖方式写入运行 manifest，并返回其规范化内容哈希。"""

    payload = canonical_json_bytes(manifest)
    file_descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o640,
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        raise
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return sha256_json(manifest)


def run_visible_portfolio_campaign(
    campaign: TrustedVisibleCampaignSpec,
    portfolio_policy: PortfolioEvaluationPolicy,
    barra_policy: BarraEvaluationPolicy,
    sources: PortfolioSources,
    artifact_root: Path,
) -> RunResult:
    """在 generation seal 后执行组合、IC、Barra 并原子发布运行产物。

    该入口只接受服务器端已经准备好的时点正确数据端口。成功计算不代表
    因子已经获得 Alpha 认证；它只表示本次冻结协议下的可见诊断产物完整。
    """

    root = artifact_root.expanduser().resolve(strict=False)
    candidate_ids = tuple(campaign.candidate_ids)
    if campaign.evaluation_policy_id != evaluation_policy_id(sources.ic_policy):
        raise FactorMinerError(
            FailureCode.DATA_RELEASE_MISMATCH,
            "组合 campaign 与 IC 评价政策身份不一致",
        )
    if any(candidate_id not in sources.candidates for candidate_id in candidate_ids):
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "组合 campaign 引用了未提供的候选",
        )
    if any(candidate_id not in sources.portfolio_panels for candidate_id in candidate_ids):
        raise FactorMinerError(
            FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID,
            "组合数据端口缺少候选面板",
        )
    if any(candidate_id not in sources.ic_panels for candidate_id in candidate_ids):
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "IC 数据端口缺少候选面板",
        )
    if any(
        candidate_id not in sources.barra_weights_by_candidate
        for candidate_id in candidate_ids
    ):
        raise FactorMinerError(
            FailureCode.BARRA_DATA_CONTRACT_INVALID,
            "Barra 权重端口缺少候选面板",
        )

    # 这是唯一的 outcome 入口；在它之前不读取组合、IC 或 Barra 数据。
    authorize_evaluation_open(
        sources.generation_family,
        sources.generation_projection,
        sources.generation_seal,
        lambda: True,
    )

    ledger = JsonlLedger(root)
    campaign_identifier = trusted_campaign_id(campaign)
    for candidate_id in candidate_ids:
        candidate = sources.candidates[candidate_id]
        ledger.register_candidate(candidate)
        ledger.append_event(
            TrialEvent(
                candidate_id=candidate_id,
                campaign_id=campaign_identifier,
                event_type=EventType.CANDIDATE_REGISTERED,
                status="registered",
                spec_hash=candidate.spec_hash,
            )
        )
    ledger.register_campaign(campaign)
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            event_type=EventType.CAMPAIGN_REGISTERED,
            status="registered",
        )
    )
    run_id = f"run_{uuid4().hex[:24]}"
    ledger.append_event(
        TrialEvent(
            campaign_id=campaign_identifier,
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            status="started",
        )
    )

    def jsonable(value: object) -> object:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [jsonable(item) for item in value]
        return value

    statuses: dict[str, CandidateTerminalStatus] = {}
    metrics: dict[str, dict[str, Any]] = {}
    candidate_artifacts: dict[str, dict[str, bytes]] = {}
    portfolio_daily: dict[str, object] = {}
    try:
        benchmark = sources.benchmark_returns.collect()
        for candidate_id in candidate_ids:
            candidate = sources.candidates[candidate_id]
            ledger.append_event(
                TrialEvent(
                    candidate_id=candidate_id,
                    campaign_id=campaign_identifier,
                    run_id=run_id,
                    event_type=EventType.OUTCOME_EXPOSED,
                    status="outcome_exposed",
                    outcome_exposed=True,
                    data_hash=candidate.spec_hash,
                )
            )
            try:
                backtest = run_portfolio_backtest(
                    sources.portfolio_panels[candidate_id],
                    sources.benchmark_returns,
                    sources.schedule,
                    portfolio_policy,
                    direction=candidate.spec.hypothesis.expected_sign.value,
                )
                portfolio_metrics = calculate_portfolio_metrics(
                    pl.DataFrame(backtest.daily_returns),
                    benchmark,
                    sources.calendar,
                    calendar_version=sources.calendar_version,
                    calendar_sha256=sources.calendar_sha256,
                )
                ic_diagnostics = evaluate_ic_horizons(
                    sources.ic_panels[candidate_id],
                    (1, 3, 5, 10, 20),
                    sources.ic_policy,
                )
                barra = calculate_barra_attribution(
                    sources.barra_weights_by_candidate[candidate_id],
                    sources.barra_benchmark_weights,
                    sources.barra_exposures,
                    sources.barra_factor_returns,
                    barra_policy,
                    identity=sources.barra_identity,
                )
                metric_payload = {
                    "candidate_id": candidate_id,
                    "candidate_spec_hash": candidate.spec_hash,
                    "hypothesis": jsonable(
                        candidate.spec.hypothesis.model_dump(mode="json")
                    ),
                    "formula": jsonable(
                        candidate.spec.expression.model_dump(mode="json")
                    ),
                    "factor_category": candidate.spec.provenance.get(
                        "factor_category", "unclassified"
                    ),
                    "quality_status": "not_assessed",
                    "portfolio": jsonable(portfolio_metrics.model_dump(mode="json")),
                    "ic": jsonable(ic_diagnostics.model_dump(mode="json")),
                    "barra": jsonable(barra.model_dump(mode="json")),
                }
                metrics[candidate_id] = metric_payload
                portfolio_daily[candidate_id] = jsonable(backtest.daily_returns)
                candidate_artifacts[candidate_id] = {
                    "portfolio/backtest.json": canonical_json_bytes(
                        jsonable(backtest.model_dump(mode="json"))
                    ),
                    "portfolio/metrics.json": canonical_json_bytes(
                        jsonable(portfolio_metrics.model_dump(mode="json"))
                    ),
                    "ic/diagnostics.json": canonical_json_bytes(
                        jsonable(ic_diagnostics.model_dump(mode="json"))
                    ),
                    "barra/attribution.json": canonical_json_bytes(
                        jsonable(barra.model_dump(mode="json"))
                    ),
                    "candidate_status.json": canonical_json_bytes(
                        {
                            "candidate_id": candidate_id,
                            "status": "portfolio_diagnostics_completed",
                            "quality_status": "not_assessed",
                        }
                    ),
                }
                statuses[candidate_id] = CandidateTerminalStatus.VISIBLE_PASSED
            except FactorMinerError as error:
                statuses[candidate_id] = CandidateTerminalStatus.EVALUATION_FAILED
                metrics[candidate_id] = {
                    "candidate_id": candidate_id,
                    "quality_status": "not_assessed",
                    "error": str(error),
                }
                candidate_artifacts[candidate_id] = {
                    "candidate_status.json": canonical_json_bytes(
                        {
                            "candidate_id": candidate_id,
                            "status": CandidateTerminalStatus.EVALUATION_FAILED.value,
                            "quality_status": "not_assessed",
                            "failure_code": error.code,
                        }
                    )
                }

        artifacts: dict[str, bytes] = {
            "run/portfolio_policy.json": canonical_json_bytes(
                portfolio_policy.model_dump(mode="json")
            ),
            "run/barra_policy.json": canonical_json_bytes(
                barra_policy.model_dump(mode="json")
            ),
            "run/metrics.json": canonical_json_bytes(jsonable(metrics)),
            "run/statuses.json": canonical_json_bytes(
                {key: value.value for key, value in statuses.items()}
            ),
            "portfolio/metrics.json": canonical_json_bytes(
                jsonable(
                    {
                        key: value["portfolio"]
                        for key, value in metrics.items()
                        if "portfolio" in value
                    }
                )
            ),
            "portfolio/daily.json": canonical_json_bytes(jsonable(portfolio_daily)),
            "ic/diagnostics.json": canonical_json_bytes(
                jsonable(
                    {
                        key: value["ic"]
                        for key, value in metrics.items()
                        if "ic" in value
                    }
                )
            ),
            "barra/attribution.json": canonical_json_bytes(
                jsonable(
                    {
                        key: value["barra"]
                        for key, value in metrics.items()
                        if "barra" in value
                    }
                )
            ),
        }
        for candidate_id, files in candidate_artifacts.items():
            for relative_path, payload in files.items():
                artifacts[f"candidates/{candidate_id}/{relative_path}"] = payload
        manifest = publish_run_artifacts(root, run_id, artifacts)
        verify_published_run(root, run_id)
        artifact_refs: dict[str, tuple[str, ...]] = {}
        artifact_hashes: dict[str, tuple[str, ...]] = {}
        for candidate_id, status in statuses.items():
            prefix = f"candidates/{candidate_id}/"
            relatives = tuple(
                item.relative_path
                for item in manifest.artifacts
                if item.relative_path.startswith(prefix)
            )
            artifact_refs[candidate_id] = tuple(
                str(root / "artifacts" / "runs" / run_id / relative)
                for relative in relatives
            )
            artifact_hashes[candidate_id] = tuple(
                item.sha256
                for item in manifest.artifacts
                if item.relative_path in relatives
            )
            event_type = (
                EventType.VISIBLE_PASSED
                if status is CandidateTerminalStatus.VISIBLE_PASSED
                else EventType.EVALUATION_FAILED
            )
            ledger.append_event(
                TrialEvent(
                    candidate_id=candidate_id,
                    campaign_id=campaign_identifier,
                    run_id=run_id,
                    event_type=event_type,
                    status=status.value,
                    outcome_exposed=True,
                    artifact_refs=artifact_refs[candidate_id],
                    data_hash=sha256_json(metrics[candidate_id]),
                )
            )
        ledger.append_event(
            TrialEvent(
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=EventType.RUN_COMPLETED,
                status="completed",
                outcome_exposed=True,
                artifact_refs=(str(root / "artifacts" / "runs" / run_id / "run_manifest.json"),),
            )
        )
        return RunResult(
            run_id=run_id,
            campaign_id=campaign_identifier,
            statuses=statuses,
            artifact_refs=artifact_refs,
            artifact_hashes=artifact_hashes,
            metrics=metrics,
            visible_only=True,
            run_manifest_path=str(
                root / "artifacts" / "runs" / run_id / "run_manifest.json"
            ),
            run_manifest_sha256=manifest.manifest_sha256,
            sealed_oos_used=False,
            production_eligible=False,
        )
    except BaseException:
        ledger.append_event(
            TrialEvent(
                campaign_id=campaign_identifier,
                run_id=run_id,
                event_type=EventType.INTERRUPTED,
                status="interrupted",
                outcome_exposed=True,
            )
        )
        raise
