"""V0.4 YAML、每日 IC、状态快照到覆盖图谱的正式编排。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl

from factor_miner.coverage_catalog import load_legacy_factor_catalog
from factor_miner.coverage_cluster import build_coverage_clusters
from factor_miner.coverage_performance import summarize_factor_performance
from factor_miner.coverage_regime import build_regime_profiles
from factor_miner.coverage_schema import RegisteredCoverageGraphSpec
from factor_miner.coverage_signal import build_signal_pattern_edges
from factor_miner.coverage_snapshot import (
    CoverageGraphBuildResult,
    coverage_catalog_sha256,
    publish_coverage_graph,
)
from factor_miner.coverage_structure import build_structural_edges
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_snapshot import verify_regime_snapshot


def build_coverage_graph(
    *,
    catalog_path: Path,
    daily_ic_path: Path,
    regime_snapshot_root: Path,
    artifact_root: Path,
    registered_spec: RegisteredCoverageGraphSpec,
) -> CoverageGraphBuildResult:
    """核对全部冻结身份后，确定性构建并发布 V0.4 图谱。"""

    spec = registered_spec.spec
    regime = verify_regime_snapshot(regime_snapshot_root)
    if regime.regime_snapshot_id != spec.regime_snapshot_id:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "CoverageGraphSpec 引用的状态快照与输入目录不一致",
        )
    nodes = load_legacy_factor_catalog(catalog_path)
    if coverage_catalog_sha256(nodes) != spec.factor_catalog_sha256:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "生产因子目录内容与 factor_catalog_sha256 不一致",
        )
    if _sha256_file(daily_ic_path) != spec.daily_ic_manifest_sha256:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每日 IC 文件与 daily_ic_manifest_sha256 不一致",
        )
    try:
        daily_ic = pl.read_parquet(daily_ic_path)
        filtered_regimes = pl.read_parquet(
            regime.snapshot_root / "filtered_regimes.parquet"
        )
    except (OSError, pl.exceptions.PolarsError) as error:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "覆盖图谱输入 Parquet 无法读取",
        ) from error
    _validate_daily_ic_against_spec(daily_ic, registered_spec)
    structural_edges = build_structural_edges(nodes, spec.structural_policy)
    signal_edges = build_signal_pattern_edges(
        nodes,
        daily_ic,
        spec.pair_policy,
    )
    performance = summarize_factor_performance(nodes, daily_ic)
    profiles = build_regime_profiles(
        nodes,
        daily_ic,
        filtered_regimes,
        regime.regime_snapshot_id,
        spec.regime_annotations,
        spec.regime_policy,
    )
    clusters = build_coverage_clusters(
        nodes,
        structural_edges,
        signal_edges,
        spec.cluster_policy,
    )
    return publish_coverage_graph(
        artifact_root=artifact_root,
        registered_spec=registered_spec,
        nodes=nodes,
        structural_edges=structural_edges,
        signal_edges=signal_edges,
        factor_performance=performance,
        regime_profiles=profiles,
        clusters=clusters,
    )


def _validate_daily_ic_against_spec(
    daily_ic: pl.DataFrame,
    registered_spec: RegisteredCoverageGraphSpec,
) -> None:
    """每日 IC 的统一评价身份必须与冻结 Spec 完全相同。"""

    spec = registered_spec.spec
    expected = {
        "evaluation_policy_id": spec.evaluation_policy_id,
        "data_release_id": spec.data_release_id,
        "label_id": spec.label_id,
        "visible_start": spec.visible_start,
        "visible_end": spec.visible_end,
    }
    missing = set(expected) - set(daily_ic.columns)
    if missing:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"每日 IC 缺少冻结评价身份列：{sorted(missing)}",
        )
    identities = daily_ic.select(list(expected)).unique()
    if identities.height != 1 or identities.row(0, named=True) != expected:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每日 IC 评价身份与 CoverageGraphSpec 不一致",
        )
    dates = daily_ic.get_column("date")
    if dates.min() != spec.visible_start or dates.max() != spec.visible_end:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "每日 IC 实际日期边界与冻结可见区间不一致",
        )


def _sha256_file(path: Path) -> str:
    """返回输入文件字节 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            f"覆盖图谱输入文件不可读：{path}",
        ) from error
    return digest.hexdigest()
