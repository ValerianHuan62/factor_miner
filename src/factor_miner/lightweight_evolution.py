"""轻量研究终态到下一轮记忆与因子图谱的适配层。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.coverage_schema import CoverageFactorNode
from factor_miner.dsl import canonical_ast, canonical_ast_hash, iter_ast_nodes
from factor_miner.ledger import HashChainJsonlStore, atomic_write_immutable
from factor_miner.lightweight_expressions import LightweightExpressionBatch
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightReviewBatch,
)
from factor_miner.lightweight_schema import LightweightBatchManifest
from factor_miner.long_only_protocol import DirectionDecision
from factor_miner.research_campaign_runner import LightweightCampaignEvaluationResult
from factor_miner.semantic_coverage import (
    SemanticPlanTags,
    build_semantic_coverage,
    build_semantic_quota,
)


_HASH = r"^[0-9a-f]{64}$"
_CJK = re.compile(r"[\u3400-\u9fff]")


class LightweightEvolutionContext(BaseModel):
    """当前批次冻结的旧记忆与旧图谱身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_snapshot_id: str = Field(min_length=1)
    memory_snapshot_hash: str = Field(pattern=_HASH)
    coverage_graph_id: str = Field(min_length=1)
    coverage_graph_hash: str = Field(pattern=_HASH)


class PublishedLightweightRun(BaseModel):
    """刷新层消费的强类型轻量发布包。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    hypotheses: LightweightHypothesisBatch
    review: LightweightReviewBatch
    expressions: LightweightExpressionBatch
    manifest: LightweightBatchManifest
    evaluation: LightweightCampaignEvaluationResult
    intraday_field_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_identities(self) -> PublishedLightweightRun:
        """发布包的审批、表达式、清单与评价必须属于同一批。"""

        if self.hypotheses.run_id != self.review.run_id:
            raise ValueError("轻量发布包的运行身份不一致")
        if self.review.review_sha256 != self.expressions.review_sha256:
            raise ValueError("轻量发布包的表达式没有绑定冻结审批")
        if self.manifest.manifest_sha256 != self.evaluation.manifest_sha256:
            raise ValueError("轻量发布包的评价没有绑定冻结清单")
        if self.manifest.family_size != self.expressions.family_size:
            raise ValueError("轻量发布包的候选族规模不一致")
        return self


class LightweightMemoryEntry(BaseModel):
    """批准、拒绝和全部候选终态共用的轻量记忆条目。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(pattern=r"^lwmem_[0-9a-f]{24}$")
    entry_sha256: str = Field(pattern=_HASH)
    run_id: str
    entry_kind: Literal["hypothesis", "candidate"]
    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    candidate_slot_id: str | None = Field(
        default=None, pattern=r"^H(0[1-9]|10):C00[1-3]$"
    )
    terminal_status: str
    decision: Literal["approved", "rejected"] | None = None
    draft_sha256: str = Field(pattern=_HASH)
    candidate_id: str | None = None
    candidate_spec_hash: str | None = Field(default=None, pattern=_HASH)
    ast_hash: str | None = Field(default=None, pattern=_HASH)
    data_source_tags: tuple[str, ...] = ()
    hypothesis_direction: Literal["positive", "negative"] | None = None
    selected_direction: Literal["positive", "negative"] | None = None
    direction_relation: Literal["supported", "reversed", "unresolved"] | None = None
    direction_source: Literal[
        "discovery_window_frozen", "discovery_window_unresolved"
    ] | None = None
    direction_record_sha256: str | None = Field(default=None, pattern=_HASH)
    failure_reason: str | None = None
    evaluation_summary: dict[str, str] = Field(default_factory=dict)
    semantic_plan: SemanticPlanTags | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> LightweightMemoryEntry:
        """两类条目不能互相伪造字段，内容身份必须稳定。"""

        if self.entry_kind == "hypothesis":
            if self.candidate_slot_id is not None or self.decision is None:
                raise ValueError("假设记忆必须且只能记录审批终态")
        elif self.candidate_slot_id is None or self.decision is not None:
            raise ValueError("候选记忆必须记录候选槽终态")
        direction_values = (
            self.hypothesis_direction,
            self.selected_direction,
            self.direction_relation,
            self.direction_source,
            self.direction_record_sha256,
        )
        if any(value is not None for value in direction_values):
            if self.direction_relation == "unresolved":
                if (
                    self.hypothesis_direction is None
                    or self.selected_direction is not None
                    or self.direction_source != "discovery_window_unresolved"
                    or self.direction_record_sha256 is not None
                ):
                    raise ValueError("未决方向记忆必须保留事前方向且不得伪造冻结记录")
                payload = self.model_dump(mode="json", exclude={"entry_id", "entry_sha256"})
                if self.semantic_plan is None:
                    payload.pop("semantic_plan", None)
                digest = sha256_json(payload)
                if self.entry_sha256 != digest or self.entry_id != f"lwmem_{digest[:24]}":
                    raise ValueError("轻量记忆条目内容身份不一致")
                return self
            if any(value is None for value in direction_values):
                raise ValueError("方向记忆必须完整绑定冻结发现记录")
            if self.direction_source != "discovery_window_frozen":
                raise ValueError("已冻结方向必须标记 discovery_window_frozen")
            expected_relation = (
                "supported"
                if self.hypothesis_direction == self.selected_direction
                else "reversed"
            )
            if self.direction_relation != expected_relation:
                raise ValueError("方向关系与事前方向及冻结发现方向不一致")
        payload = self.model_dump(mode="json", exclude={"entry_id", "entry_sha256"})
        if self.semantic_plan is None:
            payload.pop("semantic_plan", None)
        digest = sha256_json(payload)
        if self.entry_sha256 != digest or self.entry_id != f"lwmem_{digest[:24]}":
            raise ValueError("轻量记忆条目内容身份不一致")
        return self

    @classmethod
    def build(cls, **payload: object) -> LightweightMemoryEntry:
        """构造内容寻址条目。"""

        for field in (
            "hypothesis_direction",
            "selected_direction",
            "direction_relation",
            "direction_source",
            "direction_record_sha256",
        ):
            payload.setdefault(field, None)
        if payload.get("semantic_plan") is None:
            payload.pop("semantic_plan", None)
        elif isinstance(payload.get("semantic_plan"), SemanticPlanTags):
            payload["semantic_plan"] = payload["semantic_plan"].model_dump(mode="json")
        digest = sha256_json(payload)
        return cls(entry_id=f"lwmem_{digest[:24]}", entry_sha256=digest, **payload)


class LightweightMemoryEvent(BaseModel):
    """轻量记忆追加事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    sequence: int = 0
    entry_id: str
    entry_sha256: str = Field(pattern=_HASH)
    written_at: datetime
    previous_event_hash: str | None = Field(default=None, pattern=_HASH)
    supersedes_event_id: str | None = None
    event_hash: str | None = Field(default=None, pattern=_HASH)


class LightweightMemorySnapshot(BaseModel):
    """供下一批读取的独立轻量记忆快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(pattern=r"^lwmemsnap_[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=_HASH)
    source_memory_snapshot_id: str
    source_memory_snapshot_hash: str = Field(pattern=_HASH)
    run_id: str
    entry_ids: tuple[str, ...]
    hypothesis_entry_count: int = Field(ge=0)
    candidate_entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> LightweightMemorySnapshot:
        """快照计数和内容身份必须相互一致。"""

        if len(self.entry_ids) != self.hypothesis_entry_count + self.candidate_entry_count:
            raise ValueError("轻量记忆快照计数不一致")
        payload = self.model_dump(mode="json", exclude={"snapshot_id", "snapshot_sha256"})
        digest = sha256_json(payload)
        if self.snapshot_sha256 != digest or self.snapshot_id != f"lwmemsnap_{digest[:24]}":
            raise ValueError("轻量记忆快照内容身份不一致")
        return self


class LightweightMemoryStore:
    """只写入 research_memory/lightweight 的单写入者 Store。"""

    def __init__(self, artifact_root: Path) -> None:
        self.root = artifact_root.expanduser().resolve(strict=False) / "research_memory" / "lightweight"
        self.objects_root = self.root / "entries"
        self.snapshots_root = self.root / "snapshots"
        self._events = HashChainJsonlStore(
            self.root / "events.jsonl",
            self.root / "events.jsonl.lock",
            LightweightMemoryEvent,
        )

    def publish(
        self,
        entries: tuple[LightweightMemoryEntry, ...],
        *,
        source_context: LightweightEvolutionContext,
        run_id: str,
        written_at: datetime,
    ) -> LightweightMemorySnapshot:
        """幂等追加完整终态并发布内容寻址快照。"""

        if written_at.tzinfo is None or written_at.utcoffset() is None:
            raise ValueError("written_at 必须带时区")
        for entry in entries:
            path = self.objects_root / f"{entry.entry_id}.json"
            atomic_write_immutable(path, canonical_json_bytes(entry.model_dump(mode="json")))
            known = {item.entry_id: item for item in self._events.verify()}
            existing = known.get(entry.entry_id)
            if existing is not None:
                if existing.entry_sha256 != entry.entry_sha256:
                    raise ValueError("同一轻量记忆条目不可覆盖")
                continue
            self._events.append(
                LightweightMemoryEvent(
                    event_id=f"lwmemevt_{entry.entry_sha256[:24]}",
                    entry_id=entry.entry_id,
                    entry_sha256=entry.entry_sha256,
                    written_at=written_at.astimezone(timezone.utc),
                )
            )
        all_entries = tuple(
            LightweightMemoryEntry.model_validate_json(
                (self.objects_root / f"{event.entry_id}.json").read_bytes()
            )
            for event in self._events.verify()
        )
        payload = {
            "source_memory_snapshot_id": source_context.memory_snapshot_id,
            "source_memory_snapshot_hash": source_context.memory_snapshot_hash,
            "run_id": run_id,
            "entry_ids": tuple(item.entry_id for item in all_entries),
            "hypothesis_entry_count": sum(
                item.entry_kind == "hypothesis" for item in all_entries
            ),
            "candidate_entry_count": sum(
                item.entry_kind == "candidate" for item in all_entries
            ),
        }
        digest = sha256_json(payload)
        snapshot = LightweightMemorySnapshot(
            snapshot_id=f"lwmemsnap_{digest[:24]}",
            snapshot_sha256=digest,
            **payload,
        )
        atomic_write_immutable(
            self.snapshots_root / f"{snapshot.snapshot_id}.json",
            canonical_json_bytes(snapshot.model_dump(mode="json")),
        )
        return snapshot

    def load_snapshot_entries(
        self,
        snapshot_id: str,
    ) -> tuple[LightweightMemoryEntry, ...]:
        """按快照顺序读取累计记忆条目。"""

        snapshot = LightweightMemorySnapshot.model_validate_json(
            (self.snapshots_root / f"{snapshot_id}.json").read_bytes()
        )
        return tuple(
            LightweightMemoryEntry.model_validate_json(
                (self.objects_root / f"{entry_id}.json").read_bytes()
            )
            for entry_id in snapshot.entry_ids
        )


class LightweightCoverageCatalogEntry(BaseModel):
    """一个具有真实 typed AST 的轻量图谱节点输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: CoverageFactorNode
    canonical_ast: dict[str, object]
    data_source_tags: tuple[str, ...] = ()
    daily_ic_reference: dict[str, object] | None = None


class LightweightCoverageCatalog(BaseModel):
    """显式传给图谱 builder 的轻量目录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_coverage_graph_id: str
    source_coverage_graph_hash: str = Field(pattern=_HASH)
    entries: tuple[LightweightCoverageCatalogEntry, ...]
    catalog_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_identity(self) -> LightweightCoverageCatalog:
        if self.catalog_sha256 != sha256_json(
            self.model_dump(mode="json", exclude={"catalog_sha256"})
        ):
            raise ValueError("轻量覆盖目录内容身份不一致")
        return self


class PublishedLightweightCoverageGraph(BaseModel):
    """外部图谱 builder 返回的已核验身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_id: str
    graph_sha256: str = Field(pattern=_HASH)


class PublishedLightweightGapBrief(BaseModel):
    """下一批可用的中文缺口简报身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_id: str
    brief_sha256: str = Field(pattern=_HASH)
    description_cn: str

    @model_validator(mode="after")
    def validate_chinese(self) -> PublishedLightweightGapBrief:
        if not _CJK.search(self.description_cn):
            raise ValueError("轻量缺口简报必须包含中文说明")
        return self


class EvolutionRefreshResult(BaseModel):
    """记忆、图谱和中文简报三项齐全后的刷新结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_memory_snapshot_id: str
    source_memory_snapshot_hash: str = Field(pattern=_HASH)
    next_memory_snapshot_id: str
    next_memory_snapshot_hash: str = Field(pattern=_HASH)
    source_coverage_graph_id: str
    source_coverage_graph_hash: str = Field(pattern=_HASH)
    next_coverage_graph_id: str
    next_coverage_graph_hash: str = Field(pattern=_HASH)
    gap_brief_id: str
    gap_brief_hash: str = Field(pattern=_HASH)
    hypothesis_entry_count: int = Field(ge=0)
    candidate_entry_count: int = Field(ge=0)
    result_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_identity(self) -> EvolutionRefreshResult:
        if self.result_sha256 != sha256_json(
            self.model_dump(mode="json", exclude={"result_sha256"})
        ):
            raise ValueError("轻量进化刷新结果内容身份不一致")
        return self


class LightweightEvolutionDependencies(Protocol):
    """刷新层的三个幂等发布端口。"""

    def publish_memory(
        self,
        entries: tuple[LightweightMemoryEntry, ...],
        source_context: LightweightEvolutionContext,
        run_id: str,
    ) -> LightweightMemorySnapshot: ...

    def publish_coverage_graph(
        self, catalog: LightweightCoverageCatalog
    ) -> PublishedLightweightCoverageGraph: ...

    def publish_gap_brief(
        self,
        memory: LightweightMemorySnapshot,
        graph: PublishedLightweightCoverageGraph,
    ) -> PublishedLightweightGapBrief: ...


def _evaluation_summary(item: object) -> dict[str, str]:
    """从确认期统计生成离散反馈，不泄露精确回测数值。"""

    output = getattr(item, "output", {})
    if not isinstance(output, Mapping) or getattr(item, "status", None) != "evaluated":
        return {
            "outcome_band": "inconclusive",
            "ic_band": "unknown",
            "rank_ic_band": "unknown",
            "hac_significance_band": "unknown",
            "portfolio_band": "unknown",
            "redundancy_band": "unknown",
        }

    metrics = output.get("candidate_metrics")
    diagnostics = output.get("ic_diagnostics")
    portfolio = output.get("portfolio_metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    portfolio = portfolio if isinstance(portfolio, Mapping) else {}
    passed = metrics.get("confirmation_passed")
    ic_mean = diagnostics.get("ic_mean")
    ic_value = abs(float(ic_mean)) if isinstance(ic_mean, (int, float)) else None
    ic_band = (
        "high" if ic_value is not None and ic_value >= 0.02
        else "medium" if ic_value is not None and ic_value >= 0.01
        else "low" if ic_value is not None
        else "unknown"
    )
    rank_ic = diagnostics.get("rank_ic_mean")
    rank_value = abs(float(rank_ic)) if isinstance(rank_ic, (int, float)) else None
    rank_band = (
        "high" if rank_value is not None and rank_value >= 0.03
        else "medium" if rank_value is not None and rank_value >= 0.01
        else "low" if rank_value is not None
        else "unknown"
    )
    series = portfolio.get("series")
    series = series if isinstance(series, Mapping) else {}
    target = series.get("target_long_net_return")
    target = target if isinstance(target, Mapping) else {}
    information_ratio = target.get("information_ratio")
    portfolio_band = (
        "high" if isinstance(information_ratio, (int, float)) and information_ratio >= 0.5
        else "medium" if isinstance(information_ratio, (int, float)) and information_ratio > 0
        else "low" if isinstance(information_ratio, (int, float))
        else "unknown"
    )
    return {
        "outcome_band": "passed" if passed is True else "failed",
        "ic_band": ic_band,
        "rank_ic_band": rank_band,
        "hac_significance_band": "passed" if passed is True else "failed",
        "portfolio_band": portfolio_band,
        "redundancy_band": "unknown",
    }


def build_lightweight_memory_feedback(
    entries: tuple[LightweightMemoryEntry, ...],
) -> dict[str, object]:
    """把累计记忆压缩成可供下一轮模型使用的离散反馈。"""

    candidates = tuple(item for item in entries if item.entry_kind == "candidate")
    evaluated = tuple(item for item in candidates if item.terminal_status == "evaluated")
    failures: set[str] = set()
    for item in candidates:
        reason = item.failure_reason or ""
        if "过于简单" in reason or "节点" in reason:
            failures.add("invalid_complexity")
        if "单位" in reason:
            failures.add("unit_mismatch")
        if "窗口" in reason:
            failures.add("invalid_window")
        if "深度" in reason or "超过限制" in reason:
            failures.add("ast_limit")
        if "重复" in reason or "冗余" in reason:
            failures.add("duplicate_structure")
    summaries = tuple(item.evaluation_summary for item in evaluated)

    def average_band(field: str) -> str:
        values = {"low": 0, "medium": 1, "high": 2}
        scores = [values[str(item.get(field))] for item in summaries if item.get(field) in values]
        if not scores:
            return "unknown"
        average = sum(scores) / len(scores)
        return "high" if average >= 1.5 else "medium" if average >= 0.5 else "low"

    count = len(candidates)
    evaluated_share = len(evaluated) / count if count else 0.0
    reversal_share = (
        sum(item.direction_relation == "reversed" for item in evaluated) / len(evaluated)
        if evaluated
        else 0.0
    )
    guidance = {
        "avoid_previous_ast_duplicates",
        "prioritize_positive_excess_information_ratio",
    }
    if "invalid_complexity" in failures:
        guidance.add("respect_minimum_structural_complexity")
    if "unit_mismatch" in failures:
        guidance.add("respect_unit_compatibility")
    if {"ast_limit", "invalid_window"}.intersection(failures):
        guidance.add("respect_ast_and_window_limits")
    semantic_coverage = build_semantic_coverage(
        tuple(
            item.semantic_plan
            for item in entries
            if item.entry_kind == "hypothesis"
        )
    )
    semantic_quota = build_semantic_quota(semantic_coverage)
    return {
        "candidate_history_band": "high" if count >= 90 else "medium" if count >= 30 else "low",
        "evaluated_share_band": (
            "high" if evaluated_share >= 0.75 else "medium" if evaluated_share >= 0.4 else "low"
        ),
        "ic_quality_band": average_band("ic_band"),
        "rank_ic_quality_band": average_band("rank_ic_band"),
        "portfolio_quality_band": average_band("portfolio_band"),
        "direction_reversal_band": (
            "high" if reversal_share >= 0.67 else "medium" if reversal_share >= 0.34 else "low"
        ),
        "dominant_failure_patterns": tuple(sorted(failures or {"none"})),
        "generation_guidance": tuple(sorted(guidance)),
        "semantic_coverage": semantic_coverage.model_dump(mode="json"),
        "semantic_quota": semantic_quota.model_dump(mode="json"),
    }


def _frozen_direction_memory(item: object) -> dict[str, str | None]:
    """从 Task 3 的冻结方向决定提取可公开的离散记忆。"""

    output = getattr(item, "output", {})
    if not isinstance(output, Mapping):
        return {}
    raw_decision = output.get("direction_decision")
    raw_sha256 = output.get("direction_record_sha256")
    if raw_decision is None and raw_sha256 is None:
        return {}
    if not isinstance(raw_decision, Mapping) or not isinstance(raw_sha256, str):
        raise ValueError("方向记忆必须完整绑定冻结发现记录")
    decision = DirectionDecision.model_validate(raw_decision)
    return {
        "hypothesis_direction": decision.hypothesis_direction,
        "selected_direction": decision.selected_direction,
        "direction_relation": decision.hypothesis_relation,
        "direction_source": "discovery_window_frozen",
        "direction_record_sha256": raw_sha256,
    }


def _direction_failure_reason(direction: Mapping[str, str | None]) -> str | None:
    """保留方向发现对事前假设的中文否证说明。"""

    if direction.get("direction_relation") == "unresolved":
        return "方向发现未决，确认结果另行记录"
    if direction.get("direction_relation") != "reversed":
        return None
    labels = {"positive": "正向", "negative": "负向"}
    return (
        f"事前{labels[str(direction['hypothesis_direction'])]}假设在方向发现区间被反转，确认结果另行记录"
    )


def _unresolved_direction_memory(hypothesis_direction: str) -> dict[str, str | None]:
    """在方向发现未完成或未决时保留事前方向，而不是静默留空。"""

    if hypothesis_direction not in {"positive", "negative"}:
        raise ValueError("未决方向记忆缺少合法事前方向")
    return {
        "hypothesis_direction": hypothesis_direction,
        "selected_direction": None,
        "direction_relation": "unresolved",
        "direction_source": "discovery_window_unresolved",
        "direction_record_sha256": None,
    }


def _combined_failure_reason(
    failure_reason: str | None,
    direction: Mapping[str, str | None],
) -> str | None:
    """保留原失败原因，并追加离散方向状态的中文说明。"""

    direction_reason = _direction_failure_reason(direction)
    if failure_reason and direction_reason:
        return f"{failure_reason}；{direction_reason}"
    return failure_reason or direction_reason


def build_lightweight_memory_entries(
    publication: PublishedLightweightRun,
) -> tuple[LightweightMemoryEntry, ...]:
    """把十条审批和完整候选族转换为下一轮离散记忆。"""

    decision_by_slot = {item.logical_slot_id: item for item in publication.review.decisions}
    draft_by_slot = {item.logical_slot_id: item for item in publication.hypotheses.hypotheses}
    entries: list[LightweightMemoryEntry] = []
    for slot_id in tuple(f"H{i:02d}" for i in range(1, 11)):
        decision = decision_by_slot[slot_id]
        entries.append(
            LightweightMemoryEntry.build(
                run_id=publication.run_id,
                entry_kind="hypothesis",
                logical_slot_id=slot_id,
                candidate_slot_id=None,
                terminal_status=decision.decision,
                decision=decision.decision,
                draft_sha256=decision.draft_sha256,
                candidate_id=None,
                candidate_spec_hash=None,
                ast_hash=None,
                data_source_tags=(),
                hypothesis_direction=None,
                selected_direction=None,
                direction_relation=None,
                direction_source=None,
                direction_record_sha256=None,
                failure_reason=None,
                evaluation_summary={},
                semantic_plan=draft_by_slot[slot_id].semantic_plan,
            )
        )
    result_by_slot = {item.slot_id: item for item in publication.expressions.slot_results}
    evaluation_by_slot = {item.slot_id: item for item in publication.evaluation.slot_evaluations}
    intraday = set(publication.intraday_field_ids)
    for binding in publication.manifest.candidate_bindings:
        expression_result = result_by_slot[binding.slot_id]
        evaluation = evaluation_by_slot[binding.slot_id]
        candidate = expression_result.candidate
        tags = ()
        ast_hash = None
        if candidate is not None:
            ast_hash = canonical_ast_hash(candidate.spec.expression)
            if intraday.intersection(candidate.spec.required_fields):
                tags = ("intraday_aggregate",)
        direction = _frozen_direction_memory(evaluation)
        if not direction:
            direction = _unresolved_direction_memory(
                candidate.spec.hypothesis.expected_sign.value
                if candidate is not None
                else (
                    "negative"
                    if draft_by_slot[binding.slot_id.split(":", 1)[0]].expected_direction.startswith("负向")
                    else "positive"
                )
            )
        entries.append(
            LightweightMemoryEntry.build(
                run_id=publication.run_id,
                entry_kind="candidate",
                logical_slot_id=binding.slot_id.split(":", 1)[0],
                candidate_slot_id=binding.slot_id,
                terminal_status=evaluation.status,
                decision=None,
                draft_sha256=draft_by_slot[binding.slot_id.split(":", 1)[0]].draft_sha256,
                candidate_id=binding.source_candidate_id,
                candidate_spec_hash=binding.candidate_spec_hash,
                ast_hash=ast_hash,
                data_source_tags=tags,
                **direction,
                failure_reason=_combined_failure_reason(
                    binding.failure_reason or evaluation.failure_reason,
                    direction,
                ),
                evaluation_summary=_evaluation_summary(evaluation),
            )
        )
    return tuple(entries)


def build_lightweight_coverage_catalog(
    publication: PublishedLightweightRun,
    source_context: LightweightEvolutionContext,
) -> LightweightCoverageCatalog:
    """仅用真实 typed AST 构造显式图谱输入，不扫描目录。"""

    evaluation_by_slot = {item.slot_id: item for item in publication.evaluation.slot_evaluations}
    intraday = set(publication.intraday_field_ids)
    entries = []
    for result in publication.expressions.slot_results:
        candidate = result.candidate
        if candidate is None:
            continue
        evaluation = evaluation_by_slot[result.slot_id]
        direction = _frozen_direction_memory(evaluation)
        if not direction:
            continue
        expression = candidate.spec.expression
        nodes = iter_ast_nodes(expression)
        operators = tuple(sorted({node.op for node in nodes if node.op not in {"field", "const"}}))
        windows = tuple(sorted({int(value) for node in nodes for value in (node.window, node.period) if value}))
        fields = tuple(sorted(candidate.spec.required_fields))
        encoded = json.dumps(canonical_ast(expression), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        tags = ("intraday_aggregate",) if intraday.intersection(fields) else ()
        daily_ref = evaluation.output.get("daily_ic_reference")
        entries.append(
            LightweightCoverageCatalogEntry(
                node=CoverageFactorNode(
                    factor_id=candidate.candidate_id,
                    factor_name_cn=candidate.spec.hypothesis.claim[:80],
                    node_status="candidate",
                    structure_source="typed_ast",
                    formula_expr=encoded,
                    formula_hash=sha256_json(canonical_ast(expression)),
                    ast_hash=canonical_ast_hash(expression),
                    input_fields=fields,
                    operator_tags=operators,
                    windows=windows,
                    lookback_window=candidate.spec.max_lookback,
                    lag_days=1,
                    category="自主研究候选",
                    subcategory="分钟聚合" if tags else "日频字段",
                    description=candidate.spec.hypothesis.mechanism,
                    preprocess_method="原始因子层，不含截面预处理",
                    neutralization="由冻结评价协议另行处理",
                    orientation_sign=-1 if direction["selected_direction"] == "negative" else 1,
                    orientation_source="discovery_window_frozen",
                    source="Dashboard 批准的轻量研究候选",
                    research_family_id=publication.run_id,
                ),
                canonical_ast=canonical_ast(expression),
                data_source_tags=tags,
                daily_ic_reference=(dict(daily_ref) if isinstance(daily_ref, Mapping) else None),
            )
        )
    payload = {
        "source_coverage_graph_id": source_context.coverage_graph_id,
        "source_coverage_graph_hash": source_context.coverage_graph_hash,
        "entries": tuple(item.model_dump(mode="json") for item in entries),
    }
    return LightweightCoverageCatalog(
        **payload,
        catalog_sha256=sha256_json(payload),
    )


def refresh_lightweight_evolution(
    publication: PublishedLightweightRun,
    source_context: LightweightEvolutionContext,
    dependencies: LightweightEvolutionDependencies,
) -> EvolutionRefreshResult:
    """依次发布下一轮记忆、图谱和中文 gap brief。"""

    if (
        publication.manifest.memory_snapshot_hash != source_context.memory_snapshot_hash
        or publication.manifest.coverage_graph_hash != source_context.coverage_graph_hash
    ):
        raise ValueError("当前 manifest 没有绑定刷新前的记忆与图谱")
    memory_entries = build_lightweight_memory_entries(publication)
    memory = dependencies.publish_memory(memory_entries, source_context, publication.run_id)
    catalog = build_lightweight_coverage_catalog(publication, source_context)
    graph = dependencies.publish_coverage_graph(catalog)
    brief = dependencies.publish_gap_brief(memory, graph)
    payload = {
        "source_memory_snapshot_id": source_context.memory_snapshot_id,
        "source_memory_snapshot_hash": source_context.memory_snapshot_hash,
        "next_memory_snapshot_id": memory.snapshot_id,
        "next_memory_snapshot_hash": memory.snapshot_sha256,
        "source_coverage_graph_id": source_context.coverage_graph_id,
        "source_coverage_graph_hash": source_context.coverage_graph_hash,
        "next_coverage_graph_id": graph.graph_id,
        "next_coverage_graph_hash": graph.graph_sha256,
        "gap_brief_id": brief.brief_id,
        "gap_brief_hash": brief.brief_sha256,
        "hypothesis_entry_count": memory.hypothesis_entry_count,
        "candidate_entry_count": memory.candidate_entry_count,
    }
    return EvolutionRefreshResult(**payload, result_sha256=sha256_json(payload))
