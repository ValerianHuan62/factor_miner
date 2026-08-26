"""Dashboard 自主研究的可恢复单机 Worker。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict

from factor_miner.autonomous_schema import (
    AutonomousResearchState,
    AutonomousStage,
    ResearchCommand,
    ResearchCommandType,
)
from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.lightweight_expressions import LightweightExpressionBatch
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightHypothesisDecision,
    LightweightReviewBatch,
    freeze_lightweight_review,
)
from factor_miner.lightweight_schema import LightweightBatchManifest
from factor_miner.research_campaign_runner import LightweightCampaignEvaluationResult
from factor_miner.research_control import ResearchControlStore


class ResearchWorkerConfig(BaseModel):
    """单机 Worker 的本地产物配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: Path


class ResearchWorkerDependencies(Protocol):
    """Worker 唯一允许调用的阶段端口。"""

    def prepare_context(self, state: AutonomousResearchState) -> Mapping[str, object]: ...

    def generate_hypotheses(
        self,
        state: AutonomousResearchState,
        context: Mapping[str, object],
    ) -> LightweightHypothesisBatch: ...

    def generate_expressions(
        self,
        hypotheses: LightweightHypothesisBatch,
        review: LightweightReviewBatch,
    ) -> LightweightExpressionBatch: ...

    def freeze_manifest(
        self,
        context: Mapping[str, object],
        expressions: LightweightExpressionBatch,
    ) -> LightweightBatchManifest: ...

    def evaluate(
        self,
        manifest: LightweightBatchManifest,
    ) -> LightweightCampaignEvaluationResult: ...

    def publish(
        self,
        manifest: LightweightBatchManifest,
        evaluation: LightweightCampaignEvaluationResult,
    ) -> Mapping[str, object]: ...

    def project_control_state(self, state: AutonomousResearchState) -> None: ...

    def project(self, publication: Mapping[str, object]) -> Mapping[str, object]: ...

    def refresh_evolution(
        self,
        publication: Mapping[str, object],
        context: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def refresh_rejected_hypotheses(
        self,
        hypotheses: LightweightHypothesisBatch,
        review: LightweightReviewBatch,
        context: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class WorkerTickResult(BaseModel):
    """一次轮询的可观察结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    run_id: str | None = None
    stage: AutonomousStage | None = None


def _object(value: Mapping[str, object], *, label: str) -> dict[str, object]:
    result = dict(value)
    if not result:
        raise ValueError(f"{label}不能为空")
    return result


class ResearchWorker:
    """以不可变阶段对象推进一个本地自主研究批次。"""

    def __init__(
        self,
        config: ResearchWorkerConfig,
        *,
        dependencies: ResearchWorkerDependencies,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.dependencies = dependencies
        self.store = ResearchControlStore(config.artifact_root)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def process_once(self) -> WorkerTickResult:
        """推进一个可恢复阶段；等待审批时不调用任何模型。"""

        state = self.store.active_state()
        if state is None:
            resumed = self._consume_resume()
            if resumed is not None:
                state = resumed
            else:
                command = next(
                    (
                        item
                        for item in self.store.pending_commands()
                        if item.command_type is ResearchCommandType.START
                    ),
                    None,
                )
                if command is None:
                    return WorkerTickResult(action="空闲")
                state = self._start(command)

        try:
            self._ensure_control_projection(state)
            if state.stage is AutonomousStage.CONTEXT_PREPARING:
                return self._prepare_and_generate(state)
            if state.stage is AutonomousStage.HYPOTHESIS_GENERATING:
                return self._retry_hypothesis_generation(state)
            if state.stage is AutonomousStage.AWAITING_REVIEW:
                return self._process_review(state)
            if state.stage is AutonomousStage.EXPRESSION_GENERATING:
                return self._generate_and_freeze(state)
            if state.stage is AutonomousStage.MANIFEST_FROZEN:
                return self._advance(state, AutonomousStage.EVALUATING, action="开始评价")
            if state.stage is AutonomousStage.EVALUATING:
                return self._evaluate_and_publish(state)
            if state.stage is AutonomousStage.PUBLISHED:
                return self._project_publication(state)
            if state.stage is AutonomousStage.PROJECTED:
                return self._refresh_evolution(state)
            if state.stage is AutonomousStage.EVOLUTION_REFRESHED:
                return self._advance(state, AutonomousStage.COMPLETED, action="运行完成")
            return WorkerTickResult(action="等待", run_id=state.run_id, stage=state.stage)
        except Exception as error:
            failed = self._fail(self.store.load_state(state.run_id), error)
            return WorkerTickResult(action="运行失败", run_id=failed.run_id, stage=failed.stage)

    def _start(self, command: ResearchCommand) -> AutonomousResearchState:
        run_id = f"autrun_{sha256_json({'command_id': command.command_id})[:24]}"
        state = AutonomousResearchState.build(
            run_id=run_id,
            sequence=0,
            stage=AutonomousStage.CONTEXT_PREPARING,
            created_at=command.requested_at,
            updated_at=command.requested_at,
            command_ids=(command.command_id,),
            stage_refs={"启动命令": command.command_sha256},
        )
        self.store.publish_state(state)
        self.store.mark_processed(command.command_id, processed_at=self._now())
        return state

    def _run_root(self, run_id: str) -> Path:
        return self.store.runs_root / run_id / "objects"

    def _write_mapping(self, run_id: str, name: str, value: Mapping[str, object]) -> str:
        payload = _object(value, label=name)
        _atomic_write_immutable(
            self._run_root(run_id) / f"{name}.json",
            canonical_json_bytes(payload),
        )
        return sha256_json(payload)

    def _write_model(self, run_id: str, name: str, value: BaseModel) -> str:
        payload = value.model_dump(mode="json")
        _atomic_write_immutable(
            self._run_root(run_id) / f"{name}.json",
            canonical_json_bytes(payload),
        )
        return sha256_json(payload)

    def _read_mapping(self, run_id: str, name: str) -> dict[str, object]:
        value = json.loads((self._run_root(run_id) / f"{name}.json").read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"阶段对象 {name} 损坏")
        return value

    def _read_model(self, run_id: str, name: str, model: type[BaseModel]) -> BaseModel:
        return model.model_validate_json(
            (self._run_root(run_id) / f"{name}.json").read_bytes()
        )

    def _next(
        self,
        state: AutonomousResearchState,
        stage: AutonomousStage,
        *,
        refs: Mapping[str, str] | None = None,
        command_ids: tuple[str, ...] | None = None,
        last_error: str | None = None,
    ) -> AutonomousResearchState:
        result = AutonomousResearchState.build(
            run_id=state.run_id,
            sequence=state.sequence + 1,
            stage=stage,
            created_at=state.created_at,
            updated_at=self._now(),
            command_ids=command_ids or state.command_ids,
            stage_refs={**state.stage_refs, **dict(refs or {})},
            last_error=last_error,
        )
        self.store.publish_state(result)
        return result

    def _advance(
        self,
        state: AutonomousResearchState,
        stage: AutonomousStage,
        *,
        action: str,
    ) -> WorkerTickResult:
        next_state = self._next(state, stage)
        if next_state.stage.terminal:
            self.dependencies.project_control_state(next_state)
        return WorkerTickResult(action=action, run_id=state.run_id, stage=next_state.stage)

    def _ensure_control_projection(self, state: AutonomousResearchState) -> None:
        marker = self._run_root(state.run_id) / "control_projection" / f"{state.state_sha256}.json"
        # 每个 tick 都刷新 PostgreSQL 心跳；状态 marker 只证明该快照至少成功投影过一次。
        self.dependencies.project_control_state(state)
        if not marker.is_file():
            _atomic_write_immutable(
                marker,
                canonical_json_bytes({"state_sha256": state.state_sha256}),
            )

    def _prepare_and_generate(self, state: AutonomousResearchState) -> WorkerTickResult:
        context = _object(self.dependencies.prepare_context(state), label="研究上下文")
        context_hash = self._write_mapping(state.run_id, "context", context)
        generating = self._next(
            state,
            AutonomousStage.HYPOTHESIS_GENERATING,
            refs={"研究上下文": context_hash},
        )
        hypotheses = self.dependencies.generate_hypotheses(generating, context)
        hypothesis_hash = self._write_model(state.run_id, "hypotheses", hypotheses)
        waiting = self._next(
            generating,
            AutonomousStage.AWAITING_REVIEW,
            refs={"假设批次": hypothesis_hash},
        )
        return WorkerTickResult(action="等待审批", run_id=state.run_id, stage=waiting.stage)

    def _retry_hypothesis_generation(
        self,
        state: AutonomousResearchState,
    ) -> WorkerTickResult:
        """复用已经冻结的上下文，重试失败的假设生成阶段。"""

        context = self._read_mapping(state.run_id, "context")
        hypotheses = self.dependencies.generate_hypotheses(state, context)
        hypothesis_hash = self._write_model(state.run_id, "hypotheses", hypotheses)
        waiting = self._next(
            state,
            AutonomousStage.AWAITING_REVIEW,
            refs={"假设批次": hypothesis_hash},
        )
        return WorkerTickResult(
            action="等待审批",
            run_id=state.run_id,
            stage=waiting.stage,
        )

    def _process_review(self, state: AutonomousResearchState) -> WorkerTickResult:
        pending = tuple(
            item
            for item in self.store.pending_commands()
            if item.target_run_id == state.run_id
            and item.command_type in {
                ResearchCommandType.REVIEW_DECISION,
                ResearchCommandType.FREEZE_REVIEW,
            }
        )
        decisions_root = self._run_root(state.run_id) / "decisions"
        freeze_command: ResearchCommand | None = None
        command_ids = list(state.command_ids)
        for command in pending:
            if command.command_type is ResearchCommandType.REVIEW_DECISION:
                decision = LightweightHypothesisDecision.model_validate(command.body)
                if decision.run_id != state.run_id:
                    raise ValueError("审批决定绑定了错误的研究批次")
                _atomic_write_immutable(
                    decisions_root / f"{decision.logical_slot_id}.json",
                    canonical_json_bytes(decision.model_dump(mode="json")),
                )
                self.store.mark_processed(command.command_id, processed_at=self._now())
                command_ids.append(command.command_id)
            else:
                freeze_command = command
        if freeze_command is None:
            return WorkerTickResult(action="等待审批", run_id=state.run_id, stage=state.stage)
        paths = sorted(decisions_root.glob("H*.json")) if decisions_root.is_dir() else []
        if len(paths) != 10:
            return WorkerTickResult(action="审批尚未完成", run_id=state.run_id, stage=state.stage)
        hypotheses = self._read_model(
            state.run_id, "hypotheses", LightweightHypothesisBatch
        )
        assert isinstance(hypotheses, LightweightHypothesisBatch)
        decisions = tuple(
            LightweightHypothesisDecision.model_validate_json(path.read_bytes())
            for path in paths
        )
        review = freeze_lightweight_review(hypotheses, decisions)
        review_hash = self._write_model(state.run_id, "review", review)
        self.store.mark_processed(freeze_command.command_id, processed_at=self._now())
        command_ids.append(freeze_command.command_id)
        frozen = self._next(
            state,
            AutonomousStage.REVIEW_FROZEN,
            refs={"冻结审批": review_hash},
            command_ids=tuple(command_ids),
        )
        if review.approved_hypothesis_count == 0:
            context = self._read_mapping(state.run_id, "context")
            refresh = _object(
                self.dependencies.refresh_rejected_hypotheses(
                    hypotheses, review, context
                ),
                label="全拒绝假设记忆刷新",
            )
            refresh_hash = self._write_mapping(
                state.run_id, "rejected_hypothesis_refresh", refresh
            )
            terminal = self._next(
                frozen,
                AutonomousStage.NO_APPROVED_HYPOTHESIS,
                refs={"全拒绝假设记忆": refresh_hash},
            )
            return WorkerTickResult(action="全部假设已拒绝", run_id=state.run_id, stage=terminal.stage)
        expression = self._next(frozen, AutonomousStage.EXPRESSION_GENERATING)
        return WorkerTickResult(action="审批已冻结", run_id=state.run_id, stage=expression.stage)

    def _generate_and_freeze(self, state: AutonomousResearchState) -> WorkerTickResult:
        context = self._read_mapping(state.run_id, "context")
        hypotheses = self._read_model(state.run_id, "hypotheses", LightweightHypothesisBatch)
        review = self._read_model(state.run_id, "review", LightweightReviewBatch)
        assert isinstance(hypotheses, LightweightHypothesisBatch)
        assert isinstance(review, LightweightReviewBatch)
        expression_path = self._run_root(state.run_id) / "expressions.json"
        if expression_path.is_file():
            expressions = self._read_model(
                state.run_id, "expressions", LightweightExpressionBatch
            )
            assert isinstance(expressions, LightweightExpressionBatch)
        else:
            expressions = self.dependencies.generate_expressions(hypotheses, review)
            if expressions.ready_slot_count == 0:
                raise ValueError(
                    "表达式生成失败：没有任何候选通过合法性、复杂度与结构差异检查"
                )
            self._write_model(state.run_id, "expressions", expressions)
        if expressions.review_sha256 != review.review_sha256:
            raise ValueError("表达式批次没有绑定当前冻结审批")
        expression_hash = sha256_json(expressions.model_dump(mode="json"))
        manifest_path = self._run_root(state.run_id) / "manifest.json"
        if manifest_path.is_file():
            manifest = self._read_model(
                state.run_id, "manifest", LightweightBatchManifest
            )
            assert isinstance(manifest, LightweightBatchManifest)
        else:
            manifest = self.dependencies.freeze_manifest(context, expressions)
        if manifest.family_size != review.candidate_family_size:
            raise ValueError("冻结 manifest 与审批确定的候选族规模不一致")
        manifest_hash = sha256_json(manifest.model_dump(mode="json"))
        if not manifest_path.is_file():
            self._write_model(state.run_id, "manifest", manifest)
        frozen = self._next(
            state,
            AutonomousStage.MANIFEST_FROZEN,
            refs={"表达式批次": expression_hash, "冻结清单": manifest_hash},
        )
        return WorkerTickResult(action="候选已登记", run_id=state.run_id, stage=frozen.stage)

    def _evaluate_and_publish(self, state: AutonomousResearchState) -> WorkerTickResult:
        manifest = self._read_model(state.run_id, "manifest", LightweightBatchManifest)
        assert isinstance(manifest, LightweightBatchManifest)
        evaluation = self.dependencies.evaluate(manifest)
        if evaluation.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("评价结果没有绑定当前冻结 manifest")
        evaluation_hash = self._write_model(state.run_id, "evaluation", evaluation)
        publication = _object(
            self.dependencies.publish(manifest, evaluation), label="发布结果"
        )
        publication_hash = self._write_mapping(state.run_id, "publication", publication)
        published = self._next(
            state,
            AutonomousStage.PUBLISHED,
            refs={"评价结果": evaluation_hash, "发布结果": publication_hash},
        )
        return WorkerTickResult(action="产物已发布", run_id=state.run_id, stage=published.stage)

    def _project_publication(self, state: AutonomousResearchState) -> WorkerTickResult:
        publication = self._read_mapping(state.run_id, "publication")
        projection = _object(self.dependencies.project(publication), label="数据库投影")
        projection_hash = self._write_mapping(state.run_id, "projection", projection)
        projected = self._next(
            state,
            AutonomousStage.PROJECTED,
            refs={"数据库投影": projection_hash},
        )
        return WorkerTickResult(action="数据库已投影", run_id=state.run_id, stage=projected.stage)

    def _refresh_evolution(self, state: AutonomousResearchState) -> WorkerTickResult:
        publication = self._read_mapping(state.run_id, "publication")
        context = self._read_mapping(state.run_id, "context")
        refresh = _object(
            self.dependencies.refresh_evolution(publication, context),
            label="记忆与图谱刷新",
        )
        refresh_hash = self._write_mapping(state.run_id, "evolution_refresh", refresh)
        refreshed = self._next(
            state,
            AutonomousStage.EVOLUTION_REFRESHED,
            refs={"进化刷新": refresh_hash},
        )
        return WorkerTickResult(action="记忆与图谱已刷新", run_id=state.run_id, stage=refreshed.stage)

    def _fail(self, state: AutonomousResearchState, error: Exception) -> AutonomousResearchState:
        failure = {
            "previous_stage": state.stage.value,
            "previous_state_sha256": state.state_sha256,
            "message": f"阶段执行失败：{error}",
        }
        failure_hash = self._write_mapping(state.run_id, f"failure_{state.sequence:020d}", failure)
        return self._next(
            state,
            AutonomousStage.FAILED,
            refs={"失败记录": failure_hash},
            last_error=str(failure["message"]),
        )

    def _consume_resume(self) -> AutonomousResearchState | None:
        command = next(
            (
                item
                for item in self.store.pending_commands()
                if item.command_type is ResearchCommandType.RESUME
            ),
            None,
        )
        if command is None or command.target_run_id is None:
            return None
        failed = self.store.load_state(command.target_run_id)
        if failed.stage is not AutonomousStage.FAILED:
            raise ValueError("继续命令只能用于失败的研究批次")
        failure_path = sorted(self._run_root(failed.run_id).glob("failure_*.json"))[-1]
        failure = json.loads(failure_path.read_text("utf-8"))
        previous_stage = AutonomousStage(str(failure["previous_stage"]))
        self.store.mark_processed(command.command_id, processed_at=self._now())
        return self._next(
            failed,
            previous_stage,
            command_ids=failed.command_ids + (command.command_id,),
            last_error=None,
        )
