"""确定性原始因子计算和质量报告。"""

from __future__ import annotations

from datetime import date
import hashlib
import os
from pathlib import Path
import tempfile
import time

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.compiler import CompiledFactorPlan, build_polars_expr
from factor_miner.data_source import (
    DataRequest,
    DataSource,
    FactorInputRequest,
    FactorInputSource,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import FactorNode


class FactorQuality(BaseModel):
    """原始因子 artifact 的确定性质量摘要和耗时诊断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_count: int = Field(ge=0)
    date_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    null_ratio: float = Field(ge=0, le=1)
    median_daily_coverage: float = Field(ge=0, le=1)
    all_null_dates: tuple[date, ...]
    elapsed_seconds: float = Field(ge=0)


class FactorArtifact(BaseModel):
    """保存原始因子文件位置、计划哈希、质量和内容哈希。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_path: Path
    factor_column: str = "raw_factor"
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: FactorQuality
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def compute_raw_factor(
    plan: CompiledFactorPlan,
    source: DataSource,
    request: DataRequest,
    output_path: Path,
) -> FactorArtifact:
    """按已编译计划计算原始因子并原子写入 Parquet。

    参数：
        plan: 已完成 DSL 校验的确定性编译计划。
        source: 必须先完成合同检查的数据源端口。
        request: 包含保存区间和至少足够 lookback 的数据请求。
        output_path: QuantLake 外的显式 artifact 路径。

    返回：
        写入完成后的 `FactorArtifact`。

    异常：
        请求缺少字段或 warmup、输入 schema 不完整、因子全为空时抛出领域错误。
    """

    _validate_request(plan, request)
    source.inspect()
    lazy_frame = source.scan(request)
    return _compute_scanned_factor(
        plan,
        lazy_frame,
        request.start,
        request.end,
        source,
        output_path,
    )


def compute_trusted_raw_factor(
    plan: CompiledFactorPlan,
    source: FactorInputSource,
    request: FactorInputRequest,
    output_path: Path,
) -> FactorArtifact:
    """通过 input-only 端口计算 V0.1 原始因子。"""

    _validate_trusted_request(plan, request)
    source.inspect_inputs()
    lazy_frame = source.scan_inputs(request)
    return _compute_scanned_factor(
        plan,
        lazy_frame,
        request.start,
        request.end,
        source,
        output_path,
    )


def _compute_scanned_factor(
    plan: CompiledFactorPlan,
    lazy_frame: pl.LazyFrame,
    start: date,
    end: date,
    source: object,
    output_path: Path,
) -> FactorArtifact:
    """对已经由显式端口扫描的 input 执行共享确定性计算。"""

    schema_names = set(lazy_frame.collect_schema().names())
    required_columns = {
        "date",
        "asset",
        "valid_for_factor_compute",
        *plan.required_fields,
    }
    missing_columns = required_columns - schema_names
    if missing_columns:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"原始因子计算输入缺少列：{sorted(missing_columns)}",
        )

    started = time.perf_counter()
    expression = FactorNode.model_validate(plan.expression)
    computed = (
        lazy_frame.sort(["date", "asset"])
        .with_columns(build_polars_expr(expression).alias("_unmasked_factor"))
        .with_columns(
            pl.when(pl.col("_unmasked_factor").is_finite() == True)
            .then(pl.col("_unmasked_factor"))
            .otherwise(pl.lit(None))
            .alias("raw_factor")
        )
        .with_columns(
            pl.when(pl.col("valid_for_factor_compute") == True)
            .then(pl.col("raw_factor"))
            .otherwise(pl.lit(None))
            .alias("raw_factor")
        )
        .filter(
            pl.col("date").is_between(start, end, closed="both")
        )
        .select(["date", "asset", "raw_factor", "valid_for_factor_compute"])
        .sort(["date", "asset"])
        .collect()
    )
    quality = _quality(computed, time.perf_counter() - started)
    if quality.row_count == 0 or quality.null_ratio == 1.0:
        raise FactorMinerError(
            FailureCode.FACTOR_ALL_NULL,
            "保存区间内原始因子全部为空",
        )
    resolved_output = output_path.expanduser().resolve(strict=False)
    _validate_output_boundary(source, resolved_output)
    _atomic_write_parquet(computed, resolved_output)
    return FactorArtifact(
        artifact_path=resolved_output,
        plan_hash=plan.plan_hash,
        quality=quality,
        artifact_sha256=_sha256_file(resolved_output),
    )


def _validate_request(plan: CompiledFactorPlan, request: DataRequest) -> None:
    """验证请求覆盖计划字段和 lookback。"""

    missing = set(plan.required_fields) - set(request.required_fields)
    if missing:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"数据请求缺少编译计划字段：{sorted(missing)}",
        )
    if request.warmup_days < plan.required_lookback:
        raise FactorMinerError(
            FailureCode.FACTOR_COVERAGE_TOO_LOW,
            "数据请求 warmup 不足以覆盖编译计划 lookback",
        )


def _validate_trusted_request(
    plan: CompiledFactorPlan,
    request: FactorInputRequest,
) -> None:
    """验证 trusted input 请求覆盖计划字段和观察数 warmup。"""

    missing = set(plan.required_fields) - set(request.required_fields)
    if missing:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            f"input 请求缺少编译计划字段：{sorted(missing)}",
        )
    if request.warmup_observations < plan.required_lookback:
        raise FactorMinerError(
            FailureCode.FACTOR_COVERAGE_TOO_LOW,
            "input 请求 warmup observations 不足以覆盖编译计划 lookback",
        )


def _validate_output_boundary(source: object, output_path: Path) -> None:
    """拒绝将 artifact 写入数据源暴露的 QuantLake 根目录。"""

    quantlake_root = getattr(source, "quantlake_root", None)
    if quantlake_root is None:
        return
    resolved_root = Path(quantlake_root).expanduser().resolve(strict=False)
    if output_path == resolved_root or resolved_root in output_path.parents:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "原始因子 artifact 不能写入 QuantLake 根目录或其子目录",
        )


def _quality(frame: pl.DataFrame, elapsed_seconds: float) -> FactorQuality:
    """从最终保存区间计算质量摘要，不改变原始值。"""

    row_count = frame.height
    null_count = frame.get_column("raw_factor").null_count()
    daily = (
        frame.group_by("date", maintain_order=True)
        .agg(
            pl.col("raw_factor").is_not_null().sum().alias("valid_count"),
            pl.len().alias("total_count"),
        )
        .with_columns(
            (pl.col("valid_count") / pl.col("total_count")).cast(pl.Float64).alias("coverage")
        )
        .sort("date")
    )
    all_null_dates = tuple(
        daily.filter(pl.col("valid_count") == 0).get_column("date").to_list()
    )
    median = daily.get_column("coverage").median()
    return FactorQuality(
        row_count=row_count,
        date_count=frame.get_column("date").n_unique(),
        asset_count=frame.get_column("asset").n_unique(),
        null_ratio=(null_count / row_count) if row_count else 1.0,
        median_daily_coverage=float(median) if median is not None else 0.0,
        all_null_dates=all_null_dates,
        elapsed_seconds=max(0.0, float(elapsed_seconds)),
    )


def _atomic_write_parquet(frame: pl.DataFrame, output_path: Path) -> None:
    """将 Parquet 写入同目录临时文件后原子替换目标。"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        frame.write_parquet(temporary_path, compression="zstd", statistics=True)
        descriptor = os.open(temporary_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_path, output_path)
        directory_descriptor = os.open(output_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _sha256_file(path: Path) -> str:
    """计算已写入 artifact 的完整 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
