"""跨平台自主研究 Worker 的显式依赖装配。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dashboard.pg_store import PostgresDashboardStore
from factor_miner.autonomous_schema import AutonomousResearchState
from factor_miner.barra_schema import BarraEvaluationPolicy
from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.dashboard_projection import project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.dsl import canonical_ast_hash
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import FieldAvailabilityRegistry
from factor_miner.ledger import atomic_write_bytes, atomic_write_immutable
from factor_miner.lightweight_evolution import (
    LightweightEvolutionContext,
    LightweightMemoryEntry,
    LightweightMemorySnapshot,
    LightweightMemoryStore,
    PublishedLightweightCoverageGraph,
    PublishedLightweightGapBrief,
    PublishedLightweightRun,
    build_lightweight_memory_feedback,
    refresh_lightweight_evolution,
)
from factor_miner.lightweight_expressions import (
    LightweightExpressionBatch,
    build_lightweight_expression_request,
    candidate_bindings_from_expression_batch,
    parse_lightweight_expression_response,
)
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightReviewBatch,
    generate_lightweight_hypotheses,
)
from factor_miner.lightweight_schema import (
    LightweightBatchManifest,
    LightweightCandidateBinding,
    LightweightResearchConfig,
    build_lightweight_batch_manifest,
)
from factor_miner.long_only_protocol import DirectionDecision
from factor_miner.paired_shadow import (
    PairedShadowPlan,
    PairedShadowPolicy,
    PairedShadowSummary,
    build_paired_shadow_plan,
    build_paired_shadow_summary,
    publish_paired_shadow_plan,
    publish_paired_shadow_summary,
)
from factor_miner.paired_shadow_server import evaluate_paired_shadow_plan
from factor_miner.llm_online import (
    AgentRole,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    registered_llm_evolution_request_authorization,
)
from factor_miner.llm_privacy import CorporateExternalResearchPolicy
from factor_miner.llm_provider import (
    RecordedLLMResponse,
    UrllibDeepSeekTransport,
    execute_recorded_call,
    replay_recorded_call,
)
from factor_miner.llm_state import CandidateSlotState
from factor_miner.long_only_protocol import LongOnlyResearchProtocol
from factor_miner.pilot_runner import run_fixed_pilot
from factor_miner.pilot_schema import (
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotRunRequest,
    PilotSourcePaths,
)
from factor_miner.policy import EvaluationPolicySpec
from factor_miner.portfolio_artifacts import publish_run_artifacts, verify_published_run
from factor_miner.research_campaign_runner import (
    CampaignSlotEvaluation,
    LightweightCampaignEvaluationResult,
)
from factor_miner.research_control import ResearchControlStore
from factor_miner.research_evolution import (
    build_evolution_hypothesis_request,
    evolution_gap_brief_sha256,
    sanitize_evolution_gap_brief,
)
from factor_miner.research_evolution_schema import ResearchEvolutionContext


def _run_pilot_with_frozen_family(
    runner: Callable[..., object],
    *,
    frozen_family_size: int,
    **kwargs: object,
) -> object:
    """将完整 manifest 研究族规模显式传给可能分批执行的 Pilot。"""

    if frozen_family_size <= 0:
        raise ValueError("冻结研究族规模必须为正整数")
    return runner(frozen_family_size=frozen_family_size, **kwargs)


def _dashboard_portfolio_daily(value: object) -> dict[str, object]:
    """只复制 Dashboard 画图所需逐期收益，避免重复发布逐证券权重。"""

    payload = value if isinstance(value, dict) else {}
    daily = payload.get("daily")
    return {"daily": daily if isinstance(daily, list) else []}


def _aggregate_direction_decisions(
    evaluation: LightweightCampaignEvaluationResult,
) -> dict[str, object]:
    """把各子 Pilot 已冻结的发现方向汇总到主发布运行。"""

    candidates: dict[str, object] = {}
    for item in evaluation.slot_evaluations:
        if item.status != "evaluated" or item.candidate_id is None:
            continue
        raw_decision = item.output.get("direction_decision")
        record_sha256 = item.output.get("direction_record_sha256")
        if not isinstance(raw_decision, Mapping) or not isinstance(
            record_sha256, str
        ):
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"候选 {item.candidate_id} 的评价结果缺少冻结发现方向",
            )
        decision = DirectionDecision.model_validate(raw_decision)
        candidates[item.candidate_id] = {
            "decision": decision.model_dump(mode="json"),
            "direction_record_sha256": record_sha256,
        }
    return {
        "version": "pilot-direction-decisions-v1",
        "candidates": candidates,
    }


def _latest_barra_rows(value: object) -> list[dict[str, object]]:
    """从完整 Barra 历史中提取最新信号日截面。"""

    rows = [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    dates = [str(row.get("signal_date")) for row in rows if row.get("signal_date")]
    if not dates:
        return rows
    latest = max(dates)
    return [row for row in rows if str(row.get("signal_date")) == latest]


def _dashboard_barra_summary(value: object) -> dict[str, object]:
    """保留状态和最新风险截面；完整历史继续留在来源 Pilot。"""

    wrapper = dict(value) if isinstance(value, dict) else {}
    detail_value = wrapper.get("attribution")
    if not isinstance(detail_value, dict):
        return wrapper
    detail = dict(detail_value)
    for name in ("exposure_summary", "attribution", "risk_decomposition"):
        detail[name] = _latest_barra_rows(detail.get(name))
    wrapper["attribution"] = detail
    return wrapper


def _candidate_dashboard_dimensions(
    snapshot: Mapping[str, object],
    candidate_id: str,
) -> dict[str, object]:
    """从单个 Pilot 快照提取主运行需要的候选维度。"""

    def dimension(name: str) -> dict[str, object]:
        value = snapshot.get(name)
        if not isinstance(value, dict):
            return {}
        candidates = value.get("candidates")
        return candidates if isinstance(candidates, dict) else value

    return {
        "candidate_metrics": dimension("candidate_metrics").get(candidate_id, {}),
        "ic_diagnostics": dimension("ic_diagnostics").get(candidate_id, {}),
        "recent_ic_diagnostics": dimension("recent_ic_diagnostics").get(
            candidate_id, {}
        ),
        "portfolio_metrics": dimension("portfolio_metrics").get(candidate_id, {}),
        "portfolio_governance": dimension("portfolio_governance").get(
            candidate_id, {}
        ),
        "recent_portfolio_metrics": dimension("recent_portfolio_metrics").get(
            candidate_id, {}
        ),
        "portfolio_daily": _dashboard_portfolio_daily(
            dimension("portfolio_daily").get(candidate_id, {})
        ),
        "barra_attribution": _dashboard_barra_summary(
            dimension("barra_attribution").get(candidate_id, {})
        ),
    }


def _load_frozen_direction_decision(
    *,
    artifact_root: Path,
    run_id: str,
    candidate_id: str,
    candidate_spec_hash: str,
    hypothesis_direction: Literal["positive", "negative"] | None = None,
) -> tuple[DirectionDecision, str]:
    """从磁盘重验正式运行、方向决定和方向记录的完整绑定。"""

    root = artifact_root.expanduser().resolve(strict=False)
    manifest = verify_published_run(root, run_id)

    def artifact_json(relative_path: str, label: str) -> dict[str, object]:
        if not any(item.relative_path == relative_path for item in manifest.artifacts):
            raise ValueError(f"已发布 Pilot 缺少{label}产物")
        path = root / "artifacts" / "runs" / run_id / relative_path
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"已发布 Pilot {label}无法读取") from error
        if not isinstance(payload, dict):
            raise ValueError(f"已发布 Pilot {label}必须是 object")
        return payload

    payload = artifact_json("direction/decisions.json", "冻结方向决定")
    metrics = artifact_json("run/metrics.json", "运行指标")
    if payload.get("version") != "pilot-direction-decisions-v1":
        raise ValueError("已发布 Pilot 冻结方向决定版本无效")
    try:
        candidate = payload["candidates"][candidate_id]
        metric_candidate = metrics["candidates"][candidate_id]
        decision = DirectionDecision.model_validate(candidate["decision"])
        record_ref = str(candidate["direction_record_ref"])
        record_sha256 = str(candidate["direction_record_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("已发布 Pilot 冻结方向决定合同无效") from error
    if (
        not isinstance(metric_candidate, Mapping)
        or metric_candidate.get("candidate_id") != candidate_id
        or metric_candidate.get("spec_sha256") != candidate_spec_hash
        or metric_candidate.get("direction_record_ref") != record_ref
        or metric_candidate.get("direction_record_sha256") != record_sha256
    ):
        raise ValueError("已发布 Pilot 候选、Spec 或方向记录引用或哈希不一致")
    if hypothesis_direction is not None and decision.hypothesis_direction != hypothesis_direction:
        raise ValueError("已发布 Pilot 方向决定与事前假设方向不一致")
    record_path = (root / record_ref).resolve(strict=False)
    state_root = (root / "state" / "pilot_direction").resolve(strict=False)
    if record_path.parent != state_root:
        raise ValueError("已发布 Pilot 方向记录引用越界")
    try:
        record = json.loads(record_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("已发布 Pilot 方向记录无法读取") from error
    if not isinstance(record, dict) or sha256_json(record) != record_sha256:
        raise ValueError("已发布 Pilot 方向记录哈希不一致")
    expected_provenance = {
        "data_release_id": metrics.get("data_release_id"),
        "input_manifest_sha256": metrics.get("input_manifest_sha256"),
        "evaluation_policy_id": metrics.get("evaluation_policy_id"),
        "family_size": metrics.get("frozen_family_size"),
        "code_commit": metrics.get("code_commit"),
        "config_hash": metrics.get("config_hash"),
        "source_run_id": metrics.get("source_run_id"),
    }
    if (
        record.get("version") != "pilot-direction-freeze-v1"
        or record.get("decision") != decision.model_dump(mode="json")
        or not isinstance(record.get("identity"), Mapping)
        or record["identity"].get("candidate_id") != candidate_id
        or record["identity"].get("spec_sha256") != candidate_spec_hash
        or record.get("provenance") != expected_provenance
    ):
        raise ValueError("已发布 Pilot 方向记录候选、Spec 或 provenance 不一致")
    return decision, record_sha256


AUTONOMOUS_LLM_INFORMATION_CLASSES = (
    "evolution_gap_brief",
    "public_capability_only",
)

_CANDIDATE_LOCAL_EVALUATION_FAILURES = frozenset(
    {
        FailureCode.FACTOR_ALL_NULL,
        FailureCode.FACTOR_COVERAGE_TOO_LOW,
        FailureCode.STAT_FAMILY_NOT_FROZEN,
        FailureCode.INSUFFICIENT_VALID_DATES,
    }
)


def _evaluate_ready_with_isolation(
    bindings: Sequence[LightweightCandidateBinding],
    evaluator: Callable[
        [Sequence[LightweightCandidateBinding]],
        list[CampaignSlotEvaluation],
    ],
) -> list[CampaignSlotEvaluation]:
    """组内候选错误逐槽隔离，基础设施和数据合同错误继续硬失败。"""

    try:
        return evaluator(bindings)
    except FactorMinerError as group_error:
        if group_error.code not in _CANDIDATE_LOCAL_EVALUATION_FAILURES:
            raise
        if len(bindings) == 1:
            binding = bindings[0]
            return [
                CampaignSlotEvaluation(
                    slot_id=binding.slot_id,
                    slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                    status="failed",
                    candidate_id=binding.source_candidate_id,
                    candidate_spec_hash=binding.candidate_spec_hash,
                    failure_reason=f"候选统一评价失败：{group_error}",
                )
            ]

    isolated: list[CampaignSlotEvaluation] = []
    for binding in bindings:
        isolated.extend(_evaluate_ready_with_isolation((binding,), evaluator))
    return isolated


class ServerScopePolicy(BaseModel):
    """启动按钮自动授权的固定模型和匿名审批角色。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    approver_role: str = Field(min_length=1)
    authorization_hours: int = Field(default=12, ge=1, le=72)
    allowed_agent_roles: tuple[Literal["expression", "hypothesis"], ...] = (
        "expression",
        "hypothesis",
    )


class ServerResearchConfig(BaseModel):
    """真实服务器自主研究的全部显式配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: Path
    minute_aggregate_root: Path | None = None
    field_registry_path: Path
    evolution_context_path: Path
    gap_brief_path: Path
    corporate_policy_path: Path
    scope_authorization_path: Path
    evaluation_policy_path: Path
    barra_policy_path: Path | None = None
    pilot_paths: PilotSourcePaths
    pilot_request: PilotRunRequest
    provider_mode: Literal["live", "recorded"] = "live"
    recorded_hypothesis_call: Path | None = None
    recorded_expression_call: Path | None = None
    dashboard_dsn_env: str = "FM_DASHBOARD_DSN"
    poll_interval_seconds: int = Field(default=5, ge=1, le=60)
    paired_shadow: PairedShadowPolicy = PairedShadowPolicy()

    @field_validator(
        "artifact_root",
        "minute_aggregate_root",
        "field_registry_path",
        "evolution_context_path",
        "gap_brief_path",
        "corporate_policy_path",
        "scope_authorization_path",
        "evaluation_policy_path",
        "barra_policy_path",
        "recorded_hypothesis_call",
        "recorded_expression_call",
        mode="before",
    )
    @classmethod
    def validate_absolute_path(cls, value: object, info: object) -> object:
        """服务器路径不允许相对值或隐式默认。"""

        if value is None:
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{getattr(info, 'field_name', 'path')} 必须是绝对路径")
        return str(path)

    @model_validator(mode="after")
    def validate_provider_mode(self) -> ServerResearchConfig:
        """录制与在线模式互斥，且 Pilot 产物根必须一致。"""

        if self.provider_mode == "recorded" and (
            self.recorded_hypothesis_call is None
            or self.recorded_expression_call is None
        ):
            raise ValueError("录制模式必须显式提供两次调用目录")
        if self.pilot_request.artifact_root != self.artifact_root:
            raise ValueError("Pilot 请求与 Worker artifact_root 必须一致")
        barra_enabled = any(
            path is not None
            for path in (
                self.pilot_paths.barra_exposure_uri,
                self.pilot_paths.barra_factor_returns_uri,
                self.pilot_paths.barra_benchmark_weights_uri,
            )
        )
        if barra_enabled and self.barra_policy_path is None:
            raise ValueError("启用 Barra 输入时必须显式提供 barra_policy_path")
        return self


def load_server_research_config(
    path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    system_name: str | None = None,
) -> ServerResearchConfig:
    """在接触真实数据前核验路径、环境和全部 Schema。"""
    config = ServerResearchConfig.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )
    required_files = (
        config.field_registry_path,
        config.evolution_context_path,
        config.gap_brief_path,
        config.corporate_policy_path,
        config.scope_authorization_path,
        config.evaluation_policy_path,
        config.barra_policy_path,
    )
    if not config.artifact_root.is_dir():
        raise ValueError("自主研究产物根目录不存在")
    if config.minute_aggregate_root is not None and not config.minute_aggregate_root.is_dir():
        raise ValueError("显式配置的分钟聚合目录不存在")
    if any(item is not None and not item.is_file() for item in required_files):
        raise ValueError("字段注册、上下文、政策或授权配置缺失")
    if config.provider_mode == "live":
        policy = CorporateExternalResearchPolicy.model_validate_json(
            config.corporate_policy_path.read_bytes()
        )
        missing_classes = tuple(
            sorted(
                set(AUTONOMOUS_LLM_INFORMATION_CLASSES).difference(
                    policy.allowed_information_classes
                )
            )
        )
        if missing_classes:
            raise ValueError(
                f"在线自主研究外发政策缺少必需信息类别：{missing_classes}"
            )
    if config.provider_mode == "recorded":
        if not config.recorded_hypothesis_call.is_dir() or not config.recorded_expression_call.is_dir():  # type: ignore[union-attr]
            raise ValueError("录制调用目录不存在")
    values = dict(os.environ if environment is None else environment)
    if not values.get(config.dashboard_dsn_env, "").strip():
        raise ValueError(f"缺少 PostgreSQL 环境变量：{config.dashboard_dsn_env}")
    if config.provider_mode == "live" and not values.get("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("在线模式缺少 DEEPSEEK_API_KEY")
    return config


class _EvolutionPublisher:
    """把轻量记忆、图谱目录和中文 gap brief 发布为不可变对象。"""

    def __init__(self, artifact_root: Path) -> None:
        self.root = artifact_root
        self.memory = LightweightMemoryStore(artifact_root)

    def publish_memory(self, entries, source_context, run_id):
        return self.memory.publish(
            entries,
            source_context=source_context,
            run_id=run_id,
            written_at=datetime.now(timezone.utc),
        )

    def publish_coverage_graph(self, catalog):
        payload = catalog.model_dump(mode="json")
        digest = sha256_json(payload)
        graph_id = f"lwgraph_{digest[:24]}"
        atomic_write_immutable(
            self.root / "coverage" / "lightweight" / f"{graph_id}.json",
            canonical_json_bytes(payload),
        )
        return PublishedLightweightCoverageGraph(
            graph_id=graph_id,
            graph_sha256=digest,
        )

    def publish_gap_brief(self, memory, graph):
        payload = {
            "memory_snapshot_id": memory.snapshot_id,
            "coverage_graph_id": graph.graph_id,
            "description_cn": "下一轮研究优先覆盖尚未评价、失败或结构稀疏的因子方向。",
        }
        digest = sha256_json(payload)
        brief_id = f"lwgap_{digest[:24]}"
        atomic_write_immutable(
            self.root / "coverage" / "lightweight" / f"{brief_id}.json",
            canonical_json_bytes(payload),
        )
        return PublishedLightweightGapBrief(
            brief_id=brief_id,
            brief_sha256=digest,
            description_cn=str(payload["description_cn"]),
        )


class ServerResearchDependencies:
    """复用现有编译、Pilot、发布和 PostgreSQL 端口的服务器实现。"""

    def __init__(
        self,
        config: ServerResearchConfig,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.environment = dict(os.environ if environment is None else environment)
        self.registry = FieldAvailabilityRegistry.model_validate_json(
            config.field_registry_path.read_bytes()
        )
        raw_context = json.loads(config.evolution_context_path.read_text("utf-8"))
        if not isinstance(raw_context, dict):
            raise ValueError("evolution context 必须是 JSON object")
        self.context = ResearchEvolutionContext.model_validate(
            raw_context.get("context", raw_context)
        )
        raw_gap = json.loads(config.gap_brief_path.read_text("utf-8"))
        if not isinstance(raw_gap, dict):
            raise ValueError("gap brief 必须是 JSON object")
        gap_value = raw_gap.get("gap_brief", raw_gap)
        if not isinstance(gap_value, dict):
            raise ValueError("gap brief 内容必须是 JSON object")
        self.gap_brief = gap_value
        self.policy = CorporateExternalResearchPolicy.model_validate_json(
            config.corporate_policy_path.read_bytes()
        )
        self.scope = ServerScopePolicy.model_validate_json(
            config.scope_authorization_path.read_bytes()
        )
        self.evaluation_policy = EvaluationPolicySpec.model_validate_json(
            config.evaluation_policy_path.read_bytes()
        )
        self.barra_policy = (
            BarraEvaluationPolicy.model_validate_json(
                config.barra_policy_path.read_bytes()
            )
            if config.barra_policy_path is not None
            else None
        )
        self.pg = PostgresDashboardStore(self.environment[config.dashboard_dsn_env])
        self.control = ResearchControlStore(config.artifact_root)
        self.evolution = _EvolutionPublisher(config.artifact_root)
        self.memory_snapshot_id = f"memory_{self.context.memory_snapshot_hash[:24]}"
        self.coverage_graph_id = (
            f"graph_{self.context.coverage_graph_manifest_hash[:24]}"
        )
        self.current_evolution_path = (
            config.artifact_root
            / "state"
            / "autonomous_research"
            / "evolution_current.json"
        )
        if self.current_evolution_path.is_file():
            self._load_current_evolution()
        else:
            self._bootstrap_current_evolution()

    def _load_current_evolution(self) -> None:
        """从原子指针恢复上一轮真正发布的记忆和图谱。"""

        payload = json.loads(self.current_evolution_path.read_text("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("当前进化状态必须是 JSON object")
        expected = sha256_json(
            {key: value for key, value in payload.items() if key != "state_sha256"}
        )
        if payload.get("state_sha256") != expected:
            raise ValueError("当前进化状态内容身份不一致")
        context = ResearchEvolutionContext.model_validate(payload.get("context"))
        gap_brief = payload.get("gap_brief")
        if not isinstance(gap_brief, dict):
            raise ValueError("当前进化状态缺少 gap brief")
        feedback = gap_brief.get("memory_feedback")
        legacy_feedback = isinstance(feedback, dict) and "ic_quality_band" not in feedback
        semantic_feedback_missing = not isinstance(feedback, dict) or (
            "semantic_coverage" not in feedback or "semantic_quota" not in feedback
        )
        if legacy_feedback:
            gap_brief = dict(gap_brief)
            migrated_feedback = dict(feedback)
            migrated_feedback["ic_quality_band"] = "unknown"
            gap_brief["memory_feedback"] = migrated_feedback
        hash_migration = legacy_feedback
        if semantic_feedback_missing:
            snapshot_id = str(payload.get("memory_snapshot_id", ""))
            snapshot_path = (
                self.evolution.memory.snapshots_root / f"{snapshot_id}.json"
            )
            entries = (
                self.evolution.memory.load_snapshot_entries(snapshot_id)
                if snapshot_path.is_file()
                else ()
            )
            semantic_feedback = build_lightweight_memory_feedback(entries)
            gap_brief = dict(gap_brief)
            migrated_feedback = (
                dict(gap_brief["memory_feedback"])
                if isinstance(gap_brief.get("memory_feedback"), dict)
                else semantic_feedback
            )
            migrated_feedback["semantic_coverage"] = semantic_feedback["semantic_coverage"]
            migrated_feedback["semantic_quota"] = semantic_feedback["semantic_quota"]
            gap_brief["memory_feedback"] = migrated_feedback
            hash_migration = True
        if context.gap_report_hash != evolution_gap_brief_sha256(gap_brief):
            if not hash_migration:
                raise ValueError("当前进化上下文与 gap brief 不一致")
        if payload.get("memory_snapshot_hash") != context.memory_snapshot_hash:
            raise ValueError("当前进化状态的记忆身份不一致")
        if (
            payload.get("coverage_graph_hash")
            != context.coverage_graph_manifest_hash
        ):
            raise ValueError("当前进化状态的图谱身份不一致")
        self.context = context
        self.gap_brief = gap_brief
        self.memory_snapshot_id = str(payload["memory_snapshot_id"])
        self.coverage_graph_id = str(payload["coverage_graph_id"])
        if hash_migration:
            self._activate_evolution(
                memory_snapshot_id=self.memory_snapshot_id,
                memory_snapshot_hash=context.memory_snapshot_hash,
                coverage_graph_id=self.coverage_graph_id,
                coverage_graph_hash=context.coverage_graph_manifest_hash,
                gap_brief=gap_brief,
            )

    def _activate_evolution(
        self,
        *,
        memory_snapshot_id: str,
        memory_snapshot_hash: str,
        coverage_graph_id: str,
        coverage_graph_hash: str,
        gap_brief: dict[str, object],
    ) -> str:
        """原子切换下一批要读取的累计记忆、图谱和反馈。"""

        context_payload = self.context.model_dump(
            mode="json", exclude={"context_sha256"}
        )
        context_payload.update(
            {
                "memory_snapshot_hash": memory_snapshot_hash,
                "coverage_graph_manifest_hash": coverage_graph_hash,
                "gap_report_hash": evolution_gap_brief_sha256(gap_brief),
            }
        )
        context = ResearchEvolutionContext(**context_payload)
        payload: dict[str, object] = {
            "version": "autonomous-evolution-current-v1",
            "context": context.model_dump(mode="json"),
            "gap_brief": gap_brief,
            "memory_snapshot_id": memory_snapshot_id,
            "memory_snapshot_hash": memory_snapshot_hash,
            "coverage_graph_id": coverage_graph_id,
            "coverage_graph_hash": coverage_graph_hash,
        }
        payload["state_sha256"] = sha256_json(payload)
        atomic_write_bytes(
            self.current_evolution_path,
            canonical_json_bytes(payload),
        )
        self.context = context
        self.gap_brief = gap_brief
        self.memory_snapshot_id = memory_snapshot_id
        self.coverage_graph_id = coverage_graph_id
        return str(payload["state_sha256"])

    def _bootstrap_current_evolution(self) -> None:
        """首次升级时把已有轻量记忆合并，并采用最近已发布图谱。"""

        if not self.evolution.memory.objects_root.is_dir():
            return
        refresh_paths = sorted(
            self.control.runs_root.glob("autrun_*/objects/evolution_refresh.json"),
            key=lambda path: path.stat().st_mtime,
        )
        if not refresh_paths:
            return
        latest = json.loads(refresh_paths[-1].read_text("utf-8"))
        source = LightweightEvolutionContext(
            memory_snapshot_id=self.memory_snapshot_id,
            memory_snapshot_hash=self.context.memory_snapshot_hash,
            coverage_graph_id=self.coverage_graph_id,
            coverage_graph_hash=self.context.coverage_graph_manifest_hash,
        )
        snapshot = self.evolution.memory.publish(
            (),
            source_context=source,
            run_id="memory_bootstrap_current",
            written_at=datetime.now(timezone.utc),
        )
        entries = self.evolution.memory.load_snapshot_entries(snapshot.snapshot_id)
        gap_brief = dict(self.gap_brief)
        gap_brief["memory_feedback"] = build_lightweight_memory_feedback(entries)
        self._activate_evolution(
            memory_snapshot_id=snapshot.snapshot_id,
            memory_snapshot_hash=snapshot.snapshot_sha256,
            coverage_graph_id=str(latest["next_coverage_graph_id"]),
            coverage_graph_hash=str(latest["next_coverage_graph_hash"]),
            gap_brief=gap_brief,
        )

    def project_worker_heartbeat(self) -> None:
        """独立刷新 Worker 心跳，使空闲期也能显示真实在线状态。"""

        self.pg.project_worker_heartbeat()

    @staticmethod
    def _retry_invalid_provider_response(call, *, attempts: int = 3):
        """仅对供应商偶发的无效响应自动重试，不吞掉其他失败。"""

        last_error: FactorMinerError | None = None
        for _ in range(attempts):
            try:
                return call()
            except FactorMinerError as error:
                if error.code is not FailureCode.LLM_RESPONSE_INVALID:
                    raise
                last_error = error
        assert last_error is not None
        raise last_error

    def _provider_call(self, prepared: PreparedDeepSeekRequest) -> RecordedLLMResponse:
        if self.config.provider_mode == "recorded":
            path = (
                self.config.recorded_hypothesis_call
                if prepared.agent_role is AgentRole.HYPOTHESIS
                else self.config.recorded_expression_call
            )
            assert path is not None
            response = replay_recorded_call(path)
            if (
                response.record.request_sha256 != prepared.request_sha256
                or response.record.agent_role != prepared.agent_role.value
                or response.record.slot_ids != prepared.slot_ids
            ):
                raise ValueError("录制调用与当前请求身份不一致")
            return response
        def execute():
            now = datetime.now(timezone.utc)
            authorization = LLMExportAuthorization(
                authorization_id=f"dashboard_{prepared.request_sha256[:24]}",
                campaign_id=prepared.campaign_id,
                request_sha256=prepared.request_sha256,
                corporate_policy_id=self.policy.policy_id,
                approver_role=self.scope.approver_role,
                authorized_at=now,
                expires_at=now + timedelta(hours=self.scope.authorization_hours),
            )
            return execute_recorded_call(
                prepared=prepared,
                authorization=authorization,
                policy=self.policy,
                record_root=self.config.artifact_root / "llm_calls" / "autonomous",
                transport=UrllibDeepSeekTransport(),
                now=now,
            )

        return self._retry_invalid_provider_response(execute)

    def _generate_hypothesis_batch_with_retry(
        self,
        *,
        run_id: str,
        prepared: PreparedDeepSeekRequest,
        provider,
    ):
        """重试未通过完整 Schema 与中文叙事校验的假设批次。"""

        return self._retry_invalid_provider_response(
            lambda: generate_lightweight_hypotheses(
                run_id=run_id,
                context=self.context,
                prepared=prepared,
                provider=provider,
            )
        )

    def prepare_context(self, state: AutonomousResearchState) -> Mapping[str, object]:
        return {
            "evolution_context": self.context.model_dump(mode="json"),
            "gap_brief": self.gap_brief,
            "memory_snapshot_id": self.memory_snapshot_id,
            "memory_snapshot_hash": self.context.memory_snapshot_hash,
            "coverage_graph_id": self.coverage_graph_id,
            "coverage_graph_hash": self.context.coverage_graph_manifest_hash,
        }

    def generate_hypotheses(self, state, source_context):
        now = datetime.now(timezone.utc)
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=self.context.discovery_family_id,
            context_sha256=self.context.context_sha256,
            gap_report_hash=self.context.gap_report_hash,
            memory_snapshot_hash=self.context.memory_snapshot_hash,
            brief_sha256=evolution_gap_brief_sha256(self.gap_brief),
            corporate_policy_id=self.policy.policy_id,
            approver_role=self.scope.approver_role,
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=now,
            expires_at=now + timedelta(hours=self.scope.authorization_hours),
        )
        prepared = build_evolution_hypothesis_request(
            self.context,
            self.gap_brief,
            authorization=authorization,
        )

        class Provider:
            def __init__(provider_self, outer):
                provider_self.outer = outer

            def generate(provider_self, request):
                return provider_self.outer._provider_call(request)

        return self._generate_hypothesis_batch_with_retry(
            run_id=state.run_id,
            prepared=prepared,
            provider=Provider(self),
        )

    def generate_expressions(self, hypotheses, review):
        sanitized = sanitize_evolution_gap_brief(self.gap_brief)
        feedback = sanitized.get("memory_feedback")
        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=self.registry,
            memory_feedback=(feedback if isinstance(feedback, Mapping) else None),
        )
        response = self._provider_call(prepared)
        if response.content_json is None:
            raise ValueError("表达式模型没有返回 JSON object")
        return parse_lightweight_expression_response(
            prepared=prepared,
            response=response.content_json,
            hypotheses=hypotheses,
            review=review,
            registry=self.registry,
            created_at=datetime.now(timezone.utc),
        )

    def freeze_manifest(self, source_context, expressions):
        return build_lightweight_batch_manifest(
            config=LightweightResearchConfig(
                hypothesis_count=expressions.family_size // 3,
                candidates_per_hypothesis=3,
                allow_external_llm=True,
            ),
            candidate_bindings=candidate_bindings_from_expression_batch(expressions),
            evaluation_policy_hash=sha256_json(
                self.evaluation_policy.model_dump(mode="json")
            ),
            memory_snapshot_hash=str(source_context["memory_snapshot_hash"]),
            coverage_graph_hash=str(source_context["coverage_graph_hash"]),
        )

    def evaluate(self, manifest):
        state = self.control.active_state()
        if state is None:
            raise ValueError("评价阶段缺少活动自主研究状态")
        expression_path = self.control.runs_root / state.run_id / "objects" / "expressions.json"
        expressions = LightweightExpressionBatch.model_validate_json(expression_path.read_bytes())
        result_by_slot = {item.slot_id: item for item in expressions.slot_results}
        evaluations: list[CampaignSlotEvaluation] = []
        for logical_slot in sorted({item.slot_id.split(":", 1)[0] for item in manifest.candidate_bindings}):
            group = [item for item in manifest.candidate_bindings if item.slot_id.startswith(logical_slot + ":")]
            ready = [item for item in group if item.terminal_status == "ready"]
            if ready:
                def evaluate_ready(
                    subset: Sequence[LightweightCandidateBinding],
                ) -> list[CampaignSlotEvaluation]:
                    padded = list(subset) + [subset[-1]] * (3 - len(subset))
                    candidates = PilotFixedCandidateFile(
                        version="pilot-fixed-candidates-v1",
                        candidates=tuple(
                            PilotFixedCandidate(
                                candidate_id=f"pilot_fixed_{index:03d}",
                                spec=result_by_slot[binding.slot_id].candidate.spec,
                            )
                            for index, binding in enumerate(padded, start=1)
                        ),
                    )
                    request = self.config.pilot_request.model_copy(
                        update={
                            "candidate_file_sha256": sha256_json(
                                candidates.model_dump(mode="json")
                            ),
                            "visible_start": LongOnlyResearchProtocol().discovery_start,
                            "visible_end": LongOnlyResearchProtocol().confirmation_end,
                        }
                    )
                    pilot = _run_pilot_with_frozen_family(
                        run_fixed_pilot,
                        frozen_family_size=manifest.family_size,
                        candidates=candidates,
                        paths=self.config.pilot_paths,
                        request=request,
                        evaluation_policy=self.evaluation_policy,
                        barra_policy=self.barra_policy,
                    )
                    snapshot = project_run_artifacts(
                        self.config.artifact_root,
                        pilot.run_id,
                        InMemoryDashboardStore(),
                    ).model_dump(mode="json")

                    group_results: list[CampaignSlotEvaluation] = []
                    for index, binding in enumerate(subset, start=1):
                        pilot_id = f"pilot_fixed_{index:03d}"
                        candidate = result_by_slot[binding.slot_id].candidate
                        direction, direction_record_sha256 = _load_frozen_direction_decision(
                            artifact_root=self.config.pilot_request.artifact_root,
                            run_id=pilot.run_id,
                            candidate_id=pilot_id,
                            candidate_spec_hash=binding.candidate_spec_hash,
                            hypothesis_direction=candidate.spec.hypothesis.expected_sign.value,
                        )
                        group_results.append(
                            CampaignSlotEvaluation(
                                slot_id=binding.slot_id,
                                slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                                status="evaluated",
                                candidate_id=binding.source_candidate_id,
                                candidate_spec_hash=binding.candidate_spec_hash,
                                ast_hash=(
                                    canonical_ast_hash(candidate.spec.expression)
                                    if candidate is not None
                                    else None
                                ),
                                output={
                                    "candidate_spec": candidate.spec.model_dump(mode="json"),
                                    "direction_decision": direction.model_dump(mode="json"),
                                    "direction_record_sha256": direction_record_sha256,
                                    **_candidate_dashboard_dimensions(snapshot, pilot_id),
                                    "source_pilot_run_id": pilot.run_id,
                                },
                            )
                        )
                    return group_results

                evaluations.extend(
                    _evaluate_ready_with_isolation(tuple(ready), evaluate_ready)
                )
            for binding in group:
                if binding.terminal_status == "failed":
                    evaluations.append(
                        CampaignSlotEvaluation(
                            slot_id=binding.slot_id,
                            slot_state=CandidateSlotState.GENERATION_FAILED,
                            status="failed",
                            failure_reason=binding.failure_reason,
                        )
                    )
        ordered = {item.slot_id: item for item in evaluations}
        return LightweightCampaignEvaluationResult.build(
            manifest=manifest,
            slot_evaluations=tuple(ordered[item.slot_id] for item in manifest.candidate_bindings),
        )

    def publish(self, manifest, evaluation):
        run_id = f"run_{sha256_json({'manifest': manifest.manifest_sha256, 'evaluation': evaluation.result_sha256})[:24]}"
        candidate_metrics = {}
        ic = {}
        recent_ic = {}
        portfolio = {}
        governance = {}
        recent_portfolio = {}
        daily = {}
        barra = {}
        directions = _aggregate_direction_decisions(evaluation)
        artifacts = {
            "run/metrics.json": b"{}",
        }
        for item in evaluation.slot_evaluations:
            if item.status != "evaluated" or item.candidate_id is None:
                continue
            candidate_id = item.candidate_id
            candidate_metrics[candidate_id] = item.output.get("candidate_metrics", {})
            ic[candidate_id] = item.output.get("ic_diagnostics", {})
            recent_ic[candidate_id] = item.output.get("recent_ic_diagnostics", {})
            portfolio[candidate_id] = item.output.get("portfolio_metrics", {})
            governance[candidate_id] = item.output.get("portfolio_governance", {})
            recent_portfolio[candidate_id] = item.output.get(
                "recent_portfolio_metrics", {}
            )
            daily[candidate_id] = item.output.get("portfolio_daily", {})
            barra[candidate_id] = item.output.get("barra_attribution", {})
            artifacts[f"candidates/{candidate_id}/spec.json"] = canonical_json_bytes(
                item.output["candidate_spec"]
            )
        artifacts.update(
            {
                "run/metrics.json": canonical_json_bytes(
                    {
                        "candidates": candidate_metrics,
                        "campaign_metadata": {
                            "kind": "lightweight_autonomous",
                            "manifest_sha256": manifest.manifest_sha256,
                            "bonferroni_denominator": manifest.family_size,
                        },
                    }
                ),
                "ic/diagnostics.json": canonical_json_bytes({"candidates": ic}),
                "ic/recent.json": canonical_json_bytes({"candidates": recent_ic}),
                "portfolio/metrics.json": canonical_json_bytes({"candidates": portfolio}),
                "portfolio/governance.json": canonical_json_bytes(
                    {"candidates": governance}
                ),
                "portfolio/recent_metrics.json": canonical_json_bytes(
                    {"candidates": recent_portfolio}
                ),
                "portfolio/daily.json": canonical_json_bytes({"candidates": daily}),
                "barra/attribution.json": canonical_json_bytes({"candidates": barra}),
                "direction/decisions.json": canonical_json_bytes(directions),
                "run/lightweight_evaluation.json": canonical_json_bytes(evaluation.model_dump(mode="json")),
            }
        )
        published = publish_run_artifacts(self.config.artifact_root, run_id, artifacts)
        snapshot = project_run_artifacts(
            self.config.artifact_root,
            run_id,
            InMemoryDashboardStore(),
        )
        state = self.control.active_state()
        assert state is not None
        objects = self.control.runs_root / state.run_id / "objects"
        hypotheses = LightweightHypothesisBatch.model_validate_json((objects / "hypotheses.json").read_bytes())
        review = LightweightReviewBatch.model_validate_json((objects / "review.json").read_bytes())
        expressions = LightweightExpressionBatch.model_validate_json((objects / "expressions.json").read_bytes())
        return {
            "published_run_id": run_id,
            "artifact_manifest_sha256": published.manifest_sha256,
            "snapshot": snapshot.model_dump(mode="json"),
            "hypotheses": hypotheses.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "expressions": expressions.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }

    def project_control_state(self, state):
        objects = self.control.runs_root / state.run_id / "objects"
        hypotheses = (
            LightweightHypothesisBatch.model_validate_json((objects / "hypotheses.json").read_bytes())
            if (objects / "hypotheses.json").is_file()
            else None
        )
        review = (
            LightweightReviewBatch.model_validate_json((objects / "review.json").read_bytes())
            if (objects / "review.json").is_file()
            else None
        )
        self.pg.project_control_state(state, hypotheses=hypotheses, review=review)

    def project(self, publication):
        snapshot = publication.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("自主研究发布缺少 Dashboard snapshot")
        snapshot = dict(snapshot)
        direction_payload = snapshot.get("direction_decisions")
        if not isinstance(direction_payload, dict) or not isinstance(
            direction_payload.get("candidates"), dict
        ):
            evaluation = LightweightCampaignEvaluationResult.model_validate(
                publication.get("evaluation")
            )
            snapshot["direction_decisions"] = _aggregate_direction_decisions(
                evaluation
            )
        run_id = str(publication["published_run_id"])
        self.pg.replace_run_snapshot(run_id, snapshot)
        definitions = snapshot.get("candidate_definitions")
        if not isinstance(definitions, dict):
            raise ValueError("自主研究发布缺少候选定义，无法记录实际成功因子数")
        state = self.control.active_state()
        if state is None:
            raise ValueError("数据库投影阶段缺少活动自主研究状态")
        self.pg.update_research_run_factor_count(state.run_id, len(definitions))
        return {"projected_run_id": run_id, "snapshot_sha256": snapshot["snapshot_sha256"]}

    def refresh_evolution(self, publication, source_context):
        bundle = PublishedLightweightRun(
            run_id=str(source_context.get("run_id") or self.control.active_state().run_id),
            hypotheses=publication["hypotheses"],
            review=publication["review"],
            expressions=publication["expressions"],
            manifest=publication["manifest"],
            evaluation=publication["evaluation"],
            intraday_field_ids=tuple(
                item.field_id
                for item in self.registry.fields
                if item.economic_type == "intraday_aggregate"
            ),
        )
        context = LightweightEvolutionContext(
            memory_snapshot_id=str(source_context["memory_snapshot_id"]),
            memory_snapshot_hash=str(source_context["memory_snapshot_hash"]),
            coverage_graph_id=str(source_context["coverage_graph_id"]),
            coverage_graph_hash=str(source_context["coverage_graph_hash"]),
        )
        refresh = refresh_lightweight_evolution(
            bundle,
            context,
            self.evolution,
        )
        entries = self.evolution.memory.load_snapshot_entries(
            refresh.next_memory_snapshot_id
        )
        gap_brief = dict(self.gap_brief)
        feedback = build_lightweight_memory_feedback(entries)
        shadow = self._run_paired_shadow(
            bundle,
            source_published_run_id=str(publication["published_run_id"]),
        )
        if self.config.paired_shadow.enabled:
            guidance = set(feedback["generation_guidance"])
            guidance.update(self._recent_shadow_preferences())
            feedback["generation_guidance"] = tuple(sorted(guidance))
        gap_brief["memory_feedback"] = feedback
        state_sha256 = self._activate_evolution(
            memory_snapshot_id=refresh.next_memory_snapshot_id,
            memory_snapshot_hash=refresh.next_memory_snapshot_hash,
            coverage_graph_id=refresh.next_coverage_graph_id,
            coverage_graph_hash=refresh.next_coverage_graph_hash,
            gap_brief=gap_brief,
        )
        result = refresh.model_dump(mode="json")
        result["current_evolution_state_sha256"] = state_sha256
        result["paired_shadow_status"] = shadow.status
        result["paired_shadow_summary_sha256"] = shadow.summary_sha256
        return result

    def _run_paired_shadow(
        self,
        bundle: PublishedLightweightRun,
        *,
        source_published_run_id: str,
    ) -> PairedShadowSummary:
        """在主发布之后运行可失败、但不反转主结论的独立影子层。"""

        base = (
            self.config.artifact_root
            / "state"
            / "autonomous_research"
            / "runs"
            / bundle.run_id
            / "paired_shadow"
        )
        summary_path = base / "summary.json"
        if summary_path.is_file():
            return PairedShadowSummary.model_validate_json(summary_path.read_bytes())
        plan_path = base / "plan.json"
        if plan_path.is_file():
            plan = PairedShadowPlan.model_validate_json(plan_path.read_bytes())
        else:
            candidates = tuple(
                (item.slot_id, item.candidate)
                for item in bundle.expressions.slot_results
                if item.candidate is not None
            )
            plan = build_paired_shadow_plan(
                autonomous_run_id=bundle.run_id,
                source_published_run_id=source_published_run_id,
                candidates=candidates,
                registry=self.registry,
                policy=self.config.paired_shadow,
                created_at=datetime.now(timezone.utc),
            )
            publish_paired_shadow_plan(self.config.artifact_root, plan)
        if not plan.policy.enabled or not plan.probes:
            summary = build_paired_shadow_summary(
                plan,
                (),
                status="not_executed",
            )
        else:
            try:
                results = evaluate_paired_shadow_plan(
                    plan,
                    paths=self.config.pilot_paths,
                    request=self.config.pilot_request,
                    evaluation_policy=self.evaluation_policy,
                )
                summary = build_paired_shadow_summary(plan, results)
            except Exception as error:
                summary = build_paired_shadow_summary(
                    plan,
                    (),
                    status="failed",
                    error_cn=f"影子评价失败：{error}",
                )
        publish_paired_shadow_summary(self.config.artifact_root, summary)
        return summary

    def run_paired_shadow_for_run(self, autonomous_run_id: str) -> PairedShadowSummary:
        """为已经发布的一个自主批次补跑独立影子层，不刷新主记忆指针。"""

        objects = self.control.runs_root / autonomous_run_id / "objects"
        publication = json.loads((objects / "publication.json").read_text("utf-8"))
        if not isinstance(publication, dict):
            raise ValueError("影子补跑缺少主发布对象")
        bundle = PublishedLightweightRun(
            run_id=autonomous_run_id,
            hypotheses=LightweightHypothesisBatch.model_validate_json(
                (objects / "hypotheses.json").read_bytes()
            ),
            review=LightweightReviewBatch.model_validate_json(
                (objects / "review.json").read_bytes()
            ),
            expressions=LightweightExpressionBatch.model_validate_json(
                (objects / "expressions.json").read_bytes()
            ),
            manifest=LightweightBatchManifest.model_validate_json(
                (objects / "manifest.json").read_bytes()
            ),
            evaluation=LightweightCampaignEvaluationResult.model_validate_json(
                (objects / "evaluation.json").read_bytes()
            ),
            intraday_field_ids=tuple(
                item.field_id
                for item in self.registry.fields
                if item.economic_type == "intraday_aggregate"
            ),
        )
        summary = self._run_paired_shadow(
            bundle,
            source_published_run_id=str(publication["published_run_id"]),
        )
        if self.config.paired_shadow.enabled and summary.status == "completed":
            self._activate_recent_shadow_memory()
        return summary

    def _activate_recent_shadow_memory(self) -> str:
        """把最近影子偏好并入当前脱敏反馈，使下一批启动时立即可用。"""

        gap_brief = dict(self.gap_brief)
        raw_feedback = gap_brief.get("memory_feedback")
        if not isinstance(raw_feedback, Mapping):
            raise ValueError("当前进化状态缺少可追加影子偏好的记忆反馈")
        feedback = dict(raw_feedback)
        raw_guidance = feedback.get("generation_guidance")
        if not isinstance(raw_guidance, (list, tuple)):
            raise ValueError("当前记忆反馈缺少生成指引")
        guidance = {str(item) for item in raw_guidance}
        guidance.update(self._recent_shadow_preferences())
        feedback["generation_guidance"] = tuple(sorted(guidance))
        gap_brief["memory_feedback"] = feedback
        return self._activate_evolution(
            memory_snapshot_id=self.memory_snapshot_id,
            memory_snapshot_hash=self.context.memory_snapshot_hash,
            coverage_graph_id=self.coverage_graph_id,
            coverage_graph_hash=self.context.coverage_graph_manifest_hash,
            gap_brief=gap_brief,
        )

    def _recent_shadow_preferences(self) -> tuple[str, ...]:
        """只保留最近若干批已经通过门控的离散局部偏好。"""

        paths = sorted(
            self.control.runs_root.glob("autrun_*/paired_shadow/summary.json"),
            key=lambda item: item.stat().st_mtime,
        )[-self.config.paired_shadow.memory_retention_batches :]
        values: set[str] = set()
        for path in paths:
            try:
                summary = PairedShadowSummary.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if summary.status == "completed":
                values.update(summary.accepted_preference_codes)
        return tuple(sorted(values))

    def refresh_rejected_hypotheses(self, hypotheses, review, source_context):
        entries = tuple(
            LightweightMemoryEntry.build(
                run_id=hypotheses.run_id,
                entry_kind="hypothesis",
                logical_slot_id=decision.logical_slot_id,
                candidate_slot_id=None,
                terminal_status="rejected",
                decision="rejected",
                draft_sha256=decision.draft_sha256,
                candidate_id=None,
                candidate_spec_hash=None,
                ast_hash=None,
                data_source_tags=(),
                failure_reason=None,
                evaluation_summary={},
            )
            for decision in review.decisions
        )
        context = LightweightEvolutionContext(
            memory_snapshot_id=str(source_context["memory_snapshot_id"]),
            memory_snapshot_hash=str(source_context["memory_snapshot_hash"]),
            coverage_graph_id=str(source_context["coverage_graph_id"]),
            coverage_graph_hash=str(source_context["coverage_graph_hash"]),
        )
        memory = self.evolution.publish_memory(entries, context, hypotheses.run_id)
        return memory.model_dump(mode="json")
