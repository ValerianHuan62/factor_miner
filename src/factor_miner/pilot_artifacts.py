"""Pilot 已发布运行的只读候选 Spec 装载。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Callable, TYPE_CHECKING

from factor_miner.barra_schema import BarraEvaluationPolicy
from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.long_only_protocol import LongOnlyResearchProtocol
from factor_miner.pilot_schema import (
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotRunRequest,
    PilotSourcePaths,
)
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.schema import EvaluationPolicySpec

if TYPE_CHECKING:
    from factor_miner.pilot_runner import FixedPilotPublication


def _artifact_error(message: str) -> FactorMinerError:
    """构造已发布候选产物合同错误。"""

    return FactorMinerError(FailureCode.PILOT_INPUT_CONTRACT_INVALID, message)


def canonical_pilot_config_hash(config_path: Path) -> str:
    """从实际配置规范 payload 重算身份，排除自引用的 config_hash 字段。"""

    try:
        payload = json.loads(config_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _artifact_error("实际 Pilot 配置无法解析") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise _artifact_error("实际 Pilot 配置缺少 request object")
    canonical_payload = dict(payload)
    request_payload = dict(payload["request"])
    request_payload.pop("config_hash", None)
    canonical_payload["request"] = request_payload
    return sha256_json(canonical_payload)


_PILOT_SOURCE_TREE_TARGETS = (
    "src",
    "dashboard",
    "pyproject.toml",
    "uv.lock",
    "migrations",
)


def resolve_pilot_code_commit(repository_root: Path) -> str:
    """从干净的真实 Git worktree 读取当前 40 位 HEAD。"""

    root = repository_root.expanduser().resolve(strict=False)
    if not (root / ".git").exists():
        raise _artifact_error("部署目录缺少真实 .git metadata")
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_PILOT_SOURCE_TREE_TARGETS,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        raise _artifact_error("无法核验 Git source tree 状态")
    if status.stdout.strip():
        raise _artifact_error("Git source tree dirty，拒绝现有运行复算")
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip() if completed.returncode == 0 else None
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise _artifact_error("实际 code_commit 必须是 40 位小写 Git SHA")
    return commit


def verify_reevaluation_request_identity(
    *,
    config_path: Path,
    request: PilotRunRequest,
    actual_code_commit: str,
) -> PilotRunRequest:
    """拒绝请求声明与当前部署代码、实际配置 payload 的任何漂移。"""

    if request.code_commit != actual_code_commit:
        raise _artifact_error("请求 code_commit 与当前部署 HEAD 不一致")
    actual_config_hash = canonical_pilot_config_hash(config_path)
    if request.config_hash != actual_config_hash:
        raise _artifact_error("请求 config_hash 与实际配置规范 payload 不一致")
    return request


def load_published_candidate_specs(
    artifact_root: Path,
    source_run_id: str,
) -> PilotFixedCandidateFile:
    """从不可变运行清单读取全部候选 Spec，并保留原 candidate_id。"""

    manifest = verify_published_run(artifact_root, source_run_id)
    run_root = (
        artifact_root.expanduser().resolve(strict=False)
        / "artifacts"
        / "runs"
        / source_run_id
    )
    candidates: list[PilotFixedCandidate] = []
    for ref in sorted(manifest.artifacts, key=lambda item: item.relative_path):
        relative = PurePosixPath(ref.relative_path)
        if (
            len(relative.parts) != 3
            or relative.parts[0] != "candidates"
            or relative.parts[2] != "spec.json"
        ):
            continue
        candidate_id = relative.parts[1]
        try:
            payload = json.loads((run_root / Path(*relative.parts)).read_text("utf-8"))
            candidates.append(
                PilotFixedCandidate(candidate_id=candidate_id, spec=payload)
            )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise _artifact_error(
                f"已发布候选 Spec 无法按冻结 Schema 读取：{candidate_id}"
            ) from error
    if not candidates:
        raise _artifact_error("来源运行没有清单引用的候选 Spec")
    return PilotFixedCandidateFile(
        version="pilot-candidates-v2",
        candidates=tuple(candidates),
    )


def reevaluate_existing_pilot_run(
    *,
    artifact_root: Path,
    source_run_id: str,
    paths: PilotSourcePaths,
    request: PilotRunRequest,
    evaluation_policy: EvaluationPolicySpec | None = None,
    barra_policy: BarraEvaluationPolicy | None = None,
    runner: Callable[..., "FixedPilotPublication"] | None = None,
) -> "FixedPilotPublication":
    """用来源运行的全部不可变 Spec 生成一个全新 long-only 运行。

    本入口只读来源清单和 Spec，不构造假设、不调用任何 LLM provider，也不
    覆盖来源目录。全部候选在一次 runner 调用中发布，从而保持单一新 run。
    """

    candidates = load_published_candidate_specs(artifact_root, source_run_id)
    if len(candidates.candidates) != 29:
        raise _artifact_error("正式现有运行复算要求来源 run 恰好包含 29 个 Spec")
    manifest = verify_published_run(artifact_root, source_run_id)
    metrics_ref = next(
        (
            item
            for item in manifest.artifacts
            if item.relative_path == "run/metrics.json"
        ),
        None,
    )
    if metrics_ref is None:
        raise _artifact_error("29 Spec 来源运行缺少 run/metrics.json")
    metrics_path = (
        artifact_root.expanduser().resolve(strict=False)
        / "artifacts"
        / "runs"
        / source_run_id
        / "run"
        / "metrics.json"
    )
    try:
        metrics = json.loads(metrics_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _artifact_error("来源运行 metrics 无法解析") from None
    if not isinstance(metrics, dict) or metrics.get("candidate_count") != 29:
        raise _artifact_error("来源运行 candidate_count 必须等于 29")
    metric_candidates = metrics.get("candidates")
    if not isinstance(metric_candidates, dict):
        raise _artifact_error("来源运行 metrics 缺少 candidates object")
    source_ids = {item.candidate_id for item in candidates.candidates}
    if set(metric_candidates) != source_ids:
        raise _artifact_error("来源运行 candidate_count、metrics 与 manifest Spec 不一致")
    protocol = LongOnlyResearchProtocol()
    frozen_request = request.model_copy(
        update={
            "visible_start": protocol.discovery_start,
            "visible_end": protocol.confirmation_end,
            "candidate_file_sha256": sha256_json(
                candidates.model_dump(mode="json")
            ),
            "artifact_root": artifact_root,
        }
    )
    if runner is None:
        from factor_miner.pilot_runner import run_fixed_pilot

        runner = run_fixed_pilot
    publication = runner(
        candidates=candidates,
        paths=paths,
        request=frozen_request,
        evaluation_policy=evaluation_policy,
        barra_policy=barra_policy,
        source_run_id=source_run_id,
        frozen_family_size=29,
    )
    if getattr(publication, "run_id", None) == source_run_id:
        raise _artifact_error("复算必须发布全新运行，不能复用来源 run_id")
    return publication
