"""运行模式、服务器边界和数据 provenance 配置。"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Mapping
import platform as platform_module

from factor_miner.canonical import canonical_json_bytes
from factor_miner.errors import FactorMinerError, FailureCode


class ExecutionMode(StrEnum):
    """因子研究引擎支持的运行模式。"""

    DESIGN = "design"
    SYNTHETIC = "synthetic"
    SMOKE = "smoke"
    VISIBLE = "visible"


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """不可变的运行时配置及其解析后的路径。

    参数：
        mode: 当前执行模式。
        platform_name: 当前运行平台名称。
        artifact_root: 独立 artifact 根目录。
        quantlake_root: QuantLake 根目录；非真实模式可以为空。
        market_uri: 行情数据路径；非真实模式可以为空。
        state_uri: 状态数据路径；非真实模式可以为空。
        label_uri: 标签数据路径；非真实模式可以为空。
        data_origin: 数据来源标识。
        resolved_release_id: 已解析的数据 release 标识。
        release_manifest_sha256: release manifest 哈希。
        schema_version: 数据 schema 版本。
        market_cutoff: 行情数据截止日期。
        adjustment_convention: 复权口径。
        calendar_version: 交易日历版本。
        state_table_version: 状态表版本。
        state_table_cutoff: 状态表截止日期。
        environment: 全部 FM_ 环境变量的只读映射。

    返回：
        无。该类用于承载已经完成边界校验的配置。
    """

    mode: ExecutionMode
    platform_name: str
    artifact_root: Path
    quantlake_root: Path | None
    market_uri: Path | None
    state_uri: Path | None
    label_uri: Path | None
    data_origin: str | None
    resolved_release_id: str | None
    release_manifest_sha256: str | None
    schema_version: str | None
    market_cutoff: str | None
    adjustment_convention: str | None
    calendar_version: str | None
    state_table_version: str | None
    state_table_cutoff: str | None
    environment: Mapping[str, str]
    release_manifest_path: Path | None = None
    config_path: Path | None = None
    config_hash: str | None = None
    code_commit: str | None = None
    smoke_input_root: Path | None = None
    derived_release_root: Path | None = None

    @property
    def state_root(self) -> Path:
        """返回由 artifact root 派生的状态目录。"""

        return self.artifact_root / "state"

    @property
    def runs_root(self) -> Path:
        """返回由 artifact root 派生的运行产物目录。"""

        return self.artifact_root / "artifacts" / "runs"

    @property
    def research_memory_root(self) -> Path:
        """返回显式研究记忆目录。

        研究记忆只能落在独立 artifact root 下，禁止通过当前工作目录或仓库
        根目录做隐式解析。
        """

        return resolve_research_memory_root(self)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """经过实际文件与 Git 状态核验的运行身份。"""

    release_manifest_sha256: str
    config_hash: str
    code_commit: str
    uv_lock_sha256: str


def resolve_research_memory_root(
    profile_or_artifact_root: RuntimeProfile | Path,
) -> Path:
    """显式解析 research memory 根目录。

    参数：
        profile_or_artifact_root: 运行时配置或已验证的 artifact 根目录。

    返回：
        `<artifact_root>/research_memory` 的规范绝对路径。
    """

    artifact_root = (
        profile_or_artifact_root.artifact_root
        if isinstance(profile_or_artifact_root, RuntimeProfile)
        else Path(profile_or_artifact_root)
    )
    resolved = artifact_root.expanduser().resolve(strict=False)
    return (resolved / "research_memory").resolve(strict=False)


_REAL_MODES = frozenset({ExecutionMode.SMOKE, ExecutionMode.VISIBLE})
_DATA_URI_KEYS = ("FM_MARKET_URI", "FM_STATE_URI", "FM_LABEL_URI")
_PROVENANCE_KEYS = (
    "FM_DATA_ORIGIN",
    "FM_RESOLVED_RELEASE_ID",
    "FM_RELEASE_MANIFEST_SHA256",
    "FM_SCHEMA_VERSION",
    "FM_MARKET_CUTOFF",
    "FM_ADJUSTMENT_CONVENTION",
    "FM_CALENDAR_VERSION",
    "FM_STATE_TABLE_VERSION",
    "FM_STATE_TABLE_CUTOFF",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _boundary_error(message: str) -> FactorMinerError:
    """构造统一的运行时边界错误。

    参数：
        message: 具体失败说明。

    返回：
        带有运行时边界错误代码的异常对象。
    """

    return FactorMinerError(FailureCode.RUNTIME_BOUNDARY_ERROR, message)


def _read_value(environment: Mapping[str, str], key: str) -> str:
    """读取非空环境变量。

    参数：
        environment: 环境变量映射。
        key: 要读取的变量名。

    返回：
        去除首尾空白后的变量值。

    异常：
        缺失或为空时抛出运行时边界错误。
    """

    value = str(environment.get(key, "")).strip()
    if not value:
        raise _boundary_error(f"缺少必需环境变量：{key}")
    return value


def _resolve_absolute_path(value: str, key: str) -> Path:
    """将路径解析为绝对路径，并拒绝隐式相对路径。

    参数：
        value: 原始路径文本。
        key: 对应环境变量名。

    返回：
        不要求目标已存在的规范化绝对路径。
    """

    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _boundary_error(f"环境变量 {key} 必须是绝对路径")
    return path.resolve(strict=False)


def _absolute_path_without_symlink_resolution(value: str, key: str) -> Path:
    """解析绝对路径但保留用户配置的软链接文本。"""

    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _boundary_error(f"环境变量 {key} 必须是绝对路径")
    return path.absolute()


def _optional_path(environment: Mapping[str, str], key: str) -> Path | None:
    """解析可选路径，空值保持为空而不生成默认路径。

    参数：
        environment: 环境变量映射。
        key: 对应环境变量名。

    返回：
        解析后的路径，或表示未配置的 None。
    """

    value = str(environment.get(key, "")).strip()
    if not value:
        return None
    return _resolve_absolute_path(value, key)


def _is_within(path: Path, root: Path) -> bool:
    """判断路径是否等于或位于指定根目录内。

    参数：
        path: 待检查路径。
        root: 根目录。

    返回：
        如果 path 等于 root 或位于 root 内则返回 True。
    """

    return path == root or root in path.parents


def _repository_roots() -> tuple[Path, ...]:
    """返回需要排除的当前仓库路径集合。

    返回：
        当前工作目录和 Python 包所属项目根目录。
    """

    package_root = Path(__file__).resolve().parents[2]
    current_root = Path.cwd().resolve()
    if package_root == current_root:
        return (package_root,)
    return package_root, current_root


def _validate_real_paths(
    mode: ExecutionMode,
    quantlake_root: Path | None,
    artifact_root: Path,
    data_paths: Mapping[str, Path | None],
    smoke_input_root: Path,
    derived_release_root: Path | None,
) -> None:
    """校验真实模式的 QuantLake、artifact 和数据路径边界。

    参数：
        quantlake_root: QuantLake 根目录。
        artifact_root: 独立 artifact 根目录。
        data_paths: 行情、状态和标签路径。

    返回：
        无。所有边界通过时正常返回，否则抛出运行时边界错误。
    """

    data_root = Path("/data")
    if not _is_within(artifact_root, data_root):
        raise _boundary_error("真实模式的 FM_ARTIFACT_ROOT 必须位于 /data 下")

    repository_roots = _repository_roots()
    required_keys = ("FM_MARKET_URI", "FM_STATE_URI")
    for key in required_keys:
        path = data_paths[key]
        if path is None:
            raise _boundary_error(f"真实模式缺少数据路径：{key}")
        if not _is_within(path, data_root):
            raise _boundary_error(f"{key} 必须位于 /data 下")
        if any(_is_within(path, root) for root in repository_roots):
            raise _boundary_error(f"{key} 不能指向仓库本地路径")

    if mode is ExecutionMode.SMOKE:
        if data_paths["FM_LABEL_URI"] is not None:
            raise _boundary_error("冒烟测试不得配置或读取 FM_LABEL_URI")
        if not _is_within(smoke_input_root, artifact_root):
            raise _boundary_error("FM_SMOKE_INPUT_ROOT 必须位于 artifact root 内")
        for key in required_keys:
            path = data_paths[key]
            assert path is not None
            if not _is_within(path, smoke_input_root):
                raise _boundary_error(f"冒烟测试的 {key} 必须位于有界输入根目录")
        return

    expected_quantlake = Path("/data/quantlake")
    if quantlake_root != expected_quantlake:
        raise _boundary_error("可见验证的 FM_QUANTLAKE_ROOT 必须精确等于 /data/quantlake")
    resolved_quantlake = quantlake_root.resolve(strict=False) if quantlake_root else None
    if resolved_quantlake is None or not _is_within(resolved_quantlake, data_root):
        raise _boundary_error("/data/quantlake 的软链接目标必须位于 /data 下")
    if _is_within(artifact_root, expected_quantlake) or _is_within(
        artifact_root, resolved_quantlake
    ):
        raise _boundary_error("FM_ARTIFACT_ROOT 不能位于 QuantLake 内")
    label_path = data_paths["FM_LABEL_URI"]
    if label_path is None:
        raise _boundary_error("可见验证缺少数据路径：FM_LABEL_URI")
    resolved_derived = (
        derived_release_root.resolve(strict=False) if derived_release_root else None
    )
    if resolved_derived is not None:
        if not _is_within(resolved_derived, data_root):
            raise _boundary_error("FM_DERIVED_RELEASE_ROOT 必须位于 /data 下")
        if _is_within(resolved_derived, resolved_quantlake) or _is_within(
            resolved_derived, artifact_root
        ):
            raise _boundary_error("派生发布根目录必须与 QuantLake 和运行产物根目录分离")
    allowed_release_root = resolved_derived or resolved_quantlake
    for key in (*required_keys, "FM_LABEL_URI"):
        path = data_paths[key]
        assert path is not None
        if _is_within(path, smoke_input_root):
            raise _boundary_error(f"可见验证的 {key} 不能位于冒烟输入根目录")
        if not _is_within(path.resolve(strict=False), allowed_release_root):
            raise _boundary_error(f"可见验证的 {key} 必须位于已声明发布目录")


def _validate_provenance(environment: Mapping[str, str]) -> None:
    """校验真实模式所需的完整数据 provenance。

    参数：
        environment: 环境变量映射。

    返回：
        无。缺失字段或来源不正确时抛出运行时边界错误。
    """

    for key in _PROVENANCE_KEYS:
        _read_value(environment, key)
    if environment["FM_DATA_ORIGIN"].strip() != "server_quantlake":
        raise _boundary_error("真实模式的 FM_DATA_ORIGIN 必须为 server_quantlake")
    _require_sha256(environment["FM_RELEASE_MANIFEST_SHA256"], "FM_RELEASE_MANIFEST_SHA256")
    _require_sha256(_read_value(environment, "FM_CONFIG_HASH"), "FM_CONFIG_HASH")
    code_commit = _read_value(environment, "FM_CODE_COMMIT")
    if _GIT_COMMIT_PATTERN.fullmatch(code_commit) is None:
        raise _boundary_error("FM_CODE_COMMIT 必须是 40 位小写十六进制 Git 提交")
    _read_value(environment, "FM_RELEASE_MANIFEST_PATH")
    _read_value(environment, "FM_CONFIG_PATH")


def _require_sha256(value: str, key: str) -> str:
    """要求值为完整的小写 SHA-256。"""

    normalized = value.strip()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise _boundary_error(f"{key} 必须是 64 位小写十六进制 SHA-256")
    return normalized


def canonical_config_hash(path: Path) -> str:
    """计算私有环境文件去除自引用哈希字段后的规范内容哈希。"""

    if not path.is_file():
        raise _boundary_error(f"私有配置文件不存在：{path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key == "FM_CONFIG_HASH":
            continue
        if key.startswith("FM_"):
            values[key] = value.strip().strip('"').strip("'")
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def _sha256_file(path: Path, name: str) -> str:
    """读取实际文件并计算 SHA-256。"""

    if not path.is_file():
        raise _boundary_error(f"{name} 文件不存在：{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_derived_release_manifest(
    profile: RuntimeProfile,
    manifest: dict[str, object],
) -> None:
    """核验服务器侧派生发布与只读 QuantLake 上游的完整内容绑定。"""

    derived_root = profile.derived_release_root
    quantlake_root = profile.quantlake_root
    if derived_root is None or quantlake_root is None:
        raise _boundary_error("派生发布缺少根目录")
    if manifest.get("release_kind") != "quantlake_derived_v1":
        raise _boundary_error("派生发布 release_kind 非法")
    if manifest.get("transform_id") != "factor_miner_standard_panel_v1":
        raise _boundary_error("派生发布 transform_id 非法")
    declared_quantlake = manifest.get("upstream_quantlake_root")
    declared_derived = manifest.get("derived_release_root")
    if not isinstance(declared_quantlake, str) or Path(declared_quantlake).resolve() != quantlake_root.resolve():
        raise _boundary_error("派生发布 QuantLake 上游根目录不一致")
    if not isinstance(declared_derived, str) or Path(declared_derived).resolve() != derived_root.resolve():
        raise _boundary_error("派生发布根目录与配置不一致")
    upstream_files = manifest.get("upstream_files")
    if not isinstance(upstream_files, list) or not upstream_files:
        raise _boundary_error("派生发布缺少上游文件清单")
    for item in upstream_files:
        if not isinstance(item, dict):
            raise _boundary_error("派生发布上游文件记录非法")
        path_text = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(path_text, str) or not isinstance(expected_hash, str):
            raise _boundary_error("派生发布上游文件缺少路径或哈希")
        path = Path(path_text).resolve(strict=False)
        if not _is_within(path, quantlake_root.resolve(strict=False)):
            raise _boundary_error("派生发布上游文件不在 QuantLake 内")
        if _sha256_file(path, "派生发布上游文件") != _require_sha256(
            expected_hash, "upstream sha256"
        ):
            raise _boundary_error("派生发布上游文件哈希不一致")
    for name, configured in {
        "market": profile.market_uri,
        "state": profile.state_uri,
        "label": profile.label_uri,
    }.items():
        declared_path = manifest.get(f"{name}_uri")
        expected_hash = manifest.get(f"{name}_sha256")
        if configured is None or not isinstance(declared_path, str) or not isinstance(expected_hash, str):
            raise _boundary_error(f"派生发布缺少 {name} 路径或哈希")
        if configured.resolve(strict=False) != Path(declared_path).resolve(strict=False):
            raise _boundary_error(f"派生发布 {name} 路径不一致")
        if _sha256_file(configured, f"派生发布 {name}") != _require_sha256(
            expected_hash, f"{name}_sha256"
        ):
            raise _boundary_error(f"派生发布 {name} 哈希不一致")


def verify_runtime_identity(
    profile: RuntimeProfile,
    *,
    repository_root: Path | None = None,
    git_head: str | None = None,
    git_status: str | None = None,
) -> RuntimeIdentity:
    """核验发布清单、私有配置、Git 提交和锁文件的实际内容。"""

    assert_real_data_allowed(profile)
    manifest_path = profile.release_manifest_path
    config_path = profile.config_path
    if manifest_path is None or config_path is None:
        raise _boundary_error("真实运行缺少发布清单或私有配置路径")
    actual_manifest_hash = _sha256_file(manifest_path, "发布清单")
    if actual_manifest_hash != profile.release_manifest_sha256:
        raise _boundary_error("发布清单实际 SHA-256 与配置不一致")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _boundary_error(f"发布清单不是合法 JSON：{error}") from error
    if not isinstance(manifest, dict):
        raise _boundary_error("发布清单根节点必须是对象")
    if manifest.get("release_id") != profile.resolved_release_id:
        raise _boundary_error("发布清单 release_id 与配置不一致")

    if profile.derived_release_root is not None:
        _verify_derived_release_manifest(profile, manifest)

    if profile.mode is ExecutionMode.VISIBLE:
        required_manifest_paths = {
            "market_uri": profile.market_uri,
            "state_uri": profile.state_uri,
            "label_uri": profile.label_uri,
        }
        for key, configured in required_manifest_paths.items():
            declared = manifest.get(key)
            if configured is None or not isinstance(declared, str):
                raise _boundary_error(f"发布清单缺少 {key}")
            if configured.resolve(strict=False) != Path(declared).resolve(strict=False):
                raise _boundary_error(f"{key} 与发布清单不一致")
        release_root_text = manifest.get("release_root")
        label_root_text = manifest.get("label_release_root")
        if not isinstance(release_root_text, str) or not isinstance(label_root_text, str):
            raise _boundary_error("发布清单缺少 release_root 或 label_release_root")
        release_root = Path(release_root_text).resolve(strict=False)
        label_root = Path(label_root_text).resolve(strict=False)
        assert profile.market_uri is not None and profile.state_uri is not None
        assert profile.label_uri is not None
        if not _is_within(profile.market_uri.resolve(strict=False), release_root) or not _is_within(
            profile.state_uri.resolve(strict=False), release_root
        ):
            raise _boundary_error("行情或状态 URI 不在清单声明的发布根目录")
        if not _is_within(profile.label_uri.resolve(strict=False), label_root):
            raise _boundary_error("标签 URI 不在清单声明的标签发布根目录")

    actual_config_hash = canonical_config_hash(config_path)
    if actual_config_hash != profile.config_hash:
        raise _boundary_error("私有配置规范哈希与 FM_CONFIG_HASH 不一致")

    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    if git_head is None:
        git_head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    if git_status is None:
        git_status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    if git_head != profile.code_commit:
        raise _boundary_error("FM_CODE_COMMIT 与实际 Git HEAD 不一致")
    if git_status.strip():
        raise _boundary_error("正式运行要求 Git 工作区干净")

    lock_path = root / "uv.lock"
    lock_hash = _sha256_file(lock_path, "uv.lock")
    return RuntimeIdentity(
        release_manifest_sha256=actual_manifest_hash,
        config_hash=actual_config_hash,
        code_commit=git_head,
        uv_lock_sha256=lock_hash,
    )


def load_runtime_profile(
    environment: Mapping[str, str], platform_name: str | None = None
) -> RuntimeProfile:
    """加载并校验运行时配置，不对空值或路径做静默 fallback。

    参数：
        environment: 外部传入的环境变量映射。
        platform_name: 用于测试或调用方注入的平台名称；省略时读取当前平台。

    返回：
        通过模式、路径和 provenance 校验的不可变 `RuntimeProfile`。

    异常：
        配置缺失、平台不允许或路径越界时抛出 `FactorMinerError`。
    """

    raw_environment = {
        str(key): str(value)
        for key, value in environment.items()
        if str(key).startswith("FM_")
    }
    mode_text = _read_value(raw_environment, "FM_MODE")
    try:
        mode = ExecutionMode(mode_text)
    except ValueError as error:
        raise _boundary_error(f"不支持的 FM_MODE：{mode_text}") from error

    resolved_platform = platform_name or platform_module.system()
    artifact_root = _resolve_absolute_path(
        _read_value(raw_environment, "FM_ARTIFACT_ROOT"), "FM_ARTIFACT_ROOT"
    )
    quantlake_value = str(raw_environment.get("FM_QUANTLAKE_ROOT", "")).strip()
    quantlake_root = (
        _absolute_path_without_symlink_resolution(quantlake_value, "FM_QUANTLAKE_ROOT")
        if quantlake_value
        else None
    )
    data_paths = {
        key: _optional_path(raw_environment, key) for key in _DATA_URI_KEYS
    }
    smoke_input_root = _optional_path(raw_environment, "FM_SMOKE_INPUT_ROOT") or (
        artifact_root / "inputs"
    )
    derived_release_root = _optional_path(environment, "FM_DERIVED_RELEASE_ROOT")

    if mode in _REAL_MODES:
        if resolved_platform != "Linux":
            raise _boundary_error(
                f"真实模式只能在 Linux 执行，当前平台为 {resolved_platform}"
            )
        _validate_real_paths(
            mode,
            quantlake_root,
            artifact_root,
            data_paths,
            smoke_input_root,
            derived_release_root,
        )
        _validate_provenance(raw_environment)

    return RuntimeProfile(
        mode=mode,
        platform_name=resolved_platform,
        artifact_root=artifact_root,
        quantlake_root=quantlake_root,
        market_uri=data_paths["FM_MARKET_URI"],
        state_uri=data_paths["FM_STATE_URI"],
        label_uri=data_paths["FM_LABEL_URI"],
        data_origin=str(raw_environment.get("FM_DATA_ORIGIN", "")).strip() or None,
        resolved_release_id=str(
            raw_environment.get("FM_RESOLVED_RELEASE_ID", "")
        ).strip()
        or None,
        release_manifest_sha256=str(
            raw_environment.get("FM_RELEASE_MANIFEST_SHA256", "")
        ).strip()
        or None,
        schema_version=str(raw_environment.get("FM_SCHEMA_VERSION", "")).strip()
        or None,
        market_cutoff=str(raw_environment.get("FM_MARKET_CUTOFF", "")).strip()
        or None,
        adjustment_convention=str(
            raw_environment.get("FM_ADJUSTMENT_CONVENTION", "")
        ).strip()
        or None,
        calendar_version=str(
            raw_environment.get("FM_CALENDAR_VERSION", "")
        ).strip()
        or None,
        state_table_version=str(
            raw_environment.get("FM_STATE_TABLE_VERSION", "")
        ).strip()
        or None,
        state_table_cutoff=str(
            raw_environment.get("FM_STATE_TABLE_CUTOFF", "")
        ).strip()
        or None,
        environment=MappingProxyType(raw_environment),
        release_manifest_path=_optional_path(raw_environment, "FM_RELEASE_MANIFEST_PATH"),
        config_path=_optional_path(raw_environment, "FM_CONFIG_PATH"),
        config_hash=str(raw_environment.get("FM_CONFIG_HASH", "")).strip() or None,
        code_commit=str(raw_environment.get("FM_CODE_COMMIT", "")).strip() or None,
        smoke_input_root=smoke_input_root,
        derived_release_root=derived_release_root,
    )


def assert_real_data_allowed(profile: RuntimeProfile) -> None:
    """断言配置允许访问真实数据。

    参数：
        profile: 已加载的运行时配置。

    返回：
        无。配置满足真实模式要求时正常返回。

    异常：
        design、synthetic 或其他非真实模式不能访问真实数据。
    """

    if profile.mode not in _REAL_MODES:
        raise _boundary_error(
            f"运行模式 {profile.mode.value} 不允许访问真实数据"
        )
    if profile.platform_name != "Linux":
        raise _boundary_error("真实数据访问必须运行在 Linux")
