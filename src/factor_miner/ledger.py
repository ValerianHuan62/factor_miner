"""不可变 JSON 文档与单写入者哈希链 JSONL 账本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_schema import (
    RegisteredRegimeDeployment,
    RegisteredRegimeResearch,
    RegimeDeploymentSpec,
    RegimeResearchSpec,
    registered_regime_deployment,
    registered_regime_research,
)
from factor_miner.schema import (
    CampaignSpec,
    CandidateFactorSpec,
    EvaluationPolicySpec,
    IncrementalEvaluationPolicySpec,
    RegisteredCandidate,
    RegisteredResearchFamily,
    RegisteredReferenceFactorLibrary,
    RegisteredTrustedCandidate,
    ResearchFamilySpec,
    ReferenceFactorLibrarySpec,
    TrustedCandidateFactorSpec,
    TrustedVisibleCampaignSpec,
    campaign_id,
    evaluation_policy_id,
    registered_reference_factor_library,
    registered_research_family,
    registered_candidate,
    registered_trusted_candidate,
    trusted_campaign_id,
)


class EventType(StrEnum):
    """V0 工作流使用的账本事件类型。"""

    CANDIDATE_REGISTERED = "candidate_registered"
    CAMPAIGN_REGISTERED = "campaign_registered"
    RUN_STARTED = "run_started"
    OUTCOME_EXPOSED = "outcome_exposed"
    EVALUATION_COMPLETED = "evaluation_completed"
    VISIBLE_PASSED = "visible_passed"
    VISIBLE_FAILED = "visible_failed"
    COMPILE_FAILED = "compile_failed"
    COMPUTE_FAILED = "compute_failed"
    EVALUATION_FAILED = "evaluation_failed"
    REDUNDANCY_FAILED = "redundancy_failed"
    INCREMENTAL_FAILED = "incremental_failed"
    INTERRUPTED = "interrupted"
    RUN_COMPLETED = "run_completed"
    SMOKE_PASSED = "smoke_passed"


class TrialEvent(BaseModel):
    """账本中的单个不可变 trial 事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: f"event_{uuid4().hex}")
    sequence: int = Field(default=0, ge=0)
    candidate_id: str | None = None
    campaign_id: str | None = None
    run_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: EventType
    status: str = "pending"
    outcome_exposed: bool = False
    failure_code: FailureCode | None = None
    spec_hash: str | None = None
    code_hash: str | None = None
    data_hash: str | None = None
    config_hash: str | None = None
    artifact_refs: tuple[str, ...] = ()
    previous_event_hash: str | None = None
    supersedes_event_id: str | None = None
    event_hash: str | None = None

    @field_validator("event_id", "status")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """校验事件 ID 和状态文本非空。"""

        if not value.strip():
            raise ValueError("事件 ID 和状态不能为空")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区事件时间。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at 必须带有时区")
        return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class LedgerPaths:
    """由单一 artifact root 派生的账本目录布局。"""

    artifact_root: Path

    def __post_init__(self) -> None:
        """将 artifact root 固定为规范化绝对路径。"""

        object.__setattr__(
            self,
            "artifact_root",
            self.artifact_root.expanduser().resolve(strict=False),
        )

    @property
    def state_root(self) -> Path:
        """返回状态文档根目录。"""

        return self.artifact_root / "state"

    @property
    def candidates_root(self) -> Path:
        """返回候选文档目录。"""

        return self.state_root / "candidates"

    @property
    def campaigns_root(self) -> Path:
        """返回 campaign 文档目录。"""

        return self.state_root / "campaigns"

    @property
    def policies_root(self) -> Path:
        """返回评价 policy 文档目录。"""

        return self.state_root / "policies"

    @property
    def families_root(self) -> Path:
        """返回 research family 文档目录。"""

        return self.state_root / "families"

    @property
    def reference_libraries_root(self) -> Path:
        """返回 V0.2 参考因子库文档目录。"""

        return self.state_root / "reference_libraries"

    @property
    def regime_research_root(self) -> Path:
        """返回 V0.3 市场状态研究文档目录。"""

        return self.state_root / "regime_research"

    @property
    def regime_deployments_root(self) -> Path:
        """返回 V0.3 固定市场状态部署文档目录。"""

        return self.state_root / "regime_deployments"

    @property
    def ledger_root(self) -> Path:
        """返回账本目录。"""

        return self.state_root / "ledger"

    @property
    def trials_path(self) -> Path:
        """返回追加式 trial JSONL 路径。"""

        return self.ledger_root / "trials.jsonl"

    @property
    def lock_path(self) -> Path:
        """返回单写入者锁文件路径。"""

        return self.ledger_root / "trials.jsonl.lock"


def _ledger_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造账本稳定错误。

    参数：
        code: 账本失败代码。
        message: 中文失败说明。

    返回：
        携带指定代码的账本异常。
    """

    return FactorMinerError(code, message)


def _atomic_write_immutable(path: Path, payload: bytes) -> Path:
    """以临时文件、fsync 和原子替换写入不可变文档。

    参数：
        path: 目标文档路径。
        payload: 规范化 JSON 字节。

    返回：
        已确认内容一致的目标路径。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return path
        raise _ledger_error(FailureCode.LEDGER_CORRUPT, f"不可变文档内容不一致：{path}")
    atomic_write_bytes(path, payload)
    return path


atomic_write_immutable = _atomic_write_immutable


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """以临时文件、fsync 和原子替换写入字节文档。

    该 helper 允许覆盖既有文件，调用方必须自行定义覆盖语义；现有账本不可变
    文档继续通过 `_atomic_write_immutable` 施加“内容一致才能复用”的约束。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise _ledger_error(FailureCode.LEDGER_CORRUPT, f"原子写入文档失败：{path}") from error
    return path


def _event_hash(event: TrialEvent) -> str:
    """计算排除 event_hash 字段后的事件哈希。

    参数：
        event: 已补全 sequence 和 previous hash 的事件。

    返回：
        事件内容的规范化 SHA-256 哈希。
    """

    payload = event.model_dump(mode="json")
    payload.pop("event_hash", None)
    return sha256_json(payload)


HashChainEvent = TypeVar("HashChainEvent", bound=BaseModel)
HashChainTransactionResult = TypeVar("HashChainTransactionResult")


class _HashChainJsonlStore(Generic[HashChainEvent]):
    """供不同事件模型复用的单写入者规范 JSONL 哈希链。"""

    def __init__(
        self,
        path: Path,
        lock_path: Path,
        event_model: type[HashChainEvent],
    ) -> None:
        self.path = path
        self.lock_path = lock_path
        self.event_model = event_model

    @staticmethod
    def _hash(event: BaseModel) -> str:
        payload = event.model_dump(mode="json")
        payload.pop("event_hash", None)
        return sha256_json(payload)

    def verify(self) -> tuple[HashChainEvent, ...]:
        """验证完整文件、连续序号、前向哈希和事件内容哈希。"""

        if not self.path.exists():
            return ()
        content = self.path.read_bytes()
        if not content or not content.endswith(b"\n"):
            raise _ledger_error(FailureCode.LEDGER_CORRUPT, f"{self.path.name} 存在截断行")
        events: list[HashChainEvent] = []
        event_ids: set[str] = set()
        previous_hash: str | None = None
        for expected_sequence, raw_line in enumerate(content.splitlines(), start=1):
            if not raw_line:
                raise _ledger_error(
                    FailureCode.LEDGER_CORRUPT,
                    f"{self.path.name} 存在空行",
                )
            try:
                payload = json.loads(raw_line.decode("utf-8"))
                event = self.event_model.model_validate(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise _ledger_error(
                    FailureCode.LEDGER_CORRUPT,
                    f"{self.path.name} 包含非法 JSON",
                ) from error
            event_id = str(getattr(event, "event_id"))
            if event_id in event_ids:
                raise _ledger_error(FailureCode.LEDGER_CORRUPT, "事件链存在重复 event ID")
            if getattr(event, "sequence") != expected_sequence:
                raise _ledger_error(FailureCode.LEDGER_CORRUPT, "事件链 sequence 不连续")
            if getattr(event, "previous_event_hash") != previous_hash:
                raise _ledger_error(FailureCode.LEDGER_CORRUPT, "previous_event_hash 不匹配")
            event_hash = getattr(event, "event_hash")
            if event_hash is None or event_hash != self._hash(event):
                raise _ledger_error(FailureCode.LEDGER_CORRUPT, "event_hash 不匹配")
            events.append(event)
            event_ids.add(event_id)
            previous_hash = event_hash
        return tuple(events)

    def append(
        self,
        event: HashChainEvent,
        *,
        pre_append_validator: Callable[
            [tuple[HashChainEvent, ...], HashChainEvent], None
        ] | None = None,
    ) -> HashChainEvent:
        """在非阻塞文件锁内追加一个补全哈希字段的事件。"""

        def transaction(
            events: tuple[HashChainEvent, ...],
            append_locked: Callable[
                [HashChainEvent, Callable[[tuple[HashChainEvent, ...], HashChainEvent], None] | None],
                tuple[HashChainEvent, tuple[HashChainEvent, ...]],
            ],
        ) -> HashChainEvent:
            stored, _ = append_locked(event, pre_append_validator)
            return stored

        return self.with_locked_events(transaction)

    def with_locked_events(
        self,
        callback: Callable[
            [
                tuple[HashChainEvent, ...],
                Callable[
                    [HashChainEvent, Callable[[tuple[HashChainEvent, ...], HashChainEvent], None] | None],
                    tuple[HashChainEvent, tuple[HashChainEvent, ...]],
                ],
            ],
            HashChainTransactionResult,
        ],
    ) -> HashChainTransactionResult:
        """在同一把非阻塞文件锁内查看最新事件并执行受控更新。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise _ledger_error(
                    FailureCode.LEDGER_CONCURRENT_WRITER,
                    f"无法取得 {self.path.name} 单写入者锁",
                ) from error
            current_events = self.verify()

            def append_locked(
                candidate: HashChainEvent,
                validator: Callable[[tuple[HashChainEvent, ...], HashChainEvent], None] | None = None,
            ) -> tuple[HashChainEvent, tuple[HashChainEvent, ...]]:
                nonlocal current_events
                known_ids = {str(getattr(item, "event_id")) for item in current_events}
                if str(getattr(candidate, "event_id")) in known_ids:
                    raise _ledger_error(FailureCode.LEDGER_CORRUPT, "event ID 已存在")
                supersedes = getattr(candidate, "supersedes_event_id", None)
                if supersedes is not None and supersedes not in known_ids:
                    raise _ledger_error(
                        FailureCode.LEDGER_CORRUPT,
                        "supersedes_event_id 未指向已有事件",
                    )
                if validator is not None:
                    validator(current_events, candidate)
                stored = candidate.model_copy(
                    update={
                        "sequence": len(current_events) + 1,
                        "previous_event_hash": (
                            getattr(current_events[-1], "event_hash") if current_events else None
                        ),
                        "event_hash": None,
                    }
                )
                stored = stored.model_copy(update={"event_hash": self._hash(stored)})
                with self.path.open("ab") as stream:
                    stream.write(canonical_json_bytes(stored.model_dump(mode="json")))
                    stream.write(b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                current_events = (*current_events, stored)
                return stored, current_events

            return callback(current_events, append_locked)
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


HashChainJsonlStore = _HashChainJsonlStore


class JsonlLedger:
    """单机单写入者的不可变文档和追加式 JSONL 账本。"""

    def __init__(self, artifact_root: Path | LedgerPaths) -> None:
        """初始化账本目录，但不读取或写入任何真实数据。

        参数：
            artifact_root: artifact 根目录，或已经派生的 `LedgerPaths`。

        返回：
            无。目录在首次写入时创建。
        """

        self.paths = (
            artifact_root
            if isinstance(artifact_root, LedgerPaths)
            else LedgerPaths(Path(artifact_root))
        )
        self.paths.candidates_root.mkdir(parents=True, exist_ok=True)
        self.paths.campaigns_root.mkdir(parents=True, exist_ok=True)
        self.paths.policies_root.mkdir(parents=True, exist_ok=True)
        self.paths.families_root.mkdir(parents=True, exist_ok=True)
        self.paths.reference_libraries_root.mkdir(parents=True, exist_ok=True)
        self.paths.regime_research_root.mkdir(parents=True, exist_ok=True)
        self.paths.regime_deployments_root.mkdir(parents=True, exist_ok=True)
        self.paths.ledger_root.mkdir(parents=True, exist_ok=True)
        self._trial_store = _HashChainJsonlStore(
            self.paths.trials_path,
            self.paths.lock_path,
            TrialEvent,
        )

    def register_candidate(
        self,
        candidate: RegisteredCandidate
        | CandidateFactorSpec
        | RegisteredTrustedCandidate
        | TrustedCandidateFactorSpec,
    ) -> Path:
        """原子登记候选文档，并拒绝内容覆盖。

        参数：
            candidate: 已登记候选或合法候选 spec。

        返回：
            候选 JSON 文档路径。
        """

        if isinstance(candidate, (RegisteredCandidate, RegisteredTrustedCandidate)):
            record = candidate
        elif isinstance(candidate, TrustedCandidateFactorSpec):
            record = registered_trusted_candidate(candidate)
        else:
            record = registered_candidate(candidate)
        path = self.paths.candidates_root / f"{record.candidate_id}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )

    def register_campaign(
        self, campaign: CampaignSpec | TrustedVisibleCampaignSpec
    ) -> Path:
        """原子登记 campaign 文档，并拒绝内容覆盖。

        参数：
            campaign: 已冻结且通过 schema 校验的 campaign spec。

        返回：
            campaign JSON 文档路径。
        """

        identifier = (
            trusted_campaign_id(campaign)
            if isinstance(campaign, TrustedVisibleCampaignSpec)
            else campaign_id(campaign)
        )
        path = self.paths.campaigns_root / f"{identifier}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(campaign.model_dump(mode="json")),
        )

    def register_evaluation_policy(
        self,
        policy: EvaluationPolicySpec | IncrementalEvaluationPolicySpec,
    ) -> Path:
        """原子登记内容寻址的 V0.1 或 V0.2 evaluation policy。"""

        identifier = evaluation_policy_id(policy)
        path = self.paths.policies_root / f"{identifier}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(policy.model_dump(mode="json")),
        )

    def register_reference_factor_library(
        self,
        library: ReferenceFactorLibrarySpec | RegisteredReferenceFactorLibrary,
    ) -> Path:
        """原子登记内容寻址的 V0.2 参考因子库。"""

        record = (
            library
            if isinstance(library, RegisteredReferenceFactorLibrary)
            else registered_reference_factor_library(library)
        )
        path = (
            self.paths.reference_libraries_root
            / f"{record.reference_factor_library_id}.json"
        )
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )

    def register_research_family(
        self,
        family: ResearchFamilySpec | RegisteredResearchFamily,
    ) -> Path:
        """原子登记内容寻址的跨 Campaign research family。"""

        record = (
            family
            if isinstance(family, RegisteredResearchFamily)
            else registered_research_family(family)
        )
        path = self.paths.families_root / f"{record.research_family_id}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )

    def register_regime_research(
        self,
        research: RegimeResearchSpec | RegisteredRegimeResearch,
    ) -> Path:
        """原子登记内容寻址的 V0.3 市场状态研究。"""

        record = (
            research
            if isinstance(research, RegisteredRegimeResearch)
            else registered_regime_research(research)
        )
        path = self.paths.regime_research_root / f"{record.regime_research_id}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )

    def register_regime_deployment(
        self,
        deployment: RegimeDeploymentSpec | RegisteredRegimeDeployment,
    ) -> Path:
        """原子登记内容寻址的 V0.3 固定市场状态部署。"""

        record = (
            deployment
            if isinstance(deployment, RegisteredRegimeDeployment)
            else registered_regime_deployment(deployment)
        )
        path = (
            self.paths.regime_deployments_root
            / f"{record.regime_deployment_id}.json"
        )
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )

    def _verify_unlocked(self) -> list[TrialEvent]:
        """在调用方已经处理锁的情况下验证完整事件链。"""
        return list(self._trial_store.verify())

    def append_event(self, event: TrialEvent) -> TrialEvent:
        """在单写入者锁内追加一个带哈希链的事件。

        参数：
            event: 尚未由账本分配 sequence 和 event_hash 的事件。

        返回：
            已补全 sequence、previous_event_hash 和 event_hash 的事件。

        异常：
            锁被占用、事件重复、链损坏或 supersedes 目标不存在时抛出错误。
        """

        return self._trial_store.append(event)

    def read_events(self) -> list[TrialEvent]:
        """验证并读取完整事件链。

        返回：
            按 sequence 排序的事件列表。
        """

        return self._verify_unlocked()

    def verify(self) -> list[TrialEvent]:
        """验证账本 JSONL 的完整性并返回事件列表。

        返回：
            通过完整哈希链校验的事件列表。

        异常：
            损坏、截断、重复、乱序或哈希不匹配时抛出 `LEDGER_CORRUPT`。
        """

        return self._verify_unlocked()


def recover_interrupted_runs(artifact_root: Path) -> tuple[str, ...]:
    """为存在开始事件但没有运行终态的 run 追加幂等中断事件。"""

    ledger = JsonlLedger(artifact_root)
    events = ledger.verify()
    started: dict[str, TrialEvent] = {}
    terminal_run_ids: set[str] = set()
    for event in events:
        if event.run_id is None:
            continue
        if event.event_type is EventType.RUN_STARTED:
            started[event.run_id] = event
        if event.event_type in {EventType.RUN_COMPLETED, EventType.INTERRUPTED}:
            terminal_run_ids.add(event.run_id)
    recovered: list[str] = []
    for run_id, start_event in started.items():
        if run_id in terminal_run_ids:
            continue
        ledger.append_event(
            TrialEvent(
                campaign_id=start_event.campaign_id,
                run_id=run_id,
                event_type=EventType.INTERRUPTED,
                status="interrupted",
                outcome_exposed=any(
                    event.run_id == run_id and event.outcome_exposed for event in events
                ),
            )
        )
        recovered.append(run_id)
    return tuple(recovered)
