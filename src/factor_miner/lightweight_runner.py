"""轻量研究的单入口阶段协调器。"""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.lightweight_schema import LightweightBatchManifest
from factor_miner.research_campaign_runner import LightweightCampaignEvaluationResult


class LightweightStage(str, Enum):
    """轻量运行的六个可恢复阶段。"""

    CONTEXT_READY = "context_ready"
    BATCH_FROZEN = "batch_frozen"
    EVALUATED = "evaluated"
    PUBLISHED = "published"
    PROJECTED = "projected"
    EVOLUTION_REFRESHED = "evolution_refreshed"


class LightweightRunConfig(BaseModel):
    """服务器本地轻量运行配置；密钥和 DSN 不进入本对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: Path
    hypothesis_count: int = Field(default=10, gt=0)
    candidates_per_hypothesis: int = Field(default=3, gt=0)
    allow_external_llm: bool = True


class LightweightRunSummary(BaseModel):
    """不含原始研究数据的轻量运行摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(pattern=r"^lightrun_[0-9a-f]{24}$")
    stage: LightweightStage
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_run_id: str = Field(min_length=1)


class LightweightRunDependencies(Protocol):
    """轻量协调器消费的领域阶段端口。"""

    def prepare_context(self, config: LightweightRunConfig) -> Mapping[str, object]: ...

    def freeze_batch(
        self,
        config: LightweightRunConfig,
        context: Mapping[str, object],
    ) -> LightweightBatchManifest: ...

    def evaluate(
        self,
        config: LightweightRunConfig,
        manifest: LightweightBatchManifest,
    ) -> LightweightCampaignEvaluationResult: ...

    def publish(
        self,
        config: LightweightRunConfig,
        manifest: LightweightBatchManifest,
        evaluation: LightweightCampaignEvaluationResult,
    ) -> Mapping[str, object]: ...

    def project(
        self,
        config: LightweightRunConfig,
        publication: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def refresh_evolution(
        self,
        config: LightweightRunConfig,
        publication: Mapping[str, object],
    ) -> Mapping[str, object]: ...


def _read_object(path: Path) -> dict[str, object]:
    """读取阶段对象并拒绝非 object 根节点。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"轻量阶段文件根节点不是 object：{path.name}")
    return value


def _mapping(value: Mapping[str, object], *, stage: LightweightStage) -> dict[str, object]:
    """复制阶段输出，避免依赖对象在写入后原地修改。"""

    result = dict(value)
    if not result:
        raise ValueError(f"轻量阶段 {stage.value} 输出不能为空")
    return result


def run_lightweight_research(
    config_path: Path,
    *,
    dependencies: LightweightRunDependencies,
) -> LightweightRunSummary:
    """按不可变阶段文件执行或恢复一个轻量研究批次。"""

    config = LightweightRunConfig.model_validate_json(config_path.read_bytes())
    config_identity = config.model_dump(mode="json")
    run_id = f"lightrun_{sha256_json(config_identity)[:24]}"
    run_root = config.artifact_root / "state" / "lightweight_runs" / run_id

    context_path = run_root / "01_context.json"
    if not context_path.exists():
        context = _mapping(
            dependencies.prepare_context(config),
            stage=LightweightStage.CONTEXT_READY,
        )
        _atomic_write_immutable(context_path, canonical_json_bytes(context))
    context = _read_object(context_path)

    manifest_path = run_root / "02_batch_manifest.json"
    if not manifest_path.exists():
        manifest = dependencies.freeze_batch(config, context)
        _atomic_write_immutable(
            manifest_path,
            canonical_json_bytes(manifest.model_dump(mode="json")),
        )
    manifest = LightweightBatchManifest.model_validate_json(manifest_path.read_bytes())

    evaluation_path = run_root / "03_evaluation.json"
    if not evaluation_path.exists():
        evaluation = dependencies.evaluate(config, manifest)
        if evaluation.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("轻量评价结果没有绑定当前冻结 manifest")
        _atomic_write_immutable(
            evaluation_path,
            canonical_json_bytes(evaluation.model_dump(mode="json")),
        )
    evaluation = LightweightCampaignEvaluationResult.model_validate_json(
        evaluation_path.read_bytes()
    )
    if evaluation.manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("已保存轻量评价结果没有绑定当前冻结 manifest")

    publication_path = run_root / "04_publication.json"
    if not publication_path.exists():
        publication = _mapping(
            dependencies.publish(config, manifest, evaluation),
            stage=LightweightStage.PUBLISHED,
        )
        _atomic_write_immutable(publication_path, canonical_json_bytes(publication))
    publication = _read_object(publication_path)
    published_run_id = publication.get("published_run_id")
    if not isinstance(published_run_id, str) or not published_run_id:
        raise ValueError("轻量发布摘要缺少 published_run_id")

    projection_path = run_root / "05_projection.json"
    if not projection_path.exists():
        projection = _mapping(
            dependencies.project(config, publication),
            stage=LightweightStage.PROJECTED,
        )
        _atomic_write_immutable(projection_path, canonical_json_bytes(projection))

    evolution_path = run_root / "06_evolution_refresh.json"
    if not evolution_path.exists():
        evolution = _mapping(
            dependencies.refresh_evolution(config, publication),
            stage=LightweightStage.EVOLUTION_REFRESHED,
        )
        _atomic_write_immutable(evolution_path, canonical_json_bytes(evolution))

    return LightweightRunSummary(
        run_id=run_id,
        stage=LightweightStage.EVOLUTION_REFRESHED,
        manifest_sha256=manifest.manifest_sha256,
        published_run_id=published_run_id,
    )
