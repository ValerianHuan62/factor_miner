"""阶段 A 服务器输入扫描和合同核验。"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Collection

import polars as pl

from factor_miner.canonical import sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.data_source import FactorInputRequest, InputProvenance
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.pilot_schema import (
    BarraAvailability,
    BenchmarkIdentity,
    PilotFixedCandidateFile,
    PilotInputManifest,
    PilotSourcePaths,
)
from factor_miner.portfolio_schema import TradingCalendarIdentity


def _error(code: FailureCode, message: str) -> FactorMinerError:
    """构造稳定的 Pilot 合同错误。"""

    return FactorMinerError(code, message)


def _require_path(path: Path | None, name: str, code: FailureCode) -> Path:
    """验证显式路径存在。"""

    if path is None:
        raise _error(code, f"缺少显式输入路径：{name}")
    if not path.exists():
        raise _error(code, f"输入路径不存在：{name}={path}")
    return path


def _parquet_paths(path: Path, name: str) -> tuple[Path, ...]:
    """解析一个 Parquet 文件或目录，不生成隐式默认路径。"""

    path = _require_path(path, name, FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    if path.is_file():
        if path.suffix.lower() != ".parquet":
            raise _error(FailureCode.FIELD_MISSING, f"{name} 必须是 Parquet：{path}")
        return (path,)
    files = tuple(sorted(item for item in path.rglob("*.parquet") if item.is_file()))
    if not files:
        raise _error(FailureCode.FIELD_MISSING, f"{name} 目录没有 Parquet 文件：{path}")
    return files


def _scan_parquet(path: Path, name: str) -> pl.LazyFrame:
    """以 LazyFrame 方式读取一个数据集，先只解析 schema。"""

    files = _parquet_paths(path, name)
    return pl.concat([pl.scan_parquet(file) for file in files], how="vertical_relaxed")


def _scan_manifest_partitions(
    source_path: Path,
    manifest_path: Path | None,
    name: str,
    start: date,
    end: date,
    *,
    partition_key: str = "partitions",
) -> pl.LazyFrame:
    """仅打开发布 manifest 中与研究窗口重叠的 Parquet 分区。"""

    if manifest_path is None:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} 缺少显式发布 manifest 身份",
        )
    manifest = _read_json(manifest_path, f"{name}_manifest")
    source_identity_key = (
        "source_sha256"
        if partition_key == "partitions"
        else f"{partition_key.removesuffix('_partitions')}_source_sha256"
    )
    source_sha256 = manifest.get(source_identity_key)
    partitions = manifest.get(partition_key)
    if (
        manifest.get("version") not in {"pilot-source-manifest-v1", "quantlake-release-v1"}
        or not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or not isinstance(partitions, list)
        or not partitions
    ):
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} 发布 manifest 身份或分区清单无效",
        )
    source = _require_path(
        source_path,
        name,
        FailureCode.PILOT_INPUT_CONTRACT_INVALID,
    ).resolve(strict=False)
    base = source if source.is_dir() else source.parent
    selected: list[Path] = []
    normalized_partitions: list[dict[str, str]] = []
    for item in partitions:
        if not isinstance(item, dict):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区必须是 object",
            )
        relative = item.get("path")
        partition_sha256 = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative.strip()
            or not isinstance(partition_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", partition_sha256) is None
        ):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区身份无效",
            )
        partition_start = _parse_date(item.get("start"), "start", f"{name}_manifest")
        partition_end = _parse_date(item.get("end"), "end", f"{name}_manifest")
        if partition_end < partition_start:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区日期反向",
            )
        normalized_partitions.append(
            {
                "path": relative,
                "start": partition_start.isoformat(),
                "end": partition_end.isoformat(),
                "sha256": partition_sha256,
            }
        )
        if partition_end < start or partition_start > end:
            continue
        resolved = (base / relative).resolve(strict=False)
        if base != resolved and base not in resolved.parents:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区越过数据根目录",
            )
        if source.is_file() and resolved != source:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区与显式文件不一致",
            )
        _require_path(resolved, name, FailureCode.PILOT_INPUT_CONTRACT_INVALID)
        actual_sha256 = _file_or_tree_sha256(resolved)
        if actual_sha256 != partition_sha256:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} 选中分区 SHA256 与发布 manifest 不一致",
            )
        selected.append(resolved)
    if source_sha256 != sha256_json(
        sorted(normalized_partitions, key=lambda item: item["path"])
    ):
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} source_sha256 与分区身份不一致",
        )
    if not selected:
        raise _error(
            FailureCode.FACTOR_COVERAGE_TOO_LOW,
            f"{name} manifest 没有覆盖请求窗口的分区",
        )
    return pl.concat(
        [pl.scan_parquet(path) for path in sorted(set(selected))],
        how="vertical_relaxed",
    )


def _manifest_source_sha256(manifest_path: Path, name: str) -> str:
    """读取已发布派生源 manifest 声明的整体内容身份。"""

    payload = _read_json(manifest_path, f"{name}_manifest")
    value = payload.get("source_sha256")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} manifest 缺少有效 source_sha256",
        )
    return value


def _partition_manifest_path(paths: PilotSourcePaths) -> Path:
    """返回独立分区清单；未配置时兼容旧版 release manifest。"""

    return paths.partition_manifest_uri or paths.release_manifest_uri


def _manifest_date_column(manifest_path: Path, name: str) -> str:
    """读取派生源 manifest 冻结的日期列，避免扫描后猜列名。"""

    payload = _read_json(manifest_path, f"{name}_manifest")
    value = payload.get("date_column")
    if value not in {"date", "trade_date"}:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} manifest 缺少冻结 date_column",
        )
    return str(value)


def _manifest_identity_and_cutoff(
    manifest_path: Path,
    name: str,
    *,
    partition_key: str = "partitions",
) -> tuple[str, date]:
    """只从发布 manifest 计算分区身份与声明截止日，不打开数据分区。"""

    payload = _read_json(manifest_path, f"{name}_manifest")
    partitions = payload.get(partition_key)
    source_key = (
        "source_sha256"
        if partition_key == "partitions"
        else f"{partition_key.removesuffix('_partitions')}_source_sha256"
    )
    if not isinstance(partitions, list) or not partitions:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} manifest 缺少分区身份",
        )
    normalized: list[dict[str, str]] = []
    ends: list[date] = []
    for item in partitions:
        if not isinstance(item, dict):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区必须是 object",
            )
        relative = item.get("path")
        digest = item.get("sha256")
        start = _parse_date(item.get("start"), "start", f"{name}_manifest")
        end = _parse_date(item.get("end"), "end", f"{name}_manifest")
        if (
            not isinstance(relative, str)
            or not relative.strip()
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or end < start
        ):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} manifest 分区身份无效",
            )
        normalized.append(
            {
                "path": relative,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sha256": digest,
            }
        )
        ends.append(end)
    expected = sha256_json(sorted(normalized, key=lambda item: item["path"]))
    if payload.get(source_key) != expected:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} source_sha256 与分区身份不一致",
        )
    return expected, max(ends)


def _file_or_tree_sha256(path: Path) -> str:
    """为文件或目录生成确定性 SHA-256。"""

    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    files = _parquet_paths(path, "输入目录")
    entries = []
    for file in files:
        entries.append(
            {
                "path": str(file.relative_to(path)),
                "sha256": _file_or_tree_sha256(file),
            }
        )
    return sha256_json(entries)


def _read_json(path: Path, name: str) -> dict[str, object]:
    """读取并限制为 JSON 对象。"""

    path = _require_path(path, name, FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"无法读取 {name}：{error}") from error
    if not isinstance(payload, dict):
        raise _error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"{name} 必须是 JSON 对象")
    return payload


def _required_text(payload: dict[str, object], key: str, name: str) -> str:
    """读取 JSON 中的非空文本字段。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"{name} 缺少非空字段：{key}")
    return value.strip()


def _parse_date(value: object, key: str, name: str) -> date:
    """读取 ISO 日期字段。"""

    if not isinstance(value, str):
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, f"{name} 缺少日期字段：{key}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, f"{name} 日期无效：{key}={value}") from error


def _validate_keys(frame: pl.LazyFrame, keys: tuple[str, ...], name: str) -> None:
    """验证数据集主键非空且唯一。"""

    schema = frame.collect_schema()
    missing = set(keys).difference(schema.names())
    if missing:
        raise _error(FailureCode.FIELD_MISSING, f"{name} 缺少主键字段：{sorted(missing)}")
    nulls = frame.select([pl.col(key).is_null().any().alias(key) for key in keys]).collect()
    if any(bool(value) for value in nulls.row(0)):
        raise _error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"{name} 主键包含空值")
    duplicate = (
        frame.select(list(keys))
        .group_by(list(keys))
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
        .collect()
    )
    if duplicate.height:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"{name} 主键重复：{duplicate.to_dicts()[0]}",
        )


def _require_columns(frame: pl.LazyFrame, columns: Collection[str], name: str) -> dict[str, pl.DataType]:
    """验证数据集包含合同列并返回 schema。"""

    schema = dict(frame.collect_schema())
    missing = set(columns).difference(schema)
    if missing:
        raise _error(FailureCode.FIELD_MISSING, f"{name} 缺少字段：{sorted(missing)}")
    return schema


def _max_date(frame: pl.LazyFrame, column: str, name: str) -> date:
    """读取数据集最大日期。"""

    value = frame.select(pl.col(column).max()).collect().item()
    if not isinstance(value, date):
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, f"{name} 没有有效截止日")
    return value


def _normalize_date_column(
    frame: pl.LazyFrame,
    column: str,
    name: str,
) -> pl.LazyFrame:
    """将 Date 或午夜无时区 Datetime 规范化为 ``date``。"""

    schema = dict(frame.collect_schema())
    dtype = schema.get(column)
    if dtype == pl.Date:
        return frame.rename({column: "date"})
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is not None:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} 日期字段不得带时区：{column}",
            )
        non_midnight = (
            frame.filter(pl.col(column) != pl.col(column).dt.truncate("1d"))
            .limit(1)
            .collect()
        )
        if non_midnight.height:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{name} Datetime 日期必须全部位于午夜：{column}",
            )
        normalized = frame.with_columns(
            pl.col(column).cast(pl.Date).alias("date")
        )
        return normalized if column == "date" else normalized.drop(column)
    raise _error(
        FailureCode.PILOT_INPUT_CONTRACT_INVALID,
        f"{name} 日期字段必须是 Date 或无时区午夜 Datetime：{column}",
    )


def _read_field_map(path: Path) -> dict[str, str]:
    """从字段注册表提取 canonical 字段映射。"""

    try:
        table = pl.read_csv(path, encoding="utf8-lossy")
    except Exception as error:
        raise _error(FailureCode.FIELD_MISSING, f"无法读取字段注册表：{error}") from error
    normalized = {column.lstrip("\ufeff").strip(): column for column in table.columns}
    if {"canonical_field", "source_field"}.issubset(normalized):
        canonical_column = normalized["canonical_field"]
        source_column = normalized["source_field"]
        rows = table.select([canonical_column, source_column]).drop_nulls().to_dicts()
        mapping = {str(row[canonical_column]).strip(): str(row[source_column]).strip() for row in rows}
    elif "字段名" in normalized:
        column = normalized["字段名"]
        values = table.select(column).drop_nulls().to_series().to_list()
        mapping = {str(value).strip(): str(value).strip() for value in values}
    else:
        raise _error(FailureCode.FIELD_MISSING, "字段注册表缺少 canonical_field/source_field 或 字段名")
    if any(not key or not value for key, value in mapping.items()):
        raise _error(FailureCode.FIELD_MISSING, "字段注册表包含空映射")
    if len(mapping) != len(set(mapping)):
        raise _error(FailureCode.FIELD_MISSING, "字段注册表 canonical 字段重复")
    return mapping


def _validate_calendar(
    path: Path,
    manifest_path: Path | None,
    version: str,
) -> TradingCalendarIdentity:
    """验证实际交易日历。"""

    if manifest_path is None:
        raise _error(
            FailureCode.CALENDAR_CONTRACT_INVALID,
            "交易日日历缺少发布 manifest",
        )
    frame = _scan_manifest_partitions(
        path,
        manifest_path,
        "calendar",
        date(2021, 1, 1),
        date.max,
    )
    schema = _require_columns(frame, ("trade_date", "is_open"), "交易日日历")
    if schema["trade_date"] != pl.Date or schema["is_open"] != pl.Boolean:
        raise _error(FailureCode.CALENDAR_CONTRACT_INVALID, "交易日日历字段类型不符合合同")
    _validate_keys(frame, ("trade_date",), "交易日日历")
    if frame.filter(pl.col("is_open")).limit(3).collect().height < 3:
        raise _error(FailureCode.CALENDAR_CONTRACT_INVALID, "交易日日历开放日不足")
    return TradingCalendarIdentity(
        calendar_version=version,
        calendar_sha256=_manifest_source_sha256(manifest_path, "calendar"),
    )


def _validate_benchmark(
    path: Path,
    manifest_path: Path | None,
    version: str,
) -> BenchmarkIdentity:
    """验证 CSI300 指数开盘价输入。"""

    if manifest_path is None:
        raise _error(
            FailureCode.BENCHMARK_SOURCE_UNAVAILABLE,
            "CSI300 缺少发布 manifest",
        )
    frame = _scan_manifest_partitions(
        path,
        manifest_path,
        "benchmark",
        date(2021, 1, 1),
        date.max,
    )
    date_column = _manifest_date_column(manifest_path, "benchmark")
    frame = frame.filter(pl.col(date_column).cast(pl.Date) >= date(2021, 1, 1))
    schema = dict(frame.collect_schema())
    if date_column not in schema or "open" not in schema:
        raise _error(FailureCode.FIELD_MISSING, "CSI300 必须包含 date/trade_date 和 open")
    if schema[date_column] == pl.Date:
        normalized = frame.rename({date_column: "trade_date"})
    elif isinstance(schema[date_column], pl.Datetime):
        midnight = frame.filter(
            pl.col(date_column) != pl.col(date_column).dt.truncate("1d")
        ).limit(1).collect()
        if midnight.height:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "CSI300 Datetime 日期必须全部位于午夜",
            )
        normalized = frame.with_columns(
            pl.col(date_column).cast(pl.Date).alias("trade_date")
        ).drop(date_column)
    else:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "CSI300 日期字段必须是 Date 或无时区午夜 Datetime",
        )
    normalized = normalized.filter(pl.col("trade_date") >= date(2021, 1, 1))
    _validate_keys(normalized, ("trade_date",), "CSI300")
    cutoff = _max_date(normalized, "trade_date", "CSI300")
    return BenchmarkIdentity(
        source_uri=path,
        source_sha256=_manifest_source_sha256(manifest_path, "benchmark"),
        schema_version=version,
        cutoff=cutoff,
    )


def _barra_availability(
    paths: PilotSourcePaths,
    *,
    verify_sources: bool,
) -> BarraAvailability:
    """从发布 manifest 构造 Barra 身份，并按阶段选择是否读取真实源。"""

    required = (
        ("exposures", "barra_exposure_uri", paths.barra_exposure_uri),
        ("factor_returns", "barra_factor_returns_uri", paths.barra_factor_returns_uri),
        (
            "benchmark_weights",
            "barra_benchmark_weights_uri",
            paths.barra_benchmark_weights_uri,
        ),
    )
    missing = tuple(name for _, name, path in required if path is None)
    if missing:
        return BarraAvailability(status="not_available", missing_inputs=missing)
    if paths.barra_root is None or paths.barra_manifest_uri is None:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "启用 Barra 输入时缺少显式 barra_root 或 barra_manifest_uri",
        )
    payload = _read_json(paths.barra_manifest_uri, "barra_manifest")
    declared = payload.get("sources")
    declared_identity = payload.get("input_sha256")
    if (
        payload.get("version") != "barra-source-manifest-v1"
        or not isinstance(declared, list)
        or not declared
        or not isinstance(declared_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", declared_identity) is None
    ):
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "Barra 发布 manifest 身份无效",
        )

    configured = (
        *required,
        ("covariance", "barra_covariance_uri", paths.barra_covariance_uri),
        ("specific_risk", "barra_specific_risk_uri", paths.barra_specific_risk_uri),
    )
    expected_paths = {
        role: path.resolve(strict=False)
        for role, _, path in configured
        if path is not None
    }
    base = paths.barra_root.resolve(strict=False)
    normalized: list[dict[str, str]] = []
    resolved_sources: dict[str, Path] = {}
    for item in declared:
        if not isinstance(item, dict):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "Barra manifest source 必须是 object",
            )
        role = item.get("role")
        relative = item.get("path")
        digest = item.get("sha256")
        if (
            not isinstance(role, str)
            or role not in expected_paths
            or role in resolved_sources
            or not isinstance(relative, str)
            or not relative.strip()
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "Barra manifest source 身份无效",
            )
        resolved = (base / relative).resolve(strict=False)
        if base != resolved and base not in resolved.parents:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                "Barra manifest source 越过 barra_root",
            )
        if resolved != expected_paths[role]:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"Barra manifest source 与显式路径不一致：{role}",
            )
        normalized.append({"role": role, "path": relative, "sha256": digest})
        resolved_sources[role] = resolved
    if set(resolved_sources) != set(expected_paths):
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "Barra manifest 未完整绑定全部显式源",
        )
    normalized.sort(key=lambda item: item["role"])
    if sha256_json(normalized) != declared_identity:
        raise _error(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            "Barra input_sha256 与 manifest source 身份不一致",
        )
    if verify_sources:
        declared_by_role = {item["role"]: item["sha256"] for item in normalized}
        for role, path in resolved_sources.items():
            _require_path(path, role, FailureCode.PILOT_INPUT_CONTRACT_INVALID)
            if _file_or_tree_sha256(path) != declared_by_role[role]:
                raise _error(
                    FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                    f"Barra 源 SHA256 与发布 manifest 不一致：{role}",
                )
    return BarraAvailability(
        status="available",
        source_uris=tuple(
            path
            for role, _, path in configured
            if path is not None and role in resolved_sources
        ),
        input_sha256=declared_identity,
    )


def load_fixed_candidates(path: Path, allowed_fields: Collection[str] = ("close", "volume")) -> PilotFixedCandidateFile:
    """读取并编译验证固定三候选文件。"""

    path = _require_path(path, "candidate_file", FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    try:
        bundle = PilotFixedCandidateFile.model_validate_json(path.read_bytes())
    except Exception as error:
        raise _error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"固定候选文件无效：{error}") from error
    for candidate in bundle.candidates:
        try:
            compile_candidate(candidate.spec, allowed_fields=allowed_fields)
        except FactorMinerError:
            raise
        except Exception as error:
            raise _error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"固定候选无法编译：{candidate.candidate_id}：{error}",
            ) from error
    return bundle


class PilotQuantLakeFactorInputSource:
    """将服务器 QuantLake 行情/状态规范化为 input-only 因子端口。"""

    # 阶段 A 真实输入开放 A 股 QuantLake 当前行情表中的全部规范行情字段。
    # 状态字段仍通过独立状态表进入，不会被混入因子表达式字段。
    allowed_fields = ("open", "high", "low", "close", "volume", "amount")

    def __init__(
        self,
        paths: PilotSourcePaths,
        *,
        code_commit: str,
        config_hash: str,
        manifest: PilotInputManifest | None = None,
        discovery_only: bool = False,
    ) -> None:
        """保存显式路径和运行身份，不在构造阶段读取文件。"""

        if not code_commit.strip() or not config_hash.strip():
            raise ValueError("code_commit 和 config_hash 不能为空")
        self._paths = paths
        self._code_commit = code_commit
        self._config_hash = config_hash
        self._manifest = manifest
        self._discovery_only = discovery_only
        self._provenance: InputProvenance | None = None

    @property
    def quantlake_root(self) -> Path:
        """返回只读 QuantLake 根目录。"""

        return self._paths.quantlake_root

    def inspect_inputs(self) -> InputProvenance:
        """执行入口合同并返回 input provenance。"""

        if self._manifest is None and self._discovery_only:
            release = _read_json(self._paths.release_manifest_uri, "release_manifest")
            state = _read_json(self._paths.state_manifest_uri, "state_manifest")
            self._provenance = InputProvenance(
                data_origin="quantlake",
                resolved_release_id=_required_text(release, "release_id", "release_manifest"),
                release_manifest_sha256=_file_or_tree_sha256(self._paths.release_manifest_uri),
                schema_version=_required_text(release, "schema_version", "release_manifest"),
                market_cutoff=_parse_date(release.get("market_cutoff"), "market_cutoff", "release_manifest").isoformat(),
                adjustment_convention=str(
                    state.get("adjustment_convention")
                    or release.get("adjustment_convention")
                    or ""
                ),
                calendar_version=self._paths.calendar_version,
                state_table_version=str(
                    state.get("state_table_version") or state.get("schema_version") or ""
                ),
                state_table_cutoff=_parse_date(
                    state.get("state_table_cutoff") or state.get("l2_max_date"),
                    "state_table_cutoff",
                    "state_manifest",
                ).isoformat(),
                code_commit=self._code_commit,
                config_hash=self._config_hash,
            )
            return self._provenance
        self._manifest = self._manifest or inspect_pilot_sources(self._paths)
        self._provenance = InputProvenance(
            data_origin=self._manifest.data_origin,
            resolved_release_id=self._manifest.resolved_release_id,
            release_manifest_sha256=self._manifest.release_manifest_sha256,
            schema_version=self._manifest.schema_version,
            market_cutoff=self._manifest.market_cutoff.isoformat(),
            adjustment_convention=self._manifest.adjustment_convention,
            calendar_version=self._manifest.calendar.calendar_version,
            state_table_version=self._manifest.state_table_version,
            state_table_cutoff=self._manifest.state_table_cutoff.isoformat(),
            code_commit=self._code_commit,
            config_hash=self._config_hash,
        )
        return self._provenance

    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
        """按真实交易观察数读取 market/state，并映射为 canonical 列。"""

        if self._provenance is None:
            raise _error(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                "必须先通过 inspect_inputs 才能 scan_inputs",
            )
        missing = set(request.required_fields).difference(self.allowed_fields)
        if missing:
            raise _error(FailureCode.FIELD_MISSING, f"候选字段不在 Pilot 白名单中：{sorted(missing)}")
        history_floor = date(request.start.year - 1, 1, 1)
        market = _scan_manifest_partitions(
            self._paths.market_uri,
            _partition_manifest_path(self._paths),
            "market",
            history_floor,
            request.end,
            partition_key="market_partitions",
        ).filter(pl.col("date").is_between(history_floor, request.end, closed="both"))
        dates = (
            market.select("date")
            .filter(pl.col("date") < request.start)
            .unique()
            .sort("date")
            .collect()
            .get_column("date")
            .to_list()
        )
        if len(dates) < request.warmup_observations:
            raise _error(
                FailureCode.FACTOR_COVERAGE_TOO_LOW,
                f"交易观察历史不足：需要 {request.warmup_observations}，实际 {len(dates)}",
            )
        lower_bound = dates[-request.warmup_observations] if request.warmup_observations else request.start
        market_mapping = {
            "open": "adj_open",
            "high": "adj_high",
            "low": "adj_low",
            "close": "adj_close",
            "volume": "volume",
            "amount": "money",
        }
        expressions = []
        for field in request.required_fields:
            source_field = market_mapping.get(field)
            if source_field is None:
                raise _error(FailureCode.FIELD_MISSING, f"没有行情字段映射：{field}")
            expressions.append(pl.col(source_field).alias(field))
        market_frame = (
            market.filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select([pl.col("date"), pl.col("code").alias("asset"), *expressions])
        )
        state_path = _require_path(
            self._paths.state_uri,
            "state_uri",
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
        )
        state = _scan_manifest_partitions(
            state_path,
            _partition_manifest_path(self._paths),
            "state",
            lower_bound,
            request.end,
            partition_key="state_partitions",
        )
        state_schema = state.collect_schema()
        state_id = "asset" if "asset" in state_schema.names() else "code"
        if state_id not in state_schema.names():
            raise _error(FailureCode.FIELD_MISSING, "状态表缺少 code 或 asset")
        state_date_id = "date" if "date" in state_schema.names() else "trade_date"
        if state_date_id not in state_schema.names():
            raise _error(FailureCode.FIELD_MISSING, "状态表缺少 date 或 trade_date")
        state_frame = (
            state.rename({state_date_id: "date"})
            .filter(pl.col("date").is_between(lower_bound, request.end, closed="both"))
            .select(
                [
                    pl.col("date"),
                    pl.col(state_id).alias("asset"),
                    pl.col("valid_for_factor_compute"),
                    pl.col("valid_for_factor_rank"),
                    pl.col("valid_for_trading"),
                ]
            )
        )
        joined = market_frame.join(
            state_frame,
            on=["date", "asset"],
            how="inner",
            validate="1:1",
        )
        return (
            joined.filter(
                pl.col("asset").str.ends_with(".XSHG")
                | pl.col("asset").str.ends_with(".XSHE")
            )
            .sort(["date", "asset"])
        )


def inspect_pilot_source_identity(paths: PilotSourcePaths) -> PilotInputManifest:
    """仅用发布 metadata 构造冻结身份，不读取任何行情或标签分区。"""

    release = _read_json(paths.release_manifest_uri, "release_manifest")
    state_manifest = _read_json(paths.state_manifest_uri, "state_manifest")
    release_id = _required_text(release, "release_id", "release_manifest")
    schema_version = _required_text(release, "schema_version", "release_manifest")
    market_cutoff = _parse_date(release.get("market_cutoff"), "market_cutoff", "release_manifest")
    release_l2_cutoff = _parse_date(release.get("l2_cutoff"), "l2_cutoff", "release_manifest")
    state_cutoff = _parse_date(
        state_manifest.get("state_table_cutoff") or state_manifest.get("l2_max_date"),
        "state_table_cutoff",
        "state_manifest",
    )
    if market_cutoff != release_l2_cutoff or state_cutoff != release_l2_cutoff:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "release、行情和状态 cutoff 不一致")
    if _required_text(state_manifest, "schema_version", "state_manifest") != schema_version:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "行情与状态 schema_version 不一致")
    state_table_version = str(
        state_manifest.get("state_table_version") or state_manifest.get("schema_version") or ""
    ).strip()
    adjustment = str(
        state_manifest.get("adjustment_convention")
        or release.get("adjustment_convention")
        or ""
    ).strip()
    if not state_table_version or not adjustment:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "状态版本或复权口径缺失")
    market_path = _require_path(paths.market_uri, "market_uri", FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    state_path = _require_path(paths.state_uri, "state_uri", FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    _manifest_identity_and_cutoff(
        _partition_manifest_path(paths),
        "market",
        partition_key="market_partitions",
    )
    _manifest_identity_and_cutoff(
        _partition_manifest_path(paths),
        "state",
        partition_key="state_partitions",
    )
    field_registry = _require_path(
        paths.field_registry_uri,
        "field_registry_uri",
        FailureCode.FIELD_MISSING,
    )
    field_map = _read_field_map(field_registry)
    canonical_map = {"close": "adj_close", "volume": "volume"}
    if any(field not in field_map for field in canonical_map):
        raise _error(FailureCode.FIELD_MISSING, "字段注册表无法证明 close/volume")
    calendar_path = _require_path(
        paths.calendar_uri, "calendar_uri", FailureCode.CALENDAR_CONTRACT_INVALID
    )
    if paths.calendar_manifest_uri is None:
        raise _error(FailureCode.CALENDAR_CONTRACT_INVALID, "交易日日历缺少发布 manifest")
    calendar_sha256, _ = _manifest_identity_and_cutoff(
        paths.calendar_manifest_uri, "calendar"
    )
    benchmark_path = _require_path(
        paths.benchmark_uri,
        "benchmark_uri",
        FailureCode.BENCHMARK_SOURCE_UNAVAILABLE,
    )
    if paths.benchmark_manifest_uri is None:
        raise _error(FailureCode.BENCHMARK_SOURCE_UNAVAILABLE, "CSI300 缺少发布 manifest")
    benchmark_sha256, benchmark_cutoff = _manifest_identity_and_cutoff(
        paths.benchmark_manifest_uri, "benchmark"
    )
    return PilotInputManifest(
        data_origin=paths.data_origin,
        universe=paths.universe,
        quantlake_root=paths.quantlake_root,
        resolved_release_id=release_id,
        release_manifest_uri=paths.release_manifest_uri,
        release_manifest_sha256=_file_or_tree_sha256(paths.release_manifest_uri),
        state_manifest_uri=paths.state_manifest_uri,
        state_manifest_sha256=_file_or_tree_sha256(paths.state_manifest_uri),
        schema_version=schema_version,
        market_cutoff=market_cutoff,
        state_table_version=state_table_version,
        state_table_cutoff=state_cutoff,
        adjustment_convention=adjustment,
        market_uri=market_path,
        state_uri=state_path,
        field_registry_uri=field_registry,
        field_registry_sha256=_file_or_tree_sha256(field_registry),
        canonical_field_map=canonical_map,
        calendar_uri=calendar_path,
        calendar=TradingCalendarIdentity(
            calendar_version=paths.calendar_version,
            calendar_sha256=calendar_sha256,
        ),
        benchmark=BenchmarkIdentity(
            source_uri=benchmark_path,
            source_sha256=benchmark_sha256,
            schema_version=paths.benchmark_schema_version,
            cutoff=benchmark_cutoff,
        ),
        barra=_barra_availability(paths, verify_sources=False),
    )


def inspect_pilot_sources(paths: PilotSourcePaths) -> PilotInputManifest:
    """只读扫描服务器输入并返回完整身份。"""

    release = _read_json(paths.release_manifest_uri, "release_manifest")
    state_manifest = _read_json(paths.state_manifest_uri, "state_manifest")
    release_id = _required_text(release, "release_id", "release_manifest")
    schema_version = _required_text(release, "schema_version", "release_manifest")
    market_cutoff = _parse_date(release.get("market_cutoff"), "market_cutoff", "release_manifest")
    release_l2_cutoff = _parse_date(release.get("l2_cutoff"), "l2_cutoff", "release_manifest")
    market_release = release.get("market_release_id")
    state_release = release.get("state_release_id")
    if isinstance(market_release, str) and isinstance(state_release, str) and market_release != state_release:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "release manifest 的行情与状态 release 不一致")
    if _required_text(state_manifest, "schema_version", "state_manifest") != schema_version:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "行情与状态 schema_version 不一致")
    state_table_version = str(
        state_manifest.get("state_table_version") or state_manifest.get("schema_version") or ""
    ).strip()
    if not state_table_version:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "状态表缺少版本")
    state_cutoff_value = state_manifest.get("state_table_cutoff") or state_manifest.get("l2_max_date")
    state_cutoff = _parse_date(state_cutoff_value, "state_table_cutoff", "state_manifest")
    if market_cutoff != release_l2_cutoff or state_cutoff != release_l2_cutoff:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "release、行情和状态 cutoff 不一致")
    adjustment = str(
        state_manifest.get("adjustment_convention")
        or release.get("adjustment_convention")
        or ""
    ).strip()
    if not adjustment:
        raise _error(FailureCode.ADJUSTMENT_CONVENTION_UNKNOWN, "缺少复权口径")

    market = _scan_manifest_partitions(
        paths.market_uri,
        _partition_manifest_path(paths),
        "market",
        date(2021, 1, 1),
        date.max,
        partition_key="market_partitions",
    ).filter(pl.col("date") >= date(2021, 1, 1))
    market_schema = _require_columns(
        market,
        ("date", "code", "adj_open", "adj_close", "volume", "money", "adjust_factor"),
        "行情",
    )
    if market_schema["date"] != pl.Date:
        raise _error(FailureCode.FIELD_MISSING, "行情 date 必须是 Date")
    _validate_keys(market, ("date", "code"), "行情")
    if _max_date(market, "date", "行情") != market_cutoff:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "行情文件 cutoff 与 release 不一致")

    state_path = _require_path(paths.state_uri, "state_uri", FailureCode.PILOT_INPUT_CONTRACT_INVALID)
    state = _scan_manifest_partitions(
        state_path,
        _partition_manifest_path(paths),
        "state",
        date(2021, 1, 1),
        date.max,
        partition_key="state_partitions",
    )
    state_schema = state.collect_schema()
    state_date_column = "date" if "date" in state_schema.names() else "trade_date"
    if state_date_column not in state_schema.names():
        raise _error(FailureCode.FIELD_MISSING, "状态表缺少 date 或 trade_date")
    state = state.rename({state_date_column: "date"}).filter(
        pl.col("date") >= date(2021, 1, 1)
    )
    state_schema = _require_columns(
        state,
        (
            "date",
            "code",
            "is_st",
            "is_newly_listed",
            "is_suspended",
            "can_buy",
            "can_sell",
            "valid_for_factor_compute",
            "valid_for_factor_rank",
            "valid_for_trading",
        ),
        "状态表",
    )
    if state_schema["date"] != pl.Date:
        raise _error(FailureCode.FIELD_MISSING, "状态表 date 必须是 Date")
    _validate_keys(state, ("date", "code"), "状态表")
    if _max_date(state, "date", "状态表") != state_cutoff:
        raise _error(FailureCode.DATA_RELEASE_MISMATCH, "状态表 cutoff 与 release 不一致")

    field_map = _read_field_map(_require_path(paths.field_registry_uri, "field_registry_uri", FailureCode.FIELD_MISSING))
    canonical_map = {"close": "adj_close", "volume": "volume"}
    for field in canonical_map:
        if field not in field_map:
            raise _error(FailureCode.FIELD_MISSING, f"字段注册表无法证明字段：{field}")
        if canonical_map[field] not in market_schema:
            raise _error(FailureCode.FIELD_MISSING, f"行情字段映射不存在：{field}->{canonical_map[field]}")

    calendar_path = _require_path(paths.calendar_uri, "calendar_uri", FailureCode.CALENDAR_CONTRACT_INVALID)
    calendar = _validate_calendar(
        calendar_path,
        paths.calendar_manifest_uri,
        paths.calendar_version,
    )
    benchmark_path = _require_path(
        paths.benchmark_uri,
        "benchmark_uri",
        FailureCode.BENCHMARK_SOURCE_UNAVAILABLE,
    )
    benchmark = _validate_benchmark(
        benchmark_path,
        paths.benchmark_manifest_uri,
        paths.benchmark_schema_version,
    )
    barra = _barra_availability(paths, verify_sources=True)
    return PilotInputManifest(
        data_origin=paths.data_origin,
        universe=paths.universe,
        quantlake_root=paths.quantlake_root,
        resolved_release_id=release_id,
        release_manifest_uri=paths.release_manifest_uri,
        release_manifest_sha256=_file_or_tree_sha256(paths.release_manifest_uri),
        state_manifest_uri=paths.state_manifest_uri,
        state_manifest_sha256=_file_or_tree_sha256(paths.state_manifest_uri),
        schema_version=schema_version,
        market_cutoff=market_cutoff,
        state_table_version=state_table_version,
        state_table_cutoff=state_cutoff,
        adjustment_convention=adjustment,
        market_uri=paths.market_uri,
        state_uri=state_path,
        field_registry_uri=paths.field_registry_uri,
        field_registry_sha256=_file_or_tree_sha256(paths.field_registry_uri),
        canonical_field_map=canonical_map,
        calendar_uri=calendar_path,
        calendar=calendar,
        benchmark=benchmark,
        barra=barra,
    )
