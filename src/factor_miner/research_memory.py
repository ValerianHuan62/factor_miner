"""正式研究记忆的追加账本、不可变对象与冻结快照。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.coverage_snapshot import verify_coverage_graph
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import HashChainJsonlStore, atomic_write_bytes, atomic_write_immutable
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_state import logical_hypothesis_slot_id
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.research_evolution_schema import (
    POLLUTED_DISCOVERY_FAMILY_ID,
    DataIdentitySummary,
    EvaluationSummary,
    ResearchMemoryEntry,
    ResearchMemorySnapshot,
    build_memory_entry_identity,
    build_memory_snapshot_identity,
)
from factor_miner.long_only_protocol import DirectionDecision
from factor_miner.runtime import RuntimeProfile, resolve_research_memory_root


_HASH = r"^[0-9a-f]{64}$"
_MEMORY_EVENT_ID = r"^mementryevt_[0-9a-f]{24,64}$"


def _memory_error(code: FailureCode, message: str) -> FactorMinerError:
    return FactorMinerError(code, message)


def _manifest_hash(payload: dict[str, object]) -> str:
    return sha256_json(payload)


def build_campaign_memory_entries(
    *, family_id: str, generation_seal_id: str, run_id: str,
    slot_evaluations: Sequence[Mapping[str, object]], published_at: datetime,
    memory_snapshot_id: str, data_contract_identity_hash: str,
    source_manifest_sha256: str, coverage_graph_id: str,
    data_identity_summary: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], ...]:
    """把槽位结果转换为正式、脱敏且内容寻址的 typed entry。"""
    if family_id == POLLUTED_DISCOVERY_FAMILY_ID:
        raise _memory_error(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得写入研究记忆")
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "published_at 必须带时区")
    if len(slot_evaluations) != 120:
        raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆必须覆盖完整 120 槽")
    identity = dict(data_identity_summary or {})
    data_summary = {
        "release_hash": str(identity.get("release_hash") or sha256_json({"data_contract_identity_hash": data_contract_identity_hash})),
        "manifest_hash": str(identity.get("manifest_hash") or source_manifest_sha256),
        "field_registry_hash": str(identity.get("field_registry_hash") or sha256_json({"field_registry": "sealed"})),
        "cutoff_band": str(identity.get("cutoff_band") or "unknown"),
    }
    entries: list[dict[str, object]] = []
    for item in slot_evaluations:
        slot_id = str(item.get("slot_id", ""))
        try:
            hypothesis_slot_id = logical_hypothesis_slot_id(slot_id)
        except (ValueError, IndexError):
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"非法候选槽位：{slot_id}") from None
        status = str(item.get("status", "failed"))
        candidate_id = item.get("candidate_id")
        candidate_spec_hash = str(item.get("candidate_spec_hash") or sha256_json({"slot_id": slot_id}))
        ast_hash = str(item.get("ast_hash") or sha256_json({"slot_id": slot_id, "candidate_id": candidate_id}))

        def discrete(value: object) -> tuple[str, ...]:
            if isinstance(value, (list, tuple, set)):
                values = {str(part) for part in value if str(part).strip()}
            elif value is None:
                values = {"unknown"}
            else:
                values = {str(value)}
            return tuple(sorted(values or {"unknown"}))

        summary = item.get("evaluation_summary")
        if isinstance(summary, Mapping):
            evaluation_summary = {key: str(summary.get(key, "unknown")) for key in (
                "outcome_band", "rank_ic_band", "hac_significance_band", "portfolio_band", "redundancy_band"
            )}
        else:
            evaluation_summary = {
                "outcome_band": "inconclusive" if status in {"not_executed", "failed"} else "unknown",
                "rank_ic_band": "unknown", "hac_significance_band": "unknown",
                "portfolio_band": "unknown", "redundancy_band": "unknown",
            }
        raw_direction = item.get("direction_decision")
        direction = (
            DirectionDecision.model_validate(raw_direction)
            if isinstance(raw_direction, Mapping)
            else None
        )
        hypothesis_direction = (
            direction.hypothesis_direction if direction is not None else None
        )
        selected_direction = (
            direction.selected_direction if direction is not None else None
        )
        direction_relation = (
            direction.hypothesis_relation if direction is not None else None
        )
        direction_record_sha256 = item.get("direction_record_sha256")
        supplied_hypothesis_direction = item.get("hypothesis_direction")
        supplied_relation = item.get("direction_relation")
        if direction is None and (
            supplied_hypothesis_direction is not None or supplied_relation is not None
        ):
            if (
                supplied_hypothesis_direction not in {"positive", "negative"}
                or supplied_relation != "unresolved"
            ):
                raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "未决方向记忆必须保留合法事前方向")
            hypothesis_direction = str(supplied_hypothesis_direction)
            selected_direction = None
            direction_relation = "unresolved"
            direction_record_sha256 = None
        if direction_relation == "unresolved":
            if (
                hypothesis_direction not in {"positive", "negative"}
                or selected_direction is not None
                or direction_record_sha256 is not None
            ):
                raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "未决方向记忆必须保留事前方向且不得伪造冻结记录")
        elif any(value is not None for value in (
            hypothesis_direction,
            selected_direction,
            direction_relation,
            direction_record_sha256,
        )) and not all(value is not None for value in (
            hypothesis_direction,
            selected_direction,
            direction_relation,
            direction_record_sha256,
        )):
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "方向记忆必须完整绑定冻结发现记录")
        base: dict[str, object] = {
            "entry_kind": "campaign_slot", "discovery_family_id": family_id,
            "generation_seal_id": generation_seal_id, "run_id": run_id,
            "hypothesis_slot_id": hypothesis_slot_id, "candidate_slot_id": slot_id,
            "hypothesis_spec_hash": str(item.get("hypothesis_spec_hash") or sha256_json({"hypothesis_slot_id": hypothesis_slot_id})),
            "candidate_spec_hash": candidate_spec_hash, "ast_hash": ast_hash,
            "coverage_graph_id": coverage_graph_id, "memory_snapshot_id": memory_snapshot_id,
            "field_signature": discrete(item.get("field_signature")),
            "operator_signature": discrete(item.get("operator_signature")),
            "temporal_signature": discrete(item.get("temporal_signature")),
            "structure_signature": discrete(item.get("structure_signature")),
            "gap_labels": discrete(item.get("gap_labels")), "terminal_state": status,
            "hypothesis_direction": hypothesis_direction,
            "selected_direction": selected_direction,
            "direction_relation": direction_relation,
            "direction_source": (
                "discovery_window_unresolved"
                if direction_relation == "unresolved"
                else "discovery_window_frozen" if hypothesis_direction is not None else None
            ),
            "direction_record_sha256": direction_record_sha256,
            "failure_reason": (
                item.get("failure_reason")
                or (
                    "事前负向假设在方向发现区间被反转，确认结果另行记录"
                    if hypothesis_direction == "negative" and direction_relation == "reversed"
                    else "事前正向假设在方向发现区间被反转，确认结果另行记录"
                    if hypothesis_direction == "positive" and direction_relation == "reversed"
                    else "方向发现未决，确认结果另行记录"
                    if direction_relation == "unresolved"
                    else None
                )
            ), "duplicate_of": item.get("duplicate_of"),
            "evaluation_summary": evaluation_summary, "data_identity_summary": data_summary,
            "created_at": published_at.astimezone(timezone.utc).isoformat(),
        }
        base["memory_entry_id"] = f"mementry_{sha256_json(base)[:24]}"
        entries.append(base)
    return tuple(entries)


def publish_campaign_memory(
    artifact_root: Path,
    *,
    run_id: str,
    family_id: str,
    generation_seal_id: str,
    source_manifest_sha256: str,
    data_contract_identity_hash: str,
    slot_evaluations: Sequence[Mapping[str, object]],
    published_at: datetime,
    metadata: Mapping[str, object] | None = None,
    evolution_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """追加并冻结正式 entries、snapshot 与 reference；可重复调用但不可改写。"""
    if family_id == POLLUTED_DISCOVERY_FAMILY_ID:
        raise _memory_error(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得发布研究记忆")
    evolution = dict(evolution_metadata or {})
    coverage_graph_id = evolution.get("coverage_graph_id")
    if not isinstance(coverage_graph_id, str) or not coverage_graph_id:
        return _publish_legacy_campaign_memory(
            artifact_root=artifact_root, run_id=run_id, family_id=family_id,
            generation_seal_id=generation_seal_id, source_manifest_sha256=source_manifest_sha256,
            data_contract_identity_hash=data_contract_identity_hash,
            slot_evaluations=slot_evaluations, published_at=published_at, metadata=metadata,
        )
    verify_published_run(artifact_root, run_id)
    seed = {"run_id": run_id, "family_id": family_id, "seal": generation_seal_id, "source": source_manifest_sha256, "coverage_graph_id": coverage_graph_id}
    snapshot_id = f"memsnap_{sha256_json(seed)[:24]}"
    entries = build_campaign_memory_entries(
        family_id=family_id, generation_seal_id=generation_seal_id, run_id=run_id,
        slot_evaluations=slot_evaluations, published_at=published_at,
        memory_snapshot_id=snapshot_id, data_contract_identity_hash=data_contract_identity_hash,
        source_manifest_sha256=source_manifest_sha256, coverage_graph_id=coverage_graph_id,
        data_identity_summary=evolution.get("data_identity_summary") if isinstance(evolution.get("data_identity_summary"), Mapping) else None,
    )
    root = artifact_root.expanduser().resolve(strict=False)
    memory_root = root / "research_memory"
    store = ResearchMemoryStore(research_memory_root=memory_root)
    typed_entries = tuple(ResearchMemoryEntry.model_validate(item) for item in entries)
    policy = MemorySnapshotPolicy(
        allowed_source_family_ids=(family_id,),
        include_terminal_states=tuple(sorted({str(item["terminal_state"]) for item in entries})),
    )
    def transaction(events: tuple[ResearchMemoryEvent, ...]) -> dict[str, object]:
        snapshot_model = store._build_snapshot_locked(
            events, cutoff=published_at, source_family_ids=(family_id,),
            coverage_graph_id=coverage_graph_id, policy=policy,
            snapshot_id=snapshot_id, created_at=published_at,
        )
        snapshot = snapshot_model.model_dump(mode="json")
        entries_by_id = {
            entry.memory_entry_id: entry
            for entry in (store._load_entry_from_event(event) for event in events)
        }
        snapshot_entries = tuple(entries_by_id[entry_id] for entry_id in snapshot_model.entry_ids)
        terminal_counts = {
            status: sum(entry.terminal_state == status for entry in snapshot_entries)
            for status in sorted({entry.terminal_state for entry in snapshot_entries})
        }
        reference = {"version": "research-memory-reference-v1", "run_id": run_id, "family_id": family_id,
                     "generation_seal_id": generation_seal_id, "source_manifest_sha256": source_manifest_sha256,
                     "published_at": published_at.astimezone(timezone.utc).isoformat(), "memory_snapshot_id": snapshot_id,
                     "memory_snapshot_sha256": snapshot["snapshot_sha256"], "entry_count": snapshot_model.entry_count,
                     "coverage_graph_id": snapshot["coverage_graph_id"],
                     "source_family_ids": snapshot["source_family_ids"], "cutoff_at": snapshot["cutoff_at"],
                     "terminal_counts": terminal_counts,
                     "context_id": evolution.get("context_id"),
                     "coverage_graph_manifest_hash": evolution.get("coverage_graph_manifest_hash"),
                     "source_memory_snapshot_hash": evolution.get("memory_snapshot_hash"),
                     "gap_report_hash": evolution.get("gap_report_hash"), "context_sha256": evolution.get("context_sha256"),
                     "approval_batch_hash": evolution.get("approval_batch_hash"), "design_policy_hash": evolution.get("design_policy_hash"),
                     "approval_count": int(evolution.get("approval_count", 0)),
                     "gap_summary": dict(evolution.get("gap_summary", {})) if isinstance(evolution.get("gap_summary"), Mapping) else {},
                     "metadata": dict(metadata or {}), "evolution_metadata": evolution, "projection_status": "pending"}
        reference["reference_sha256"] = sha256_json(reference)
        PublishedCampaignMemoryReference.model_validate(reference)
        atomic_write_immutable(memory_root / "references" / f"{run_id}.json", canonical_json_bytes(reference))
        return reference

    return store.publish_batch(typed_entries, transaction)


def _publish_legacy_campaign_memory(**kwargs: Any) -> dict[str, object]:
    """兼容旧调用但不污染正式 research_memory 账本。"""
    root = Path(kwargs["artifact_root"]).expanduser().resolve(strict=False) / "research_memory_legacy"
    root.mkdir(parents=True, exist_ok=True)
    payload = {"version": "research-memory-legacy-v1", "run_id": kwargs["run_id"],
               "family_id": kwargs["family_id"], "generation_seal_id": kwargs["generation_seal_id"],
               "source_manifest_sha256": kwargs["source_manifest_sha256"],
               "published_at": kwargs["published_at"].astimezone(timezone.utc).isoformat(),
               "entry_count": len(kwargs["slot_evaluations"]), "metadata": dict(kwargs.get("metadata") or {})}
    payload["legacy_sha256"] = sha256_json(payload)
    atomic_write_immutable(root / f"{kwargs['run_id']}.json", canonical_json_bytes(payload))
    return payload


class MemorySnapshotPolicy(BaseModel):
    """冻结可用 family 白名单与快照选择规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_source_family_ids: tuple[str, ...] = Field(min_length=1)
    excluded_family_ids: tuple[str, ...] = (POLLUTED_DISCOVERY_FAMILY_ID,)
    include_terminal_states: tuple[str, ...] = ("completed", "published", "visible_passed", "visible_failed")
    require_published_run: bool = True
    require_data_contract_identity: bool = True
    policy_version: str = "research-memory-v1"

    @field_validator("allowed_source_family_ids", "excluded_family_ids", "include_terminal_states")
    @classmethod
    def validate_sorted_unique(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if not value:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"{info.field_name} 不能为空")
        if tuple(sorted(set(value))) != value:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, f"{info.field_name} 必须唯一且按字典序排序")
        return value

    @model_validator(mode="after")
    def validate_polluted_family(self) -> MemorySnapshotPolicy:
        if POLLUTED_DISCOVERY_FAMILY_ID not in self.excluded_family_ids:
            raise _memory_error(FailureCode.POLLUTED_FAMILY_REJECTED, "快照策略必须显式排除污染 family")
        return self


class ResearchMemoryEvent(BaseModel):
    """entries.jsonl 中的单条追加事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(pattern=_MEMORY_EVENT_ID)
    sequence: int = Field(default=0, ge=0)
    memory_entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$")
    entry_sha256: str = Field(pattern=_HASH)
    object_relative_path: str = Field(min_length=1)
    written_at: datetime
    supersedes_entry_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$")
    previous_event_hash: str | None = Field(default=None, pattern=_HASH)
    event_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("written_at")
    @classmethod
    def validate_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "written_at 必须带时区")
        return value.astimezone(timezone.utc)


class ResearchMemoryIndex(BaseModel):
    """从正式 entries 和 snapshots 可重建的索引文件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "research-memory-index-v1"
    entry_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    last_entry_event_id: str | None = Field(default=None, pattern=_MEMORY_EVENT_ID)
    last_entry_event_hash: str | None = Field(default=None, pattern=_HASH)
    entries: dict[str, str]
    snapshots: dict[str, str]


class PublishedRunInputManifest(BaseModel):
    """研究记忆依赖的正式运行输入清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    generation_seal_id: str = Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$")
    generation_manifest_sha256: str = Field(pattern=_HASH)
    evaluation_policy_id: str = Field(min_length=1)
    data_contract_identity_hash: str = Field(pattern=_HASH)
    statistical_budget_hash: str = Field(pattern=_HASH)
    published_at: datetime

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "run/input_manifest.json 的 published_at 必须带时区")
        return value.astimezone(timezone.utc)


class PublishedCampaignMemoryReference(BaseModel):
    """已发布运行关联的正式研究记忆引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "research-memory-reference-v1"
    run_id: str = Field(pattern=r"^run_[0-9a-f]{24}$")
    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24,64}$")
    generation_seal_id: str = Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$")
    source_manifest_sha256: str = Field(pattern=_HASH)
    published_at: datetime
    memory_snapshot_id: str = Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24,64}$")
    memory_snapshot_sha256: str = Field(pattern=_HASH)
    coverage_graph_id: str | None = None
    source_family_ids: tuple[str, ...] = ()
    cutoff_at: datetime | None = None
    coverage_graph_manifest_hash: str | None = Field(default=None, pattern=_HASH)
    source_memory_snapshot_hash: str | None = Field(default=None, pattern=_HASH)
    gap_report_hash: str | None = Field(default=None, pattern=_HASH)
    context_sha256: str | None = Field(default=None, pattern=_HASH)
    approval_batch_hash: str | None = Field(default=None, pattern=_HASH)
    design_policy_hash: str | None = Field(default=None, pattern=_HASH)
    approval_count: int = Field(ge=0, le=10)
    entry_count: int = Field(ge=0)
    terminal_counts: dict[str, int]
    gap_summary: dict[str, object]
    memory_entries: tuple[dict[str, object], ...] = ()
    context_id: str | None = None
    metadata: dict[str, object] = {}
    evolution_metadata: dict[str, object] = {}
    projection_status: str = "pending"
    reference_sha256: str | None = Field(default=None, pattern=_HASH)

    @field_validator("published_at")
    @classmethod
    def validate_published_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆引用的 published_at 必须带时区")
        return value.astimezone(timezone.utc)


class ResearchMemoryStore:
    """正式研究记忆的唯一文件接口。"""

    def __init__(
        self,
        artifact_root: Path | RuntimeProfile | None = None,
        *,
        research_memory_root: Path | None = None,
    ) -> None:
        if research_memory_root is None:
            if artifact_root is None:
                raise TypeError("必须显式提供 artifact_root 或 research_memory_root")
            resolved_root = resolve_research_memory_root(artifact_root)
        else:
            resolved_root = Path(research_memory_root).expanduser().resolve(strict=False)
        self.root = resolved_root
        self.entries_path = self.root / "entries.jsonl"
        self.entry_objects_root = self.root / "entry_objects"
        self.snapshots_root = self.root / "snapshots"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / "entries.jsonl.lock"
        self.artifact_root = self.root.parent
        self._store = HashChainJsonlStore(
            self.entries_path,
            self.lock_path,
            ResearchMemoryEvent,
        )

    def append_entry(self, entry: ResearchMemoryEntry) -> Path:
        """以追加账本登记一条不可变研究记忆。"""

        build_memory_entry_identity(entry)
        self._assert_entry_contract(entry)
        object_path = self.entry_objects_root / f"{entry.memory_entry_id}.json"
        payload = canonical_json_bytes(entry.model_dump(mode="json"))
        event = ResearchMemoryEvent(
            event_id=f"mementryevt_{sha256_json({'memory_entry_id': entry.memory_entry_id, 'entry_sha256': entry.entry_sha256})[:24]}",
            memory_entry_id=entry.memory_entry_id,
            entry_sha256=entry.entry_sha256,
            object_relative_path=self._relative_object_path(entry.memory_entry_id),
            written_at=datetime.now(timezone.utc),
            supersedes_entry_id=entry.supersedes_entry_id,
        )

        def transaction(
            events: tuple[ResearchMemoryEvent, ...],
            append_locked: Any,
        ) -> Path:
            existing_by_id = {item.memory_entry_id: item for item in events}
            existing_event = existing_by_id.get(entry.memory_entry_id)
            if existing_event is not None:
                if existing_event.entry_sha256 != entry.entry_sha256:
                    raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "同一 memory_entry_id 的内容不可覆盖")
                if existing_event.object_relative_path != event.object_relative_path:
                    raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆对象路径与既有账本不一致")
                atomic_write_immutable(object_path, payload)
                self._write_index(events, self._load_snapshots())
                return object_path
            atomic_write_immutable(object_path, payload)
            _, complete_events = append_locked(event, self._validate_pre_append)
            self._write_index(complete_events, self._load_snapshots())
            return object_path

        return self._store.with_locked_events(transaction)

    def load_entry(self, memory_entry_id: str) -> ResearchMemoryEntry:
        """直接从正式对象文件恢复记忆条目。"""

        path = self.entry_objects_root / f"{memory_entry_id}.json"
        try:
            entry = ResearchMemoryEntry.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, f"记忆条目缺失或损坏：{memory_entry_id}") from error
        build_memory_entry_identity(entry)
        if entry.memory_entry_id != memory_entry_id:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆条目路径与内容身份不一致")
        return entry

    def publish_batch(
        self,
        entries: Sequence[ResearchMemoryEntry],
        operation: Callable[[tuple[ResearchMemoryEvent, ...]], dict[str, object]],
    ) -> dict[str, object]:
        """在同一写入锁内原子追加一批条目并执行快照/引用发布。"""
        typed_entries = tuple(entries)
        if len(typed_entries) != 120:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "正式记忆批次必须正好包含 120 条 typed entry")
        for entry in typed_entries:
            build_memory_entry_identity(entry)
            self._assert_entry_contract(entry)

        def all_files(root: Path) -> dict[Path, bytes]:
            return {path: path.read_bytes() for path in root.glob("**/*") if path.is_file()} if root.exists() else {}

        def restore(before: dict[object, object]) -> None:
            for path, payload in ((self.entries_path, before[self.entries_path]), (self.index_path, before[self.index_path])):
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, payload)
            for key, directory in (("objects", self.entry_objects_root), ("snapshots", self.snapshots_root), ("references", self.root / "references")):
                original = before[key]
                current = {path for path in directory.glob("**/*") if path.is_file()} if directory.exists() else set()
                for path in current - set(original):
                    path.unlink()
                for path, payload in original.items():
                    atomic_write_bytes(path, payload)

        def transaction(events: tuple[ResearchMemoryEvent, ...], append_locked: Any) -> dict[str, object]:
            before = {
                self.entries_path: self.entries_path.read_bytes() if self.entries_path.exists() else None,
                self.index_path: self.index_path.read_bytes() if self.index_path.exists() else None,
                "objects": all_files(self.entry_objects_root),
                "snapshots": all_files(self.snapshots_root),
                "references": all_files(self.root / "references"),
            }
            try:
                known_ids = {event.memory_entry_id for event in events}
                by_id = {entry.memory_entry_id: entry for entry in typed_entries}
                if len(by_id) != len(typed_entries):
                    raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "正式记忆批次含重复 memory_entry_id")
                for entry in typed_entries:
                    existing = next((event for event in events if event.memory_entry_id == entry.memory_entry_id), None)
                    if existing is not None and existing.entry_sha256 != entry.entry_sha256:
                        raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "同一 memory_entry_id 的内容不可覆盖")
                    if entry.supersedes_entry_id is not None and entry.supersedes_entry_id not in known_ids and existing is None:
                        raise _memory_error(FailureCode.LEDGER_CORRUPT, "supersedes_entry_id 未指向已有记忆条目")
                current = events
                for entry in typed_entries:
                    object_path = self.entry_objects_root / f"{entry.memory_entry_id}.json"
                    atomic_write_immutable(object_path, canonical_json_bytes(entry.model_dump(mode="json")))
                    if entry.memory_entry_id not in {event.memory_entry_id for event in current}:
                        event = ResearchMemoryEvent(
                            event_id=f"mementryevt_{sha256_json({'memory_entry_id': entry.memory_entry_id, 'entry_sha256': entry.entry_sha256})[:24]}",
                            memory_entry_id=entry.memory_entry_id, entry_sha256=entry.entry_sha256,
                            object_relative_path=self._relative_object_path(entry.memory_entry_id),
                            written_at=datetime.now(timezone.utc), supersedes_entry_id=entry.supersedes_entry_id,
                        )
                        _, current = append_locked(event, self._validate_pre_append)
                self._write_index(current, self._load_snapshots())
                return operation(current)
            except BaseException:
                restore(before)
                raise

        return self._store.with_locked_events(transaction)

    def _build_snapshot_locked(
        self, events: tuple[ResearchMemoryEvent, ...], *, cutoff: datetime,
        source_family_ids: tuple[str, ...], coverage_graph_id: str,
        policy: MemorySnapshotPolicy, snapshot_id: str | None, created_at: datetime | None,
    ) -> ResearchMemorySnapshot:
        """在已持有 writer lock 时构造并写入 snapshot。"""
        normalized_cutoff = cutoff.astimezone(timezone.utc)
        snapshots = self._load_snapshots()
        entries_with_publication = [
            (entry, self._load_published_input_manifest(entry).published_at)
            for entry in (self._load_entry_from_event(event) for event in events)
        ]
        eligible = [entry for entry, published_at in entries_with_publication
                    if entry.created_at.astimezone(timezone.utc) <= normalized_cutoff and published_at <= normalized_cutoff
                    and entry.discovery_family_id in source_family_ids
                    and entry.discovery_family_id not in policy.excluded_family_ids
                    and entry.terminal_state in policy.include_terminal_states]
        ordered = tuple(sorted(eligible, key=lambda item: (item.created_at, item.memory_entry_id)))
        entry_bindings = tuple(ResearchMemorySnapshot.EntryBinding(memory_entry_id=item.memory_entry_id, entry_sha256=item.entry_sha256)
                               for item in sorted(ordered, key=lambda item: item.memory_entry_id))
        graph_root = self.artifact_root / "artifacts" / "coverage_graphs" / coverage_graph_id
        verified_graph = verify_coverage_graph(graph_root)
        graph_manifest_hash = sha256_json(verified_graph.manifest.model_dump(mode="json"))
        normalized_created_at = created_at.astimezone(timezone.utc) if created_at else datetime.now(timezone.utc)
        base = {"created_at": normalized_created_at.isoformat(), "cutoff_at": normalized_cutoff.isoformat(),
                "source_family_ids": list(source_family_ids),
                "source_seal_ids": sorted({item.generation_seal_id for item in ordered}),
                "source_run_ids": sorted({item.run_id for item in ordered}),
                "entry_ids": [item.memory_entry_id for item in entry_bindings],
                "entry_hashes": [item.entry_sha256 for item in entry_bindings],
                "entry_bindings": [item.model_dump(mode="json") for item in entry_bindings],
                "exclusion_rules": [f"policy_version:{policy.policy_version}", f"exclude:{POLLUTED_DISCOVERY_FAMILY_ID}",
                                     f"allow:{','.join(source_family_ids)}", "cutoff:created_at_lte"],
                "entry_count": len(entry_bindings), "coverage_graph_id": verified_graph.manifest.coverage_graph_id,
                "coverage_graph_manifest_hash": graph_manifest_hash}
        snapshot = ResearchMemorySnapshot(memory_snapshot_id=snapshot_id or f"memsnap_{sha256_json(base)[:24]}",
            created_at=normalized_created_at, cutoff_at=normalized_cutoff, source_family_ids=source_family_ids,
            source_seal_ids=tuple(base["source_seal_ids"]), source_run_ids=tuple(base["source_run_ids"]),
            entry_ids=tuple(base["entry_ids"]), entry_hashes=tuple(base["entry_hashes"]), entry_bindings=entry_bindings,
            exclusion_rules=tuple(base["exclusion_rules"]), entry_count=len(entry_bindings),
            coverage_graph_id=verified_graph.manifest.coverage_graph_id, coverage_graph_manifest_hash=graph_manifest_hash)
        build_memory_snapshot_identity(snapshot)
        atomic_write_immutable(self.snapshots_root / f"{snapshot.memory_snapshot_id}.json", canonical_json_bytes(snapshot.model_dump(mode="json")))
        snapshots[snapshot.memory_snapshot_id] = snapshot
        self._write_index(events, snapshots)
        return snapshot

    def build_snapshot(
        self,
        *,
        cutoff: datetime,
        source_family_ids: tuple[str, ...],
        coverage_graph_id: str,
        policy: MemorySnapshotPolicy,
        snapshot_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ResearchMemorySnapshot:
        """基于 cutoff 之前已发布条目构建不可变快照。"""

        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "cutoff 必须带时区")
        normalized_cutoff = cutoff.astimezone(timezone.utc)
        requested_families = tuple(sorted(set(source_family_ids)))
        if not requested_families:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "source_family_ids 不能为空")
        if tuple(source_family_ids) != requested_families:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "source_family_ids 必须唯一且按字典序排序")
        if not set(requested_families).issubset(set(policy.allowed_source_family_ids)):
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "source_family_ids 超出当前研究配置白名单")
        if any(family_id in policy.excluded_family_ids for family_id in requested_families):
            raise _memory_error(FailureCode.POLLUTED_FAMILY_REJECTED, "source_family_ids 包含被排除 family")

        graph_root = self.artifact_root / "artifacts" / "coverage_graphs" / coverage_graph_id
        verified_graph = verify_coverage_graph(graph_root)
        graph_manifest_hash = sha256_json(verified_graph.manifest.model_dump(mode="json"))

        if created_at is not None and (created_at.tzinfo is None or created_at.utcoffset() is None):
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "created_at 必须带时区")
        normalized_created_at = (
            created_at.astimezone(timezone.utc)
            if created_at is not None
            else datetime.now(timezone.utc)
        )

        def transaction(
            events: tuple[ResearchMemoryEvent, ...],
            _append_locked: Any,
        ) -> ResearchMemorySnapshot:
            entries_with_publication = []
            for item in events:
                entry = self._load_entry_from_event(item)
                entries_with_publication.append(
                    (entry, self._load_published_input_manifest(entry).published_at)
                )
            snapshots = self._load_snapshots()
            eligible = [
                entry
                for entry, published_at in entries_with_publication
                if entry.created_at.astimezone(timezone.utc) <= normalized_cutoff
                and published_at <= normalized_cutoff
                and entry.discovery_family_id in requested_families
                and entry.discovery_family_id not in policy.excluded_family_ids
                and entry.terminal_state in policy.include_terminal_states
            ]
            ordered = tuple(sorted(eligible, key=lambda item: (item.created_at, item.memory_entry_id)))
            source_seal_ids = tuple(sorted({item.generation_seal_id for item in ordered}))
            source_run_ids = tuple(sorted({item.run_id for item in ordered}))
            entry_bindings = tuple(
                ResearchMemorySnapshot.EntryBinding(
                    memory_entry_id=item.memory_entry_id,
                    entry_sha256=item.entry_sha256,
                )
                for item in sorted(ordered, key=lambda item: item.memory_entry_id)
            )
            entry_ids = tuple(binding.memory_entry_id for binding in entry_bindings)
            entry_hashes = tuple(binding.entry_sha256 for binding in entry_bindings)
            base_payload = {
                "created_at": normalized_created_at.isoformat(),
                "cutoff_at": normalized_cutoff.isoformat(),
                "source_family_ids": list(requested_families),
                "source_seal_ids": list(source_seal_ids),
                "source_run_ids": list(source_run_ids),
                "entry_ids": list(entry_ids),
                "entry_hashes": list(entry_hashes),
                "entry_bindings": [binding.model_dump(mode="json") for binding in entry_bindings],
                "exclusion_rules": [
                    f"policy_version:{policy.policy_version}",
                    f"exclude:{POLLUTED_DISCOVERY_FAMILY_ID}",
                    f"allow:{','.join(requested_families)}",
                    "cutoff:created_at_lte",
                ],
                "entry_count": len(entry_ids),
                "coverage_graph_id": verified_graph.manifest.coverage_graph_id,
                "coverage_graph_manifest_hash": graph_manifest_hash,
            }
            snapshot_identity_seed = sha256_json(base_payload)
            snapshot = ResearchMemorySnapshot(
                memory_snapshot_id=snapshot_id or f"memsnap_{snapshot_identity_seed[:24]}",
                created_at=normalized_created_at,
                cutoff_at=normalized_cutoff,
                source_family_ids=requested_families,
                source_seal_ids=source_seal_ids,
                source_run_ids=source_run_ids,
                entry_ids=entry_ids,
                entry_hashes=entry_hashes,
                entry_bindings=entry_bindings,
                exclusion_rules=tuple(base_payload["exclusion_rules"]),
                entry_count=len(entry_ids),
                coverage_graph_id=verified_graph.manifest.coverage_graph_id,
                coverage_graph_manifest_hash=graph_manifest_hash,
            )
            build_memory_snapshot_identity(snapshot)
            path = self.snapshots_root / f"{snapshot.memory_snapshot_id}.json"
            atomic_write_immutable(path, canonical_json_bytes(snapshot.model_dump(mode="json")))
            snapshots[snapshot.memory_snapshot_id] = snapshot
            self._write_index(events, snapshots)
            return snapshot

        return self._store.with_locked_events(transaction)

    def load_snapshot(self, memory_snapshot_id: str) -> ResearchMemorySnapshot:
        """读取不可变 snapshot 文件并复核 hash。"""

        path = self.snapshots_root / f"{memory_snapshot_id}.json"
        try:
            snapshot = ResearchMemorySnapshot.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, f"记忆快照缺失或损坏：{memory_snapshot_id}") from error
        build_memory_snapshot_identity(snapshot)
        if snapshot.memory_snapshot_id != memory_snapshot_id:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆快照路径与内容身份不一致")
        return snapshot

    def verify(self) -> None:
        """验证账本、对象、快照和 index 的一致性。"""

        def transaction(
            events: tuple[ResearchMemoryEvent, ...],
            _append_locked: Any,
        ) -> None:
            snapshots = self._load_snapshots()
            seen_ids: set[str] = set()
            event_hash_by_entry_id: dict[str, str] = {}
            for event in events:
                if event.memory_entry_id in seen_ids:
                    raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆账本存在重复 memory_entry_id")
                seen_ids.add(event.memory_entry_id)
                entry = self._load_entry_from_event(event)
                build_memory_entry_identity(entry)
                self._assert_entry_contract(entry)
                if event.entry_sha256 != entry.entry_sha256:
                    raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆账本 entry hash 与对象文件不一致")
                event_hash_by_entry_id[event.memory_entry_id] = event.entry_sha256
            for snapshot in snapshots.values():
                build_memory_snapshot_identity(snapshot)
                for binding in snapshot.entry_bindings:
                    if binding.memory_entry_id not in event_hash_by_entry_id:
                        raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆快照引用了不存在的正式记忆条目")
                    if event_hash_by_entry_id[binding.memory_entry_id] != binding.entry_sha256:
                        raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆快照 binding 与正式账本事件或对象不一致")
            expected_index = self._build_index(events, snapshots)
            if not self.index_path.exists():
                if expected_index.entry_count == 0 and expected_index.snapshot_count == 0:
                    return
                raise _memory_error(FailureCode.LEDGER_CORRUPT, "研究记忆 index 缺失")
            try:
                actual_index = ResearchMemoryIndex.model_validate_json(self.index_path.read_bytes())
            except (OSError, ValueError) as error:
                raise _memory_error(FailureCode.LEDGER_CORRUPT, "研究记忆 index 损坏") from error
            if actual_index != expected_index:
                raise _memory_error(FailureCode.LEDGER_CORRUPT, "研究记忆 index 与正式文件不一致")
            return None

        self._store.with_locked_events(transaction)

    def _validate_pre_append(
        self,
        events: tuple[ResearchMemoryEvent, ...],
        candidate: ResearchMemoryEvent,
    ) -> None:
        known_ids = {event.memory_entry_id for event in events}
        if candidate.memory_entry_id in known_ids:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "memory_entry_id 已存在")
        if candidate.supersedes_entry_id is not None and candidate.supersedes_entry_id not in known_ids:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "supersedes_entry_id 未指向已有记忆条目")

    def _assert_entry_contract(self, entry: ResearchMemoryEntry) -> None:
        if entry.discovery_family_id == POLLUTED_DISCOVERY_FAMILY_ID:
            raise _memory_error(FailureCode.POLLUTED_FAMILY_REJECTED, "污染 family 不得写入正式研究记忆")
        if not entry.terminal_state.strip():
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "terminal_state 不能为空")
        if not entry.entry_sha256:
            raise _memory_error(FailureCode.EVOLUTION_HASH_MISMATCH, "记忆条目缺少 entry_sha256")
        if not entry.data_identity_summary.release_hash or not entry.data_identity_summary.manifest_hash or not entry.data_identity_summary.field_registry_hash:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目缺少数据身份摘要")

        ledger = LLMDiscoveryLedger(self.artifact_root)
        family = ledger.load_family(entry.discovery_family_id)
        if family.discovery_family_id != entry.discovery_family_id:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆 family 身份不一致")
        seal = ledger.load_generation_seal(entry.discovery_family_id)
        if seal.generation_seal_id != entry.generation_seal_id:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆 generation seal 身份不一致")
        if entry.candidate_slot_id not in seal.manifest.slot_object_hashes:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目 candidate_slot_id 不在 generation seal 中")

        manifest = verify_published_run(self.artifact_root, entry.run_id)
        input_manifest = self._load_published_input_manifest(entry)
        if input_manifest.family_id != entry.discovery_family_id:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目 family 与发布运行不一致")
        if input_manifest.generation_seal_id != entry.generation_seal_id:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目 seal 与发布运行不一致")
        if input_manifest.data_contract_identity_hash != seal.manifest.data_contract_identity_hash:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目缺少或破坏数据合同身份")
        if manifest.run_id != entry.run_id:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "记忆条目 run_id 与发布运行不一致")

    def _relative_object_path(self, memory_entry_id: str) -> str:
        return f"entry_objects/{memory_entry_id}.json"

    def _load_entry_from_event(self, event: ResearchMemoryEvent) -> ResearchMemoryEntry:
        path = self.root / event.object_relative_path
        expected_path = self.entry_objects_root / f"{event.memory_entry_id}.json"
        if path != expected_path:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆账本对象路径越界或不匹配")
        entry = self.load_entry(event.memory_entry_id)
        if entry.entry_sha256 != event.entry_sha256:
            raise _memory_error(FailureCode.LEDGER_CORRUPT, "记忆对象 hash 与账本不一致")
        return entry

    def _load_published_input_manifest(
        self,
        entry: ResearchMemoryEntry,
    ) -> PublishedRunInputManifest:
        input_manifest_path = (
            self.artifact_root / "artifacts" / "runs" / entry.run_id / "run" / "input_manifest.json"
        )
        try:
            payload = json.loads(input_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆缺少已发布运行的 input manifest") from error
        try:
            manifest = PublishedRunInputManifest.model_validate(payload)
        except ValueError as error:
            raise _memory_error(FailureCode.MEMORY_IMMUTABILITY_VIOLATION, "研究记忆 input manifest 缺少正式 published_at 或身份字段非法") from error
        return manifest

    def _load_snapshots(self) -> dict[str, ResearchMemorySnapshot]:
        if not self.snapshots_root.exists():
            return {}
        snapshots: dict[str, ResearchMemorySnapshot] = {}
        for path in sorted(self.snapshots_root.glob("*.json")):
            snapshot = self.load_snapshot(path.stem)
            snapshots[snapshot.memory_snapshot_id] = snapshot
        return snapshots

    def _build_index(
        self,
        events: tuple[ResearchMemoryEvent, ...],
        snapshots: dict[str, ResearchMemorySnapshot],
    ) -> ResearchMemoryIndex:
        return ResearchMemoryIndex(
            entry_count=len(events),
            snapshot_count=len(snapshots),
            last_entry_event_id=events[-1].event_id if events else None,
            last_entry_event_hash=events[-1].event_hash if events else None,
            entries={event.memory_entry_id: event.entry_sha256 for event in events},
            snapshots={
                snapshot_id: snapshot.snapshot_sha256
                for snapshot_id, snapshot in sorted(snapshots.items())
            },
        )

    def _write_index(
        self,
        events: tuple[ResearchMemoryEvent, ...],
        snapshots: dict[str, ResearchMemorySnapshot],
    ) -> None:
        index = self._build_index(events, snapshots)
        atomic_write_bytes(
            self.index_path,
            canonical_json_bytes(index.model_dump(mode="json")),
        )
