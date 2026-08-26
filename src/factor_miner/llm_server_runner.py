"""公司 Linux 边界内的受控 DeepSeek 运行器。

本模块只接收服务器已经准备并批准的精确请求包。它不把 QuantLake 读取结果
返回给调用方；调用方只能得到运行状态、槽位数量和内容哈希。覆盖图谱的读取、
脱敏和请求包构造由服务器固定编排器负责，不能由 Codex 拼接 prompt。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import platform
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.llm_brief import LLMCoverageBrief
from factor_miner.llm_online import (
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
)
from factor_miner.llm_privacy import CorporateExternalResearchPolicy
from factor_miner.llm_provider import (
    DeepSeekTransport,
    UrllibDeepSeekTransport,
    execute_recorded_call,
)


ServerLLMMode = Literal[
    "public_capability_only",
    "sanitized_coverage_brief",
]


class ServerRunSummary(BaseModel):
    """允许离开服务器进程的最小运行摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(pattern=r"^llmrun_[0-9a-f]{24}$")
    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    mode: ServerLLMMode
    status: Literal["completed", "failed"]
    request_hashes: tuple[str, ...] = Field(min_length=1)
    terminal_slot_count: int = Field(ge=0)
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_code: FailureCode | None = None


def _runner_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造不含请求正文、响应正文和路径细节的稳定错误。"""

    return FactorMinerError(code, message)


def _bundle_root(artifact_root: Path, family_id: str) -> Path:
    """返回固定的服务器请求包目录。"""

    root = artifact_root.expanduser().resolve(strict=False)
    return root / "state" / "llm_server_runs" / family_id


def _read_model(
    path: Path,
    model: type[BaseModel],
    *,
    failure_code: FailureCode = FailureCode.LEDGER_CORRUPT,
) -> BaseModel:
    """从固定服务器路径读取严格 JSON 模型。"""

    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        raise _runner_error(
            failure_code,
            "服务器 LLM 请求包缺失或 Schema 无效",
        ) from None


def _validate_sanitized_payload(request: PreparedDeepSeekRequest) -> None:
    """要求覆盖摘要通过专用模型，防止把任意内部图谱冒充脱敏 brief。"""

    payload = request.export_payload
    if payload.get("information_class") != "sanitized_coverage_brief":
        raise _runner_error(
            FailureCode.LLM_POLICY_NOT_AUTHORIZED,
            "脱敏模式的 information_class 不匹配",
        )
    brief_payload = payload.get("brief", payload)
    if not isinstance(brief_payload, dict):
        raise _runner_error(
            FailureCode.LLM_PRIVACY_VIOLATION,
            "脱敏模式缺少结构化 coverage brief",
        )
    try:
        LLMCoverageBrief.model_validate(brief_payload)
    except ValueError:
        raise _runner_error(
            FailureCode.LLM_PRIVACY_VIOLATION,
            "coverage brief 未通过固定脱敏 Schema",
        ) from None


def _validate_mode_payload(
    request: PreparedDeepSeekRequest,
    mode: ServerLLMMode,
) -> None:
    """把服务器运行模式绑定到外发 payload 的信息类别。"""

    if mode == "public_capability_only":
        if request.export_payload.get("information_class") != mode:
            raise _runner_error(
                FailureCode.LLM_POLICY_NOT_AUTHORIZED,
                "公共能力模式的 information_class 不匹配",
            )
        return
    _validate_sanitized_payload(request)


def _artifact_manifest(root: Path) -> tuple[dict[str, object], str]:
    """只记录相对路径和文件哈希，构造服务器产物清单身份。"""

    files: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "run_summary.json":
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest: dict[str, object] = {"version": "server-run-v1", "files": files}
    return manifest, sha256_json(manifest)


def _run_id(
    family_id: str,
    mode: ServerLLMMode,
    request: PreparedDeepSeekRequest,
    policy: CorporateExternalResearchPolicy,
    authorization: LLMExportAuthorization,
) -> str:
    """由研究族、模式和精确授权内容寻址运行。"""

    identity = {
        "family_id": family_id,
        "mode": mode,
        "request_sha256": request.request_sha256,
        "policy_id": policy.policy_id,
        "authorization_id": authorization.authorization_id,
    }
    return f"llmrun_{sha256_json(identity)[:24]}"


def _write_run_manifest(
    artifact_root: Path,
    summary: ServerRunSummary,
    manifest: dict[str, object],
) -> None:
    """把不含原始 prompt/response 的运行清单发布到标准产物目录。"""

    path = artifact_root / "artifacts" / "runs" / summary.run_id / "run_manifest.json"
    payload = {
        "run_id": summary.run_id,
        "family_id": summary.family_id,
        "mode": summary.mode,
        "status": summary.status,
        "request_hashes": summary.request_hashes,
        "terminal_slot_count": summary.terminal_slot_count,
        "artifact_manifest_sha256": summary.artifact_manifest_sha256,
        "failure_code": summary.failure_code,
        "files": manifest["files"],
    }
    _atomic_write_immutable(path, canonical_json_bytes(payload))


def run_server_side_llm_pipeline(
    family_id: str,
    mode: ServerLLMMode,
    artifact_root: Path,
    *,
    transport: DeepSeekTransport | None = None,
) -> ServerRunSummary:
    """在公司 Linux 服务器执行一个已授权的 DeepSeek 请求包。

    请求包由固定服务器编排器预先写入：

    ``state/llm_server_runs/{family_id}/approved_request.json``
    ``state/llm_server_runs/{family_id}/corporate_policy.json``
    ``state/llm_server_runs/{family_id}/export_authorization.json``

    本函数不接受 prompt、原始数据或 API key 参数，也不在返回值中携带模型正文。
    """

    if platform.system() != "Linux":
        raise _runner_error(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "真实 DeepSeek 运行只允许在公司 Linux",
        )
    if not family_id.startswith("llmfamily_"):
        raise _runner_error(
            FailureCode.SPEC_SCHEMA_INVALID,
            "family_id 格式无效",
        )

    artifact_root = artifact_root.expanduser().resolve(strict=False)
    bundle_root = _bundle_root(artifact_root, family_id)
    request = _read_model(
        bundle_root / "approved_request.json",
        PreparedDeepSeekRequest,
    )
    policy = _read_model(
        bundle_root / "corporate_policy.json",
        CorporateExternalResearchPolicy,
        failure_code=FailureCode.LLM_POLICY_NOT_AUTHORIZED,
    )
    authorization = _read_model(
        bundle_root / "export_authorization.json",
        LLMExportAuthorization,
        failure_code=FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
    )
    assert isinstance(request, PreparedDeepSeekRequest)
    assert isinstance(policy, CorporateExternalResearchPolicy)
    assert isinstance(authorization, LLMExportAuthorization)
    _validate_mode_payload(request, mode)
    run_id = _run_id(family_id, mode, request, policy, authorization)
    run_root = artifact_root / "artifacts" / "runs" / run_id
    summary_path = bundle_root / "run_summary.json"
    if summary_path.is_file():
        try:
            return ServerRunSummary.model_validate_json(summary_path.read_bytes())
        except ValueError:
            raise _runner_error(
                FailureCode.LEDGER_CORRUPT,
                "服务器 LLM 运行摘要损坏",
            ) from None

    try:
        response = execute_recorded_call(
            prepared=request,
            authorization=authorization,
            policy=policy,
            record_root=run_root,
            transport=transport or UrllibDeepSeekTransport(),
            now=datetime.now(timezone.utc),
        )
    except FactorMinerError as error:
        if error.code not in {
            FailureCode.LLM_PROVIDER_UNAVAILABLE,
            FailureCode.LLM_RESPONSE_INVALID,
        }:
            raise
        status = "failed"
        failure_code: FailureCode | None = error.code
    else:
        status = "completed"
        failure_code = None

    manifest, manifest_hash = _artifact_manifest(run_root)
    summary = ServerRunSummary(
        run_id=run_id,
        family_id=family_id,
        mode=mode,
        status=status,
        request_hashes=(request.request_sha256,),
        terminal_slot_count=len(request.slot_ids),
        artifact_manifest_sha256=manifest_hash,
        failure_code=failure_code,
    )
    _write_run_manifest(artifact_root, summary, manifest)
    _atomic_write_immutable(
        summary_path,
        canonical_json_bytes(summary.model_dump(mode="json")),
    )
    return summary
