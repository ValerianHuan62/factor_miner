"""阶段 A 固定候选的因子面板编排。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, NormalDist
from typing import Callable, Collection, Literal, Mapping

import polars as pl

from factor_miner.barra_attribution import BarraAttributionResult, calculate_barra_attribution
from factor_miner.barra_data_source import (
    DerivedBarraInputs,
    build_barra_portfolio_weights,
    load_derived_barra_inputs,
)
from factor_miner.barra_schema import (
    BarraEvaluationPolicy,
    BarraInputIdentity,
    barra_policy_id,
    validate_barra_input_identity,
)
from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.compiler import CompiledFactorPlan, compile_candidate
from factor_miner.compute import FactorArtifact, compute_trusted_raw_factor
from factor_miner.data_source import FactorInputRequest, FactorInputSource
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ic_diagnostics import FROZEN_HORIZONS, ICDiagnostics, evaluate_ic_horizons
from factor_miner.ledger import atomic_write_immutable
from factor_miner.long_only_protocol import (
    DirectionDecision,
    DiscoveryRankICSummary,
    LongOnlyResearchProtocol,
    select_direction,
)
from factor_miner.pilot_schema import (
    BarraAvailability,
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotInputManifest,
    PilotRunRequest,
    PilotSourcePaths,
)
from factor_miner.pilot_sources import (
    PilotQuantLakeFactorInputSource,
    _file_or_tree_sha256,
    _manifest_date_column,
    _scan_manifest_partitions,
    _normalize_date_column,
    inspect_pilot_source_identity,
    inspect_pilot_sources,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.portfolio_data_source import align_open_to_open_panel
from factor_miner.portfolio_artifacts import PublishedRunManifest, publish_run_artifacts
from factor_miner.portfolio_evaluation import PortfolioBacktestResult, run_target_long_backtest
from factor_miner.portfolio_schema import (
    PortfolioEvaluationPolicy,
    TradingCalendarIdentity,
    portfolio_policy_id,
)
from factor_miner.portfolio_statistics import PortfolioMetrics, calculate_portfolio_metrics
from factor_miner.schema import EvaluationPolicySpec, evaluation_policy_id
from factor_miner.trading_schedule import RebalanceWindow, build_tuesday_rebalance_schedule


@dataclass(frozen=True, slots=True)
class FixedSignalPanel:
    """一个固定候选的可见信号面板及其编译身份。"""

    candidate_id: str
    compiler_candidate_id: str
    ast_hash: str
    plan_hash: str
    frame: pl.DataFrame
    rank_mask: pl.DataFrame
    trading_mask: pl.DataFrame
    artifact: FactorArtifact | None = None


def _runner_error(message: str) -> FactorMinerError:
    """构造 Pilot 因子面板合同错误。"""

    return FactorMinerError(FailureCode.PILOT_INPUT_CONTRACT_INVALID, message)


def _validate_signal_panel(frame: pl.DataFrame, candidate_id: str) -> None:
    """验证面板输出主键和字段。"""

    required = {"signal_date", "security_id", "factor_value"}
    missing = required.difference(frame.columns)
    if missing:
        raise _runner_error(f"{candidate_id} 信号面板缺少字段：{sorted(missing)}")
    duplicate = (
        frame.group_by(["signal_date", "security_id"])
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
    )
    if duplicate.height:
        raise _runner_error(f"{candidate_id} 信号面板主键重复")
    if frame.select(pl.col("signal_date").is_null().any()).item():
        raise _runner_error(f"{candidate_id} 信号面板 signal_date 为空")
    if frame.select(pl.col("security_id").is_null().any()).item():
        raise _runner_error(f"{candidate_id} 信号面板 security_id 为空")


def _allowed_fields(source: FactorInputSource, request: FactorInputRequest) -> tuple[str, ...]:
    """读取数据源显式声明的字段白名单。"""

    declared = getattr(source, "allowed_fields", None)
    values: Collection[str] = declared if declared is not None else request.required_fields
    result = tuple(sorted(set(values)))
    if not result:
        raise _runner_error("Pilot 因子输入字段白名单不能为空")
    return result


def _compile_for_candidate(
    candidate: PilotFixedCandidate,
    allowed_fields: Collection[str],
) -> CompiledFactorPlan:
    """对固定候选调用唯一 typed AST 编译器。"""

    try:
        return compile_candidate(candidate.spec, allowed_fields=allowed_fields)
    except FactorMinerError:
        raise
    except Exception as error:
        raise _runner_error(f"固定候选编译失败：{error}") from error


def compute_fixed_signal_panels(
    candidates: PilotFixedCandidateFile,
    source: FactorInputSource,
    request: FactorInputRequest,
    artifact_root: Path,
) -> tuple[FixedSignalPanel, ...]:
    """计算一批冻结候选的原始信号面板。

    计算先由既有 input-only 数据源提供 lookback，再由既有确定性编译器和
    `compute_trusted_raw_factor` 计算；可见区间外的 warmup 行只参与窗口，
    不进入最终信号面板。状态 mask 只将不可用值置为 null，不做零填充。
    """

    if not candidates.candidates:
        raise _runner_error("固定候选集合不能为空")
    output_root = artifact_root.expanduser().resolve(strict=False)
    allowed_fields = _allowed_fields(source, request)
    source.inspect_inputs()
    rank_mask = (
        source.scan_inputs(request)
        .select(["date", "asset", "valid_for_factor_rank"])
        .filter(pl.col("date").is_between(request.start, request.end, closed="both"))
        .collect()
        .rename(
            {
                "date": "signal_date",
                "asset": "security_id",
            }
        )
    )
    if rank_mask.select(
        pl.struct(["signal_date", "security_id"]).is_duplicated().any()
    ).item():
        raise _runner_error("valid_for_factor_rank 掩码主键重复")
    trading_mask = (
        source.scan_inputs(request)
        .select(["date", "asset", "valid_for_trading"])
        .filter(pl.col("date").is_between(request.start, request.end, closed="both"))
        .collect()
        .rename(
            {
                "date": "signal_date",
                "asset": "security_id",
            }
        )
    )
    if trading_mask.select(
        pl.struct(["signal_date", "security_id"]).is_duplicated().any()
    ).item():
        raise _runner_error("valid_for_trading 掩码主键重复")
    panels: list[FixedSignalPanel] = []
    for candidate in candidates.candidates:
        plan = _compile_for_candidate(candidate, allowed_fields)
        output_path = output_root / candidate.candidate_id / "raw_factor.parquet"
        artifact = compute_trusted_raw_factor(
            plan,
            source,
            request,
            output_path,
        )
        raw = pl.read_parquet(artifact.artifact_path)
        required = {"date", "asset", "raw_factor"}
        missing = required.difference(raw.columns)
        if missing:
            raise _runner_error(
                f"{candidate.candidate_id} 原始因子产物缺少字段：{sorted(missing)}"
            )
        panel = (
            raw.select(
                [
                    pl.col("date").alias("signal_date"),
                    pl.col("asset").alias("security_id"),
                    pl.col("raw_factor").alias("factor_value"),
                ]
            )
            .sort(["signal_date", "security_id"])
        )
        _validate_signal_panel(panel, candidate.candidate_id)
        panels.append(
            FixedSignalPanel(
                candidate_id=candidate.candidate_id,
                compiler_candidate_id=plan.candidate_id,
                ast_hash=plan.ast_hash,
                plan_hash=plan.plan_hash,
                frame=panel,
                rank_mask=rank_mask,
                trading_mask=trading_mask,
                artifact=artifact,
            )
        )
    return tuple(panels)


def _open_market_frame(market_panel: pl.LazyFrame) -> pl.LazyFrame:
    """验证并标准化 open-to-open 开盘价面板。"""

    required = {"trade_date", "security_id", "open"}
    missing = required.difference(market_panel.collect_schema().names())
    if missing:
        raise _runner_error(f"开盘价面板缺少字段：{sorted(missing)}")
    duplicate = (
        market_panel.group_by(["trade_date", "security_id"])
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
        .collect()
    )
    if duplicate.height:
        raise _runner_error("开盘价面板 trade_date/security_id 主键重复")
    if market_panel.filter(pl.col("open").is_null() | (pl.col("open") <= 0)).limit(1).collect().height:
        raise _runner_error("开盘价面板包含缺失或非正开盘价")
    return market_panel.rename({"security_id": "asset"})


def _calendar_sessions(
    calendar: pl.DataFrame,
    max_horizon: int = max(FROZEN_HORIZONS),
) -> tuple[date, ...]:
    """提取已核验交易日历中的开放日。"""

    required = {"trade_date", "is_open"}
    if set(calendar.columns) != required:
        raise _runner_error("交易日日历必须精确包含 trade_date 和 is_open")
    if calendar.schema["trade_date"] != pl.Date or calendar.schema["is_open"] != pl.Boolean:
        raise _runner_error("交易日日历字段类型不正确")
    if calendar.select(pl.col("trade_date").is_duplicated().any()).item():
        raise _runner_error("交易日日历 trade_date 重复")
    sessions = tuple(
        sorted(calendar.filter(pl.col("is_open")).get_column("trade_date").to_list())
    )
    if len(sessions) < max_horizon + 2:
        raise _runner_error("交易日日历不足以构造冻结 IC 期限")
    return sessions


def build_open_to_open_ic_panel(
    signal_panel: FixedSignalPanel,
    market_panel: pl.LazyFrame,
    calendar: pl.DataFrame,
    horizons: tuple[int, ...] = FROZEN_HORIZONS,
) -> pl.LazyFrame:
    """从信号面板构造冻结多期限 open-to-open 标签。"""

    if (
        not horizons
        or len(horizons) != len(set(horizons))
        or any(horizon not in FROZEN_HORIZONS for horizon in horizons)
    ):
        raise _runner_error("IC 期限必须是冻结集合 (1, 3, 5, 10, 20) 的非空子集")
    sessions = _calendar_sessions(calendar, max(horizons))
    signal = signal_panel.frame.lazy().rename(
        {
            "signal_date": "date",
            "security_id": "asset",
        }
    )
    rank_mask = signal_panel.rank_mask.lazy().rename(
        {
            "signal_date": "date",
            "security_id": "asset",
        }
    )
    signal_dates = signal_panel.frame.get_column("signal_date").unique().sort().to_list()
    session_index = {item: index for index, item in enumerate(sessions)}
    schedule_rows: list[dict[str, date]] = []
    for signal_date in signal_dates:
        if signal_date not in session_index:
            raise _runner_error(f"信号日不在交易日历：{signal_date}")
        entry_index = session_index[signal_date] + 1
        if entry_index + max(horizons) >= len(sessions):
            continue
        row = {"date": signal_date, "entry_date": sessions[entry_index]}
        row.update(
            {
                f"exit_date_{horizon}": sessions[entry_index + horizon]
                for horizon in horizons
            }
        )
        schedule_rows.append(row)
    if not schedule_rows:
        raise _runner_error("信号区间没有足够未来交易日构造 IC 标签")
    schedule = pl.DataFrame(schedule_rows).lazy()
    base = signal.join(rank_mask, on=["date", "asset"], how="left")
    if base.filter(pl.col("valid_for_factor_rank").is_null()).limit(1).collect().height:
        raise _runner_error("信号面板缺少 valid_for_factor_rank 掩码")
    base = base.join(schedule, on="date", how="inner")
    market = _open_market_frame(market_panel)
    entry = market.rename(
        {
            "trade_date": "entry_date",
            "open": "entry_open",
        }
    )
    labeled = base.join(entry, on=["entry_date", "asset"], how="left")
    for horizon in horizons:
        exit_frame = market.rename(
            {
                "trade_date": f"exit_date_{horizon}",
                "open": f"exit_open_{horizon}",
            }
        )
        labeled = labeled.join(
            exit_frame,
            on=[f"exit_date_{horizon}", "asset"],
            how="left",
        ).with_columns(
            (
                pl.col(f"exit_open_{horizon}") / pl.col("entry_open") - 1.0
            ).alias(f"forward_return_{horizon}")
        )
    return labeled.select(
        [
            "date",
            "asset",
            "factor_value",
            "valid_for_factor_rank",
            *[f"forward_return_{horizon}" for horizon in horizons],
        ]
    )


def evaluate_fixed_candidate_ic(
    signal_panel: FixedSignalPanel,
    market_panel: pl.LazyFrame,
    calendar: pl.DataFrame,
    *,
    policy: EvaluationPolicySpec | None = None,
) -> ICDiagnostics:
    """使用既有冻结 IC evaluator 生成一个候选的完整诊断。"""

    evaluation_policy = policy or company_a_share_visible_policy()
    labeled = build_open_to_open_ic_panel(signal_panel, market_panel, calendar)
    return evaluate_ic_horizons(labeled, FROZEN_HORIZONS, evaluation_policy)


@dataclass(frozen=True, slots=True)
class LongOnlyWindowEvaluation:
    """方向发现、正式确认和近期展示的分段评价结果。"""

    direction: DirectionDecision
    confirmation: ICDiagnostics
    recent: ICDiagnostics
    confirmation_passed: bool
    stress_test_eligible: bool
    direction_record_sha256: str
    direction_record_path: Path
    confirmation_record_sha256: str
    confirmation_record_path: Path
    family_size: int


def _window_slice(
    panel: pl.LazyFrame,
    start: date,
    end: date,
) -> pl.LazyFrame:
    """只按信号观察日切片，不改变已经冻结的标签与掩码。"""

    return panel.filter(pl.col("date").is_between(start, end, closed="both"))


def _confirmation_passed(
    diagnostics: ICDiagnostics,
    *,
    selected_direction: Literal["positive", "negative"],
    policy: EvaluationPolicySpec,
    family_size: int,
) -> bool:
    """按冻结方向、最小效应和 Bonferroni-HAC 闸门判断确认资格。"""

    oriented_mean = (
        diagnostics.rank_ic_mean
        if selected_direction == "positive"
        else -diagnostics.rank_ic_mean
    )
    oriented_t = (
        diagnostics.rank_ic_hac_t
        if selected_direction == "positive"
        else -diagnostics.rank_ic_hac_t
    )
    critical = NormalDist().inv_cdf(1.0 - policy.alpha / (2.0 * family_size))
    return oriented_mean >= policy.min_abs_mean_rank_ic and oriented_t >= critical


def evaluate_discovery_rank_ic(
    labeled_panel: pl.LazyFrame,
    policy: EvaluationPolicySpec,
) -> float:
    """只从发现期五日标签计算逐日截面 RankIC 均值。"""

    return mean(value for _, value in evaluate_discovery_daily_rank_ic(labeled_panel, policy))


def evaluate_discovery_daily_rank_ic(
    labeled_panel: pl.LazyFrame,
    policy: EvaluationPolicySpec,
) -> tuple[tuple[date, float], ...]:
    """返回发现期五日逐日 RankIC，供同日期配对影子评价复用。"""

    data = labeled_panel.collect()
    required = {
        "date",
        "asset",
        "factor_value",
        policy.rank_mask_column,
        "forward_return_5",
    }
    missing = required.difference(data.columns)
    if missing:
        raise _runner_error(f"发现期五日 RankIC 输入缺少字段：{sorted(missing)}")
    values: list[tuple[date, float]] = []
    eligible = data.filter(
        (pl.col(policy.rank_mask_column) == True)
        & pl.col("factor_value").is_finite()
        & pl.col("forward_return_5").is_finite()
    )
    for day in eligible.partition_by("date", maintain_order=True):
        if day.height < policy.min_names_per_date:
            continue
        ranked = day.select(
            pl.col("factor_value").rank("average").alias("factor_rank"),
            pl.col("forward_return_5").rank("average").alias("return_rank"),
        )
        correlation = ranked.select(
            pl.corr("factor_rank", "return_rank")
        ).item()
        if correlation is None or not math.isfinite(correlation):
            raise _runner_error("发现期 RankIC 截面存在常数或非有限输入")
        values.append((day.get_column("date")[0], float(correlation)))
    if len(values) < policy.min_valid_dates:
        raise _runner_error("发现期五日 RankIC 有效日期不足")
    return tuple(values)


def _evaluation_record_provenance(
    *,
    input_manifest: PilotInputManifest,
    evaluation_policy: EvaluationPolicySpec,
    family_size: int,
    request: PilotRunRequest,
    source_run_id: str | None,
) -> dict[str, object]:
    """生成方向与确认不可变记录共同绑定的正式运行身份。"""

    return {
        "data_release_id": input_manifest.resolved_release_id,
        "input_manifest_sha256": sha256_json(
            input_manifest.model_dump(mode="json")
        ),
        "evaluation_policy_id": evaluation_policy_id(evaluation_policy),
        "family_size": family_size,
        "code_commit": request.code_commit,
        "config_hash": request.config_hash,
        "source_run_id": source_run_id,
    }


def _freeze_direction_decision(
    *,
    rank_ic: float,
    hypothesis_direction: Literal["positive", "negative"],
    direction_record_path: Path,
    protocol: LongOnlyResearchProtocol,
    identity: Mapping[str, object],
    provenance: Mapping[str, object],
) -> tuple[DirectionDecision, str]:
    """原子写入并回读核验方向记录，返回冻结决定及 payload 哈希。"""

    decision = select_direction(
        DiscoveryRankICSummary(
            protocol=protocol,
            window_start=protocol.discovery_start,
            window_end=protocol.discovery_end,
            rank_ic=rank_ic,
        ),
        hypothesis_direction=hypothesis_direction,
    )
    payload = {
        "version": "pilot-direction-freeze-v1",
        "protocol": protocol.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "identity": dict(identity),
        "provenance": dict(provenance),
    }
    content = canonical_json_bytes(payload)
    atomic_write_immutable(direction_record_path, content)
    try:
        persisted = direction_record_path.read_bytes()
    except OSError as error:
        raise _runner_error("方向冻结文件原子写入后无法回读核验") from error
    if persisted != content:
        raise _runner_error("方向冻结文件原子写入后内容不一致")
    return decision, sha256_json(payload)


def _freeze_confirmation_diagnostics(
    *,
    candidate_id: str,
    spec_sha256: str,
    diagnostics: ICDiagnostics,
    confirmation_record_path: Path,
    protocol: LongOnlyResearchProtocol,
    provenance: Mapping[str, object],
    direction_record_sha256: str,
) -> str:
    """原子冻结正式确认诊断及其精确研究窗口。"""

    payload = {
        "version": "pilot-confirmation-freeze-v1",
        "candidate_id": candidate_id,
        "spec_sha256": spec_sha256,
        "window_start": protocol.confirmation_start.isoformat(),
        "window_end": protocol.confirmation_end.isoformat(),
        "valid_date_count": len(diagnostics.daily),
        "provenance": dict(provenance),
        "direction_record_sha256": direction_record_sha256,
        "diagnostics": diagnostics.model_dump(mode="json"),
    }
    content = canonical_json_bytes(payload)
    atomic_write_immutable(confirmation_record_path, content)
    try:
        persisted = confirmation_record_path.read_bytes()
    except OSError as error:
        raise _runner_error("确认诊断原子写入后无法回读核验") from error
    if persisted != content:
        raise _runner_error("确认诊断原子写入后内容不一致")
    return hashlib.sha256(content).hexdigest()


def evaluate_long_only_windows(
    labeled_panel: pl.LazyFrame,
    *,
    hypothesis_direction: Literal["positive", "negative"],
    candidate_id: str,
    spec_sha256: str,
    record_provenance: Mapping[str, object],
    direction_record_path: Path,
    evaluation_policy: EvaluationPolicySpec | None = None,
    family_size: int = 1,
    protocol: LongOnlyResearchProtocol | None = None,
    diagnostics_evaluator: Callable[
        [pl.LazyFrame, tuple[int, ...], EvaluationPolicySpec],
        ICDiagnostics,
    ] = evaluate_ic_horizons,
    discovery_rank_ic_evaluator: Callable[
        [pl.LazyFrame, EvaluationPolicySpec], float
    ] = evaluate_discovery_rank_ic,
) -> LongOnlyWindowEvaluation:
    """先冻结发现方向，再只读评价确认期和近期切片。

    方向文件通过不可变原子写入完成后，本函数才会构建确认期统计。这样即使
    确认结果与发现期符号相反，也不能反向改变目标多头方向。
    """

    if family_size <= 0:
        raise _runner_error("Bonferroni 检验族规模必须为正整数")
    frozen = protocol or LongOnlyResearchProtocol()
    policy = evaluation_policy or company_a_share_visible_policy()
    discovery_rank_ic = discovery_rank_ic_evaluator(
        _window_slice(
            labeled_panel,
            frozen.discovery_start,
            frozen.discovery_end,
        ),
        policy,
    )
    decision, direction_sha256 = _freeze_direction_decision(
        rank_ic=discovery_rank_ic,
        hypothesis_direction=hypothesis_direction,
        direction_record_path=direction_record_path,
        protocol=frozen,
        identity={"candidate_id": candidate_id, "spec_sha256": spec_sha256},
        provenance=record_provenance,
    )

    confirmation = diagnostics_evaluator(
        _window_slice(
            labeled_panel,
            frozen.confirmation_start,
            frozen.confirmation_end,
        ),
        FROZEN_HORIZONS,
        policy,
    )
    confirmation_record_path = direction_record_path.with_name(
        f"confirmation_{direction_record_path.name}"
    )
    confirmation_sha256 = _freeze_confirmation_diagnostics(
        candidate_id=candidate_id,
        spec_sha256=spec_sha256,
        diagnostics=confirmation,
        confirmation_record_path=confirmation_record_path,
        protocol=frozen,
        provenance=record_provenance,
        direction_record_sha256=direction_sha256,
    )
    recent = diagnostics_evaluator(
        _window_slice(labeled_panel, frozen.recent_start, frozen.recent_end),
        FROZEN_HORIZONS,
        policy,
    )
    passed = _confirmation_passed(
        confirmation,
        selected_direction=decision.selected_direction,
        policy=policy,
        family_size=family_size,
    )
    return LongOnlyWindowEvaluation(
        direction=decision,
        confirmation=confirmation,
        recent=recent,
        confirmation_passed=passed,
        stress_test_eligible=passed,
        direction_record_sha256=direction_sha256,
        direction_record_path=direction_record_path,
        confirmation_record_sha256=confirmation_sha256,
        confirmation_record_path=confirmation_record_path,
        family_size=family_size,
    )


@dataclass(frozen=True, slots=True)
class FixedPortfolioEvaluation:
    """一个固定候选的组合回测结果和实际日历指标。"""

    backtest: PortfolioBacktestResult
    metrics: PortfolioMetrics


@dataclass(frozen=True, slots=True)
class FixedBarraEvaluation:
    """一个固定候选的可选 Barra 状态、归因结果和输入身份。"""

    availability: BarraAvailability
    attribution: BarraAttributionResult | None
    identity: BarraInputIdentity | None
    reason: str | None = None


def _barra_not_available(
    missing_inputs: Collection[str],
    *,
    identity: BarraInputIdentity | None,
    source_uris: tuple[Path, ...],
    input_sha256: str | None,
    reason: str | None = None,
) -> FixedBarraEvaluation:
    """构造不阻塞 Pilot 的 Barra 不可用结果。"""

    unique_missing = tuple(dict.fromkeys(missing_inputs))
    if not unique_missing:
        raise _runner_error("Barra not_available 必须有具体缺失输入")
    return FixedBarraEvaluation(
        availability=BarraAvailability(
            status="not_available",
            missing_inputs=unique_missing,
            source_uris=source_uris,
            input_sha256=input_sha256,
        ),
        attribution=None,
        identity=identity,
        reason=reason,
    )


def _barra_columns(
    frame: pl.LazyFrame | None,
    name: str,
    missing_inputs: list[str],
) -> tuple[str, ...]:
    """读取 Barra lazy frame 的显式字段；合同异常降级为不可用。"""

    if frame is None:
        missing_inputs.append(name)
        return ()
    try:
        return tuple(frame.collect_schema().names())
    except Exception:
        missing_inputs.append(name)
        return ()


def evaluate_fixed_candidate_barra(
    portfolio_weights: pl.LazyFrame | None,
    benchmark_weights: pl.LazyFrame | None,
    exposures: pl.LazyFrame | None,
    factor_returns: pl.LazyFrame | None,
    policy: BarraEvaluationPolicy,
    *,
    identity: BarraInputIdentity | None,
    covariance: pl.LazyFrame | None = None,
    specific_risk: pl.LazyFrame | None = None,
    source_uris: tuple[Path, ...] = (),
    input_sha256: str | None = None,
) -> FixedBarraEvaluation:
    """运行可选 Barra 归因，缺合同项时只返回 ``not_available``。

    完整输入会调用现有 `calculate_barra_attribution`。Barra 不是阶段 A
    主流程的发布闸门，因此缺行业暴露、基准权重、因子收益或输入身份时，
    只记录具体缺失项；主流程的 IC、组合回测和发布状态不被吞掉。
    """

    missing: list[str] = []
    portfolio_columns = _barra_columns(portfolio_weights, "portfolio_weights", missing)
    benchmark_columns = _barra_columns(benchmark_weights, "benchmark_weights", missing)
    exposure_columns = _barra_columns(exposures, "exposures", missing)
    return_columns = _barra_columns(factor_returns, "factor_returns", missing)
    if policy.attribution_mode == "full_risk_decomposition":
        covariance_columns = _barra_columns(covariance, "covariance", missing)
        specific_risk_columns = _barra_columns(
            specific_risk,
            "specific_risk",
            missing,
        )
        if covariance_columns and not {
            "signal_date", "factor_a", "factor_b", "covariance"
        }.issubset(covariance_columns):
            missing.append("covariance")
        if specific_risk_columns and not {
            "signal_date", "security_id", "specific_risk"
        }.issubset(specific_risk_columns):
            missing.append("specific_risk")

    if portfolio_columns and not {
        "signal_date",
        "entry_date",
        "portfolio",
        "security_id",
        "weight",
        "realized_return",
        "benchmark_return",
    }.issubset(portfolio_columns):
        missing.append("portfolio_weights")
    if benchmark_columns and not {
        "signal_date",
        "entry_date",
        "security_id",
        "weight",
    }.issubset(benchmark_columns):
        missing.append("benchmark_weights")

    factors: tuple[str, ...] = ()
    if exposure_columns:
        if not {"signal_date", "security_id"}.issubset(exposure_columns):
            missing.append("exposures")
        else:
            factor_columns = tuple(
                column
                for column in exposure_columns
                if column not in {"signal_date", "security_id"}
            )
            industry_columns = tuple(
                column
                for column in factor_columns
                if column not in policy.required_style_factors
            )
            if not industry_columns:
                missing.append("industry_exposures")
            for required in policy.required_style_factors:
                if required not in exposure_columns:
                    missing.append(f"style_exposure:{required}")
            factors = industry_columns + policy.required_style_factors

    if return_columns and not {
        "entry_date",
        "factor",
        "factor_return",
    }.issubset(return_columns):
        missing.append("factor_returns")
    elif factors and factor_returns is not None:
        try:
            available_returns = set(factor_returns.collect().get_column("factor").to_list())
        except Exception:
            missing.append("factor_returns")
        else:
            missing.extend(
                f"factor_returns:{factor}"
                for factor in factors
                if factor not in available_returns
            )

    identity_error: str | None = None
    if identity is None:
        missing.append("barra_input_identity")
    else:
        try:
            validate_barra_input_identity(policy, identity)
        except FactorMinerError as error:
            missing.append("barra_input_identity")
            identity_error = str(error)
        else:
            identity_error = None

    if missing:
        return _barra_not_available(
            missing,
            identity=identity,
            source_uris=source_uris,
            input_sha256=input_sha256,
            reason=identity_error if "barra_input_identity" in missing else None,
        )

    try:
        result = calculate_barra_attribution(
            portfolio_weights,
            benchmark_weights,
            exposures,
            factor_returns,
            policy,
            identity=identity,
            covariance=covariance,
            specific_risk=specific_risk,
        )
    except FactorMinerError as error:
        message = str(error)
        if "基准权重" in message:
            missing_name = "benchmark_weights"
        elif "暴露" in message:
            missing_name = "exposures"
        elif "因子收益" in message:
            missing_name = "factor_returns"
        else:
            missing_name = "barra_contract"
        return _barra_not_available(
            (missing_name,),
            identity=identity,
            source_uris=source_uris,
            input_sha256=input_sha256,
            reason=message,
        )
    return FixedBarraEvaluation(
        availability=BarraAvailability(
            status="available",
            source_uris=source_uris,
            input_sha256=input_sha256,
        ),
        attribution=result,
        identity=identity,
    )


def evaluate_fixed_backtest_barra(
    backtest: PortfolioBacktestResult,
    inputs: DerivedBarraInputs,
    policy: BarraEvaluationPolicy,
    *,
    source_uris: tuple[Path, ...] = (),
    input_sha256: str | None = None,
) -> FixedBarraEvaluation:
    """把固定 Pilot 组合结果接入派生 Barra 数据和现有归因器。"""

    return evaluate_fixed_candidate_barra(
        build_barra_portfolio_weights(backtest),
        inputs.benchmark_weights,
        inputs.exposures,
        inputs.factor_returns,
        policy,
        identity=inputs.identity,
        covariance=inputs.covariance,
        specific_risk=inputs.specific_risk,
        source_uris=source_uris,
        input_sha256=input_sha256,
    )


@dataclass(frozen=True, slots=True)
class FixedPilotCandidateResult:
    """一个固定候选的完整 IC、组合和 Barra 结果。"""

    candidate_id: str
    ic: ICDiagnostics | None
    portfolio: FixedPortfolioEvaluation | None
    barra: FixedBarraEvaluation | None
    windows: LongOnlyWindowEvaluation | None = None
    recent_portfolio_metrics: PortfolioMetrics | None = None


@dataclass(frozen=True, slots=True)
class FixedPilotPublication:
    """一个不可变 Pilot 运行的发布结果。"""

    run_id: str
    manifest: PublishedRunManifest
    artifact_root: Path

    @property
    def manifest_path(self) -> Path:
        """返回本次运行清单路径。"""

        return (
            self.artifact_root.expanduser().resolve(strict=False)
            / "artifacts"
            / "runs"
            / self.run_id
            / "run_manifest.json"
        )


def _json_bytes(payload: object) -> bytes:
    """将已经过 Schema 验证的 JSON 对象规范化为字节。"""

    def jsonable(value: object) -> object:
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, tuple | list):
            return [jsonable(item) for item in value]
        return value

    return canonical_json_bytes(jsonable(payload))


def _barra_result_payload(result: FixedBarraEvaluation) -> dict[str, object]:
    """序列化 Barra 可选状态和数据身份。"""

    return {
        "status": result.availability.status,
        "missing_inputs": list(result.availability.missing_inputs),
        "source_uris": [str(path) for path in result.availability.source_uris],
        "input_sha256": result.availability.input_sha256,
        "identity": (
            result.identity.model_dump(mode="json")
            if result.identity is not None
            else None
        ),
        "reason": result.reason,
        "attribution": (
            result.attribution.model_dump(mode="json")
            if result.attribution is not None
            else None
        ),
    }


def _read_frozen_record(
    path: Path,
    expected_sha256: str,
    *,
    artifact_root: Path,
    label: str,
) -> dict[str, object]:
    """从运行状态目录重读不可变记录，并核验实际字节 SHA。"""

    resolved = path.expanduser().resolve(strict=False)
    state_root = (
        artifact_root.expanduser().resolve(strict=False) / "state" / "pilot_direction"
    )
    if state_root != resolved.parent:
        raise _runner_error(f"{label}记录必须位于本次 artifact_root 的状态目录")
    try:
        content = resolved.read_bytes()
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise _runner_error(f"{label}记录无法读取或解析") from error
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise _runner_error(f"{label}记录实际字节 SHA 不一致")
    if not isinstance(payload, dict):
        raise _runner_error(f"{label}记录必须是 JSON object")
    return payload


def _validate_frozen_evaluation_records(
    *,
    candidate: PilotFixedCandidate,
    result: FixedPilotCandidateResult,
    request: PilotRunRequest,
    expected_provenance: Mapping[str, object],
) -> ICDiagnostics:
    """重读方向与确认记录，拒绝内存摘要或窗口替换。"""

    assert result.windows is not None
    protocol = LongOnlyResearchProtocol()
    direction_payload = _read_frozen_record(
        result.windows.direction_record_path,
        result.windows.direction_record_sha256,
        artifact_root=request.artifact_root,
        label="方向",
    )
    if direction_payload.get("version") != "pilot-direction-freeze-v1":
        raise _runner_error(f"{candidate.candidate_id} 方向记录版本无效")
    try:
        stored_protocol = LongOnlyResearchProtocol.model_validate(
            direction_payload.get("protocol")
        )
        stored_direction = DirectionDecision.model_validate(
            direction_payload.get("decision")
        )
    except ValueError as error:
        raise _runner_error(f"{candidate.candidate_id} 方向记录合同无效") from error
    identity = direction_payload.get("identity")
    direction_provenance = direction_payload.get("provenance")
    if (
        stored_protocol != protocol
        or stored_direction != result.windows.direction
        or not isinstance(identity, dict)
        or identity.get("candidate_id") != candidate.candidate_id
        or identity.get("spec_sha256") != candidate.spec_hash
        or direction_provenance != dict(expected_provenance)
    ):
        raise _runner_error(
            f"{candidate.candidate_id} 方向记录身份、窗口或 provenance 不一致"
        )

    confirmation_payload = _read_frozen_record(
        result.windows.confirmation_record_path,
        result.windows.confirmation_record_sha256,
        artifact_root=request.artifact_root,
        label="确认",
    )
    if confirmation_payload.get("version") != "pilot-confirmation-freeze-v1":
        raise _runner_error(f"{candidate.candidate_id} 确认记录版本无效")
    if (
        confirmation_payload.get("candidate_id") != candidate.candidate_id
        or confirmation_payload.get("spec_sha256") != candidate.spec_hash
        or confirmation_payload.get("window_start")
        != protocol.confirmation_start.isoformat()
        or confirmation_payload.get("window_end")
        != protocol.confirmation_end.isoformat()
        or confirmation_payload.get("provenance") != dict(expected_provenance)
        or confirmation_payload.get("direction_record_sha256")
        != result.windows.direction_record_sha256
    ):
        raise _runner_error(
            f"{candidate.candidate_id} 确认窗口、候选身份或 provenance 不一致"
        )
    try:
        stored_confirmation = ICDiagnostics.model_validate(
            confirmation_payload.get("diagnostics")
        )
    except ValueError as error:
        raise _runner_error(f"{candidate.candidate_id} 确认诊断合同无效") from error
    observed_dates = {item.date for item in stored_confirmation.daily}
    if (
        confirmation_payload.get("valid_date_count") != len(stored_confirmation.daily)
        or len(observed_dates) != len(stored_confirmation.daily)
        or any(
            item < protocol.confirmation_start or item > protocol.confirmation_end
            for item in observed_dates
        )
    ):
        raise _runner_error(f"{candidate.candidate_id} 确认窗口日期或计数不一致")
    if (
        stored_confirmation != result.windows.confirmation
        or result.ic != stored_confirmation
    ):
        raise _runner_error(f"{candidate.candidate_id} 确认诊断与公开 IC 身份不一致")
    return stored_confirmation


def _validate_fixed_publication_inputs(
    candidates: PilotFixedCandidateFile,
    candidate_results: Mapping[str, FixedPilotCandidateResult],
    input_manifest: PilotInputManifest | None,
    evaluation_policy: EvaluationPolicySpec,
    portfolio_policy: PortfolioEvaluationPolicy,
    barra_policy: BarraEvaluationPolicy | None,
    request: PilotRunRequest,
    frozen_family_size: int,
    source_run_id: str | None,
) -> None:
    """核验发布前必须齐全且身份一致的输入。"""

    expected_ids = tuple(candidate.candidate_id for candidate in candidates.candidates)
    if not expected_ids or len(set(expected_ids)) != len(expected_ids):
        raise _runner_error("Pilot 发布的候选身份必须非空且唯一")
    if set(candidate_results) != set(expected_ids):
        raise _runner_error("Pilot 发布缺少候选结果或包含未知候选")
    if input_manifest is None:
        raise _runner_error("Pilot 发布缺少 QuantLake/基准输入身份")
    if frozen_family_size < len(expected_ids):
        raise _runner_error("冻结研究族规模不能小于本次候选数")
    if request.data_release_id != input_manifest.resolved_release_id:
        raise _runner_error("Pilot 请求 data_release_id 与输入清单不一致")
    if request.evaluation_policy_id != evaluation_policy_id(evaluation_policy):
        raise _runner_error("Pilot 请求 evaluation_policy_id 与评价政策不一致")
    expected_portfolio_policy = portfolio_policy_id(portfolio_policy)
    expected_barra_policy = barra_policy_id(barra_policy) if barra_policy else None
    expected_provenance = _evaluation_record_provenance(
        input_manifest=input_manifest,
        evaluation_policy=evaluation_policy,
        family_size=frozen_family_size,
        request=request,
        source_run_id=source_run_id,
    )
    for candidate in candidates.candidates:
        result = candidate_results[candidate.candidate_id]
        if result.candidate_id != candidate.candidate_id:
            raise _runner_error("候选结果 candidate_id 与固定候选不一致")
        if result.ic is None:
            raise _runner_error(f"{candidate.candidate_id} 缺少 IC 结果")
        if result.portfolio is None:
            raise _runner_error(f"{candidate.candidate_id} 缺少组合或基准结果")
        if result.barra is None:
            raise _runner_error(f"{candidate.candidate_id} 缺少 Barra 可选状态")
        if result.windows is None:
            raise _runner_error(f"{candidate.candidate_id} 缺少方向发现与确认结果")
        stored_confirmation = _validate_frozen_evaluation_records(
            candidate=candidate,
            result=result,
            request=request,
            expected_provenance=expected_provenance,
        )
        expected_direction = select_direction(
            result.windows.direction.discovery_summary,
            hypothesis_direction=candidate.spec.hypothesis.expected_sign.value,
        )
        if result.windows.direction != expected_direction:
            raise _runner_error(f"{candidate.candidate_id} 冻结方向与发现期五日 RankIC 不一致")
        expected_confirmation_passed = _confirmation_passed(
            stored_confirmation,
            selected_direction=result.windows.direction.selected_direction,
            policy=evaluation_policy,
            family_size=frozen_family_size,
        )
        if result.windows.family_size != frozen_family_size:
            raise _runner_error(f"{candidate.candidate_id} Bonferroni 研究族规模不一致")
        if (
            result.windows.confirmation_passed != expected_confirmation_passed
            or result.windows.stress_test_eligible != expected_confirmation_passed
        ):
            raise _runner_error(f"{candidate.candidate_id} 确认或压力测试资格与冻结规则不一致")
        if result.recent_portfolio_metrics is None:
            raise _runner_error(f"{candidate.candidate_id} 缺少近期目标多头指标")
        if result.portfolio.backtest.direction != result.windows.direction.selected_direction:
            raise _runner_error(f"{candidate.candidate_id} 组合方向未绑定冻结发现方向")
        if result.portfolio.backtest.policy_id != expected_portfolio_policy:
            raise _runner_error(f"{candidate.candidate_id} 组合政策身份不一致")
        if (
            result.portfolio.metrics.calendar_version
            != input_manifest.calendar.calendar_version
            or result.portfolio.metrics.calendar_sha256
            != input_manifest.calendar.calendar_sha256
        ):
            raise _runner_error(f"{candidate.candidate_id} 组合日历身份不一致")
        if result.barra.attribution is not None and expected_barra_policy is None:
            raise _runner_error(f"{candidate.candidate_id} 有 Barra 归因但缺少 Barra 政策")
        if (
            result.barra.attribution is not None
            and result.barra.attribution.policy_id != expected_barra_policy
        ):
            raise _runner_error(f"{candidate.candidate_id} Barra 政策身份不一致")


def publish_fixed_pilot_run(
    *,
    candidates: PilotFixedCandidateFile,
    candidate_results: Mapping[str, FixedPilotCandidateResult],
    input_manifest: PilotInputManifest | None,
    evaluation_policy: EvaluationPolicySpec,
    portfolio_policy: PortfolioEvaluationPolicy,
    barra_policy: BarraEvaluationPolicy | None,
    request: PilotRunRequest,
    pilot_run_id: str | None = None,
    source_run_id: str | None = None,
    frozen_family_size: int | None = None,
) -> FixedPilotPublication:
    """原子发布阶段 A 候选批次的不可变运行产物。

    运行身份由候选 Spec、完整输入清单、冻结政策、可见区间、代码提交和
    配置哈希共同决定。候选指标不参与发布判定，全部候选表现差也会保留
    完整记录；底层 `publish_run_artifacts` 负责同一运行的幂等和篡改核验。
    """

    family_size = (
        len(candidates.candidates)
        if frozen_family_size is None
        else frozen_family_size
    )
    _validate_fixed_publication_inputs(
        candidates,
        candidate_results,
        input_manifest,
        evaluation_policy,
        portfolio_policy,
        barra_policy,
        request,
        family_size,
        source_run_id,
    )
    assert input_manifest is not None
    ordered_results = [candidate_results[candidate.candidate_id] for candidate in candidates.candidates]
    input_manifest_payload = input_manifest.model_dump(mode="json")
    input_manifest_sha256 = sha256_json(input_manifest_payload)
    candidate_identities: list[dict[str, object]] = []
    for candidate, result in zip(candidates.candidates, ordered_results, strict=True):
        assert result.ic is not None
        assert result.portfolio is not None
        assert result.barra is not None
        assert result.windows is not None
        candidate_identities.append(
            {
                "candidate_id": candidate.candidate_id,
                "spec_sha256": candidate.spec_hash,
                "ic_sha256": sha256_json(result.ic.model_dump(mode="json")),
                "portfolio_result_sha256": result.portfolio.backtest.result_sha256,
                "portfolio_metrics_sha256": result.portfolio.metrics.metrics_sha256,
                "barra_sha256": (
                    result.barra.attribution.result_sha256
                    if result.barra.attribution is not None
                    else sha256_json(_barra_result_payload(result.barra))
                ),
                "barra_status": result.barra.availability.status,
                "direction_record_sha256": result.windows.direction_record_sha256,
                "direction_record_ref": str(
                    result.windows.direction_record_path.resolve(strict=False).relative_to(
                        request.artifact_root.resolve(strict=False)
                    )
                ),
                "confirmation_record_sha256": (
                    result.windows.confirmation_record_sha256
                ),
                "confirmation_record_ref": str(
                    result.windows.confirmation_record_path.resolve(strict=False).relative_to(
                        request.artifact_root.resolve(strict=False)
                    )
                ),
                "selected_direction": result.windows.direction.selected_direction,
                "confirmation_passed": result.windows.confirmation_passed,
                "stress_test_eligible": result.windows.stress_test_eligible,
            }
        )
    identity_payload = {
        "candidate_file_sha256": request.candidate_file_sha256,
        "candidate_identities": sorted(
            candidate_identities,
            key=lambda item: str(item["candidate_id"]),
        ),
        "input_manifest_sha256": input_manifest_sha256,
        "evaluation_policy_id": request.evaluation_policy_id,
        "portfolio_policy_id": portfolio_policy_id(portfolio_policy),
        "barra_policy_id": barra_policy_id(barra_policy) if barra_policy else None,
        "visible_start": request.visible_start.isoformat(),
        "visible_end": request.visible_end.isoformat(),
        "data_release_id": request.data_release_id,
        "code_commit": request.code_commit,
        "config_hash": request.config_hash,
        "source_run_id": source_run_id,
        "long_only_protocol": LongOnlyResearchProtocol().model_dump(mode="json"),
        "frozen_family_size": family_size,
    }
    derived_run_id = f"run_{sha256_json(identity_payload)[:24]}"
    if pilot_run_id is not None and pilot_run_id != derived_run_id:
        raise _runner_error("显式 pilot_run_id 与不可变输入身份不一致")

    candidate_specs = {
        candidate.candidate_id: candidate.spec.model_dump(mode="json")
        for candidate in candidates.candidates
    }
    ic_payload = {
        "version": "pilot-ic-diagnostics-v1",
        "candidates": {
            candidate.candidate_id: result.ic.model_dump(mode="json")
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    recent_ic_payload = {
        "version": "pilot-recent-ic-v1",
        "window": "2025-01-01/2026-06-30",
        "candidates": {
            candidate.candidate_id: result.windows.recent.model_dump(mode="json")
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    direction_payload = {
        "version": "pilot-direction-decisions-v1",
        "candidates": {
            candidate.candidate_id: {
                "decision": result.windows.direction.model_dump(mode="json"),
                "direction_record_sha256": result.windows.direction_record_sha256,
                "direction_record_ref": str(
                    result.windows.direction_record_path.resolve(strict=False).relative_to(
                        request.artifact_root.resolve(strict=False)
                    )
                ),
            }
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    eligibility_payload = {
        "version": "pilot-stress-eligibility-v1",
        "stress_tests_executed": False,
        "candidates": {
            candidate.candidate_id: {
                "confirmation_passed": result.windows.confirmation_passed,
                "stress_test_eligible": result.windows.stress_test_eligible,
                "confirmation_record_sha256": (
                    result.windows.confirmation_record_sha256
                ),
                "confirmation_record_ref": str(
                    result.windows.confirmation_record_path.resolve(strict=False).relative_to(
                        request.artifact_root.resolve(strict=False)
                    )
                ),
            }
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    portfolio_daily_payload = {
        "version": "pilot-portfolio-v1",
        "candidates": {
            candidate.candidate_id: {
                "daily": result.portfolio.backtest.daily_returns,
                "weights": result.portfolio.backtest.weights,
            }
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    portfolio_metrics_payload = {
        "version": "pilot-portfolio-metrics-v1",
        "candidates": {
            candidate.candidate_id: result.portfolio.metrics.model_dump(mode="json")
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    portfolio_governance_payload = {
        "version": "pilot-portfolio-governance-v1",
        "candidates": {
            candidate.candidate_id: result.portfolio.backtest.governance.model_dump(
                mode="json"
            )
            for candidate, result in zip(
                candidates.candidates,
                ordered_results,
                strict=True,
            )
        },
    }
    recent_portfolio_metrics_payload = {
        "version": "pilot-recent-portfolio-metrics-v1",
        "window": "2025-01-01/2026-06-30",
        "candidates": {
            candidate.candidate_id: result.recent_portfolio_metrics.model_dump(
                mode="json"
            )
            for candidate, result in zip(
                candidates.candidates,
                ordered_results,
                strict=True,
            )
        },
    }
    barra_payload = {
        "version": "pilot-barra-v1",
        "policy_id": barra_policy_id(barra_policy) if barra_policy else None,
        "candidates": {
            candidate.candidate_id: _barra_result_payload(result.barra)
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    run_metrics = {
        "version": "pilot-run-metrics-v1",
        "status": "published",
        "pilot_run_id": derived_run_id,
        "data_release_id": request.data_release_id,
        "input_manifest_sha256": input_manifest_sha256,
        "candidate_file_sha256": request.candidate_file_sha256,
        "evaluation_policy_id": request.evaluation_policy_id,
        "portfolio_policy_id": portfolio_policy_id(portfolio_policy),
        "barra_policy_id": barra_policy_id(barra_policy) if barra_policy else None,
        "visible_start": request.visible_start.isoformat(),
        "visible_end": request.visible_end.isoformat(),
        "code_commit": request.code_commit,
        "config_hash": request.config_hash,
        "source_run_id": source_run_id,
        "long_only_protocol": LongOnlyResearchProtocol().model_dump(mode="json"),
        "frozen_family_size": family_size,
        "candidate_count": len(candidates.candidates),
        "candidates": {
            str(item["candidate_id"]): item for item in candidate_identities
        },
        "candidate_summaries": candidate_identities,
    }
    visualization_payload = {
        "version": "pilot-visualization-v1",
        "candidates": {
            candidate.candidate_id: {
                "ic_sequence": list(result.ic.ic_sequence),
                "rank_ic_sequence": list(result.ic.rank_ic_sequence),
                "portfolio_daily": result.portfolio.backtest.daily_returns,
                "portfolio_metrics": result.portfolio.metrics.model_dump(mode="json"),
            }
            for candidate, result in zip(candidates.candidates, ordered_results, strict=True)
        },
    }
    artifacts: dict[str, bytes] = {
        "run/metrics.json": _json_bytes(run_metrics),
        "run/input_manifest.json": _json_bytes(
            {
                "input_manifest_sha256": input_manifest_sha256,
                "manifest": input_manifest_payload,
            }
        ),
        "ic/diagnostics.json": _json_bytes(ic_payload),
        "ic/recent.json": _json_bytes(recent_ic_payload),
        "direction/decisions.json": _json_bytes(direction_payload),
        "run/eligibility.json": _json_bytes(eligibility_payload),
        "portfolio/daily.json": _json_bytes(portfolio_daily_payload),
        "portfolio/metrics.json": _json_bytes(portfolio_metrics_payload),
        "portfolio/governance.json": _json_bytes(portfolio_governance_payload),
        "portfolio/recent_metrics.json": _json_bytes(
            recent_portfolio_metrics_payload
        ),
        "barra/attribution.json": _json_bytes(barra_payload),
        "visualization/pilot.json": _json_bytes(visualization_payload),
    }
    for candidate in candidates.candidates:
        artifacts[f"candidates/{candidate.candidate_id}/spec.json"] = _json_bytes(
            candidate_specs[candidate.candidate_id]
        )
    manifest = publish_run_artifacts(request.artifact_root, derived_run_id, artifacts)
    return FixedPilotPublication(
        run_id=derived_run_id,
        manifest=manifest,
        artifact_root=request.artifact_root,
    )


def build_benchmark_open_to_open_returns(
    index_panel: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.LazyFrame:
    """从 CSI300 指数开盘价构造唯一 exit_date 基准收益。"""

    schema = index_panel.collect_schema().names()
    date_column = "trade_date" if "trade_date" in schema else "date"
    if date_column not in schema or "open" not in schema:
        raise _runner_error("CSI300 指数面板必须包含 date/trade_date 和 open")
    date_dtype = index_panel.collect_schema()[date_column]
    if date_dtype == pl.Date:
        index = index_panel.rename({date_column: "trade_date"})
    elif isinstance(date_dtype, pl.Datetime):
        non_midnight = index_panel.filter(
            pl.col(date_column) != pl.col(date_column).dt.truncate("1d")
        ).limit(1).collect()
        if non_midnight.height:
            raise _runner_error("CSI300 Datetime 日期必须全部位于午夜")
        index = index_panel.with_columns(
            pl.col(date_column).cast(pl.Date).alias("trade_date")
        ).drop(date_column)
    else:
        raise _runner_error("CSI300 日期字段必须是 Date 或无时区午夜 Datetime")
    duplicate = (
        index.group_by("trade_date")
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
        .collect()
    )
    if duplicate.height:
        raise _runner_error("CSI300 trade_date 主键重复")
    if index.filter(
        pl.col("open").is_null() | (pl.col("open") <= 0)
    ).limit(1).collect().height:
        raise _runner_error("CSI300 指数开盘价缺失或非正")
    schedule_frame = pl.DataFrame(
        {
            "entry_date": [item.entry_date for item in schedule],
            "exit_date": [item.exit_date for item in schedule],
        }
    ).lazy()
    entry = index.rename(
        {
            "trade_date": "entry_date",
            "open": "entry_open",
        }
    )
    exit_frame = index.rename(
        {
            "trade_date": "exit_date",
            "open": "exit_open",
        }
    )
    result = (
        schedule_frame.join(entry, on="entry_date", how="left")
        .join(exit_frame, on="exit_date", how="left")
    )
    if result.filter(
        pl.col("entry_open").is_null() | pl.col("exit_open").is_null()
    ).limit(1).collect().height:
        raise _runner_error("CSI300 缺少组合窗口对应开盘价")
    return result.select(
        [
            "exit_date",
            (pl.col("exit_open") / pl.col("entry_open") - 1.0).alias("benchmark_return"),
        ]
    )


def evaluate_fixed_candidate_portfolio(
    signal_panel: FixedSignalPanel,
    market_panel: pl.LazyFrame,
    benchmark_returns: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    calendar: pl.DataFrame,
    *,
    calendar_identity: TradingCalendarIdentity,
    policy: PortfolioEvaluationPolicy | None = None,
    direction: Literal["positive", "negative"] = "positive",
) -> FixedPortfolioEvaluation:
    """执行一个固定候选的目标多头和日历年化指标。"""

    evaluation_policy = policy or PortfolioEvaluationPolicy()
    trading_mask = signal_panel.trading_mask.lazy().rename(
        {
            "signal_date": "mask_date",
            "security_id": "mask_asset",
        }
    )
    factor = signal_panel.frame.lazy().join(
        trading_mask,
        left_on=["signal_date", "security_id"],
        right_on=["mask_date", "mask_asset"],
        how="left",
    )
    if factor.filter(pl.col("valid_for_trading").is_null()).limit(1).collect().height:
        raise _runner_error("信号面板缺少 valid_for_trading 掩码")
    factor = factor.filter(
        (pl.col("valid_for_trading") == True)
        & pl.col("factor_value").is_not_null()
    ).select(
        ["signal_date", "security_id", "factor_value"]
    )
    aligned = align_open_to_open_panel(factor, market_panel, schedule)
    backtest = run_target_long_backtest(
        aligned,
        benchmark_returns,
        schedule,
        evaluation_policy,
        direction=direction,
    )
    daily = pl.DataFrame(backtest.daily_returns)
    metrics = calculate_portfolio_metrics(
        daily,
        benchmark_returns.collect(),
        calendar,
        calendar_version=calendar_identity.calendar_version,
        calendar_sha256=calendar_identity.calendar_sha256,
    )
    return FixedPortfolioEvaluation(backtest=backtest, metrics=metrics)


def _load_pilot_calendar(
    paths: PilotSourcePaths,
    start: date,
    end: date,
) -> pl.DataFrame:
    """先下推研究边界，再收集普通 Pilot 交易日历。"""

    if paths.calendar_uri is None:
        raise _runner_error("阶段 A 主链路缺少交易日历入口")
    return (
        _scan_manifest_partitions(
            paths.calendar_uri,
            paths.calendar_manifest_uri,
            "calendar",
            start,
            end,
        )
        .filter(pl.col("trade_date").is_between(start, end, closed="both"))
        .select(["trade_date", "is_open"])
        .collect()
    )


def _load_pilot_market_open(
    paths: PilotSourcePaths,
    start: date,
    end: date,
) -> pl.LazyFrame:
    """先下推阶段边界，再返回规范化个股开盘价。"""

    if paths.market_open_uri is None:
        source = _scan_manifest_partitions(
            paths.market_uri,
            getattr(paths, "partition_manifest_uri", None) or paths.release_manifest_uri,
            "market",
            start,
            end,
            partition_key="market_partitions",
        ).filter(
            pl.col("date").is_between(start, end, closed="both")
        )
        return source.select(
            pl.col("date").alias("trade_date"),
            pl.col("code").alias("security_id"),
            pl.col("adj_open").alias("open"),
        )
    source = _scan_manifest_partitions(
        paths.market_open_uri,
        paths.market_open_manifest_uri,
        "market_open",
        start,
        end,
    ).filter(
        pl.col("date").cast(pl.Date).is_between(start, end, closed="both")
    )
    source = _normalize_date_column(
        source,
        "date",
        "CSI300 成分股开盘价",
    )
    return source.select(
        pl.col("date").alias("trade_date"),
        pl.col("order_book_id").alias("security_id"),
        pl.col("open"),
    )


def _load_pilot_benchmark(
    paths: PilotSourcePaths,
    start: date,
    end: date,
) -> pl.LazyFrame:
    """先下推阶段边界，再返回基准日期与开盘价。"""

    if paths.benchmark_uri is None:
        raise _runner_error("阶段 A 主链路缺少 CSI300 入口")
    if paths.benchmark_manifest_uri is None:
        raise _runner_error("阶段 A 主链路缺少 CSI300 发布 manifest")
    date_column = _manifest_date_column(
        paths.benchmark_manifest_uri,
        "benchmark",
    )
    source = _scan_manifest_partitions(
        paths.benchmark_uri,
        paths.benchmark_manifest_uri,
        "benchmark",
        start,
        end,
    )
    source = source.filter(
        pl.col(date_column).cast(pl.Date).is_between(start, end, closed="both")
    )
    columns = source.collect_schema().names()
    if date_column not in columns or "open" not in columns:
        raise _runner_error("CSI300 指数面板必须包含 date/trade_date 和 open")
    return source.select([date_column, "open"])


def run_fixed_pilot(
    *,
    candidates: PilotFixedCandidateFile,
    paths: PilotSourcePaths,
    request: PilotRunRequest,
    evaluation_policy: EvaluationPolicySpec | None = None,
    portfolio_policy: PortfolioEvaluationPolicy | None = None,
    barra_policy: BarraEvaluationPolicy | None = None,
    source_run_id: str | None = None,
    frozen_family_size: int | None = None,
) -> FixedPilotPublication:
    """在 Linux 服务器侧执行冻结候选批次的完整阶段 A 主链路。

    该入口只接受显式服务器路径和已经冻结的 ``PilotRunRequest``。它在同一
    运行中完成输入清单、原始因子、真实日历窗口、IC、组合和可选 Barra 状态，
    最后调用不可变发布器；不会写入 QuantLake 或任何 LLM 账本。
    """

    protocol = LongOnlyResearchProtocol()
    family_size = (
        len(candidates.candidates)
        if frozen_family_size is None
        else frozen_family_size
    )
    if family_size < len(candidates.candidates):
        raise _runner_error("冻结研究族规模不能小于本次候选数")
    if (
        request.visible_start != protocol.discovery_start
        or request.visible_end != protocol.confirmation_end
    ):
        raise _runner_error(
            "long-only-v1 运行必须完整读取 2021-01-01 至 2026-06-30，禁止旧 2012 起点"
        )
    if paths.calendar_uri is None or paths.benchmark_uri is None:
        raise _runner_error("阶段 A 主链路缺少交易日历或 CSI300 入口")
    evaluation = evaluation_policy or company_a_share_visible_policy()
    portfolio = portfolio_policy or PortfolioEvaluationPolicy()
    warmup = max(candidate.spec.max_lookback for candidate in candidates.candidates)
    required_fields = tuple(sorted({
        field
        for candidate in candidates.candidates
        for field in candidate.spec.required_fields
    }))

    # 阶段一只允许读取发现期因子输入、行情、日历与五日标签。这里不能调用
    # inspect_pilot_sources，因为完整清单会扫描确认期 benchmark/market/state。
    discovery_source = PilotQuantLakeFactorInputSource(
        paths,
        code_commit=request.code_commit,
        config_hash=request.config_hash,
        discovery_only=True,
    )
    discovery_provenance = discovery_source.inspect_inputs()
    if request.data_release_id != discovery_provenance.resolved_release_id:
        raise _runner_error("Pilot 请求 data_release_id 与服务器 release 不一致")
    discovery_request = FactorInputRequest(
        start=protocol.discovery_start,
        end=protocol.discovery_end,
        required_fields=required_fields,
        warmup_observations=warmup,
    )
    discovery_panels = compute_fixed_signal_panels(
        candidates,
        discovery_source,
        discovery_request,
        request.artifact_root / "work" / "pilot_discovery",
    )
    discovery_calendar = _load_pilot_calendar(
        paths,
        protocol.discovery_start,
        protocol.discovery_end,
    )
    discovery_market = _load_pilot_market_open(
        paths,
        protocol.discovery_start,
        protocol.discovery_end,
    )
    planned_manifest = inspect_pilot_source_identity(paths)
    if request.data_release_id != planned_manifest.resolved_release_id:
        raise _runner_error("Pilot 请求 data_release_id 与服务器 release 不一致")
    record_provenance = _evaluation_record_provenance(
        input_manifest=planned_manifest,
        evaluation_policy=evaluation,
        family_size=family_size,
        request=request,
        source_run_id=source_run_id,
    )
    candidate_by_id = {item.candidate_id: item for item in candidates.candidates}
    frozen_directions: dict[str, tuple[DirectionDecision, str, Path]] = {}
    for panel in discovery_panels:
        candidate = candidate_by_id[panel.candidate_id]
        discovery_labeled = build_open_to_open_ic_panel(
            panel,
            discovery_market,
            discovery_calendar,
            horizons=(5,),
        )
        direction_identity_payload = {
            "candidate_id": candidate.candidate_id,
            "spec_sha256": candidate.spec_hash,
            "discovery_provenance": asdict(discovery_provenance),
            "state_manifest_sha256": _file_or_tree_sha256(
                paths.state_manifest_uri
            ),
            "protocol": protocol.model_dump(mode="json"),
            "code_commit": request.code_commit,
            "config_hash": request.config_hash,
            "source_run_id": source_run_id,
        }
        direction_identity = sha256_json(direction_identity_payload)
        direction_record_path = (
            request.artifact_root
            / "state"
            / "pilot_direction"
            / f"direction_{direction_identity[:24]}.json"
        )
        decision, direction_sha256 = _freeze_direction_decision(
            rank_ic=evaluate_discovery_rank_ic(discovery_labeled, evaluation),
            hypothesis_direction=candidate.spec.hypothesis.expected_sign.value,
            direction_record_path=direction_record_path,
            protocol=protocol,
            identity=direction_identity_payload,
            provenance=record_provenance,
        )
        frozen_directions[panel.candidate_id] = (
            decision,
            direction_sha256,
            direction_record_path,
        )

    # 所有方向记录都已原子写入并逐字节回读核验；现在才允许扫描确认期。
    if set(frozen_directions) != set(candidate_by_id):
        raise _runner_error("方向冻结未覆盖全部候选，禁止进入确认期")
    manifest = inspect_pilot_sources(paths)
    if manifest != planned_manifest:
        raise _runner_error("发布 metadata 身份与完整数据合同结果不一致")
    if request.data_release_id != manifest.resolved_release_id:
        raise _runner_error("Pilot 请求 data_release_id 与服务器 release 不一致")
    if request.visible_end > manifest.benchmark.cutoff:
        raise _runner_error("Pilot visible_end 超过 CSI300 基准截止日")
    confirmation_source = PilotQuantLakeFactorInputSource(
        paths,
        code_commit=request.code_commit,
        config_hash=request.config_hash,
        manifest=manifest,
    )
    confirmation_request = FactorInputRequest(
        start=protocol.confirmation_start,
        end=protocol.confirmation_end,
        required_fields=required_fields,
        warmup_observations=warmup,
    )
    panels = compute_fixed_signal_panels(
        candidates,
        confirmation_source,
        confirmation_request,
        request.artifact_root / "work" / "pilot_confirmation",
    )
    calendar = _load_pilot_calendar(
        paths, protocol.confirmation_start, protocol.confirmation_end
    )
    market_open = _load_pilot_market_open(
        paths, protocol.confirmation_start, protocol.confirmation_end
    )
    schedule = build_tuesday_rebalance_schedule(
        calendar, protocol.confirmation_start, protocol.confirmation_end
    )
    recent_schedule = build_tuesday_rebalance_schedule(
        calendar, protocol.recent_start, protocol.recent_end
    )
    if not schedule or not recent_schedule:
        raise _runner_error("确认期或近期没有完整周二 open-to-open 窗口")
    benchmark_panel = _load_pilot_benchmark(
        paths, protocol.confirmation_start, protocol.confirmation_end
    )
    benchmark_returns = build_benchmark_open_to_open_returns(
        benchmark_panel, schedule
    )
    recent_benchmark_returns = build_benchmark_open_to_open_returns(
        benchmark_panel, recent_schedule
    )
    derived_barra: DerivedBarraInputs | None = None
    unavailable_barra: FixedBarraEvaluation
    if manifest.barra.status == "available" and barra_policy is not None:
        try:
            derived_barra = load_derived_barra_inputs(paths, schedule, barra_policy)
        except FactorMinerError as error:
            unavailable_barra = _barra_not_available(
                ("barra_contract",),
                identity=None,
                source_uris=manifest.barra.source_uris,
                input_sha256=manifest.barra.input_sha256,
                reason=str(error),
            )
        else:
            unavailable_barra = FixedBarraEvaluation(
                availability=manifest.barra,
                attribution=None,
                identity=derived_barra.identity,
            )
    else:
        unavailable_barra = _barra_not_available(
            (
                manifest.barra.missing_inputs
                if manifest.barra.status == "not_available"
                else ("barra_policy",)
            ),
            identity=None,
            source_uris=manifest.barra.source_uris,
            input_sha256=manifest.barra.input_sha256,
            reason=(
                "Barra 输入未形成可验证的行业暴露、基准权重和因子收益完整合同"
                if manifest.barra.status == "not_available"
                else "Barra 输入可用但运行未提供冻结政策"
            ),
        )
    results: dict[str, FixedPilotCandidateResult] = {}
    for panel in panels:
        labeled_panel = build_open_to_open_ic_panel(
            panel,
            market_open,
            calendar,
        )
        confirmation = evaluate_ic_horizons(
            labeled_panel,
            FROZEN_HORIZONS,
            evaluation,
        )
        recent = evaluate_ic_horizons(
            _window_slice(labeled_panel, protocol.recent_start, protocol.recent_end),
            FROZEN_HORIZONS,
            evaluation,
        )
        direction, direction_sha256, direction_record_path = frozen_directions[
            panel.candidate_id
        ]
        candidate = candidate_by_id[panel.candidate_id]
        confirmation_record_path = direction_record_path.with_name(
            direction_record_path.name.replace("direction_", "confirmation_", 1)
        )
        confirmation_sha256 = _freeze_confirmation_diagnostics(
            candidate_id=panel.candidate_id,
            spec_sha256=candidate.spec_hash,
            diagnostics=confirmation,
            confirmation_record_path=confirmation_record_path,
            protocol=protocol,
            provenance=record_provenance,
            direction_record_sha256=direction_sha256,
        )
        passed = _confirmation_passed(
            confirmation,
            selected_direction=direction.selected_direction,
            policy=evaluation,
            family_size=family_size,
        )
        windows = LongOnlyWindowEvaluation(
            direction=direction,
            confirmation=confirmation,
            recent=recent,
            confirmation_passed=passed,
            stress_test_eligible=passed,
            direction_record_sha256=direction_sha256,
            direction_record_path=direction_record_path,
            confirmation_record_sha256=confirmation_sha256,
            confirmation_record_path=confirmation_record_path,
            family_size=family_size,
        )
        portfolio_result = evaluate_fixed_candidate_portfolio(
            panel,
            market_open,
            benchmark_returns,
            schedule,
            calendar,
            calendar_identity=manifest.calendar,
            policy=portfolio,
            direction=windows.direction.selected_direction,
        )
        recent_portfolio_result = evaluate_fixed_candidate_portfolio(
            panel,
            market_open,
            recent_benchmark_returns,
            recent_schedule,
            calendar,
            calendar_identity=manifest.calendar,
            policy=portfolio,
            direction=windows.direction.selected_direction,
        )
        candidate_barra = unavailable_barra
        if derived_barra is not None and barra_policy is not None:
            candidate_barra = evaluate_fixed_backtest_barra(
                portfolio_result.backtest,
                derived_barra,
                barra_policy,
                source_uris=manifest.barra.source_uris,
                input_sha256=manifest.barra.input_sha256,
            )
        results[panel.candidate_id] = FixedPilotCandidateResult(
            candidate_id=panel.candidate_id,
            ic=windows.confirmation,
            portfolio=portfolio_result,
            barra=candidate_barra,
            windows=windows,
            recent_portfolio_metrics=recent_portfolio_result.metrics,
        )
    return publish_fixed_pilot_run(
        candidates=candidates,
        candidate_results=results,
        input_manifest=manifest,
        evaluation_policy=evaluation,
        portfolio_policy=portfolio,
        barra_policy=barra_policy,
        request=request,
        source_run_id=source_run_id,
        frozen_family_size=family_size,
    )
