"""Factor Miner 的正式 Typer 命令行入口。"""

from __future__ import annotations

import hashlib
try:
    import fcntl
except ImportError:  # Windows 使用下方的 msvcrt 文件锁。
    fcntl = None  # type: ignore[assignment]
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import typer
from pydantic import ValidationError

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.company_a_share import (
    StandardPanelFactorInputSource,
    StandardPanelOutcomeSource,
    ParquetReferenceFactorSource,
)
from factor_miner.compiler import CompiledFactorPlan, compile_candidate
from factor_miner.coverage_schema import (
    CoverageGraphSpec,
    registered_coverage_graph_spec,
)
from factor_miner.coverage_snapshot import verify_coverage_graph
from factor_miner.research_gap import load_verified_coverage_graph
from factor_miner.coverage_workflow import build_coverage_graph
from factor_miner.data_source import MARKET_COLUMNS
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import JsonlLedger, LedgerPaths, recover_interrupted_runs
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.llm_brief import (
    LLMCoverageBrief,
    LLMCoverageBriefSpec,
    registered_llm_coverage_brief,
)
from factor_miner.llm_agents import (
    HypothesisAgentOutput,
    build_expression_agent_request,
    build_format_repair_request,
    build_hypothesis_agent_request,
    build_hypothesis_tool_continuation,
    build_semantic_lint_agent_request,
)
from factor_miner.llm_candidate import (
    CandidateExpressionBatch,
    SemanticLintBatch,
    convert_candidate,
)
from factor_miner.field_registry import FieldAvailabilityRegistry
from factor_miner.llm_hypothesis import RegisteredCoverageGapHypothesis
from factor_miner.research_evolution import (
    LogicalEvolutionHypothesisDraft,
    approve_evolution_hypotheses,
    build_evolution_hypothesis_request,
    evolution_gap_brief_sha256,
    parse_evolution_hypothesis_response,
    require_approved_evolution_batch,
)
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapReport,
    ResearchEvolutionContext,
    ResearchMemorySnapshot,
    EvolutionHypothesisApproval,
    build_evolution_context,
    build_gap_report_identity,
)
from factor_miner.research_memory import MemorySnapshotPolicy, ResearchMemoryStore
from factor_miner.llm_literature import (
    CrossrefMetadataAdapter,
    LiteratureQuery,
    validate_literature_query,
)
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_online import (
    AgentRole,
    LLMCampaignScopeAuthorization,
    LLMEvolutionRequestAuthorization,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    registered_llm_campaign_scope_authorization,
)
from factor_miner.llm_privacy import (
    CorporateExternalResearchPolicy,
    build_export_preview,
    registered_corporate_external_research_policy,
    scan_export_payload,
)
from factor_miner.llm_provider import (
    UrllibDeepSeekTransport,
    execute_recorded_call,
    replay_recorded_call,
)
from factor_miner.llm_orchestrator import (
    ApprovedBatchDependencies,
    GenerationSealDependencies,
    prepare_approved_batch_export,
    resume_approved_batch,
    run_approved_batch,
    seal_completed_generation,
)
from factor_miner.llm_server_runner import run_server_side_llm_pipeline
from factor_miner.shadow_generators import generate_shadow_arms
from factor_miner.llm_schema import (
    EvaluationDependencyInterval,
    LLMDiscoveryResearchFamilySpec,
    RegisteredLLMDiscoveryResearchFamily,
    registered_llm_discovery_family,
)
from factor_miner.llm_seal import (
    build_generation_seal,
    verify_generation_seal,
)
from factor_miner.llm_state import (
    CANDIDATE_TERMINALS,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    expected_candidate_slot_ids,
)
from factor_miner.research_campaign_runner import (
    MappingCampaignPilotRunner,
    evaluate_sealed_campaign,
    publish_campaign_evaluation,
    run_approved_campaign_generation,
)
from factor_miner.policy import (
    company_a_share_incremental_policy,
    company_a_share_visible_policy,
)
from factor_miner.runtime import (
    ExecutionMode,
    assert_real_data_allowed,
    load_runtime_profile,
    verify_runtime_identity,
)
from factor_miner.regime_data import (
    CompanyAShareRegimeDataSource,
    RegimeDataRequest,
)
from factor_miner.regime_features import build_market_features
from factor_miner.regime_research import (
    regime_research_report_from_payload,
    regime_research_report_payload,
    run_regime_research,
)
from factor_miner.regime_schema import (
    RegimeDeploymentSpec,
    RegimeResearchSpec,
    RegisteredRegimeDeployment,
    RegisteredRegimeResearch,
    registered_regime_deployment,
    registered_regime_research,
)
from factor_miner.regime_snapshot import verify_regime_snapshot
from factor_miner.regime_workflow import (
    build_regime_snapshot,
    validate_regime_deployment,
)
from factor_miner.schema import (
    CampaignSpec,
    CandidateFactorSpec,
    EvaluationPolicySpec,
    IncrementalEvaluationPolicySpec,
    ReferenceFactorLibrarySpec,
    RegisteredCandidate,
    RegisteredReferenceFactorLibrary,
    RegisteredResearchFamily,
    RegisteredTrustedCandidate,
    ResearchFamilySpec,
    TrustedCandidateFactorSpec,
    TrustedVisibleCampaignSpec,
    campaign_id,
    evaluation_policy_id,
    registered_reference_factor_library,
    registered_research_family,
    registered_candidate,
    registered_trusted_candidate,
    trusted_campaign_id,
    validate_incremental_policy_library,
    validate_trusted_campaign,
)
from factor_miner.workflow import (
    RunResult,
    run_incremental_visible_campaign,
    run_trusted_smoke_campaign,
    run_trusted_visible_campaign,
)


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="独立、可审计的因子候选研究命令行入口。",
)
regime_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="研究、冻结、构建并核验 point-in-time 市场状态日历。",
)
coverage_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="构建并核验数学形式与实际信号模式双层因子覆盖图谱。",
)
llm_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="登记、封存并核验受控离线协议；不连接外部模型或结果数据。",
)
pilot_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="执行受控候选研究、产物发布和只读 Dashboard 投影。",
)
campaign_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="执行正式研究族的 120 槽治理、评价和发布。",
)
evolution_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="准备脱敏研究上下文、生成草案并执行人工十槽批准闸门。",
)
research_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="运行 Dashboard 驱动的单机自主研究 Worker。",
)
barra_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="下载、规范化并核验服务器侧 Barra 风险模型派生数据。",
)
screening_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="冻结候选筛选政策并生成方向感知稳健性报告。",
)
data_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="准备并核验跨市场本地数据发布。",
)
app.add_typer(regime_app, name="regime")
app.add_typer(coverage_app, name="coverage")
app.add_typer(llm_app, name="llm")
app.add_typer(pilot_app, name="pilot")
app.add_typer(campaign_app, name="campaign")
app.add_typer(evolution_app, name="evolution")
app.add_typer(research_app, name="research")
app.add_typer(barra_app, name="barra")
app.add_typer(screening_app, name="screening")
app.add_typer(data_app, name="data")


@data_app.command("prepare-local")
def data_prepare_local_command(
    input_path: Path = typer.Argument(..., help="含标准 OHLCV 列的 CSV 或 Parquet。"),
    output_root: Path = typer.Option(..., "--output-root", help="新的标准数据发布目录。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", help="与数据目录分离的运行产物目录。"),
    adjustment_convention: str = typer.Option(..., "--adjustment-convention"),
    calendar_version: str = typer.Option(..., "--calendar-version"),
    data_origin: str = typer.Option("local_files", "--data-origin"),
    assume_tradable: bool = typer.Option(
        False,
        "--assume-tradable",
        help="仅在输入没有三类有效性 mask 且用户明确确认时使用。",
    ),
) -> None:
    """把本地行情转换为带清单、标签隔离和配置哈希的标准发布。"""

    try:
        from factor_miner.local_data import prepare_local_release

        release = prepare_local_release(
            input_path,
            output_root,
            artifact_root=artifact_root,
            adjustment_convention=adjustment_convention,
            calendar_version=calendar_version,
            data_origin=data_origin,
            assume_tradable=assume_tradable,
        )
        _print_json({
            "status": "prepared",
            "release_id": release.release_id,
            "release_root": str(release.release_root),
            "manifest_path": str(release.manifest_path),
            "config_path": str(release.config_path),
        })
    except Exception as error:
        _fail(error)


def _evolution_json(root: Path, names: tuple[str, ...], *, label: str) -> dict[str, Any]:
    """从显式演化输入根读取唯一的正式 JSON 对象。"""

    for name in names:
        candidate = root / name
        if candidate.is_file():
            payload = _read_json(candidate)
            if isinstance(payload, dict):
                return payload
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"{label} 必须是 JSON object")
    matches: list[Path] = []
    for candidate in sorted(root.rglob("*.json")):
        try:
            payload = _read_json(candidate)
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and any(key in payload for key in ("snapshot_sha256", "report_sha256", "manifest_sha256")):
            matches.append(candidate)
    if len(matches) != 1:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"{label} 未找到唯一正式 JSON")
    payload = _read_json(matches[0])
    if not isinstance(payload, dict):
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"{label} 必须是 JSON object")
    return payload


def _require_raw_identity(payload: object, field: str, *, label: str) -> str:
    """在 schema 解析前要求输入显式携带并匹配内容 hash。"""

    if not isinstance(payload, dict):
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 必须是 JSON object")
    actual = payload.get(field)
    if not isinstance(actual, str) or not actual.strip():
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 缺少非空 {field}")
    identity_payload = {
        key: value for key, value in payload.items() if key != field
    }
    # semantic_plan 是后加的非阻断字段；旧草案的 model_dump 会显式带 null，
    # 但历史 draft_sha256 在该字段存在前生成，必须继续按缺失字段复核。
    if field == "draft_sha256" and identity_payload.get("semantic_plan") is None:
        identity_payload.pop("semantic_plan", None)
    expected = sha256_json(identity_payload)
    if actual != expected:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 的 {field} 与内容不一致")
    return actual


def _require_raw_approval_batch_identity(payload: object, *, label: str) -> str:
    """只按批准批次 schema 的三字段身份核验 wrapper 中的批次 hash。"""

    if not isinstance(payload, dict):
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 必须是 JSON object")
    actual = payload.get("approval_batch_sha256")
    if not isinstance(actual, str) or not actual.strip():
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 缺少非空 approval_batch_sha256")
    batch_payload = {
        field: payload.get(field)
        for field in ("context_sha256", "discovery_family_id", "approvals")
    }
    batch = ApprovedEvolutionHypothesisBatch.model_validate(batch_payload)
    expected = batch.approval_batch_sha256
    if actual != expected:
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            f"{label} 的 approval_batch_sha256 与批准批次内容不一致",
        )
    return actual


def _require_raw_gap_report(payload: object, *, label: str) -> dict[str, Any]:
    """在 CoverageGapReport 自动补 hash 前验证报告和每张卡片。"""

    if not isinstance(payload, dict):
        raise FactorMinerError(FailureCode.COVERAGE_GAP_INVALID, f"{label} 必须是 JSON object")
    _require_raw_identity(payload, "report_sha256", label=label)
    cards = payload.get("gap_cards")
    if not isinstance(cards, list) or not cards:
        raise FactorMinerError(FailureCode.COVERAGE_GAP_INVALID, f"{label} 缺少 gap_cards")
    for index, card in enumerate(cards):
        _require_raw_identity(card, "card_sha256", label=f"{label}.gap_cards[{index}]")
    return payload


_DRAFT_BATCH_PROVENANCE_FIELDS = (
    "request_sha256",
    "report_sha256",
    "gap_card_sha256",
    "context_sha256",
    "discovery_family_id",
)


def _draft_batch_sha256(payload: dict[str, Any]) -> str:
    """从生成时的原始身份字段和十个 draft hash 计算批次身份。"""

    drafts = payload.get("drafts")
    if not isinstance(drafts, list) or len(drafts) != 10:
        raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "draft-batch 必须正好包含十个 drafts")
    draft_hashes: list[str] = []
    for index, item in enumerate(drafts):
        draft_hashes.append(_require_raw_identity(item, "draft_sha256", label=f"drafts[{index}]"))
    identity = {
        "request_sha256": payload.get("request_sha256"),
        "report_sha256": payload.get("report_sha256"),
        "gap_card_sha256": payload.get("gap_card_sha256"),
        "context_sha256": payload.get("context_sha256"),
        "discovery_family_id": payload.get("discovery_family_id"),
        "draft_sha256": draft_hashes,
    }
    for field in _DRAFT_BATCH_PROVENANCE_FIELDS:
        value = identity[field]
        if not isinstance(value, (str, dict)) or not value:
            raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"draft-batch 缺少原始 provenance：{field}")
    if not isinstance(identity["request_sha256"], str) or len(identity["request_sha256"]) != 64:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "draft-batch request_sha256 非法")
    return sha256_json(identity)


def _validate_draft_batch_provenance(
    payload: dict[str, Any],
    *,
    context_payload: dict[str, Any],
    context: ResearchEvolutionContext,
    label: str,
) -> str:
    """只接受生成产物携带的 provenance，不从当前 context 反向补齐。"""

    for field in _DRAFT_BATCH_PROVENANCE_FIELDS + ("draft_batch_sha256",):
        if field not in payload or payload[field] in (None, "", {}):
            raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} 缺少 {field}")
    expected_report = _gap_report_from_payload(context_payload)
    if payload["context_sha256"] != context.context_sha256:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} context_sha256 与 context 不一致")
    if payload["discovery_family_id"] != context.discovery_family_id:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} discovery_family_id 与 context 不一致")
    if payload["report_sha256"] != expected_report.report_sha256:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} report_sha256 与 context 不一致")
    expected_gap_hashes = {card.gap_id: card.card_sha256 for card in expected_report.gap_cards}
    if payload["gap_card_sha256"] != expected_gap_hashes:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} gap_card_sha256 与 context 不一致")
    expected_batch_hash = _draft_batch_sha256(payload)
    if payload["draft_batch_sha256"] != expected_batch_hash:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"{label} draft_batch_sha256 不一致")
    return expected_batch_hash


def _load_formal_evolution_inputs(
    coverage_graph_root: Path,
    memory_root: Path,
    *,
    memory_snapshot_id: str | None = None,
    gap_report_path: Path | None = None,
    memory_policy: MemorySnapshotPolicy,
) -> tuple[Any, ResearchMemorySnapshot, CoverageGapReport]:
    """只从正式文件布局加载并核验演化输入身份链。"""

    graph = load_verified_coverage_graph(coverage_graph_root)
    graph_manifest_hash = sha256_json(graph.manifest.model_dump(mode="json"))

    # memory-root 是正式 research_memory 根；index 是唯一目录索引，不能用
    # rglob 在任意 JSON 中猜测 snapshot。
    store = ResearchMemoryStore(research_memory_root=memory_root)
    try:
        index_payload = _read_json(store.index_path)
        if not isinstance(index_payload, dict) or not isinstance(index_payload.get("snapshots"), dict) or not isinstance(index_payload.get("entries"), dict):
            raise ValueError("index 结构无效")
    except (OSError, ValueError, TypeError) as error:
        raise FactorMinerError(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "正式 memory index 缺失或无法读取") from error
    store.verify()
    try:
        snapshot_ids = tuple(sorted(index_payload.get("snapshots", {}).keys()))
    except (AttributeError, TypeError, ValueError) as error:
        raise FactorMinerError(
            FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
            "正式 memory index 缺失或无法读取 snapshot 索引",
        ) from error
    if memory_snapshot_id is not None:
        if memory_snapshot_id not in snapshot_ids:
            raise FactorMinerError(
                FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
                "--memory-snapshot-id 不在正式 memory index 中",
            )
        selected_snapshot_id = memory_snapshot_id
    elif len(snapshot_ids) == 1:
        selected_snapshot_id = snapshot_ids[0]
    else:
        raise FactorMinerError(
            FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
            "memory-root 含多个 snapshot 时必须显式指定 --memory-snapshot-id",
        )
    snapshot_path = store.snapshots_root / f"{selected_snapshot_id}.json"
    snapshot_payload = _read_json(snapshot_path)
    _require_raw_identity(snapshot_payload, "snapshot_sha256", label="memory snapshot")
    if index_payload["snapshots"].get(selected_snapshot_id) != snapshot_payload["snapshot_sha256"]:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "memory index 与 snapshot hash 不一致")
    snapshot = store.load_snapshot(selected_snapshot_id)
    if snapshot.coverage_graph_id != graph.manifest.coverage_graph_id:
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "memory snapshot coverage_graph_id 与指定 coverage graph 不一致",
        )
    if snapshot.coverage_graph_manifest_hash != graph_manifest_hash:
        raise FactorMinerError(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "memory snapshot coverage_graph_manifest_hash 与 graph manifest 不一致",
        )
    if not set(snapshot.source_family_ids).issubset(set(memory_policy.allowed_source_family_ids)):
        raise FactorMinerError(FailureCode.POLLUTED_FAMILY_REJECTED, "memory snapshot family 超出当前 policy 白名单")
    # 快照是正式的 cutoff 选择结果；再次核对每条 binding 的内容和时间，
    # 防止旧污染条目或 cutoff 后条目借快照身份进入上下文。
    for binding in snapshot.entry_bindings:
        if index_payload["entries"].get(binding.memory_entry_id) != binding.entry_sha256:
            raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, f"memory index entry hash 不一致：{binding.memory_entry_id}")
        entry_payload = _read_json(store.entry_objects_root / f"{binding.memory_entry_id}.json")
        _require_raw_identity(entry_payload, "entry_sha256", label=f"memory entry {binding.memory_entry_id}")
        entry = store.load_entry(binding.memory_entry_id)
        if entry.entry_sha256 != binding.entry_sha256:
            raise FactorMinerError(
                FailureCode.EVOLUTION_HASH_MISMATCH,
                f"memory snapshot entry hash 不一致：{binding.memory_entry_id}",
            )
        if entry.created_at > snapshot.cutoff_at:
            raise FactorMinerError(
                FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
                f"memory entry 晚于 snapshot cutoff：{binding.memory_entry_id}",
            )
        published_at = store._load_published_input_manifest(entry).published_at
        if published_at > snapshot.cutoff_at:
            raise FactorMinerError(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"memory entry published_at 晚于 snapshot cutoff：{binding.memory_entry_id}")
        if entry.terminal_state not in memory_policy.include_terminal_states:
            raise FactorMinerError(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"memory entry terminal_state 不在 policy：{binding.memory_entry_id}")
        if entry.discovery_family_id not in snapshot.source_family_ids or entry.discovery_family_id not in memory_policy.allowed_source_family_ids:
            raise FactorMinerError(
                FailureCode.POLLUTED_FAMILY_REJECTED,
                f"memory entry family 不在 snapshot 正式白名单：{binding.memory_entry_id}",
            )

    report_path = gap_report_path or (Path(coverage_graph_root).parent / "gap_report.json")
    if not report_path.is_file():
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            "指定 coverage graph 缺少正式 gap_report.json",
        )
    report_payload = _read_json(report_path)
    if not isinstance(report_payload, dict):
        raise FactorMinerError(FailureCode.COVERAGE_GAP_INVALID, "gap_report.json 必须是 JSON object")
    report_raw = report_payload.get("gap_report", report_payload)
    _require_raw_gap_report(report_raw, label="gap report")
    report = CoverageGapReport.model_validate(report_raw)
    build_gap_report_identity(report)
    if report.coverage_graph_id != graph.manifest.coverage_graph_id:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "gap report coverage_graph_id 不一致")
    if report.graph_manifest_hash != graph_manifest_hash:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "gap report graph_manifest_hash 不一致")
    if report.memory_snapshot_hash != snapshot.snapshot_sha256:
        raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "gap report memory_snapshot_hash 不一致")
    return graph, snapshot, report


def _write_immutable_json(path: Path, payload: Any) -> None:
    """以内容寻址习惯写入不可变 CLI 产物。"""

    _atomic_write_immutable(path, canonical_json_bytes(payload))


def _context_from_payload(payload: dict[str, Any]) -> ResearchEvolutionContext:
    """从上下文文件兼容读取扁平或嵌套 context。"""

    value = payload.get("context", payload)
    if not isinstance(value, dict):
        raise FactorMinerError(FailureCode.EVOLUTION_CONTEXT_INVALID, "context 必须是 JSON object")
    _require_raw_identity(value, "context_sha256", label="context")
    return ResearchEvolutionContext.model_validate(value)


def _gap_report_from_payload(payload: dict[str, Any]) -> CoverageGapReport:
    """读取并复核严格演化所需的脱敏 gap report。"""

    value = payload.get("gap_report")
    if not isinstance(value, dict):
        raise FactorMinerError(FailureCode.COVERAGE_GAP_INVALID, "context 缺少 gap_report")
    _require_raw_gap_report(value, label="context.gap_report")
    return CoverageGapReport.model_validate(value)


def _load_strict_campaign_binding(
    artifact_root: Path,
    family: RegisteredLLMDiscoveryResearchFamily,
) -> dict[str, object]:
    """从执行根目录读取 strict seal/evaluation 的冻结绑定。"""

    context_path = artifact_root / "evolution_context.json"
    approval_path = artifact_root / "approved_hypotheses.json"
    if not context_path.is_file() or not approval_path.is_file():
        return {}
    context = _context_from_payload(_read_json(context_path))
    approval_payload = _read_json(approval_path)
    approval = ApprovedEvolutionHypothesisBatch.model_validate(
        {
            key: approval_payload[key]
            for key in (
                "context_sha256",
                "discovery_family_id",
                "approvals",
                "approval_batch_sha256",
            )
        }
    )
    state = LLMDiscoveryLedger(artifact_root).project_state(
        family.discovery_family_id
    )
    family_root = (
        artifact_root
        / "state"
        / "llm_discovery_families"
        / family.discovery_family_id
        / "candidate_slots"
    )
    slot_objects: dict[str, Mapping[str, object]] = {}
    for state_slot_id in expected_candidate_slot_ids(family.spec):
        arm_id, number = state_slot_id.split(":", 1)
        object_slot_id = f"{arm_id}:C{number}"
        path = family_root / f"{object_slot_id.replace(':', '__')}.json"
        slot_objects[state_slot_id] = _read_json(path)
    return {
        "evolution_context": context,
        "approval_batch": approval,
        "slot_objects": slot_objects,
        "gap_report": _gap_report_from_payload(_read_json(context_path)),
    }


@evolution_app.command("prepare-context")
def evolution_prepare_context_command(
    coverage_graph_root: Path = typer.Option(..., "--coverage-graph-root"),
    memory_root: Path = typer.Option(..., "--memory-root"),
    memory_snapshot_id: str | None = typer.Option(None, "--memory-snapshot-id"),
    gap_report_path: Path | None = typer.Option(None, "--gap-report"),
    family_id: str = typer.Option(..., "--family-id"),
    policy_path: Path = typer.Option(..., "--policy"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """只读取截止日前已完成记忆和指定 coverage graph，冻结演化上下文。"""

    try:
        policy = _read_json(policy_path)
        if not isinstance(policy, dict):
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "policy 必须是 JSON object")
        for key in ("allowed_source_family_ids", "include_terminal_states"):
            if not isinstance(policy.get(key), list) or not policy[key]:
                raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"policy 缺少非空 {key}")
        memory_policy = MemorySnapshotPolicy.model_validate(
            {key: policy[key] for key in MemorySnapshotPolicy.model_fields if key in policy}
        )
        graph, memory, report = _load_formal_evolution_inputs(
            coverage_graph_root,
            memory_root,
            memory_snapshot_id=memory_snapshot_id,
            gap_report_path=gap_report_path,
            memory_policy=memory_policy,
        )
        required = ("field_registry_hash", "evaluation_policy_hash", "design_policy_hash")
        missing = tuple(key for key in required if not isinstance(policy.get(key), str))
        if missing:
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"policy 缺少冻结身份字段：{missing}")
        context = build_evolution_context(
            discovery_family_id=family_id,
            coverage_graph_manifest_hash=sha256_json(graph.manifest.model_dump(mode="json")),
            memory_snapshot_hash=memory.snapshot_sha256 or "",
            gap_report_hash=report.report_sha256 or "",
            field_registry_hash=policy["field_registry_hash"],
            evaluation_policy_hash=policy["evaluation_policy_hash"],
            design_policy_hash=policy["design_policy_hash"],
            external_model_redaction_policy=str(policy.get("external_model_redaction_policy", "evolution-gap-brief-v1")),
        )
        gap_brief = {"gap_cards": [card.model_dump(mode="json") for card in report.gap_cards]}
        result = context.model_dump(mode="json") | {
            "gap_report": report.model_dump(mode="json"),
            "memory_snapshot": memory.model_dump(mode="json"),
            "gap_brief": gap_brief,
            "policy_sha256": sha256_json(policy),
            "cutoff_at": memory.cutoff_at.isoformat(),
        }
        _write_immutable_json(output_path, result)
        _print_json({"status": "prepared", "context_sha256": context.context_sha256, "output": str(output_path)})
    except Exception as error:
        _fail(error)


@evolution_app.command("generate-hypotheses")
def evolution_generate_hypotheses_command(
    context_path: Path = typer.Option(..., "--context"),
    authorization_path: Path = typer.Option(..., "--authorization"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """生成并审计十个脱敏逻辑草案；产物绝不保存原始模型响应。"""

    try:
        context_payload = _read_json(context_path)
        context = _context_from_payload(context_payload)
        report = _gap_report_from_payload(context_payload)
        brief = context_payload.get("gap_brief") or {"gap_cards": [card.model_dump(mode="json") for card in report.gap_cards]}
        auth_payload = _read_json(authorization_path)
        if not isinstance(auth_payload, dict):
            raise FactorMinerError(FailureCode.LLM_EXPORT_NOT_AUTHORIZED, "authorization 必须是 JSON object")
        auth_fields = set(LLMEvolutionRequestAuthorization.model_fields)
        auth = LLMEvolutionRequestAuthorization.model_validate(
            {key: value for key, value in auth_payload.items() if key in auth_fields}
        )
        request = build_evolution_hypothesis_request(context, brief, authorization=auth)
        response_payload = auth_payload.get("response") or auth_payload.get("response_payload")
        response_digest: str
        provider = "deepseek"
        model = auth.authorized_model
        if isinstance(response_payload, dict):
            response = response_payload
            response_digest = sha256_json(response)
        elif isinstance(auth_payload.get("record_directory"), str):
            recorded = replay_recorded_call(Path(auth_payload["record_directory"]))
            if recorded.content_json is None:
                raise FactorMinerError(FailureCode.LLM_RESPONSE_INVALID, "录制响应缺少 JSON 内容")
            response = recorded.content_json
            response_digest = recorded.record.response_sha256
            model = recorded.record.model
        else:
            raise FactorMinerError(FailureCode.LLM_PROVIDER_UNAVAILABLE, "正式生成需要已授权的录制响应或服务器传输入口")
        drafts = parse_evolution_hypothesis_response(response, context=context)
        result = {
            "context_sha256": context.context_sha256,
            "discovery_family_id": context.discovery_family_id,
            "report_sha256": report.report_sha256,
            "gap_card_sha256": {card.gap_id: card.card_sha256 for card in report.gap_cards},
            "request_sha256": request.request_sha256,
            "response_digest": response_digest,
            "provider": provider,
            "model": model,
            "prompt_bundle": {"system": request.body["messages"][0], "user": request.body["messages"][1]},
            "drafts": [item.model_dump(mode="json") for item in drafts],
        }
        result["draft_batch_sha256"] = _draft_batch_sha256(result)
        _write_immutable_json(output_path, result)
        _print_json({"status": "drafted", "request_sha256": request.request_sha256, "response_digest": response_digest, "hypothesis_count": 10, "output": str(output_path)})
    except Exception as error:
        _fail(error)


@evolution_app.command("approve-hypotheses")
def evolution_approve_hypotheses_command(
    context_path: Path = typer.Option(..., "--context"),
    draft_batch_path: Path = typer.Option(..., "--draft-batch"),
    decisions_path: Path = typer.Option(..., "--decisions"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """唯一把十个草案转换为可执行批准批次的人工入口。"""

    try:
        context_payload = _read_json(context_path)
        context = _context_from_payload(context_payload)
        draft_payload = _read_json(draft_batch_path)
        if not isinstance(draft_payload, dict):
            raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "draft-batch 必须是 JSON object")
        _require_raw_identity(context_payload.get("context", context_payload), "context_sha256", label="context")
        _validate_draft_batch_provenance(
            draft_payload,
            context_payload=context_payload,
            context=context,
            label="draft-batch",
        )
        drafts_raw = draft_payload.get("drafts") if isinstance(draft_payload, dict) else None
        if not isinstance(drafts_raw, list):
            raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "draft-batch 缺少 drafts")
        for index, item in enumerate(drafts_raw):
            _require_raw_identity(item, "draft_sha256", label=f"drafts[{index}]")
        drafts = tuple(LogicalEvolutionHypothesisDraft.model_validate(item) for item in drafts_raw)
        decisions_payload = _read_json(decisions_path)
        raw_decisions = decisions_payload.get("decisions") if isinstance(decisions_payload, dict) else decisions_payload
        if not isinstance(raw_decisions, list):
            raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "decisions 必须是数组")
        for index, item in enumerate(raw_decisions):
            _require_raw_identity(item, "decision_sha256", label=f"decisions[{index}]")
        decisions = tuple(EvolutionHypothesisApproval.model_validate(item) for item in raw_decisions)
        role = str(decisions_payload.get("approval_role", "")) if isinstance(decisions_payload, dict) else None
        batch = approve_evolution_hypotheses(drafts, decisions, context=context, expected_approval_role=role)
        result = {
            "request_sha256": draft_payload["request_sha256"],
            "report_sha256": draft_payload["report_sha256"],
            "gap_card_sha256": draft_payload["gap_card_sha256"],
            "context_sha256": context.context_sha256,
            "discovery_family_id": context.discovery_family_id,
            "draft_batch_sha256": draft_payload["draft_batch_sha256"],
            "drafts": [item.model_dump(mode="json") for item in drafts],
            **batch.model_dump(mode="json"),
        }
        _write_immutable_json(output_path, result)
        _print_json({"status": "approved", "approval_batch_sha256": batch.approval_batch_sha256, "approval_count": 10, "output": str(output_path)})
    except Exception as error:
        _fail(error)


def _load_fixed_pilot_results(payload: dict[str, Any]) -> dict[str, Any]:
    """从冻结 JSON 载入已经完成计算的三个候选结果。"""

    from factor_miner.barra_attribution import BarraAttributionResult
    from factor_miner.barra_schema import BarraInputIdentity
    from factor_miner.ic_diagnostics import ICDiagnostics
    from factor_miner.pilot_runner import (
        FixedBarraEvaluation,
        FixedPilotCandidateResult,
        FixedPortfolioEvaluation,
    )
    from factor_miner.pilot_schema import BarraAvailability
    from factor_miner.portfolio_evaluation import PortfolioBacktestResult
    from factor_miner.portfolio_statistics import PortfolioMetrics

    raw_results = payload.get("candidate_results")
    if not isinstance(raw_results, dict):
        raise FactorMinerError(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "prepared-run 缺少 candidate_results object",
        )
    results: dict[str, Any] = {}
    for candidate_id, raw in raw_results.items():
        if not isinstance(raw, dict):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{candidate_id} candidate result 必须是 object",
            )
        portfolio_raw = raw.get("portfolio")
        portfolio = None
        if portfolio_raw is not None:
            if not isinstance(portfolio_raw, dict):
                raise FactorMinerError(
                    FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                    f"{candidate_id} portfolio result 结构无效",
                )
            portfolio = FixedPortfolioEvaluation(
                backtest=PortfolioBacktestResult.model_validate(portfolio_raw["backtest"]),
                metrics=PortfolioMetrics.model_validate(portfolio_raw["metrics"]),
            )
        barra_raw = raw.get("barra")
        barra = None
        if barra_raw is not None:
            if not isinstance(barra_raw, dict):
                raise FactorMinerError(
                    FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                    f"{candidate_id} Barra result 结构无效",
                )
            availability_raw = barra_raw.get("availability", barra_raw)
            if isinstance(availability_raw, dict):
                availability_raw = {
                    key: availability_raw[key]
                    for key in ("status", "missing_inputs", "source_uris", "input_sha256")
                    if key in availability_raw
                }
            attribution_raw = barra_raw.get("attribution")
            identity_raw = barra_raw.get("identity")
            barra = FixedBarraEvaluation(
                availability=BarraAvailability.model_validate(availability_raw),
                attribution=(
                    BarraAttributionResult.model_validate(attribution_raw)
                    if attribution_raw is not None
                    else None
                ),
                identity=(
                    BarraInputIdentity.model_validate(identity_raw)
                    if identity_raw is not None
                    else None
                ),
                reason=barra_raw.get("reason"),
            )
        results[str(candidate_id)] = FixedPilotCandidateResult(
            candidate_id=str(candidate_id),
            ic=(ICDiagnostics.model_validate(raw["ic"]) if raw.get("ic") is not None else None),
            portfolio=portfolio,
            barra=barra,
        )
    return results


@pilot_app.command("run-fixed")
def pilot_run_fixed_command(
    candidates: Path = typer.Option(..., "--candidates", help="固定候选 JSON 路径。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    prepared_run: Path = typer.Option(
        ...,
        "--prepared-run",
        help="服务器侧已完成计算的冻结结果 JSON；本命令只负责合同校验和发布。",
    ),
    dsn: str = typer.Option(..., "--dsn", envvar="FM_DASHBOARD_DSN", help="Dashboard PostgreSQL DSN。"),
) -> None:
    """发布固定候选结果；数据库投影失败不删除本地产物。"""

    try:
        from factor_miner.pilot_runner import publish_fixed_pilot_run
        from factor_miner.pilot_schema import (
            PilotInputManifest,
            PilotRunRequest,
        )
        from factor_miner.pilot_sources import load_fixed_candidates
        from factor_miner.policy import company_a_share_visible_policy
        from factor_miner.portfolio_schema import PortfolioEvaluationPolicy

        payload = _read_json(prepared_run)
        if not isinstance(payload, dict):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "prepared-run 根节点必须是 object",
            )
        candidate_file = load_fixed_candidates(candidates)
        input_manifest = PilotInputManifest.model_validate(payload.get("input_manifest"))
        request_payload = payload.get("request")
        if not isinstance(request_payload, dict):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "prepared-run 缺少 request object",
            )
        request_payload = dict(request_payload)
        request_payload["artifact_root"] = str(artifact_root)
        request = PilotRunRequest.model_validate(request_payload)
        evaluation_policy = company_a_share_visible_policy()
        if payload.get("evaluation_policy") is not None:
            from factor_miner.schema import EvaluationPolicySpec

            evaluation_policy = EvaluationPolicySpec.model_validate(
                payload["evaluation_policy"]
            )
        portfolio_policy = PortfolioEvaluationPolicy.model_validate(
            payload.get("portfolio_policy", PortfolioEvaluationPolicy().model_dump(mode="json"))
        )
        barra_policy = None
        if payload.get("barra_policy") is not None:
            from factor_miner.barra_schema import BarraEvaluationPolicy

            barra_policy = BarraEvaluationPolicy.model_validate(payload["barra_policy"])
        publication = publish_fixed_pilot_run(
            candidates=candidate_file,
            candidate_results=_load_fixed_pilot_results(payload),
            input_manifest=input_manifest,
            evaluation_policy=evaluation_policy,
            portfolio_policy=portfolio_policy,
            barra_policy=barra_policy,
            request=request,
        )
        try:
            from dashboard.pg_store import PostgresDashboardStore
            from factor_miner.pilot_projection import project_pilot_run

            project_pilot_run(
                artifact_root,
                publication.run_id,
                PostgresDashboardStore(dsn),
            )
        except Exception as error:
            _print_json(
                {
                    "status": "published",
                    "pilot_run_id": publication.run_id,
                    "manifest_path": str(publication.manifest_path),
                    "projection_status": "failed",
                    "projection_error": str(error),
                    "retry_command": (
                        f"factor-miner pilot project {publication.run_id} "
                        f"--artifact-root {artifact_root} --dsn <DSN>"
                    ),
                }
            )
            raise typer.Exit(code=1)
        _print_json(
            {
                "status": "published",
                "pilot_run_id": publication.run_id,
                "manifest_path": str(publication.manifest_path),
                "projection_status": "projected",
            }
        )
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


@pilot_app.command("reevaluate-existing")
def pilot_reevaluate_existing_command(
    source_run_id: str = typer.Option(
        ...,
        "--source-run-id",
        help="包含不可变候选 Spec 的已发布来源 run_id。",
    ),
    config_path: Path = typer.Option(
        ...,
        "--config",
        help="服务器 Pilot 路径与请求配置 JSON。",
    ),
    artifact_root: Path = typer.Option(
        ...,
        "--artifact-root",
        envvar="FM_ARTIFACT_ROOT",
    ),
    dsn: str = typer.Option(..., "--dsn", envvar="FM_DASHBOARD_DSN", help="Dashboard PostgreSQL DSN。"),
) -> None:
    """只读复算已发布 Spec，在一个全新运行中执行 long-only 协议。"""

    try:
        from factor_miner.pilot_artifacts import (
            reevaluate_existing_pilot_run,
            resolve_pilot_code_commit,
            verify_reevaluation_request_identity,
        )
        from factor_miner.pilot_llm import PilotServerConfig

        config = PilotServerConfig.model_validate(_read_json(config_path))
        actual_commit = resolve_pilot_code_commit(Path(__file__).resolve().parents[2])
        verified_request = verify_reevaluation_request_identity(
            config_path=config_path,
            request=config.request,
            actual_code_commit=actual_commit,
        )
        publication = reevaluate_existing_pilot_run(
            artifact_root=artifact_root,
            source_run_id=source_run_id,
            paths=config.paths,
            request=verified_request,
        )
        from dashboard.pg_store import PostgresDashboardStore
        from factor_miner.pilot_projection import project_pilot_run

        project_pilot_run(
            artifact_root,
            publication.run_id,
            PostgresDashboardStore(dsn),
        )
        _print_json(
            {
                "status": "published",
                "source_run_id": source_run_id,
                "pilot_run_id": publication.run_id,
                "candidate_source": "published_specs_only",
                "llm_called": False,
                "manifest_path": str(publication.manifest_path),
                "projection_status": "projected",
            }
        )
    except Exception as error:
        _fail(error)


@pilot_app.command("project")
def pilot_project_command(
    pilot_run_id: str = typer.Argument(..., help="已发布 Pilot 的 run_id。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    dsn: str = typer.Option(..., "--dsn", help="Dashboard PostgreSQL DSN。"),
) -> None:
    """只投影已有 published Pilot，本命令不计算因子或指标。"""

    try:
        from dashboard.pg_store import PostgresDashboardStore
        from factor_miner.pilot_projection import project_pilot_run

        snapshot = project_pilot_run(
            artifact_root,
            pilot_run_id,
            PostgresDashboardStore(dsn),
        )
        _print_json(
            {
                "status": "projected",
                "pilot_run_id": pilot_run_id,
                "snapshot_sha256": snapshot.snapshot_sha256,
            }
        )
    except Exception as error:
        _fail(error)


@campaign_app.command("run-approved")
def campaign_run_approved_command(
    family_id: str | None = typer.Argument(None, help="新的正式 discovery family ID。"),
    approved_hypotheses_path: Path | None = typer.Argument(None),
    policy_path: Path | None = typer.Argument(None),
    payloads_path: Path | None = typer.Argument(None),
    field_registry_path: Path | None = typer.Argument(None),
    authorizations_path: Path | None = typer.Argument(None),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
    coverage_hypotheses_path: Path | None = typer.Option(
        None,
        "--coverage-hypotheses",
        help="可选的 coverage 父假设文件；未提供时从批准集合筛选。",
    ),
    evolution_context_path: Path | None = typer.Option(None, "--evolution-context"),
    evolution_approved_path: Path | None = typer.Option(None, "--approved-hypotheses"),
) -> None:
    """登记四条 arm 的槽位、运行批准输入并收口 120 个终态槽。"""

    try:
        strict_context = None
        strict_batch = None
        if evolution_context_path is not None or evolution_approved_path is not None:
            if evolution_context_path is None or evolution_approved_path is None:
                raise FactorMinerError(FailureCode.EVOLUTION_CONTEXT_INVALID, "严格演化路径必须同时给出 context 和 approved-hypotheses")
            strict_context = _context_from_payload(_read_json(evolution_context_path))
            strict_batch_payload = _read_json(evolution_approved_path)
            if not isinstance(strict_batch_payload, dict):
                raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "approved-batch 必须是 JSON object")
            _validate_draft_batch_provenance(
                strict_batch_payload,
                context_payload=_read_json(evolution_context_path),
                context=strict_context,
                label="approved-batch",
            )
            _require_raw_approval_batch_identity(strict_batch_payload, label="approved-batch")
            raw_approvals = strict_batch_payload.get("approvals")
            if not isinstance(raw_approvals, list):
                raise FactorMinerError(FailureCode.APPROVAL_COUNT_INVALID, "approved-batch 缺少 approvals")
            for index, approval in enumerate(raw_approvals):
                _require_raw_identity(approval, "decision_sha256", label=f"approved-batch.approvals[{index}]")
            batch_fields = set(ApprovedEvolutionHypothesisBatch.model_fields)
            strict_batch = ApprovedEvolutionHypothesisBatch.model_validate(
                {key: value for key, value in strict_batch_payload.items() if key in batch_fields}
            )
            if family_id is not None and family_id != strict_context.discovery_family_id:
                raise FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, "family 与 evolution context 不一致")
            family_id = strict_context.discovery_family_id
            require_approved_evolution_batch(strict_batch, context_sha256=strict_context.context_sha256, discovery_family_id=family_id)
        if not all(item is not None for item in (family_id, approved_hypotheses_path, policy_path, payloads_path, field_registry_path, authorizations_path)):
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "Stage-C 冻结参数不完整")
        assert family_id is not None and approved_hypotheses_path is not None and policy_path is not None and payloads_path is not None and field_registry_path is not None and authorizations_path is not None
        dependencies, hypotheses = _load_approved_batch_dependencies(
            family_id=family_id,
            artifact_root=artifact_root,
            approved_hypotheses_path=approved_hypotheses_path,
            policy_path=policy_path,
            payloads_path=payloads_path,
            field_registry_path=field_registry_path,
            authorizations_path=authorizations_path,
        )
        coverage = None
        if coverage_hypotheses_path is not None:
            coverage = _parse_approved_hypotheses(
                _read_json(coverage_hypotheses_path)
            )
        result = run_approved_campaign_generation(
            family_id,
            dependencies.family,
            dependencies,
            hypotheses,
            coverage_hypotheses=coverage,
            approval_batch=strict_batch,
            evolution_context=strict_context,
        )
        _print_json(result.model_dump(mode="json"))
        if result.status != "completed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


@campaign_app.command("seal")
def campaign_seal_command(
    family_id: str = typer.Argument(...),
    seal_input_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """从服务器本地 120 个槽对象重算并登记 generation seal。"""

    try:
        family = LLMDiscoveryLedger(artifact_root).load_family(family_id)
        seal = seal_completed_generation(
            family_id,
            GenerationSealDependencies(artifact_root=artifact_root, family=family),
            _read_json(seal_input_path),
        )
        _print_json(
            {
                "status": "sealed",
                "family_id": family_id,
                "generation_seal_id": seal.generation_seal_id,
                "manifest_sha256": seal.manifest_sha256,
            }
        )
    except Exception as error:
        _fail(error)


@campaign_app.command("evaluate")
def campaign_evaluate_command(
    family_id: str = typer.Argument(...),
    evaluation_results_path: Path = typer.Argument(
        ...,
        help="服务器统一候选评价器输出的按 slot_id 索引 JSON。",
    ),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
    dsn: str = typer.Option(..., "--dsn", envvar="FM_DASHBOARD_DSN"),
) -> None:
    """在 seal 后评价全部槽位并发布研究族诊断。"""

    try:
        ledger = LLMDiscoveryLedger(artifact_root)
        family = ledger.load_family(family_id)
        seal = ledger.load_generation_seal(family_id)
        payload = _read_json(evaluation_results_path)
        if not isinstance(payload, dict):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "评价结果必须是按 slot_id 索引的 JSON object",
            )
        raw_slots = payload.get("slots", payload)
        if not isinstance(raw_slots, dict):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "评价结果缺少 slots object",
            )
        binding = _load_strict_campaign_binding(artifact_root, family)
        result = evaluate_sealed_campaign(
            seal,
            family,
            MappingCampaignPilotRunner(raw_slots),
            **{
                key: binding[key]
                for key in ("evolution_context", "approval_batch", "slot_objects")
                if key in binding
            },
        )
        publication = publish_campaign_evaluation(
            artifact_root,
            family,
            seal,
            result,
            **binding,
        )
        try:
            from dashboard.pg_store import PostgresDashboardStore
            from factor_miner.dashboard_projection import project_run_artifacts

            project_run_artifacts(
                artifact_root,
                publication.run_id,
                PostgresDashboardStore(dsn),
            )
            projection_status = "projected"
        except Exception as error:
            _print_json(
                {
                    "status": "published",
                    "run_id": publication.run_id,
                    "manifest_path": str(publication.manifest_path),
                    "projection_status": "failed",
                    "projection_error": str(error),
                    "retry_command": (
                        f"factor-miner campaign evaluate {family_id} "
                        f"{evaluation_results_path} --artifact-root {artifact_root} "
                        "--dsn <DSN>"
                    ),
                }
            )
            raise typer.Exit(code=1)
        _print_json(
            {
                "status": "published",
                "run_id": publication.run_id,
                "manifest_path": str(publication.manifest_path),
                "generation_seal_id": seal.generation_seal_id,
                "registered_slot_count": result.registered_slot_count,
                "failed_slot_count": result.failed_slot_count,
                "projection_status": projection_status,
            }
        )
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


@campaign_app.command("verify")
def campaign_verify_command(
    family_id: str = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """核验新 family 的事件链、120 槽终态、seal 和可选运行清单。"""

    try:
        ledger = LLMDiscoveryLedger(artifact_root)
        family = ledger.load_family(family_id)
        state = ledger.project_state(family_id)
        seal = ledger.load_generation_seal(family_id)
        from factor_miner.llm_seal import verify_generation_seal

        binding = _load_strict_campaign_binding(artifact_root, family)
        verify_generation_seal(
            family,
            state,
            seal,
            **{
                key: binding[key]
                for key in ("evolution_context", "approval_batch", "slot_objects")
                if key in binding
            },
        )
        _print_json(
            {
                "status": "ok",
                "family_id": family_id,
                "family_generation_state": state.family_generation_state,
                "candidate_slot_count": len(state.candidate_slot_states),
                "terminal_slot_count": sum(
                    value in CANDIDATE_TERMINALS
                    for value in state.candidate_slot_states.values()
                ),
                "event_count": len(state.applied_event_ids),
                "generation_seal_id": seal.generation_seal_id,
            }
        )
    except Exception as error:
        _fail(error)


def _load_exact_llm_authorization(path: Path) -> LLMExportAuthorization:
    """加载受控候选流程的精确请求授权，不接受研究族范围授权。"""

    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("阶段 B 只接受精确 LLMExportAuthorization 对象")
    if "scope_sha256" in payload or "scope_authorization" in payload:
        raise FactorMinerError(
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
            "阶段 B 不接受旧 family 的范围授权",
        )
    return LLMExportAuthorization.model_validate(payload)


def _stage_state_sequence(artifact_root: Path, stage_run_id: str) -> int:
    """读取当前阶段状态序号；首次运行从零开始。"""

    from factor_miner.pilot_llm import load_stage_state

    try:
        return load_stage_state(artifact_root, stage_run_id).sequence
    except FactorMinerError:
        return -1


def _save_stage_state_update(
    *,
    artifact_root: Path,
    stage_run_id: str,
    **values: Any,
) -> None:
    """追加一个受控候选流程状态快照，不覆盖历史状态。"""

    from factor_miner.pilot_llm import (
        PilotStageBState,
        save_stage_state,
    )

    previous_sequence = _stage_state_sequence(artifact_root, stage_run_id)
    state = PilotStageBState(
        sequence=previous_sequence + 1,
        stage_run_id=stage_run_id,
        **values,
    )
    save_stage_state(artifact_root, state)


@pilot_app.command("prepare-hypothesis")
def pilot_prepare_hypothesis_command(
    public_brief: Path = typer.Argument(..., help="只含公开研究简报和已核验来源的 JSON。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """准备 DeepSeek 假设请求；不访问网络、不读取服务器行情。"""

    try:
        from factor_miner.pilot_llm import (
            build_hypothesis_generation_request,
            build_prepared_hypothesis_request,
            build_stage_run_id,
            save_stage_state,
            PilotStageBState,
        )

        payload = _read_json(public_brief)
        if not isinstance(payload, dict):
            raise ValueError("公开研究简报必须是 JSON object")
        raw_refs = payload.get("verified_source_refs")
        if not isinstance(raw_refs, list):
            raise ValueError("公开研究简报必须包含 verified_source_refs 数组")
        request = build_hypothesis_generation_request(
            public_brief=str(payload.get("public_brief", "")),
            verified_source_refs=tuple(str(item) for item in raw_refs),
        )
        prepared = build_prepared_hypothesis_request(request)
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(prepared.model_dump(mode="json")),
        )
        stage_run_id = build_stage_run_id(request.request_id)
        save_stage_state(
            artifact_root,
            PilotStageBState(
                stage_run_id=stage_run_id,
                status="hypothesis_request_prepared",
                request_id=request.request_id,
                request_sha256=request.request_sha256,
            ),
        )
        _print_json(
            {
                "status": "awaiting_external_authorization",
                "request_id": request.request_id,
                "request_sha256": request.request_sha256,
                "deepseek_request_sha256": prepared.request_sha256,
                "output_path": str(output_path),
                "authorization_command": (
                    "factor-miner llm authorize-export <request.json> "
                    "<policy.json> --approved-request-sha256 "
                    f"{prepared.request_sha256} ..."
                ),
            }
        )
    except Exception as error:
        _fail(error)


@pilot_app.command("generate-hypothesis")
def pilot_generate_hypothesis_command(
    prepared_request: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    authorization_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    record_root: Path = typer.Option(..., "--record-root"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """执行脱敏 DeepSeek 假设请求并等待人工批准。"""

    try:
        from factor_miner.pilot_llm import (
            HypothesisGenerationResponse,
            RecordedDeepSeekHypothesisProvider,
            build_stage_run_id,
            request_from_prepared_hypothesis,
        )

        prepared = PreparedDeepSeekRequest.model_validate(_read_json(prepared_request))
        request = request_from_prepared_hypothesis(prepared)
        provider = RecordedDeepSeekHypothesisProvider(
            policy=CorporateExternalResearchPolicy.model_validate(_read_json(policy_path)),
            authorization=_load_exact_llm_authorization(authorization_path),
            record_root=record_root,
        )
        response = provider.generate_one(request)
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(response.model_dump(mode="json")),
        )
        _save_stage_state_update(
            artifact_root=artifact_root,
            stage_run_id=build_stage_run_id(request.request_id),
            status="awaiting_human_approval",
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            hypothesis_sha256=sha256_json(response.hypothesis.model_dump(mode="json")),
            provider_call_id=response.provider_call_id,
            hypothesis=response.hypothesis,
        )
        _print_json(
            {
                "status": "awaiting_human_approval",
                "request_id": response.request_id,
                "provider_call_id": response.provider_call_id,
                "response_sha256": response.response_sha256,
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@pilot_app.command("approve-hypothesis")
def pilot_approve_hypothesis_command(
    response_path: Path = typer.Argument(..., help="已录制并校验的假设响应 JSON。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    output_path: Path = typer.Option(..., "--output"),
    approver_role: str = typer.Option(..., "--approver-role"),
    decision: str = typer.Option("approved", "--decision"),
) -> None:
    """记录人工批准/否决；不修改原假设，不触碰 family 账本。"""

    try:
        from factor_miner.pilot_llm import (
            HypothesisGenerationResponse,
            approve_hypothesis,
            build_stage_run_id,
        )

        response = HypothesisGenerationResponse.model_validate(_read_json(response_path))
        approval = approve_hypothesis(
            response,
            approver_role=approver_role,
            decision=decision,
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(approval.model_dump(mode="json")),
        )
        _save_stage_state_update(
            artifact_root=artifact_root,
            stage_run_id=build_stage_run_id(response.request_id),
            status="hypothesis_approved" if decision == "approved" else "hypothesis_rejected",
            request_id=response.request_id,
            request_sha256=response.request_sha256,
            hypothesis_sha256=approval.hypothesis_sha256,
            provider_call_id=response.provider_call_id,
            approval_id=approval.approval_id,
            hypothesis=approval.hypothesis,
        )
        _print_json(
            {
                "status": approval.decision,
                "approval_id": approval.approval_id,
                "hypothesis_sha256": approval.hypothesis_sha256,
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@pilot_app.command("prepare-expression")
def pilot_prepare_expression_command(
    approval_path: Path = typer.Argument(...),
    field_registry_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """由已批准假设构造固定三表达式请求；不访问网络。"""

    try:
        from factor_miner.pilot_llm import (
            PilotHypothesisApproval,
            build_expression_generation_request,
            build_prepared_expression_request,
            build_stage_run_id,
        )

        approval = PilotHypothesisApproval.model_validate(_read_json(approval_path))
        if approval.decision != "approved":
            raise FactorMinerError(
                FailureCode.LLM_STATE_TRANSITION_INVALID,
                "只有 approved 假设可以生成表达式请求",
            )
        registry = FieldAvailabilityRegistry.model_validate(_read_json(field_registry_path))
        request = build_expression_generation_request(
            hypothesis=approval.hypothesis,
            registry=registry,
        )
        prepared = build_prepared_expression_request(request)
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(prepared.model_dump(mode="json")),
        )
        _save_stage_state_update(
            artifact_root=artifact_root,
            stage_run_id=build_stage_run_id(approval.request_id),
            status="expression_request_prepared",
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            hypothesis_sha256=approval.hypothesis_sha256,
            approval_id=approval.approval_id,
            hypothesis=approval.hypothesis,
        )
        _print_json(
            {
                "status": "awaiting_external_authorization",
                "request_id": request.request_id,
                "request_sha256": request.request_sha256,
                "deepseek_request_sha256": prepared.request_sha256,
                "candidate_slot_ids": list(request.candidate_slot_ids),
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@pilot_app.command("run-approved")
def pilot_run_approved_command(
    prepared_request: Path = typer.Argument(...),
    approval_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    authorization_path: Path = typer.Argument(...),
    field_registry_path: Path = typer.Argument(...),
    pilot_config_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
    record_root: Path = typer.Option(..., "--record-root"),
    dsn: str = typer.Option(..., "--dsn", envvar="FM_DASHBOARD_DSN"),
) -> None:
    """批准假设后生成三候选、执行研究并投影 Dashboard。"""

    try:
        from factor_miner.pilot_llm import (
            ApprovedPilotRequest,
            PilotHypothesisApproval,
            PilotServerConfig,
            RecordedDeepSeekExpressionProvider,
            build_stage_run_id,
            request_from_prepared_expression,
            run_approved_pilot,
        )

        prepared = PreparedDeepSeekRequest.model_validate(_read_json(prepared_request))
        expression_request = request_from_prepared_expression(prepared)
        approval = PilotHypothesisApproval.model_validate(_read_json(approval_path))
        registry = FieldAvailabilityRegistry.model_validate(_read_json(field_registry_path))
        config = PilotServerConfig.model_validate(_read_json(pilot_config_path))
        pilot_request = config.request.model_copy(update={"artifact_root": artifact_root})
        provider = RecordedDeepSeekExpressionProvider(
            policy=CorporateExternalResearchPolicy.model_validate(_read_json(policy_path)),
            authorization=_load_exact_llm_authorization(authorization_path),
            record_root=record_root,
        )
        result = run_approved_pilot(
            ApprovedPilotRequest(
                hypothesis=approval.hypothesis,
                approval=approval,
                expression_request=expression_request,
                pilot_request=pilot_request,
                source_paths=config.paths,
                artifact_root=artifact_root,
            ),
            provider,
            registry=registry,
        )
        _save_stage_state_update(
            artifact_root=artifact_root,
            stage_run_id=build_stage_run_id(approval.request_id),
            status="pilot_published",
            request_id=expression_request.request_id,
            request_sha256=expression_request.request_sha256,
            hypothesis_sha256=approval.hypothesis_sha256,
            provider_call_id=result.expression_response.provider_call_id,
            approval_id=approval.approval_id,
            candidate_ids=tuple(item.candidate_id for item in result.candidates.candidates),
            pilot_run_id=result.publication.run_id,
            hypothesis=approval.hypothesis,
            expression_response=result.expression_response,
        )
        projection_status = "not_started"
        projection_error: str | None = None
        try:
            from dashboard.pg_store import PostgresDashboardStore
            from factor_miner.pilot_projection import project_pilot_run

            project_pilot_run(
                artifact_root,
                result.publication.run_id,
                PostgresDashboardStore(dsn),
            )
            projection_status = "projected"
        except Exception as error:
            projection_status = "failed"
            projection_error = str(error)
        output = {
            "status": "published",
            "provider_call_id": result.expression_response.provider_call_id,
            "candidate_ids": [item.candidate_id for item in result.candidates.candidates],
            "pilot_run_id": result.publication.run_id,
            "manifest_path": str(result.publication.manifest_path),
            "projection_status": projection_status,
        }
        if projection_error is not None:
            output.update(
                {
                    "projection_error": projection_error,
                    "retry_command": (
                        f"factor-miner pilot project {result.publication.run_id} "
                        f"--artifact-root {artifact_root} --dsn <DSN>"
                    ),
                }
            )
        _print_json(output)
        if projection_error is not None:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


@llm_app.command("request-prepare")
def llm_request_prepare_command(
    agent_role: str = typer.Argument(..., help="hypothesis、expression 或 semantic_lint。"),
    public_payload_path: Path = typer.Argument(..., help="待扫描的公开/脱敏 payload JSON。"),
    campaign_id: str = typer.Option(..., "--campaign-id"),
    slot_ids_path: Path = typer.Option(..., "--slot-ids"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """构造固定 DeepSeek 请求；本命令不读取密钥也不访问网络。"""

    try:
        role = AgentRole(agent_role)
        if role is AgentRole.FORMAT_REPAIR:
            raise ValueError("request-prepare 不开放格式修复角色")
        payload = _read_json(public_payload_path)
        slot_ids = tuple(_read_json(slot_ids_path))
        builder = {
            AgentRole.HYPOTHESIS: build_hypothesis_agent_request,
            AgentRole.EXPRESSION: build_expression_agent_request,
            AgentRole.SEMANTIC_LINT: build_semantic_lint_agent_request,
        }[role]
        prepared = builder(
            campaign_id=campaign_id,
            slot_ids=slot_ids,
            public_payload=payload,
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(prepared.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "prepared",
                "agent_role": role,
                "request_sha256": prepared.request_sha256,
                "model": prepared.body["model"],
                "max_tokens": prepared.body["max_tokens"],
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("format-repair-prepare")
def llm_format_repair_prepare_command(
    root_request_path: Path = typer.Argument(...),
    record_directory: Path = typer.Argument(...),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """为假设 Schema 失败构造一次只修格式的无工具请求。"""

    try:
        root_request = PreparedDeepSeekRequest.model_validate(
            _read_json(root_request_path)
        )
        response = replay_recorded_call(record_directory)
        if response.content_json is None:
            raise FactorMinerError(
                FailureCode.LLM_RESPONSE_INVALID,
                "格式修复需要已经解析出的 JSON object",
            )
        try:
            HypothesisAgentOutput.model_validate(response.content_json)
        except ValidationError as error:
            validation_errors = tuple(
                item["msg"]
                for item in error.errors()
                if isinstance(item.get("msg"), str)
            )
        else:
            raise ValueError("原响应已经通过假设 Schema，不需要格式修复")
        prepared = build_format_repair_request(
            campaign_id=root_request.campaign_id,
            slot_ids=root_request.slot_ids,
            public_payload=root_request.export_payload,
            invalid_content=response.content_json,
            validation_errors=validation_errors,
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(prepared.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "format_repair_prepared",
                "agent_role": prepared.agent_role,
                "request_sha256": prepared.request_sha256,
                "model": prepared.body["model"],
                "max_tokens": prepared.body["max_tokens"],
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("export-preview")
def llm_export_preview_command(
    request_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
) -> None:
    """扫描外发 payload，并展示精确请求哈希、信息类别和预算。"""

    try:
        prepared = PreparedDeepSeekRequest.model_validate(_read_json(request_path))
        policy = CorporateExternalResearchPolicy.model_validate(
            _read_json(policy_path)
        )
        preview = build_export_preview(prepared.export_payload, policy)
        _print_json(
            {
                "status": "awaiting_human_authorization",
                "campaign_id": prepared.campaign_id,
                "agent_role": prepared.agent_role,
                "slot_ids": prepared.slot_ids,
                "request_sha256": prepared.request_sha256,
                "payload_sha256": preview.payload_sha256,
                "payload_size_bytes": preview.payload_size_bytes,
                "information_classes": preview.information_classes,
                "endpoint": prepared.endpoint,
                "model": prepared.body["model"],
                "max_tokens": prepared.body["max_tokens"],
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("policy-build")
def llm_policy_build_command(
    policy_spec_path: Path = typer.Argument(
        ...,
        help="不含 policy_id 与 policy_sha256 的服务器私有政策 Spec。",
    ),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """从公司批准范围生成内容寻址、带有效期的私有外发政策。"""

    try:
        payload = _read_json(policy_spec_path)
        policy = registered_corporate_external_research_policy(**payload)
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(policy.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "policy_built",
                "policy_id": policy.policy_id,
                "policy_sha256": policy.policy_sha256,
                "valid_until": policy.valid_until.isoformat(),
                "approver_role": policy.approver_role,
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("authorize-export")
def llm_authorize_export_command(
    request_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    approved_request_sha256: str = typer.Option(
        ...,
        "--approved-request-sha256",
    ),
    approver_role: str = typer.Option(..., "--approver-role"),
    output_path: Path = typer.Option(..., "--output"),
    validity_minutes: int = typer.Option(30, "--validity-minutes", min=1, max=60),
) -> None:
    """把操作者明确输入的请求哈希登记为限时外发授权。"""

    try:
        prepared = PreparedDeepSeekRequest.model_validate(_read_json(request_path))
        policy = CorporateExternalResearchPolicy.model_validate(
            _read_json(policy_path)
        )
        scan_export_payload(prepared.export_payload, policy)
        if approved_request_sha256 != prepared.request_sha256:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "人工输入的请求哈希与精确请求不一致",
            )
        authorized_at = datetime.now(timezone.utc)
        authorization_payload = {
            "campaign_id": prepared.campaign_id,
            "request_sha256": prepared.request_sha256,
            "corporate_policy_id": policy.policy_id,
            "approver_role": approver_role,
            "authorized_at": authorized_at,
            "expires_at": authorized_at + timedelta(minutes=validity_minutes),
        }
        authorization = LLMExportAuthorization(
            authorization_id=(
                "authorization_"
                + sha256_json(
                    {
                        **authorization_payload,
                        "authorized_at": authorized_at.isoformat(),
                        "expires_at": (
                            authorized_at + timedelta(minutes=validity_minutes)
                        ).isoformat(),
                    }
                )[:24]
            ),
            **authorization_payload,
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(authorization.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "authorized",
                "authorization_id": authorization.authorization_id,
                "request_sha256": authorization.request_sha256,
                "expires_at": authorization.expires_at.isoformat(),
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("authorize-scope")
def llm_authorize_scope_command(
    family_id: str = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    output_path: Path = typer.Option(..., "--output"),
    information_class: str = typer.Option(
        "field_capabilities",
        "--information-class",
        help="允许自动化批次外发的信息类别；表达式批次默认使用 field_capabilities。",
    ),
    max_hypotheses: int = typer.Option(10, "--max-hypotheses", min=1, max=20),
    max_requests: int = typer.Option(40, "--max-requests", min=1, max=100),
    validity_hours: int = typer.Option(168, "--validity-hours", min=1, max=720),
) -> None:
    """为个人试运行生成一次研究族范围授权，不再逐个批准请求哈希。"""

    try:
        policy = CorporateExternalResearchPolicy.model_validate(
            _read_json(policy_path)
        )
        if information_class not in policy.allowed_information_classes:
            raise FactorMinerError(
                FailureCode.LLM_POLICY_NOT_AUTHORIZED,
                "个人试运行信息类别不在公司政策允许范围内",
            )
        authorized_at = datetime.now(timezone.utc)
        expires_at = min(
            policy.valid_until,
            authorized_at + timedelta(hours=validity_hours),
        )
        if expires_at <= authorized_at:
            raise FactorMinerError(
                FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
                "公司外发政策已经过期，不能生成范围授权",
            )
        scope = registered_llm_campaign_scope_authorization(
            family_id=family_id,
            corporate_policy_id=policy.policy_id,
            approver_role=policy.approver_role,
            allowed_agent_roles=(
                AgentRole.EXPRESSION,
                AgentRole.SEMANTIC_LINT,
            ),
            allowed_information_classes=(information_class,),
            max_hypotheses=max_hypotheses,
            max_requests=max_requests,
            authorized_at=authorized_at,
            expires_at=expires_at,
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(scope.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "scope_authorized",
                "authorization_id": scope.authorization_id,
                "family_id": scope.family_id,
                "allowed_agent_roles": scope.allowed_agent_roles,
                "allowed_information_classes": scope.allowed_information_classes,
                "max_hypotheses": scope.max_hypotheses,
                "max_requests": scope.max_requests,
                "expires_at": scope.expires_at.isoformat(),
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("provider-call")
def llm_provider_call_command(
    request_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    authorization_path: Path = typer.Argument(...),
    record_root: Path = typer.Option(..., "--record-root"),
) -> None:
    """发送一次获准请求；只输出审计元数据，不打印模型正文。"""

    try:
        authorization_payload = _read_json(authorization_path)
        if isinstance(authorization_payload, dict) and "scope_authorization" in authorization_payload:
            authorization_payload = authorization_payload["scope_authorization"]
        if (
            isinstance(authorization_payload, dict)
            and "scope_sha256" in authorization_payload
        ):
            authorization = LLMCampaignScopeAuthorization.model_validate(
                authorization_payload
            )
        else:
            authorization = LLMExportAuthorization.model_validate(
                authorization_payload
            )
        response = execute_recorded_call(
            prepared=PreparedDeepSeekRequest.model_validate(
                _read_json(request_path)
            ),
            authorization=authorization,
            policy=CorporateExternalResearchPolicy.model_validate(
                _read_json(policy_path)
            ),
            record_root=record_root,
            transport=UrllibDeepSeekTransport(),
            now=datetime.now(timezone.utc),
        )
        _print_json(
            {
                "status": "recorded",
                "call_id": response.record.call_id,
                "response_sha256": response.record.response_sha256,
                "finish_reason": response.record.finish_reason,
                "has_final_json": response.content_json is not None,
                "tool_call_count": len(response.tool_calls),
                "record_directory": str(response.record_directory),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("server-run-approved")
def llm_server_run_approved_command(
    family_id: str = typer.Argument(..., help="已登记的 LLM discovery family ID。"),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
    mode: str = typer.Option(
        "public_capability_only",
        "--mode",
        help="public_capability_only 或 sanitized_coverage_brief。",
    ),
) -> None:
    """执行已准备且已授权的 DeepSeek 请求包。"""

    try:
        if mode not in {
            "public_capability_only",
            "sanitized_coverage_brief",
        }:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                "LLM server-run mode 无效",
            )
        summary = run_server_side_llm_pipeline(
            family_id,
            mode,  # type: ignore[arg-type]
            artifact_root,
        )
        _print_json(summary.model_dump(mode="json"))
        if summary.status == "failed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as error:
        _fail(error)


def _load_approved_batch_dependencies(
    *,
    family_id: str,
    artifact_root: Path,
    approved_hypotheses_path: Path,
    policy_path: Path,
    payloads_path: Path,
    field_registry_path: Path,
    authorizations_path: Path,
) -> tuple[ApprovedBatchDependencies, tuple[RegisteredCoverageGapHypothesis, ...]]:
    """从服务器私有配置装载批准批次依赖，不打印正文。"""

    family = LLMDiscoveryLedger(artifact_root).load_family(family_id)
    hypotheses = _parse_approved_hypotheses(_read_json(approved_hypotheses_path))
    payloads = _parse_approved_payloads(
        _read_json(payloads_path),
        hypotheses,
    )
    authorizations, scope_authorization = _parse_approved_authorizations(
        _read_json(authorizations_path)
    )
    dependencies = ApprovedBatchDependencies(
        artifact_root=artifact_root,
        family=family,
        policy=CorporateExternalResearchPolicy.model_validate(
            _read_json(policy_path)
        ),
        public_payload_by_hypothesis_id=payloads,
        authorization_by_request_hash={
            item.request_sha256: item for item in authorizations
        },
        scope_authorization=scope_authorization,
        field_registry=FieldAvailabilityRegistry.model_validate(
            _read_json(field_registry_path)
        ),
    )
    return dependencies, hypotheses


def _parse_approved_hypotheses(
    payload: object,
) -> tuple[RegisteredCoverageGapHypothesis, ...]:
    """接受批量文件和兼容的单候选冻结文件两种形状。"""

    if isinstance(payload, dict):
        if "hypotheses" in payload:
            payload = payload["hypotheses"]
        elif "hypothesis_id" in payload:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("批准假设文件必须是 hypotheses 数组或单个假设对象")
    return tuple(
        RegisteredCoverageGapHypothesis.model_validate(item)
        for item in payload
    )


def _parse_approved_payloads(
    payload: object,
    hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
) -> dict[str, dict[str, object]]:
    """把批量 payload 或单个冻结 payload 绑定到假设身份。"""

    if isinstance(payload, dict) and "payloads" in payload:
        payload = payload["payloads"]
    if not isinstance(payload, dict):
        raise ValueError("批准批次 payload 文件必须是对象映射或单个 payload")
    if "information_class" in payload:
        return {item.hypothesis_id: dict(payload) for item in hypotheses}
    result: dict[str, dict[str, object]] = {}
    for hypothesis in hypotheses:
        value = payload.get(hypothesis.hypothesis_id)
        if not isinstance(value, dict):
            raise ValueError("批准批次 payload 缺少假设对应的结构化对象")
        result[hypothesis.hypothesis_id] = dict(value)
    return result


def _parse_approved_authorizations(
    payload: object,
) -> tuple[tuple[LLMExportAuthorization, ...], LLMCampaignScopeAuthorization | None]:
    """接受旧精确授权，或新的个人试运行范围授权。"""

    if isinstance(payload, dict):
        if "scope_authorization" in payload:
            scope = LLMCampaignScopeAuthorization.model_validate(
                payload["scope_authorization"]
            )
            exact_payload = payload.get("authorizations", [])
            if not isinstance(exact_payload, list):
                raise ValueError("authorizations 必须是数组")
            return (
                tuple(
                    LLMExportAuthorization.model_validate(item)
                    for item in exact_payload
                ),
                scope,
            )
        if "scope_sha256" in payload:
            return (), LLMCampaignScopeAuthorization.model_validate(payload)
        if "authorizations" in payload:
            payload = payload["authorizations"]
        elif "request_sha256" in payload:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("外发授权文件必须是 authorizations 数组或单个授权对象")
    return (
        tuple(LLMExportAuthorization.model_validate(item) for item in payload),
        None,
    )


@llm_app.command("prepare-approved")
def llm_prepare_approved_command(
    family_id: str = typer.Argument(...),
    approved_hypotheses_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    payloads_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """在服务器生成批准批次的精确表达式请求哈希，不访问网络。"""

    try:
        family = LLMDiscoveryLedger(artifact_root).load_family(family_id)
        hypotheses = _parse_approved_hypotheses(_read_json(approved_hypotheses_path))
        payloads = _parse_approved_payloads(
            _read_json(payloads_path),
            hypotheses,
        )
        dependencies = ApprovedBatchDependencies(
            artifact_root=artifact_root,
            family=family,
            policy=CorporateExternalResearchPolicy.model_validate(
                _read_json(policy_path)
            ),
            public_payload_by_hypothesis_id=payloads,
            authorization_by_request_hash={},
        )
        bundle = prepare_approved_batch_export(
            family_id,
            hypotheses,
            dependencies,
        )
        _print_json(
            {
                "status": "awaiting_human_authorization",
                "bundle_id": bundle.bundle_id,
                "family_id": bundle.family_id,
                "request_hashes": bundle.request_hashes,
                "candidate_slot_ids": bundle.candidate_slot_ids,
            }
        )
    except Exception as error:
        _fail(error)


def _run_approved_command(
    *,
    resume: bool,
    family_id: str,
    approved_hypotheses_path: Path,
    policy_path: Path,
    payloads_path: Path,
    field_registry_path: Path,
    authorizations_path: Path,
    artifact_root: Path,
) -> None:
    """执行或恢复批准批次并只输出运行摘要。"""

    dependencies, hypotheses = _load_approved_batch_dependencies(
        family_id=family_id,
        artifact_root=artifact_root,
        approved_hypotheses_path=approved_hypotheses_path,
        policy_path=policy_path,
        payloads_path=payloads_path,
        field_registry_path=field_registry_path,
        authorizations_path=authorizations_path,
    )
    result = (
        resume_approved_batch if resume else run_approved_batch
    )(
        family_id,
        hypotheses,
        dependencies,
    )
    _print_json(result.model_dump(mode="json"))


@llm_app.command("run-approved")
def llm_run_approved_command(
    family_id: str = typer.Argument(...),
    approved_hypotheses_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    payloads_path: Path = typer.Argument(...),
    field_registry_path: Path = typer.Argument(...),
    authorizations_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """执行批准假设的表达式、semantic lint 和候选登记。"""

    try:
        _run_approved_command(
            resume=False,
            family_id=family_id,
            approved_hypotheses_path=approved_hypotheses_path,
            policy_path=policy_path,
            payloads_path=payloads_path,
            field_registry_path=field_registry_path,
            authorizations_path=authorizations_path,
            artifact_root=artifact_root,
        )
    except Exception as error:
        _fail(error)


@llm_app.command("resume-approved")
def llm_resume_approved_command(
    family_id: str = typer.Argument(...),
    approved_hypotheses_path: Path = typer.Argument(...),
    policy_path: Path = typer.Argument(...),
    payloads_path: Path = typer.Argument(...),
    field_registry_path: Path = typer.Argument(...),
    authorizations_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """恢复批准批次，跳过已发布的不可变中间产物。"""

    try:
        _run_approved_command(
            resume=True,
            family_id=family_id,
            approved_hypotheses_path=approved_hypotheses_path,
            policy_path=policy_path,
            payloads_path=payloads_path,
            field_registry_path=field_registry_path,
            authorizations_path=authorizations_path,
            artifact_root=artifact_root,
        )
    except Exception as error:
        _fail(error)


@llm_app.command("literature-resolve")
def llm_literature_resolve_command(
    root_request_path: Path = typer.Argument(...),
    tool_call_record_directory: Path = typer.Argument(...),
    allowed_vocabulary_path: Path = typer.Argument(...),
    output_request_path: Path = typer.Option(..., "--output-request"),
    literature_root: Path = typer.Option(..., "--literature-root"),
) -> None:
    """通过 Crossref 解析工具调用并生成下一份待授权请求。"""

    try:
        root_request = PreparedDeepSeekRequest.model_validate(
            _read_json(root_request_path)
        )
        response = replay_recorded_call(tool_call_record_directory)
        vocabulary = frozenset(_read_json(allowed_vocabulary_path))
        records = []
        adapter = CrossrefMetadataAdapter()
        for tool_call in response.tool_calls:
            query = LiteratureQuery.model_validate(tool_call.arguments)
            validate_literature_query(query, vocabulary)
            record = adapter.search(
                query,
                retrieved_at=datetime.now(timezone.utc),
            )
            record_path = (
                literature_root
                / f"crossref_{record.response_sha256[:24]}.json"
            )
            _atomic_write_immutable(
                record_path,
                canonical_json_bytes(record.model_dump(mode="json")),
            )
            records.append(record)
        continuation = build_hypothesis_tool_continuation(
            root_request=root_request,
            response=response,
            literature_records=tuple(records),
        )
        _atomic_write_immutable(
            output_request_path,
            canonical_json_bytes(continuation.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "awaiting_continuation_authorization",
                "request_sha256": continuation.request_sha256,
                "literature_record_count": len(records),
                "output_request_path": str(output_request_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("response-validate")
def llm_response_validate_command(
    agent_role: str = typer.Argument(...),
    record_directory: Path = typer.Argument(...),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """离线重放最终响应，并按对应角色 Schema 写入不可变对象。"""

    try:
        role = AgentRole(agent_role)
        response = replay_recorded_call(record_directory)
        if response.content_json is None:
            raise FactorMinerError(
                FailureCode.LLM_RESPONSE_INVALID,
                "该响应仍是工具调用，尚无最终 JSON",
            )
        schema = {
            AgentRole.HYPOTHESIS: HypothesisAgentOutput,
            AgentRole.EXPRESSION: CandidateExpressionBatch,
            AgentRole.SEMANTIC_LINT: SemanticLintBatch,
        }.get(role)
        if schema is None:
            raise ValueError("response-validate 不接受格式修复角色")
        validated = schema.model_validate(response.content_json)
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(validated.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "validated",
                "agent_role": role,
                "call_id": response.record.call_id,
                "output_path": str(output_path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("candidate-convert")
def llm_candidate_convert_command(
    candidate_batch_path: Path = typer.Argument(...),
    lint_batch_path: Path = typer.Argument(...),
    hypothesis_path: Path = typer.Argument(...),
    field_registry_path: Path = typer.Argument(...),
    provenance_path: Path = typer.Argument(...),
    candidate_slot_id: str = typer.Option(..., "--candidate-slot-id"),
    output_path: Path = typer.Option(..., "--output"),
) -> None:
    """把通过硬校验与 semantic lint 的公开 AST 转成可信候选 Spec。"""

    try:
        candidates = CandidateExpressionBatch.model_validate(
            _read_json(candidate_batch_path)
        )
        lint_batch = SemanticLintBatch.model_validate(
            _read_json(lint_batch_path)
        )
        draft = next(
            item
            for item in candidates.candidates
            if item.candidate_slot_id == candidate_slot_id
        )
        lint = next(
            item
            for item in lint_batch.decisions
            if item.candidate_slot_id == candidate_slot_id
        )
        candidate = convert_candidate(
            draft=draft,
            hypothesis=RegisteredCoverageGapHypothesis.model_validate(
                _read_json(hypothesis_path)
            ),
            lint=lint,
            registry=FieldAvailabilityRegistry.model_validate(
                _read_json(field_registry_path)
            ),
            created_at=datetime.now(timezone.utc),
            provenance=_read_json(provenance_path),
        )
        registered = registered_trusted_candidate(candidate)
        plan = compile_candidate(
            registered,
            allowed_fields=tuple(
                item.field_id
                for item in FieldAvailabilityRegistry.model_validate(
                    _read_json(field_registry_path)
                ).fields
                if item.eligible_for_factor and item.point_in_time_guarantee
            ),
        )
        _atomic_write_immutable(
            output_path,
            canonical_json_bytes(candidate.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "candidate_ready_for_registration",
                "candidate_id": registered.candidate_id,
                "candidate_spec_hash": registered.spec_hash,
                "compiled_plan_hash": plan.plan_hash,
                "required_fields": candidate.required_fields,
                "max_lookback": candidate.max_lookback,
                "output_path": str(output_path),
            }
        )
    except StopIteration:
        _fail(ValueError("候选或 semantic lint 批次不存在指定槽位"))
    except Exception as error:
        _fail(error)


@llm_app.command("brief-build-synthetic")
def llm_brief_build_synthetic_command(
    input_path: Path = typer.Argument(..., help="包含 spec 与 brief 的合成 JSON。"),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
    synthetic: bool = typer.Option(False, "--synthetic"),
) -> None:
    """只登记程序生成的合成 brief，不读取真实覆盖图谱。"""

    try:
        if not synthetic or os.environ.get("FM_MODE", "").strip() == "visible":
            raise FactorMinerError(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "brief-build-synthetic 只允许显式合成模式",
            )
        payload = _read_json(input_path)
        registered = registered_llm_coverage_brief(
            LLMCoverageBriefSpec.model_validate(payload["spec"]),
            LLMCoverageBrief.model_validate(payload["brief"]),
        )
        path = LLMDiscoveryLedger(artifact_root).register_brief(registered)
        _print_json(
            {
                "status": "ok",
                "brief_id": registered.brief_id,
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("family-register")
def llm_family_register_command(
    spec_path: Path = typer.Argument(..., help="发现研究族 Spec JSON。"),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """登记四臂、120 槽和双数据身份的不可变 family。"""

    try:
        family = registered_llm_discovery_family(
            LLMDiscoveryResearchFamilySpec.model_validate(_read_json(spec_path))
        )
        path = LLMDiscoveryLedger(artifact_root).register_family(family)
        _print_json(
            {
                "status": "ok",
                "discovery_family_id": family.discovery_family_id,
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("event-append")
def llm_event_append_command(
    event_path: Path = typer.Argument(..., help="单条 LLMDiscoveryEvent JSON。"),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """先按当前投影校验状态边，再向独立哈希链追加事件。"""

    try:
        event = LLMDiscoveryEvent.model_validate(_read_json(event_path))
        ledger = LLMDiscoveryLedger(artifact_root)
        stored = ledger.append_validated_event(event.discovery_family_id, event)
        _print_json(stored.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@llm_app.command("family-seal")
def llm_family_seal_command(
    discovery_family_id: str = typer.Argument(...),
    seal_input_path: Path = typer.Argument(..., help="封存所需全部 hash 与依赖区间。"),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """仅在 120 槽全部终结后登记 seal 并追加 generation_sealed。"""

    try:
        ledger = LLMDiscoveryLedger(artifact_root)
        family = ledger.load_family(discovery_family_id)
        state = ledger.project_state(discovery_family_id)
        payload = _read_json(seal_input_path)
        seal = build_generation_seal(
            family,
            state,
            slot_object_hashes=payload["slot_object_hashes"],
            evaluation_dependency_intervals=tuple(
                EvaluationDependencyInterval.model_validate(item)
                for item in payload["evaluation_dependency_intervals"]
            ),
            llm_campaign_spec_hashes=tuple(payload["llm_campaign_spec_hashes"]),
            prompt_bundle_hashes=tuple(payload["prompt_bundle_hashes"]),
            model_identity=payload["model_identity"],
            generator_identity_hashes=payload["generator_identity_hashes"],
        )
        path = ledger.register_generation_seal(seal)
        stored = ledger.append_event(
            LLMDiscoveryEvent(
                event_id=f"event-generation-sealed-{seal.generation_seal_id[8:]}",
                discovery_family_id=discovery_family_id,
                target_kind=DiscoveryObjectKind.FAMILY,
                target_id=discovery_family_id,
                to_state=FamilyGenerationState.GENERATION_SEALED,
                created_at=datetime.now(timezone.utc),
            )
        )
        _print_json(
            {
                "status": "ok",
                "generation_seal_id": seal.generation_seal_id,
                "seal_path": str(path),
                "event_hash": stored.event_hash,
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("family-seal-auto")
def llm_family_seal_auto_command(
    discovery_family_id: str = typer.Argument(...),
    seal_input_path: Path = typer.Argument(
        ...,
        help="只包含 outcome 前冻结元数据的 seal JSON；槽 hash 由服务器本地重算。",
    ),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """从服务器槽对象重算 hash，并在完整 120 槽后登记 seal。"""

    try:
        family = LLMDiscoveryLedger(artifact_root).load_family(discovery_family_id)
        dependencies = GenerationSealDependencies(
            artifact_root=artifact_root,
            family=family,
        )
        payload = _read_json(seal_input_path)
        seal = seal_completed_generation(discovery_family_id, dependencies, payload)
        _print_json(
            {
                "status": "ok",
                "discovery_family_id": discovery_family_id,
                "generation_seal_id": seal.generation_seal_id,
                "manifest_sha256": seal.manifest_sha256,
            }
        )
    except Exception as error:
        _fail(error)


@llm_app.command("shadow-run")
def llm_shadow_run_command(
    discovery_family_id: str = typer.Argument(...),
    approved_coverage_hypotheses_path: Path = typer.Argument(
        ...,
        help="已批准 coverage 假设 JSON；只接受 coverage_outcome_llm 槽位。",
    ),
    field_registry_path: Path = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """在服务器按固定顺序生成 mechanical 与 grammar shadow arm。"""

    try:
        family = LLMDiscoveryLedger(artifact_root).load_family(discovery_family_id)
        hypotheses = _parse_approved_hypotheses(
            _read_json(approved_coverage_hypotheses_path)
        )
        summary = generate_shadow_arms(
            discovery_family_id,
            family,
            artifact_root,
            hypotheses,
            FieldAvailabilityRegistry.model_validate(
                _read_json(field_registry_path)
            ),
        )
        _print_json(summary.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@llm_app.command("family-verify")
def llm_family_verify_command(
    discovery_family_id: str = typer.Argument(...),
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """验证不可变 family、完整事件链、投影和可选 seal。"""

    try:
        ledger = LLMDiscoveryLedger(artifact_root)
        state = ledger.project_state(discovery_family_id)
        seal_path = (
            ledger.family_root(discovery_family_id) / "generation_seal.json"
        )
        seal_id = None
        if seal_path.is_file():
            family = ledger.load_family(discovery_family_id)
            seal = ledger.load_generation_seal(discovery_family_id)
            verify_generation_seal(family, state, seal)
            seal_id = seal.generation_seal_id
        _print_json(
            {
                "status": "ok",
                "discovery_family_id": discovery_family_id,
                "family_generation_state": state.family_generation_state,
                "candidate_slot_count": len(state.candidate_slot_states),
                "event_count": len(state.applied_event_ids),
                "generation_seal_id": seal_id,
            }
        )
    except Exception as error:
        _fail(error)


@regime_app.command("research")
def regime_research_command(
    spec_path: Path = typer.Argument(..., help="RegimeResearchSpec JSON 路径。"),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """在公司 Linux 的 input-only 数据上执行冻结的模型比较。"""

    try:
        environment = _environment(env_file)
        profile = load_runtime_profile(environment)
        assert_real_data_allowed(profile)
        identity = verify_runtime_identity(profile)
        spec = RegimeResearchSpec.model_validate(_read_json(spec_path))
        source = CompanyAShareRegimeDataSource(
            profile,
            code_commit=identity.code_commit,
            config_hash=identity.config_hash,
        )
        provenance = source.inspect_inputs()
        panel = source.scan_inputs(
            RegimeDataRequest(start=spec.research_start, end=spec.research_end)
        ).collect()
        features = {
            aggregation: build_market_features(
                panel,
                spec.feature_policy,
                aggregation,
                min_daily_assets=spec.min_daily_assets,
            ).frame
            for aggregation in spec.return_aggregations
        }
        report = run_regime_research(features, spec)
        registered = registered_regime_research(spec)
        ledger = JsonlLedger(profile.artifact_root)
        ledger.register_regime_research(registered)
        report_path = (
            profile.artifact_root
            / "artifacts"
            / "regime_research"
            / registered.regime_research_id
            / "regime_research_report.json"
        )
        report_payload = regime_research_report_payload(report)
        report_payload["input_provenance"] = asdict(provenance)
        report_payload["runtime_identity"] = {
            "release_manifest_sha256": identity.release_manifest_sha256,
            "config_hash": identity.config_hash,
            "code_commit": identity.code_commit,
            "uv_lock_sha256": identity.uv_lock_sha256,
        }
        _atomic_write_immutable(
            report_path,
            canonical_json_bytes(report_payload),
        )
        _print_json(
            {
                "status": "ok",
                "regime_research_id": registered.regime_research_id,
                "resolved_release_id": provenance.resolved_release_id,
                "recommendations": {
                    key.value: value
                    for key, value in report.recommendations.items()
                },
                "report_path": str(report_path),
            }
        )
    except Exception as error:
        _fail(error)


@regime_app.command("register-deployment")
def regime_register_deployment_command(
    deployment_path: Path = typer.Argument(
        ...,
        help="RegimeDeploymentSpec JSON 路径。",
    ),
    report_path: Path = typer.Option(..., "--report-path"),
    artifact_root: Path = typer.Option(
        ...,
        "--artifact-root",
        envvar="FM_ARTIFACT_ROOT",
    ),
) -> None:
    """人工选定研究候选后登记唯一、不可变的正式模型配置。"""

    try:
        deployment = registered_regime_deployment(
            RegimeDeploymentSpec.model_validate(_read_json(deployment_path))
        )
        research_path = (
            LedgerPaths(artifact_root).regime_research_root
            / f"{deployment.spec.regime_research_id}.json"
        )
        if not research_path.is_file():
            raise FactorMinerError(
                FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
                "部署引用的市场状态研究尚未登记",
            )
        research = RegisteredRegimeResearch.model_validate(
            _read_json(research_path)
        )
        report = regime_research_report_from_payload(_read_json(report_path))
        validate_regime_deployment(research, report, deployment)
        path = JsonlLedger(artifact_root).register_regime_deployment(deployment)
        _print_json(
            {
                "status": "ok",
                "regime_deployment_id": deployment.regime_deployment_id,
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@regime_app.command("build")
def regime_build_command(
    deployment_id: str = typer.Option(..., "--deployment-id"),
    start: str = typer.Option(..., "--start", help="构建起始日，格式 YYYY-MM-DD。"),
    end: str = typer.Option(..., "--end", help="构建截止日，格式 YYYY-MM-DD。"),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """按固定配置构建 filtered 状态日历并发布不可变快照。"""

    try:
        environment = _environment(env_file)
        profile = load_runtime_profile(environment)
        assert_real_data_allowed(profile)
        identity = verify_runtime_identity(profile)
        path = (
            LedgerPaths(profile.artifact_root).regime_deployments_root
            / f"{deployment_id}.json"
        )
        if not path.is_file():
            raise FactorMinerError(
                FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
                f"找不到已登记市场状态部署：{deployment_id}",
            )
        deployment = RegisteredRegimeDeployment.model_validate(_read_json(path))
        if deployment.regime_deployment_id != deployment_id:
            raise FactorMinerError(
                FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
                "部署 ID 与不可变文档内容不一致",
            )
        source = CompanyAShareRegimeDataSource(
            profile,
            code_commit=identity.code_commit,
            config_hash=identity.config_hash,
        )
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError as error:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                "--start 与 --end 必须使用 YYYY-MM-DD",
            ) from error
        result = build_regime_snapshot(
            source,
            deployment,
            start_date,
            end_date,
            profile.artifact_root,
        )
        _print_json(
            {
                "status": "ok",
                "regime_snapshot_id": result.regime_snapshot_id,
                "snapshot_root": str(result.snapshot_root),
            }
        )
    except Exception as error:
        _fail(error)


@regime_app.command("verify")
def regime_verify_command(
    snapshot_root: Path = typer.Argument(..., help="状态快照目录。"),
) -> None:
    """只读核验状态快照，不重新训练或改变任何状态。"""

    try:
        result = verify_regime_snapshot(snapshot_root)
        _print_json(
            {
                "status": "ok",
                "regime_snapshot_id": result.regime_snapshot_id,
                "regime_deployment_id": result.manifest.regime_deployment_id,
                "provenance": result.manifest.provenance,
                "visible_start": result.manifest.visible_start.isoformat(),
                "visible_end": result.manifest.visible_end.isoformat(),
            }
        )
    except Exception as error:
        _fail(error)


@coverage_app.command("build")
def coverage_build_command(
    spec_path: Path = typer.Argument(..., help="CoverageGraphSpec JSON 路径。"),
    catalog_path: Path = typer.Option(..., "--catalog", help="生产因子 YAML 路径。"),
    daily_ic_path: Path = typer.Option(
        ...,
        "--daily-ic",
        help="冻结评价身份下的每日 IC 长表 Parquet。",
    ),
    regime_snapshot_root: Path = typer.Option(
        ...,
        "--regime-snapshot",
        help="已经核验的市场状态快照目录。",
    ),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """只在公司 Linux 使用冻结输入构建不可变覆盖图谱。"""

    try:
        environment = _environment(env_file)
        profile = load_runtime_profile(environment)
        assert_real_data_allowed(profile)
        identity = verify_runtime_identity(profile)
        registered = registered_coverage_graph_spec(
            CoverageGraphSpec.model_validate(_read_json(spec_path))
        )
        actual_runtime = {
            "code_commit": identity.code_commit,
            "config_hash": identity.config_hash,
            "uv_lock_sha256": identity.uv_lock_sha256,
            "release_manifest_sha256": identity.release_manifest_sha256,
        }
        if registered.spec.runtime_provenance != actual_runtime:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                "CoverageGraphSpec 的运行身份与当前锁定环境不一致",
            )
        result = build_coverage_graph(
            catalog_path=catalog_path,
            daily_ic_path=daily_ic_path,
            regime_snapshot_root=regime_snapshot_root,
            artifact_root=profile.artifact_root,
            registered_spec=registered,
        )
        _print_json(
            {
                "status": "ok",
                "coverage_graph_id": result.coverage_graph_id,
                "snapshot_root": str(result.snapshot_root),
            }
        )
    except Exception as error:
        _fail(error)


@coverage_app.command("verify")
def coverage_verify_command(
    snapshot_root: Path = typer.Argument(..., help="覆盖图谱快照目录。"),
) -> None:
    """只读核验图谱文件、内容身份和正式行数。"""

    try:
        result = verify_coverage_graph(snapshot_root)
        _print_json(
            {
                "status": "ok",
                "coverage_graph_id": result.coverage_graph_id,
                "coverage_spec_id": result.manifest.coverage_spec_id,
                "regime_snapshot_id": result.manifest.regime_snapshot_id,
                "factor_count": result.manifest.factor_count,
                "pair_count": result.manifest.signal_edge_count,
            }
        )
    except Exception as error:
        _fail(error)


@app.command("doctor")
def doctor(
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="服务器私有 FM_ 配置文件；省略时读取当前环境变量。",
    ),
) -> None:
    """检查运行时边界、数据合同和账本，不计算因子。"""

    try:
        environment = _environment(env_file)
        profile = load_runtime_profile(environment)
        inputs_checked = False
        outcomes_checked = False
        references_checked = False
        identity = None
        if profile.mode.value in {"smoke", "visible"}:
            assert_real_data_allowed(profile)
            identity = verify_runtime_identity(profile)
            input_source = StandardPanelFactorInputSource(
                profile,
                code_commit=identity.code_commit,
                config_hash=identity.config_hash,
            )
            input_source.inspect_inputs()
            inputs_checked = True
            if environment.get("FM_REFERENCE_MANIFEST_PATH", "").strip():
                _load_reference_resources(
                    environment, company_a_share_visible_policy(), metadata_only=True
                )
                references_checked = True
            if profile.mode is ExecutionMode.VISIBLE:
                outcome_source = StandardPanelOutcomeSource(
                    profile, company_a_share_visible_policy()
                )
                outcome_source.inspect_outcome_metadata()
                outcomes_checked = True
        ledger = JsonlLedger(profile.artifact_root)
        ledger.verify()
        _print_json(
            {
                "status": "ok",
                "mode": profile.mode.value,
                "artifact_root": str(profile.artifact_root),
                "inputs_checked": inputs_checked,
                "outcomes_checked": outcomes_checked,
                "references_checked": references_checked,
                "release_manifest_sha256": (
                    identity.release_manifest_sha256 if identity else None
                ),
                "config_hash": identity.config_hash if identity else None,
                "code_commit": identity.code_commit if identity else None,
                "uv_lock_sha256": identity.uv_lock_sha256 if identity else None,
                "ledger_checked": True,
            }
        )
    except Exception as error:
        _fail(error)


@app.command("validate-spec")
def validate_spec(spec_path: Path = typer.Argument(..., help="CandidateFactorSpec JSON 路径。")) -> None:
    """校验候选 spec 并输出规范化 JSON。"""

    try:
        payload = _read_json(spec_path)
        candidate = (
            TrustedCandidateFactorSpec.model_validate(payload)
            if payload.get("spec_version") == "2"
            else CandidateFactorSpec.model_validate(payload)
        )
        _print_json(candidate.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@app.command("register-candidate")
def register_candidate_command(
    spec_path: Path = typer.Argument(..., help="CandidateFactorSpec JSON 路径。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """登记不可变候选并追加 candidate_registered 之前的状态文档。"""

    try:
        payload = _read_json(spec_path)
        candidate = (
            registered_trusted_candidate(TrustedCandidateFactorSpec.model_validate(payload))
            if payload.get("spec_version") == "2"
            else registered_candidate(CandidateFactorSpec.model_validate(payload))
        )
        ledger = JsonlLedger(artifact_root)
        path = ledger.register_candidate(candidate)
        _print_json(
            {
                "status": "ok",
                "candidate_id": candidate.candidate_id,
                "spec_hash": candidate.spec_hash,
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@app.command("register-campaign")
def register_campaign_command(
    campaign_path: Path = typer.Argument(..., help="CampaignSpec JSON 路径。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """仅在所有候选已经登记后登记不可变 campaign。"""

    try:
        payload = _read_json(campaign_path)
        if payload.get("spec_version") == "2":
            campaign = TrustedVisibleCampaignSpec.model_validate(payload)
            policy, family, candidates, _ = _load_trusted_documents(
                artifact_root,
                campaign,
            )
            validate_trusted_campaign(campaign, family, policy, candidates)
            identifier = trusted_campaign_id(campaign)
        else:
            campaign = CampaignSpec.model_validate(payload)
            candidates = _load_campaign_candidates(artifact_root, campaign)
            identifier = campaign_id(campaign)
        ledger = JsonlLedger(artifact_root)
        path = ledger.register_campaign(campaign)
        _print_json(
            {
                "status": "ok",
                "campaign_id": identifier,
                "candidate_count": len(candidates),
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@app.command("register-policy")
def register_policy_command(
    policy_path: Path = typer.Argument(..., help="EvaluationPolicySpec JSON 路径。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """登记不可变且内容寻址的评价政策。"""

    try:
        payload = _read_json(policy_path)
        if payload.get("policy_version") == "2":
            policy = IncrementalEvaluationPolicySpec.model_validate(payload)
            library = _load_registered_reference_library(
                artifact_root,
                policy.reference_factor_library_id,
            )
            try:
                validate_incremental_policy_library(policy, library)
            except ValueError as error:
                raise FactorMinerError(
                    FailureCode.REFERENCE_LIBRARY_MISMATCH,
                    str(error),
                ) from error
            if policy != company_a_share_incremental_policy(library):
                raise FactorMinerError(
                    FailureCode.SPEC_SCHEMA_INVALID,
                    "正式 CLI 只允许登记代码内置的公司 A 股 V0.2 评价政策",
                )
        else:
            policy = EvaluationPolicySpec.model_validate(payload)
            if policy != company_a_share_visible_policy():
                raise FactorMinerError(
                    FailureCode.SPEC_SCHEMA_INVALID,
                    "正式 CLI 只允许登记代码内置的公司 A 股 V0.1 评价政策",
                )
        path = JsonlLedger(artifact_root).register_evaluation_policy(policy)
        _print_json(
            {"status": "ok", "evaluation_policy_id": evaluation_policy_id(policy), "path": str(path)}
        )
    except Exception as error:
        _fail(error)


@app.command("register-reference-library")
def register_reference_library_command(
    library_path: Path = typer.Argument(
        ...,
        help="ReferenceFactorLibrarySpec JSON 路径。",
    ),
    artifact_root: Path = typer.Option(
        ...,
        "--artifact-root",
        envvar="FM_ARTIFACT_ROOT",
    ),
) -> None:
    """登记不可变且内容寻址的参考因子库。"""

    try:
        payload = _read_json(library_path)
        library = (
            RegisteredReferenceFactorLibrary.model_validate(payload)
            if "reference_factor_library_id" in payload
            else registered_reference_factor_library(
                ReferenceFactorLibrarySpec.model_validate(payload)
            )
        )
        path = JsonlLedger(artifact_root).register_reference_factor_library(
            library
        )
        _print_json(
            {
                "status": "ok",
                "reference_factor_library_id": (
                    library.reference_factor_library_id
                ),
                "spec_hash": library.spec_hash,
                "path": str(path),
            }
        )
    except Exception as error:
        _fail(error)


@app.command("register-family")
def register_family_command(
    family_path: Path = typer.Argument(..., help="ResearchFamilySpec JSON 路径。"),
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """登记不可变且跨批次共享的研究族预算。"""

    try:
        payload = _read_json(family_path)
        family = (
            RegisteredResearchFamily.model_validate(payload)
            if "research_family_id" in payload
            else registered_research_family(ResearchFamilySpec.model_validate(payload))
        )
        path = JsonlLedger(artifact_root).register_research_family(family)
        _print_json(
            {"status": "ok", "research_family_id": family.research_family_id, "path": str(path)}
        )
    except Exception as error:
        _fail(error)


@app.command("compile-spec")
def compile_spec(
    spec_path: Path = typer.Argument(..., help="CandidateFactorSpec 或 RegisteredCandidate JSON 路径。"),
    allowed_fields: str = typer.Option(
        ",".join(MARKET_COLUMNS),
        "--allowed-fields",
        help="允许的原始字段，使用逗号分隔。",
    ),
) -> None:
    """校验 typed AST 并输出确定性编译计划。"""

    try:
        payload = _read_json(spec_path)
        if "candidate_id" in payload:
            candidate = (
                RegisteredTrustedCandidate.model_validate(payload)
                if payload.get("spec", {}).get("spec_version") == "2"
                else RegisteredCandidate.model_validate(payload)
            )
        else:
            candidate = (
                registered_trusted_candidate(TrustedCandidateFactorSpec.model_validate(payload))
                if payload.get("spec_version") == "2"
                else registered_candidate(CandidateFactorSpec.model_validate(payload))
            )
        plan = compile_candidate(
            candidate,
            {field.strip() for field in allowed_fields.split(",") if field.strip()},
        )
        _print_json(plan.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@app.command("run-smoke")
def run_smoke(
    campaign_id_value: str = typer.Option(..., "--campaign-id"),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """运行已登记 campaign 的原始因子确定性 Smoke。"""

    try:
        result = _run_registered_campaign(campaign_id_value, env_file, visible=False)
        _print_json(result.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@app.command("run-visible")
def run_visible(
    campaign_id_value: str = typer.Option(..., "--campaign-id"),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """运行已登记 campaign 的 visible RankIC/HAC/Bonferroni 流程。"""

    try:
        result = _run_registered_campaign(campaign_id_value, env_file, visible=True)
        _print_json(result.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


@app.command("ledger-verify")
def ledger_verify(
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """验证 trials JSONL 的完整哈希链。"""

    try:
        events = JsonlLedger(artifact_root).verify()
        _print_json({"status": "ok", "event_count": len(events)})
    except Exception as error:
        _fail(error)


@app.command("recover-interrupted")
def recover_interrupted(
    artifact_root: Path = typer.Option(..., "--artifact-root", envvar="FM_ARTIFACT_ROOT"),
) -> None:
    """追加缺失的 interrupted 终态，不删除任何残留产物。"""

    try:
        recovered = recover_interrupted_runs(artifact_root)
        _print_json(
            {"status": "ok", "recovered_run_ids": recovered, "count": len(recovered)}
        )
    except Exception as error:
        _fail(error)


def _run_registered_campaign(
    requested_campaign_id: str,
    env_file: Path | None,
    *,
    visible: bool,
) -> RunResult:
    """加载服务器 profile、已登记候选和 campaign 后执行运行。"""

    environment = _environment(env_file)
    profile = load_runtime_profile(environment)
    assert_real_data_allowed(profile)
    identity = verify_runtime_identity(profile)
    root = profile.artifact_root
    paths = LedgerPaths(root)
    campaign_path = paths.campaigns_root / f"{requested_campaign_id}.json"
    if not campaign_path.is_file():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"找不到已登记 campaign：{requested_campaign_id}",
        )
    payload = _read_json(campaign_path)
    if payload.get("spec_version") != "2":
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "真实运行只接受 V0.1 trusted campaign；V0 记录仅供历史审计",
        )
    campaign = TrustedVisibleCampaignSpec.model_validate(payload)
    if trusted_campaign_id(campaign) != requested_campaign_id:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "campaign ID 与内容不一致")
    policy, family, candidates, reference_library = _load_trusted_documents(
        root,
        campaign,
    )
    if reference_library is not None:
        _validate_reference_manifest_binding(environment, reference_library)
    expected_mode = ExecutionMode.VISIBLE if visible else ExecutionMode.SMOKE
    if profile.mode is not expected_mode:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            f"命令要求 FM_MODE={expected_mode.value}，实际为 {profile.mode.value}",
        )
    source = StandardPanelFactorInputSource(
        profile,
        code_commit=identity.code_commit,
        config_hash=identity.config_hash,
    )
    if visible:
        reference_source, reference_plans = _load_reference_resources(
            environment, policy, metadata_only=False
        )
        if reference_source is None:
            raise FactorMinerError(FailureCode.FIELD_MISSING, "可见运行缺少参考因子数据源")
        outcome_source = StandardPanelOutcomeSource(profile, policy)
        if isinstance(policy, IncrementalEvaluationPolicySpec):
            if reference_library is None:
                raise FactorMinerError(
                    FailureCode.REFERENCE_LIBRARY_MISMATCH,
                    "V0.2 可见运行缺少已登记参考因子库",
                )
            return run_incremental_visible_campaign(
                campaign,
                candidates,
                policy,
                family,
                reference_library,
                source,
                outcome_source,
                reference_source,
                reference_plans,
                root,
                code_hash=identity.code_commit,
                config_hash=identity.config_hash,
                uv_lock_hash=identity.uv_lock_sha256,
            )
        return run_trusted_visible_campaign(
            campaign,
            candidates,
            policy,
            family,
            source,
            outcome_source,
            reference_source,
            reference_plans,
            root,
            code_hash=identity.code_commit,
            config_hash=identity.config_hash,
            uv_lock_hash=identity.uv_lock_sha256,
        )
    _, reference_plans = _load_reference_resources(
        environment, policy, metadata_only=True
    )
    return run_trusted_smoke_campaign(
        campaign,
        candidates,
        policy,
        family,
        source,
        reference_plans,
        root,
        reference_library=reference_library,
        code_hash=identity.code_commit,
        config_hash=identity.config_hash,
        uv_lock_hash=identity.uv_lock_sha256,
    )


def _uv_lock_hash() -> str:
    """返回当前项目 uv.lock 的内容哈希。"""

    lock_path = Path(__file__).resolve().parents[2] / "uv.lock"
    if not lock_path.is_file():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"找不到项目锁文件：{lock_path}",
        )
    return hashlib.sha256(lock_path.read_bytes()).hexdigest()


def _load_campaign_candidates(
    artifact_root: Path,
    campaign: CampaignSpec,
) -> dict[str, RegisteredCandidate]:
    """加载并校验 campaign 中全部已登记候选。"""

    paths = LedgerPaths(artifact_root)
    result: dict[str, RegisteredCandidate] = {}
    for identifier in campaign.candidate_ids:
        path = paths.candidates_root / f"{identifier}.json"
        if not path.is_file():
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"campaign 候选尚未登记：{identifier}",
            )
        candidate = RegisteredCandidate.model_validate(_read_json(path))
        if candidate.candidate_id != identifier:
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "候选 ID 与文档内容不一致")
        result[identifier] = candidate
    return result


def _load_trusted_documents(
    artifact_root: Path,
    campaign: TrustedVisibleCampaignSpec,
) -> tuple[
    EvaluationPolicySpec | IncrementalEvaluationPolicySpec,
    RegisteredResearchFamily,
    dict[str, RegisteredTrustedCandidate],
    RegisteredReferenceFactorLibrary | None,
]:
    """加载并交叉校验可信批次引用的全部不可变状态文档。"""

    paths = LedgerPaths(artifact_root)
    policy_path = paths.policies_root / f"{campaign.evaluation_policy_id}.json"
    family_path = paths.families_root / f"{campaign.research_family_id}.json"
    if not policy_path.is_file():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"评价政策尚未登记：{campaign.evaluation_policy_id}",
        )
    if not family_path.is_file():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"研究族尚未登记：{campaign.research_family_id}",
        )
    policy_payload = _read_json(policy_path)
    policy = (
        IncrementalEvaluationPolicySpec.model_validate(policy_payload)
        if policy_payload.get("policy_version") == "2"
        else EvaluationPolicySpec.model_validate(policy_payload)
    )
    if evaluation_policy_id(policy) != campaign.evaluation_policy_id:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "评价政策 ID 与内容不一致")
    reference_library: RegisteredReferenceFactorLibrary | None = None
    if isinstance(policy, IncrementalEvaluationPolicySpec):
        reference_library = _load_registered_reference_library(
            artifact_root,
            policy.reference_factor_library_id,
        )
        try:
            validate_incremental_policy_library(policy, reference_library)
        except ValueError as error:
            raise FactorMinerError(
                FailureCode.REFERENCE_LIBRARY_MISMATCH,
                str(error),
            ) from error
        if policy != company_a_share_incremental_policy(reference_library):
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                "已登记 V0.2 政策不是代码内置公司政策",
            )
    elif policy != company_a_share_visible_policy():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "已登记 V0.1 政策不是代码内置公司政策",
        )
    family = RegisteredResearchFamily.model_validate(_read_json(family_path))
    if family.research_family_id != campaign.research_family_id:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "研究族 ID 与内容不一致")
    candidates: dict[str, RegisteredTrustedCandidate] = {}
    for identifier in campaign.candidate_ids:
        path = paths.candidates_root / f"{identifier}.json"
        if not path.is_file():
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"可信候选尚未登记：{identifier}",
            )
        candidate = RegisteredTrustedCandidate.model_validate(_read_json(path))
        if candidate.candidate_id != identifier:
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "候选 ID 与文档内容不一致")
        candidates[identifier] = candidate
    validate_trusted_campaign(campaign, family, policy, candidates)
    return policy, family, candidates, reference_library


def _load_registered_reference_library(
    artifact_root: Path,
    identifier: str,
) -> RegisteredReferenceFactorLibrary:
    """加载并验证内容寻址的参考因子库文档。"""

    paths = LedgerPaths(artifact_root)
    path = paths.reference_libraries_root / f"{identifier}.json"
    if not path.is_file():
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            f"参考因子库尚未登记：{identifier}",
        )
    library = RegisteredReferenceFactorLibrary.model_validate(_read_json(path))
    if library.reference_factor_library_id != identifier:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "参考因子库 ID 与文档内容不一致",
        )
    return library


def _load_reference_resources(
    environment: dict[str, str],
    policy: EvaluationPolicySpec | IncrementalEvaluationPolicySpec,
    *,
    metadata_only: bool,
) -> tuple[ParquetReferenceFactorSource | None, dict[str, CompiledFactorPlan]]:
    """核验冻结参考清单，并按模式加载结构元数据或真实输出。"""

    manifest_path = Path(_required_environment(environment, "FM_REFERENCE_MANIFEST_PATH"))
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            "FM_REFERENCE_MANIFEST_PATH 必须指向存在的绝对 JSON 文件",
        )
    actual_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    expected_hash = _required_environment(environment, "FM_REFERENCE_MANIFEST_SHA256")
    if len(expected_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_hash
    ):
        raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, "参考清单 SHA-256 格式非法")
    if actual_hash != expected_hash:
        raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, "参考清单 SHA-256 与实际文件不一致")
    payload = _read_json(manifest_path)
    if payload.get("manifest_id") != policy.reference_manifest_id:
        raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, "参考清单 ID 与评价政策不一致")
    entries = payload.get("factors")
    if not isinstance(entries, list):
        raise FactorMinerError(FailureCode.FIELD_MISSING, "参考清单 factors 必须是数组")
    factor_ids = tuple(entry.get("factor_id") for entry in entries if isinstance(entry, dict))
    if factor_ids != policy.reference_factor_ids:
        raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, "参考因子集合或冻结顺序不一致")
    plans: dict[str, CompiledFactorPlan] = {}
    factor_paths: dict[str, Path] = {}
    for entry in entries:
        factor_id = entry["factor_id"]
        plan = CompiledFactorPlan.model_validate(entry.get("compiled_plan"))
        if plan.candidate_id != factor_id:
            raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, "参考编译计划 ID 与清单不一致")
        plans[factor_id] = plan
        if metadata_only:
            continue
        path = Path(str(entry.get("parquet_path", ""))).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise FactorMinerError(FailureCode.FIELD_MISSING, f"参考因子文件不存在：{factor_id}")
        parquet_hash = str(entry.get("parquet_sha256", ""))
        if hashlib.sha256(path.read_bytes()).hexdigest() != parquet_hash:
            raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, f"参考因子哈希不一致：{factor_id}")
        factor_paths[factor_id] = path.resolve()
    if metadata_only:
        return None, plans
    cutoff = str(payload.get("data_cutoff", "")).strip()
    if not cutoff:
        raise FactorMinerError(FailureCode.FIELD_MISSING, "参考清单缺少 data_cutoff")
    return (
        ParquetReferenceFactorSource(
            manifest_id=policy.reference_manifest_id,
            manifest_sha256=actual_hash,
            factor_paths=factor_paths,
            data_cutoff=cutoff,
        ),
        plans,
    )


def _validate_reference_manifest_binding(
    environment: dict[str, str],
    library: RegisteredReferenceFactorLibrary,
) -> None:
    """在打开任何参考输出前核验运行清单与已登记参考库身份。"""

    configured_hash = _required_environment(
        environment,
        "FM_REFERENCE_MANIFEST_SHA256",
    )
    if configured_hash != library.spec.reference_manifest_sha256:
        raise FactorMinerError(
            FailureCode.REFERENCE_LIBRARY_MISMATCH,
            "运行环境参考清单 SHA-256 与已登记 V0.2 参考因子库不一致",
        )


@barra_app.command("fetch-ricequant")
def barra_fetch_ricequant_command(
    env_file: Path = typer.Option(
        ...,
        "--env-file",
        help="仅含 RQ_USERNAME 与 RQ_TOKEN 的服务器私有 env 文件。",
    ),
    universe_uri: Path = typer.Option(
        ...,
        "--universe-uri",
        help="含 security_id 或 order_book_id 的服务器 Parquet/CSV。",
    ),
    derived_root: Path = typer.Option(
        ...,
        "--derived-root",
        help="独立的 factor_miner_derived Barra 发布根目录。",
    ),
    start: str = typer.Option(..., "--start", help="下载起始日 YYYY-MM-DD。"),
    end: str = typer.Option(..., "--end", help="下载截止日 YYYY-MM-DD。"),
    batch_size: int = typer.Option(500, "--batch-size", min=1),
) -> None:
    """按年度可恢复下载 v2trd/sws_2021 完整 Barra 风险输入。"""

    provider = None
    try:
        from factor_miner.ricequant_barra import (
            RiceQuantBarraDownloadRequest,
            RqdatacBarraProvider,
            fetch_ricequant_barra,
            load_ricequant_credentials,
        )

        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError as error:
            raise FactorMinerError(
                FailureCode.BARRA_DATA_CONTRACT_INVALID,
                "--start 与 --end 必须使用 YYYY-MM-DD",
            ) from error
        username, token = load_ricequant_credentials(env_file)
        provider = RqdatacBarraProvider(username, token)
        result = fetch_ricequant_barra(
            RiceQuantBarraDownloadRequest(
                derived_root=derived_root,
                universe_uri=universe_uri,
                start_date=start_date,
                end_date=end_date,
                batch_size=batch_size,
            ),
            provider,
        )
        _print_json(
            {
                "status": "ok",
                "message_cn": "米筐 Barra 派生数据已完成可恢复发布",
                "manifest_path": str(result.manifest_path),
                "completed_years": result.completed_years,
                "reused_years": result.reused_years,
            }
        )
    except Exception as error:
        _fail(error)
    finally:
        if provider is not None:
            provider.close()


@research_app.command("worker")
def research_worker_command(
    config: Path = typer.Option(..., "--config", help="服务器自主研究配置 JSON。"),
    once: bool = typer.Option(False, "--once", help="只处理一个 tick 后退出。"),
) -> None:
    """在本机启动可恢复的单机研究 Worker。"""

    try:
        from factor_miner.research_worker import ResearchWorker, ResearchWorkerConfig
        from factor_miner.server_research_dependencies import (
            ServerResearchDependencies,
            load_server_research_config,
        )

        server_config = load_server_research_config(config)
        lock_path = server_config.artifact_root / "state" / "autonomous_research" / "worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                if fcntl is None:
                    import msvcrt

                    if os.path.getsize(lock_path) == 0:
                        os.write(descriptor, b"0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("已有自主研究 Worker 正在运行") from None
            dependencies = ServerResearchDependencies(server_config)
            worker = ResearchWorker(
                ResearchWorkerConfig(artifact_root=server_config.artifact_root),
                dependencies=dependencies,
            )
            while True:
                dependencies.project_worker_heartbeat()
                result = worker.process_once()
                _print_json(result.model_dump(mode="json"))
                if once:
                    break
                time.sleep(server_config.poll_interval_seconds)
        finally:
            os.close(descriptor)
    except Exception as error:
        _fail(error)


@research_app.command("status")
def research_status_command(
    artifact_root: Path = typer.Option(..., "--artifact-root"),
) -> None:
    """读取本地正式状态并输出脱敏中文摘要。"""

    try:
        from factor_miner.research_control import ResearchControlStore

        store = ResearchControlStore(artifact_root)
        state = store.active_state()
        if state is None and store.runs_root.is_dir():
            states = [
                store._latest_state(path.name)
                for path in store.runs_root.iterdir()
                if path.is_dir()
            ]
            complete = [item for item in states if item is not None]
            state = max(complete, key=lambda item: item.updated_at) if complete else None
        _print_json(
            {
                "run_id": state.run_id if state else None,
                "stage": state.stage.value if state else None,
                "stage_label": state.stage_label if state else "空闲",
                "last_error": state.last_error if state else None,
                "pending_command_count": len(store.pending_commands()),
            }
        )
    except Exception as error:
        _fail(error)


@research_app.command("paired-shadow")
def research_paired_shadow_command(
    config: Path = typer.Option(..., "--config", help="服务器自主研究配置 JSON。"),
    run_id: str = typer.Option(..., "--run-id", help="已经发布的自主研究批次编号。"),
) -> None:
    """为一个已发布批次运行独立配对局部变异影子层。"""

    try:
        from factor_miner.server_research_dependencies import (
            ServerResearchDependencies,
            load_server_research_config,
        )

        dependencies = ServerResearchDependencies(load_server_research_config(config))
        summary = dependencies.run_paired_shadow_for_run(run_id)
        _print_json(
            {
                "status": summary.status,
                "run_id": summary.autonomous_run_id,
                "probe_count": len(summary.results),
                "accepted_preference_codes": summary.accepted_preference_codes,
                "summary_sha256": summary.summary_sha256,
                "error_cn": summary.error_cn,
            }
        )
    except Exception as error:
        _fail(error)


@screening_app.command("freeze-policy")
def screening_freeze_policy_command(
    cutoff_at: str = typer.Option(..., "--cutoff-at", help="带时区的筛选截止时点。"),
    output: Path = typer.Option(..., "--output", help="不可变筛选政策 JSON。"),
    target_min: int = typer.Option(20, "--target-min", min=1),
    target_max: int = typer.Option(30, "--target-max", min=1),
    policy_version: str = typer.Option(
        "candidate-screening-v2",
        "--policy-version",
        help="v2 使用冻结极端组差净收益去重；v1 仅用于历史主动收益报告。",
    ),
) -> None:
    """在读取逐候选结果前冻结筛选规则和目标数量。"""

    try:
        from factor_miner.candidate_screening import CandidateScreeningPolicy

        parsed = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
        if policy_version not in {"candidate-screening-v1", "candidate-screening-v2"}:
            raise ValueError("筛选政策版本必须是 candidate-screening-v1 或 candidate-screening-v2")
        policy = CandidateScreeningPolicy(
            version=policy_version,
            correlation_series=(
                "extreme_spread_net_return"
                if policy_version == "candidate-screening-v2"
                else "target_long_active_return"
            ),
            cutoff_at=parsed,
            target_min=target_min,
            target_max=target_max,
        )
        _atomic_write_immutable(
            output,
            canonical_json_bytes(policy.model_dump(mode="json")),
        )
        _print_json(
            {
                "status": "筛选政策已冻结",
                "policy_sha256": policy.policy_sha256,
                "cutoff_at": policy.cutoff_at.isoformat(),
                "target_min": policy.target_min,
                "target_max": policy.target_max,
                "output": str(output),
            }
        )
    except Exception as error:
        _fail(error)


@screening_app.command("build")
def screening_build_command(
    artifact_root: Path = typer.Option(..., "--artifact-root", help="正式不可变产物根目录。"),
    candidate_map: Path = typer.Option(..., "--candidate-map", help="稳定 huanNNN 映射 CSV。"),
    policy: Path = typer.Option(..., "--policy", help="已冻结筛选政策 JSON。"),
    output_root: Path = typer.Option(..., "--output-root", help="筛选报告独立发布根目录。"),
    regime_snapshot_root: Path | None = typer.Option(
        None,
        "--regime-snapshot-root",
        help="可选的已核验市场状态快照；覆盖不足时只作描述。",
    ),
) -> None:
    """从全部已发布候选运行构建筛选与稳健性报告。"""

    try:
        from factor_miner.candidate_screening_artifacts import (
            build_and_publish_screening_report,
        )

        publication = build_and_publish_screening_report(
            artifact_root=artifact_root,
            candidate_map_path=candidate_map,
            policy_path=policy,
            output_root=output_root,
            regime_snapshot_root=regime_snapshot_root,
        )
        _print_json(publication.model_dump(mode="json"))
    except Exception as error:
        _fail(error)


def _environment(env_file: Path | None) -> dict[str, str]:
    """合并私有配置文件和当前环境变量。"""

    environment = dict(os.environ)
    if env_file is None:
        return environment
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        environment[key.strip()] = value.strip().strip('"').strip("'")
    environment.setdefault("FM_CONFIG_PATH", str(env_file.resolve()))
    return environment


def _required_environment(environment: dict[str, str], key: str) -> str:
    """读取运行命令必需的非空环境变量。"""

    value = environment.get(key, "").strip()
    if not value:
        raise FactorMinerError(FailureCode.SPEC_SCHEMA_INVALID, f"缺少必需环境变量：{key}")
    return value


def _read_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件。"""

    return json.loads(path.read_text(encoding="utf-8"))


def _print_json(payload: Any) -> None:
    """向 stdout 输出紧凑规范化 JSON。"""

    typer.echo(canonical_json_bytes(payload).decode("utf-8"))


def _fail(error: Exception) -> None:
    """向 stderr 输出诊断并以非零状态退出。"""

    if isinstance(error, FactorMinerError):
        message = str(error)
    elif isinstance(error, ValidationError):
        message = f"SPEC_SCHEMA_INVALID: {error}"
    else:
        message = f"CLI_ERROR: {error}"
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@app.command("register-joint-diagnostic")
def register_joint_diagnostic(config: Path = typer.Argument(..., help="既有候选联合诊断配置"),
                              output: Path = typer.Argument(..., help="新的独立产物目录")) -> None:
    """冻结既有候选、原方向、输入身份和有限组合诊断预算。"""
    from factor_miner.joint_study import register_joint_study
    try:
        _print_json({"status": "registered", "root": str(register_joint_study(config, output))})
    except Exception as error:
        _fail(error)


@app.command("run-joint-diagnostic")
def run_joint_diagnostic(root: Path = typer.Argument(..., help="已登记的联合诊断目录")) -> None:
    """执行全池相关、滚动删除实验及组层重训子集 Shapley。"""
    from factor_miner.joint_study import run_joint_study
    try:
        _print_json({"status": "completed_diagnostic", "root": str(run_joint_study(root))})
    except Exception as error:
        _fail(error)


if __name__ == "__main__":
    app()
