"""内容寻址、原子发布且可核验的 V0.4 覆盖图谱快照。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.coverage_cluster import CoverageClusterAssignments
from factor_miner.coverage_performance import FactorPerformanceSummary
from factor_miner.coverage_regime import RegimeFactorProfile
from factor_miner.coverage_schema import (
    CoverageFactorNode,
    RegisteredCoverageGraphSpec,
)
from factor_miner.coverage_signal import SignalPatternEdge
from factor_miner.coverage_structure import StructuralEdge
from factor_miner.errors import FactorMinerError, FailureCode


_GRAPH_FILES = (
    "build_spec.json",
    "factor_nodes.jsonl",
    "structural_edges.parquet",
    "signal_pattern_edges.parquet",
    "factor_performance.parquet",
    "regime_profiles.parquet",
    "cluster_assignments.json",
    "coverage_summary.json",
)


class CoverageGraphManifest(BaseModel):
    """覆盖图谱正式文件集合和完整输入身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: str = "1"
    coverage_graph_id: str = Field(pattern=r"^covgraph_[0-9a-f]{24}$")
    coverage_spec_id: str = Field(pattern=r"^covspec_[0-9a-f]{24}$")
    regime_snapshot_id: str = Field(pattern=r"^regsnap_[0-9a-f]{24}$")
    factor_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    daily_ic_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible_start: date
    visible_end: date
    algorithm_version: str
    runtime_provenance: dict[str, str]
    factor_count: int = Field(ge=1)
    structural_edge_count: int = Field(ge=0)
    signal_edge_count: int = Field(ge=0)
    factor_performance_count: int = Field(ge=1)
    regime_profile_count: int = Field(ge=1)
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class CoverageGraphBuildResult:
    """已发布图谱的位置与核验清单。"""

    coverage_graph_id: str
    snapshot_root: Path
    manifest: CoverageGraphManifest


def coverage_catalog_sha256(nodes: Sequence[CoverageFactorNode]) -> str:
    """从排序后的完整节点合同生成目录内容哈希。"""

    payload = [
        node.model_dump(mode="json")
        for node in sorted(nodes, key=lambda item: item.factor_id)
    ]
    return sha256_json(payload)


def publish_coverage_graph(
    *,
    artifact_root: Path,
    registered_spec: RegisteredCoverageGraphSpec,
    nodes: Sequence[CoverageFactorNode],
    structural_edges: Sequence[StructuralEdge],
    signal_edges: Sequence[SignalPatternEdge],
    factor_performance: Sequence[FactorPerformanceSummary],
    regime_profiles: Sequence[RegimeFactorProfile],
    clusters: CoverageClusterAssignments,
) -> CoverageGraphBuildResult:
    """在独立 staging 中构建并原子发布完整图谱。"""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: item.factor_id))
    ordered_structural = tuple(
        sorted(structural_edges, key=lambda item: (item.factor_a, item.factor_b))
    )
    ordered_signal = tuple(
        sorted(signal_edges, key=lambda item: (item.factor_a, item.factor_b))
    )
    ordered_performance = tuple(
        sorted(factor_performance, key=lambda item: item.factor_id)
    )
    ordered_profiles = tuple(
        sorted(
            regime_profiles,
            key=lambda item: (item.factor_id, item.canonical_state_id),
        )
    )
    _validate_graph_inputs(
        registered_spec,
        ordered_nodes,
        ordered_structural,
        ordered_signal,
        ordered_performance,
        ordered_profiles,
        clusters,
    )
    staging = (
        Path(artifact_root)
        / "artifacts"
        / ".staging"
        / f"coverage_{uuid4().hex}"
    )
    staging.mkdir(parents=True)
    try:
        _write_json(staging / "build_spec.json", registered_spec)
        _write_jsonl(staging / "factor_nodes.jsonl", ordered_nodes)
        _write_parquet(staging / "structural_edges.parquet", ordered_structural)
        _write_parquet(staging / "signal_pattern_edges.parquet", ordered_signal)
        _write_parquet(staging / "factor_performance.parquet", ordered_performance)
        _write_parquet(staging / "regime_profiles.parquet", ordered_profiles)
        _write_json(staging / "cluster_assignments.json", clusters)
        _write_json(
            staging / "coverage_summary.json",
            _coverage_summary(
                ordered_nodes,
                ordered_structural,
                ordered_signal,
                ordered_performance,
                ordered_profiles,
                clusters,
            ),
        )
        file_hashes = {
            name: _sha256_file(staging / name)
            for name in _GRAPH_FILES
        }
        spec = registered_spec.spec
        identity_payload = {
            "manifest_version": "1",
            "coverage_spec_id": registered_spec.coverage_spec_id,
            "regime_snapshot_id": spec.regime_snapshot_id,
            "factor_catalog_sha256": spec.factor_catalog_sha256,
            "daily_ic_manifest_sha256": spec.daily_ic_manifest_sha256,
            "visible_start": spec.visible_start.isoformat(),
            "visible_end": spec.visible_end.isoformat(),
            "algorithm_version": spec.algorithm_version,
            "runtime_provenance": spec.runtime_provenance,
            "factor_count": len(ordered_nodes),
            "structural_edge_count": len(ordered_structural),
            "signal_edge_count": len(ordered_signal),
            "factor_performance_count": len(ordered_performance),
            "regime_profile_count": len(ordered_profiles),
            "files": file_hashes,
        }
        identifier = f"covgraph_{sha256_json(identity_payload)[:24]}"
        manifest = CoverageGraphManifest(
            coverage_graph_id=identifier,
            **identity_payload,
        )
        _write_json(staging / "manifest.json", manifest)
        final_root = (
            Path(artifact_root)
            / "artifacts"
            / "coverage_graphs"
            / identifier
        )
        final_root.parent.mkdir(parents=True, exist_ok=True)
        if final_root.exists():
            verified = verify_coverage_graph(final_root)
            shutil.rmtree(staging)
            return verified
        os.replace(staging, final_root)
        return verify_coverage_graph(final_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_coverage_graph(snapshot_root: Path) -> CoverageGraphBuildResult:
    """只读核对文件集合、逐文件哈希、行数和内容身份。"""

    root = Path(snapshot_root)
    try:
        manifest = CoverageGraphManifest.model_validate_json(
            (root / "manifest.json").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise _snapshot_error("覆盖图谱 manifest 缺失或非法") from error
    expected_names = {"manifest.json", *_GRAPH_FILES}
    try:
        actual_names = {path.name for path in root.iterdir()}
    except OSError as error:
        raise _snapshot_error("覆盖图谱目录不可读") from error
    if actual_names != expected_names:
        raise _snapshot_error("覆盖图谱文件集合不完整或包含未登记文件")
    if root.name != manifest.coverage_graph_id:
        raise _snapshot_error("覆盖图谱目录名与 manifest 身份不一致")
    if set(manifest.files) != set(_GRAPH_FILES):
        raise _snapshot_error("覆盖图谱 manifest 文件清单不完整")
    for name, expected_hash in manifest.files.items():
        if _sha256_file(root / name) != expected_hash:
            raise _snapshot_error(f"覆盖图谱文件哈希不一致：{name}")
    identity_payload = manifest.model_dump(mode="json")
    identity_payload.pop("coverage_graph_id")
    expected_id = f"covgraph_{sha256_json(identity_payload)[:24]}"
    if manifest.coverage_graph_id != expected_id:
        raise _snapshot_error("覆盖图谱内容身份不一致")
    _verify_counts(root, manifest)
    return CoverageGraphBuildResult(
        coverage_graph_id=manifest.coverage_graph_id,
        snapshot_root=root,
        manifest=manifest,
    )


def _validate_graph_inputs(
    registered_spec: RegisteredCoverageGraphSpec,
    nodes: tuple[CoverageFactorNode, ...],
    structural_edges: tuple[StructuralEdge, ...],
    signal_edges: tuple[SignalPatternEdge, ...],
    performance: tuple[FactorPerformanceSummary, ...],
    profiles: tuple[RegimeFactorProfile, ...],
    clusters: CoverageClusterAssignments,
) -> None:
    """发布前核对节点、边、画像和冻结输入身份。"""

    factor_ids = tuple(node.factor_id for node in nodes)
    if not factor_ids or len(set(factor_ids)) != len(factor_ids):
        raise _snapshot_error("覆盖图谱节点必须非空且 factor_id 唯一")
    if coverage_catalog_sha256(nodes) != registered_spec.spec.factor_catalog_sha256:
        raise _snapshot_error("覆盖节点与 factor_catalog_sha256 不一致")
    expected_pairs = len(nodes) * (len(nodes) - 1) // 2
    if len(structural_edges) != expected_pairs or len(signal_edges) != expected_pairs:
        raise _snapshot_error("覆盖图谱两层边数不等于无序因子对数量")
    expected_endpoints = {
        (factor_ids[first], factor_ids[second])
        for first in range(len(factor_ids))
        for second in range(first + 1, len(factor_ids))
    }
    if {
        (edge.factor_a, edge.factor_b) for edge in structural_edges
    } != expected_endpoints or {
        (edge.factor_a, edge.factor_b) for edge in signal_edges
    } != expected_endpoints:
        raise _snapshot_error("覆盖图谱边端点不完整或重复")
    if tuple(item.factor_id for item in performance) != factor_ids:
        raise _snapshot_error("覆盖图谱当前历史表现未覆盖全部因子")
    states = {
        annotation.canonical_state_id
        for annotation in registered_spec.spec.regime_annotations
    }
    expected_profiles = {
        (factor_id, state_id)
        for factor_id in factor_ids
        for state_id in states
    }
    if {
        (profile.factor_id, profile.canonical_state_id)
        for profile in profiles
    } != expected_profiles:
        raise _snapshot_error("覆盖图谱状态画像不完整或重复")
    if (
        set(clusters.factor_to_structural_cluster) != set(factor_ids)
        or set(clusters.factor_to_signal_cluster) != set(factor_ids)
    ):
        raise _snapshot_error("覆盖图谱聚类未覆盖全部因子")


def _coverage_summary(
    nodes: Sequence[CoverageFactorNode],
    structural_edges: Sequence[StructuralEdge],
    signal_edges: Sequence[SignalPatternEdge],
    performance: Sequence[FactorPerformanceSummary],
    profiles: Sequence[RegimeFactorProfile],
    clusters: CoverageClusterAssignments,
) -> dict[str, Any]:
    """生成不含完整序列的确定性聚合摘要。"""

    return {
        "factor_count": len(nodes),
        "pair_count": len(signal_edges),
        "category_counts": dict(sorted(Counter(node.category for node in nodes).items())),
        "structure_source_counts": dict(
            sorted(Counter(node.structure_source for node in nodes).items())
        ),
        "strong_structural_edges": sum(
            edge.strong_structure_edge for edge in structural_edges
        ),
        "comparable_signal_edges": sum(edge.comparable for edge in signal_edges),
        "strong_signal_edges": sum(edge.strong_signal_edge for edge in signal_edges),
        "mean_rank_ic_by_factor": {
            item.factor_id: item.mean_rank_ic
            for item in performance
        },
        "uncomparable_reason_counts": dict(
            sorted(
                Counter(
                    edge.reason
                    for edge in signal_edges
                    if not edge.comparable and edge.reason is not None
                ).items()
            )
        ),
        "regime_labels": [
            {
                "canonical_state_id": profile.canonical_state_id,
                "economic_label": profile.economic_label,
            }
            for profile in _unique_regime_labels(profiles)
        ],
        "structural_cluster_count": len(clusters.structural_clusters),
        "signal_cluster_count": len(clusters.signal_clusters),
    }


def _unique_regime_labels(
    profiles: Sequence[RegimeFactorProfile],
) -> tuple[RegimeFactorProfile, ...]:
    """按状态 ID 保留一条稳定标签记录。"""

    by_state: dict[int, RegimeFactorProfile] = {}
    for profile in profiles:
        existing = by_state.setdefault(profile.canonical_state_id, profile)
        if existing.economic_label != profile.economic_label:
            raise _snapshot_error("同一 canonical state 出现不同中文名称")
    return tuple(by_state[state] for state in sorted(by_state))


def _write_json(path: Path, value: Any) -> None:
    """写入规范 JSON 并同步落盘。"""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    with path.open("wb") as stream:
        stream.write(canonical_json_bytes(payload))
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_jsonl(path: Path, values: Sequence[BaseModel]) -> None:
    """按排序后的模型序列写入规范 JSONL。"""

    with path.open("wb") as stream:
        for value in values:
            stream.write(canonical_json_bytes(value.model_dump(mode="json")))
            stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_parquet(path: Path, values: Sequence[BaseModel]) -> None:
    """把模型记录写成排序已在上游冻结的 Parquet。"""

    records = [value.model_dump(mode="json") for value in values]
    pl.DataFrame(records, infer_schema_length=None).write_parquet(
        path,
        compression="zstd",
        statistics=True,
    )


def _sha256_file(path: Path) -> str:
    """返回文件字节 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_counts(
    root: Path,
    manifest: CoverageGraphManifest,
) -> None:
    """解析正式产物并核对清单行数。"""

    try:
        factor_count = sum(
            1
            for line in (root / "factor_nodes.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        )
        structural_count = pl.read_parquet(
            root / "structural_edges.parquet"
        ).height
        signal_count = pl.read_parquet(
            root / "signal_pattern_edges.parquet"
        ).height
        performance_count = pl.read_parquet(
            root / "factor_performance.parquet"
        ).height
        profile_count = pl.read_parquet(root / "regime_profiles.parquet").height
    except (OSError, pl.exceptions.PolarsError) as error:
        raise _snapshot_error("覆盖图谱正式文件无法解析") from error
    if (
        factor_count != manifest.factor_count
        or structural_count != manifest.structural_edge_count
        or signal_count != manifest.signal_edge_count
        or performance_count != manifest.factor_performance_count
        or profile_count != manifest.regime_profile_count
    ):
        raise _snapshot_error("覆盖图谱 manifest 行数与文件不一致")


def _snapshot_error(message: str) -> FactorMinerError:
    """构造稳定的图谱损坏错误。"""

    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)
