"""内容寻址、原子发布且可复核的 V0.3 市场状态快照。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.data_source import InputProvenance
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_research import MonthlyCandidateResult
from factor_miner.regime_features import FEATURE_COLUMNS
from factor_miner.regime_schema import (
    RegimeAnnotation,
    RegisteredRegimeDeployment,
)


_SNAPSHOT_FILES = (
    "deployment_spec.json",
    "monthly_models.jsonl",
    "filtered_regimes.parquet",
    "market_features.parquet",
    "quality_diagnostics.json",
    "annotations.jsonl",
)


class RegimeSnapshotManifest(BaseModel):
    """状态快照的完整内容清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: str = "1"
    regime_snapshot_id: str = Field(pattern=r"^regsnap_[0-9a-f]{24}$")
    regime_deployment_id: str = Field(pattern=r"^regdeploy_[0-9a-f]{24}$")
    provenance: dict[str, str]
    visible_start: date
    visible_end: date
    state_count: int = Field(ge=2, le=4)
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class RegimeBuildResult:
    """已发布状态快照的位置与已验证清单。"""

    regime_snapshot_id: str
    snapshot_root: Path
    manifest: RegimeSnapshotManifest


# 保留语义更直观的别名，正式接口使用计划中冻结的 RegimeBuildResult。
RegimeSnapshotResult = RegimeBuildResult


def _snapshot_error(message: str) -> FactorMinerError:
    """构造稳定的快照损坏错误。"""

    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)


def _json_value(value: Any) -> Any:
    """把项目模型和数值对象递归转换为规范 JSON 值。"""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _model_record(result: MonthlyCandidateResult) -> dict[str, Any]:
    """序列化月度模型，但不在模型文件重复保存逐日概率。"""

    payload = _json_value(result)
    payload.pop("raw_probabilities", None)
    payload.pop("canonical_probabilities", None)
    payload.pop("quality", None)
    payload["model_id"] = _model_id(result)
    return payload


def _model_id(result: MonthlyCandidateResult) -> str:
    """从单月模型及其失败语义派生稳定身份。"""

    payload = {
        "candidate_id": result.candidate_id,
        "model_month": result.model_month,
        "status": result.status,
        "training_start": result.training_start.isoformat(),
        "training_end": result.training_end.isoformat(),
        "fit": _json_value(result.fit),
        "standardizer": _json_value(result.standardizer),
        "mapping": _json_value(result.mapping),
        "failure_code": result.failure_code,
    }
    return f"regmodel_{sha256_json(payload)[:24]}"


def build_filtered_regime_frame(
    monthly_results: tuple[MonthlyCandidateResult, ...],
    *,
    calendar_dates: tuple[date, ...],
    state_count: int,
) -> pl.DataFrame:
    """把月度 filtered 概率转成带下一交易日可用时点的正式日表。"""

    calendar = tuple(sorted(set(calendar_dates)))
    next_date = {
        current: following
        for current, following in zip(calendar, calendar[1:], strict=False)
    }
    rows: list[dict[str, Any]] = []
    for monthly in monthly_results:
        model_id = _model_id(monthly)
        if monthly.status == "ok":
            if (
                monthly.raw_probabilities is None
                or monthly.canonical_probabilities is None
                or monthly.raw_probabilities.shape
                != (len(monthly.inference_dates), state_count)
                or monthly.canonical_probabilities.shape
                != (len(monthly.inference_dates), state_count)
            ):
                raise _snapshot_error(
                    f"{monthly.model_month} 成功月份的 filtered 概率维度不一致"
                )
        for index, observation_date in enumerate(monthly.inference_dates):
            earliest_use_date = next_date.get(observation_date)
            if earliest_use_date is None:
                continue
            if monthly.status == "ok":
                raw = monthly.raw_probabilities[index]
                canonical = monthly.canonical_probabilities[index]
                positive = canonical[canonical > 0]
                entropy = -float(
                    np.sum(positive * np.log(positive))
                ) / math.log(state_count)
                row = {
                    "raw_state_id": int(np.argmax(raw)),
                    "canonical_state_id": int(np.argmax(canonical)),
                    "raw_state_probabilities": raw.tolist(),
                    "canonical_state_probabilities": canonical.tolist(),
                    "max_probability": float(np.max(canonical)),
                    "normalized_entropy": entropy,
                    "state_mapping_cost": (
                        monthly.mapping.total_cost
                        if monthly.mapping is not None
                        else None
                    ),
                    "mapping_policy_version": "1",
                    "failure_code": None,
                    "failure_message": None,
                }
            else:
                row = {
                    "raw_state_id": None,
                    "canonical_state_id": None,
                    "raw_state_probabilities": None,
                    "canonical_state_probabilities": None,
                    "max_probability": None,
                    "normalized_entropy": None,
                    "state_mapping_cost": None,
                    "mapping_policy_version": "1",
                    "failure_code": monthly.failure_code,
                    "failure_message": monthly.failure_message,
                }
            rows.append(
                {
                    "observation_date": observation_date,
                    "earliest_use_date": earliest_use_date,
                    "model_month": monthly.model_month,
                    "model_id": model_id,
                    "status": monthly.status,
                    **row,
                }
            )
    if not rows:
        raise _snapshot_error("状态快照没有任何可发布的下一交易日概率")
    return pl.DataFrame(rows).sort(["observation_date", "model_id"])


def _market_features_with_monthly_zscores(
    market_features: pl.DataFrame,
    monthly_results: tuple[MonthlyCandidateResult, ...],
) -> pl.DataFrame:
    """把每月只由历史训练窗口估计的 z-score 附加到原始市场特征。"""

    z_columns = tuple(f"{column}_zscore" for column in FEATURE_COLUMNS)
    rows: list[dict[str, Any]] = []
    by_date = {
        row["date"]: row
        for row in market_features.select(["date", *FEATURE_COLUMNS]).to_dicts()
    }
    for monthly in monthly_results:
        for observation_date in monthly.inference_dates:
            raw = by_date.get(observation_date)
            if raw is None:
                raise _snapshot_error(
                    f"{monthly.model_month} 的市场特征日期缺失：{observation_date}"
                )
            values: list[float | None]
            if monthly.status == "ok" and monthly.standardizer is not None:
                values = [
                    (
                        float(raw[column]) - monthly.standardizer.means[index]
                    )
                    / monthly.standardizer.scales[index]
                    for index, column in enumerate(FEATURE_COLUMNS)
                ]
            else:
                values = [None] * len(FEATURE_COLUMNS)
            rows.append(
                {
                    "date": observation_date,
                    "zscore_model_month": monthly.model_month,
                    **dict(zip(z_columns, values, strict=True)),
                }
            )
    if not rows:
        return market_features.with_columns(
            pl.lit(None, dtype=pl.String).alias("zscore_model_month"),
            *[
                pl.lit(None, dtype=pl.Float64).alias(column)
                for column in z_columns
            ],
        )
    zscores = pl.DataFrame(rows)
    return market_features.join(zscores, on="date", how="left", validate="1:1")


def _write_json(path: Path, value: Any) -> None:
    """写入规范 JSON 并落盘。"""

    with path.open("wb") as stream:
        stream.write(canonical_json_bytes(_json_value(value)))
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_jsonl(path: Path, values: tuple[Any, ...]) -> None:
    """写入规范 JSONL；空集合仍创建空文件。"""

    with path.open("wb") as stream:
        for value in values:
            stream.write(canonical_json_bytes(_json_value(value)))
            stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    """返回文件字节 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity_payload(manifest: RegimeSnapshotManifest | dict[str, Any]) -> dict[str, Any]:
    """返回排除自引用快照 ID 的清单身份负载。"""

    payload = (
        manifest.model_dump(mode="json")
        if isinstance(manifest, RegimeSnapshotManifest)
        else dict(manifest)
    )
    payload.pop("regime_snapshot_id", None)
    return payload


def publish_regime_snapshot(
    *,
    artifact_root: Path,
    deployment: RegisteredRegimeDeployment,
    provenance: InputProvenance,
    monthly_results: tuple[MonthlyCandidateResult, ...],
    market_features: pl.DataFrame,
    annotations: tuple[RegimeAnnotation, ...],
) -> RegimeBuildResult:
    """在独立 staging 中构建并原子发布完整状态快照。"""

    selected = tuple(
        item
        for item in monthly_results
        if item.candidate_id == deployment.spec.research_candidate_id
        and item.inference_end >= deployment.spec.deployment_start
    )
    if not selected:
        raise _snapshot_error("部署候选在部署开始日后没有月度结果")
    calendar_dates = tuple(market_features.get_column("date").to_list())
    filtered = build_filtered_regime_frame(
        selected,
        calendar_dates=calendar_dates,
        state_count=deployment.spec.state_count,
    ).filter(
        pl.col("observation_date") >= deployment.spec.deployment_start
    )
    if filtered.is_empty():
        raise _snapshot_error("部署开始日后没有可发布的 filtered 状态")
    staging_root = (
        Path(artifact_root)
        / "artifacts"
        / ".staging"
        / f"regime_{uuid4().hex}"
    )
    staging_root.mkdir(parents=True)
    try:
        _write_json(staging_root / "deployment_spec.json", deployment)
        _write_jsonl(
            staging_root / "monthly_models.jsonl",
            tuple(_model_record(item) for item in selected),
        )
        filtered.write_parquet(
            staging_root / "filtered_regimes.parquet",
            compression="zstd",
            statistics=True,
        )
        _market_features_with_monthly_zscores(
            market_features,
            selected,
        ).sort("date").write_parquet(
            staging_root / "market_features.parquet",
            compression="zstd",
            statistics=True,
        )
        _write_json(
            staging_root / "quality_diagnostics.json",
            {
                item.model_month: _json_value(item.quality)
                for item in selected
            },
        )
        _write_jsonl(staging_root / "annotations.jsonl", annotations)
        file_hashes = {
            name: _sha256_file(staging_root / name)
            for name in _SNAPSHOT_FILES
        }
        visible_dates = filtered.get_column("observation_date")
        manifest_payload = {
            "manifest_version": "1",
            "regime_deployment_id": deployment.regime_deployment_id,
            "provenance": asdict(provenance),
            "visible_start": visible_dates.min().isoformat(),
            "visible_end": visible_dates.max().isoformat(),
            "state_count": deployment.spec.state_count,
            "files": file_hashes,
        }
        identifier = f"regsnap_{sha256_json(manifest_payload)[:24]}"
        manifest = RegimeSnapshotManifest(
            regime_snapshot_id=identifier,
            **manifest_payload,
        )
        _write_json(staging_root / "manifest.json", manifest)
        final_root = Path(artifact_root) / "artifacts" / "regimes" / identifier
        final_root.parent.mkdir(parents=True, exist_ok=True)
        if final_root.exists():
            verified = verify_regime_snapshot(final_root)
            shutil.rmtree(staging_root)
            return verified
        os.replace(staging_root, final_root)
        return verify_regime_snapshot(final_root)
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise


def verify_regime_snapshot(snapshot_root: Path) -> RegimeBuildResult:
    """验证状态快照文件集合、逐文件哈希和内容身份。"""

    root = Path(snapshot_root)
    try:
        manifest = RegimeSnapshotManifest.model_validate_json(
            (root / "manifest.json").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise _snapshot_error("状态快照 manifest 缺失或非法") from error
    expected_names = {"manifest.json", *_SNAPSHOT_FILES}
    try:
        actual_names = {path.name for path in root.iterdir()}
    except OSError as error:
        raise _snapshot_error("状态快照目录不可读") from error
    if actual_names != expected_names:
        raise _snapshot_error("状态快照文件集合不完整或包含未登记文件")
    if root.name != manifest.regime_snapshot_id:
        raise _snapshot_error("状态快照目录名与 manifest 身份不一致")
    if set(manifest.files) != set(_SNAPSHOT_FILES):
        raise _snapshot_error("状态快照 manifest 文件清单不完整")
    for name, expected_hash in manifest.files.items():
        if _sha256_file(root / name) != expected_hash:
            raise _snapshot_error(f"状态快照文件哈希不一致：{name}")
    expected_id = f"regsnap_{sha256_json(_identity_payload(manifest))[:24]}"
    if manifest.regime_snapshot_id != expected_id:
        raise _snapshot_error("状态快照内容身份不一致")
    return RegimeBuildResult(
        regime_snapshot_id=manifest.regime_snapshot_id,
        snapshot_root=root,
        manifest=manifest,
    )
