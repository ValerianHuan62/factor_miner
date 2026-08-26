"""组合、IC 和 Barra 结果的原子发布与哈希核验。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import _atomic_write_immutable


class PublishedArtifactRef(BaseModel):
    """一个已发布文件的相对路径和内容哈希。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class PublishedRunManifest(BaseModel):
    """已完整发布运行的最小清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: str = "portfolio-run-v1"
    run_id: str = Field(pattern=r"^run_[0-9a-f]{24}$")
    status: str = "published"
    artifacts: tuple[PublishedArtifactRef, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _artifact_error(message: str) -> FactorMinerError:
    """构造产物发布错误。"""

    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)


def _validate_run_id(run_id: str) -> None:
    """拒绝把任意路径作为运行身份。"""

    if len(run_id) != len("run_") + 24 or not run_id.startswith("run_"):
        raise _artifact_error("run_id 格式无效")
    if any(character not in "0123456789abcdef" for character in run_id[4:]):
        raise _artifact_error("run_id 含有非法字符")


def _validate_relative_path(relative_path: str) -> None:
    """拒绝路径穿越、临时文件和禁止的结论命名。"""

    path = Path(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or ".." in path.parts
        or relative_path.endswith("/")
        or "evidence" in relative_path.casefold()
        or "conclusion" in relative_path.casefold()
    ):
        raise _artifact_error("运行产物路径越界或使用了禁止的产物名称")


def _manifest_hash(payload: dict[str, object]) -> str:
    """对不含自身哈希的清单内容做规范哈希。"""

    return sha256_json(payload)


def publish_run_artifacts(
    artifact_root: Path,
    run_id: str,
    artifacts: dict[str, bytes],
) -> PublishedRunManifest:
    """原子发布一组完整运行产物。

    调用方必须在本函数前完成组合、IC 和 Barra 的所有校验；本函数只负责
    路径、哈希、不可变写入和发布顺序，不会把失败结果伪装成成功。
    """

    _validate_run_id(run_id)
    if not artifacts:
        raise _artifact_error("运行产物不能为空")
    for relative_path in artifacts:
        _validate_relative_path(relative_path)
    root = artifact_root.expanduser().resolve(strict=False)
    runs_root = root / "artifacts" / "runs"
    final_root = runs_root / run_id
    manifest_path = final_root / "run_manifest.json"
    refs = tuple(
        PublishedArtifactRef(
            relative_path=relative_path,
            sha256=sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        for relative_path, payload in sorted(artifacts.items())
    )
    manifest_payload: dict[str, object] = {
        "manifest_version": "portfolio-run-v1",
        "run_id": run_id,
        "status": "published",
        "artifacts": [item.model_dump(mode="json") for item in refs],
    }
    manifest = PublishedRunManifest(
        **manifest_payload,
        manifest_sha256=_manifest_hash(manifest_payload),
    )
    if final_root.exists():
        if not manifest_path.is_file():
            raise _artifact_error("已有运行目录但缺少完整 run_manifest")
        try:
            existing = PublishedRunManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, ValueError):
            raise _artifact_error("已有 run_manifest 无法核验") from None
        if existing != manifest:
            raise _artifact_error("同一 run_id 的不可变产物内容不一致")
        verify_published_run(artifact_root, run_id)
        return existing

    runs_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=runs_root))
    try:
        for relative_path, payload in artifacts.items():
            _atomic_write_immutable(staging / relative_path, payload)
        _atomic_write_immutable(
            staging / "run_manifest.json",
            canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        staging.rename(final_root)
    except Exception:
        # 保留暂存字节供中断审计和人工恢复；失败不能静默删除运行产物。
        raise
    return manifest


def verify_published_run(artifact_root: Path, run_id: str) -> PublishedRunManifest:
    """核验已发布运行清单和每个产物文件的实际哈希。"""

    _validate_run_id(run_id)
    root = artifact_root.expanduser().resolve(strict=False) / "artifacts" / "runs" / run_id
    try:
        manifest = PublishedRunManifest.model_validate_json(
            (root / "run_manifest.json").read_bytes()
        )
    except (OSError, ValueError):
        raise _artifact_error("运行清单缺失或 Schema 无效") from None
    payload = manifest.model_dump(mode="json")
    expected = dict(payload)
    expected.pop("manifest_sha256")
    if manifest.manifest_sha256 != _manifest_hash(expected):
        raise _artifact_error("运行清单自身哈希不一致")
    for item in manifest.artifacts:
        path = root / item.relative_path
        try:
            content = path.read_bytes()
        except OSError:
            raise _artifact_error("运行清单引用的产物缺失") from None
        if len(content) != item.size_bytes or sha256(content).hexdigest() != item.sha256:
            raise _artifact_error("运行产物内容哈希不一致")
    return manifest
