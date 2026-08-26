"""覆盖图谱缺口核验、三类缺口报告与外发简报脱敏边界。"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any

import polars as pl

from factor_miner.canonical import sha256_json
from factor_miner.coverage_cluster import CoverageClusterAssignments
from factor_miner.coverage_performance import FactorPerformanceSummary
from factor_miner.coverage_regime import RegimeFactorProfile
from factor_miner.coverage_schema import CoverageFactorNode, RegisteredCoverageGraphSpec
from factor_miner.coverage_signal import SignalPatternEdge
from factor_miner.coverage_snapshot import CoverageGraphManifest, verify_coverage_graph
from factor_miner.coverage_structure import StructuralEdge
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_brief import GapSelectionPolicy
from factor_miner.research_evolution_schema import (
    CoverageGapCard,
    CoverageGapReport,
    DesignDiversityPolicy,
    GapCategory,
    ResearchMemorySnapshot,
    build_memory_snapshot_identity,
)

_GRAPH_FILES = (
    "build_spec.json",
    "factor_nodes.jsonl",
    "structural_edges.parquet",
    "signal_pattern_edges.parquet",
    "factor_performance.parquet",
    "regime_profiles.parquet",
    "cluster_assignments.json",
    "coverage_summary.json",
    "manifest.json",
)
_FIELD_ALIAS_MAP = {
    "close": "close",
    "prev_close": "price_close",
    "price_close": "price_close",
    "open": "open",
    "price_open": "price_open",
    "high": "high",
    "price_high": "price_high",
    "low": "low",
    "price_low": "price_low",
    "volume": "volume",
    "amount": "amount",
    "turnover": "turnover",
    "market_cap": "market_cap",
    "free_circulation": "market_cap",
    "size_free_circulation": "market_cap",
    "money": "amount",
    "vwap": "vwap",
    "returns": "returns",
}
_OPERATOR_FAMILY_MAP = {
    "add": "arithmetic",
    "sub": "arithmetic",
    "mul": "arithmetic",
    "div": "arithmetic",
    "ratio": "arithmetic",
    "log": "arithmetic",
    "abs": "arithmetic",
    "sign": "arithmetic",
    "pow": "arithmetic",
    "mean": "aggregation",
    "sum": "aggregation",
    "prod": "aggregation",
    "avg": "aggregation",
    "rolling_mean": "rolling",
    "rolling_sum": "rolling",
    "rolling_std": "rolling",
    "rolling_min": "rolling",
    "rolling_max": "rolling",
    "rolling_rank": "rolling",
    "ma": "rolling",
    "ema": "rolling",
    "sma": "rolling",
    "std": "rolling",
    "var": "rolling",
    "min": "rolling",
    "max": "rolling",
    "ts_rank": "rolling",
    "rank_ts": "rolling",
    "delta": "temporal",
    "delay": "temporal",
    "lag": "temporal",
    "shift": "temporal",
    "diff": "temporal",
    "rank": "rank",
    "rank_cs": "rank",
    "ranking": "ranking",
    "cs_rank": "rank",
    "percentile": "ranking",
    "zscore": "cross_sectional",
    "neutralize": "cross_sectional",
    "corr": "interaction",
    "cov": "interaction",
    "beta": "interaction",
    "interaction": "interaction",
    "where": "conditional",
    "if": "conditional",
    "clip": "conditional",
}
_SORT_POLICY_VERSION = "task2-gap-report-v1"
_FORBIDDEN_TEXT = (
    "rank_ic",
    "icir",
    "sharpe",
    "max_drawdown",
    "收益",
    "夏普",
    "最大回撤",
    "ticker",
    "secret",
    "api_key",
    "token",
)
_DATE_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_FORBIDDEN_USER_PATH_MARKER = "/" + "Users/"
_PATH_PATTERN = re.compile(r"(^/)|([A-Za-z]:\\\\)|(/data/)|(" + re.escape(_FORBIDDEN_USER_PATH_MARKER) + r")")
_TICKER_PATTERN = re.compile(r"\b[0-9]{6}\.(SZ|SH)\b|\b[A-Z]{2,5}\b")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{12,64}$")


@dataclass(frozen=True, slots=True)
class VerifiedCoverageGraph:
    """已完成 manifest、内容和主键核验的覆盖图谱只读视图。"""

    root: Path
    manifest: CoverageGraphManifest
    registered_spec: RegisteredCoverageGraphSpec
    factor_nodes: tuple[CoverageFactorNode, ...]
    structural_edges: tuple[StructuralEdge, ...]
    signal_edges: tuple[SignalPatternEdge, ...]
    factor_performance: tuple[FactorPerformanceSummary, ...]
    regime_profiles: tuple[RegimeFactorProfile, ...]
    clusters: CoverageClusterAssignments
    coverage_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class _GapCandidate:
    """内部缺口候选；最终会被投影为冻结的公共 gap card。"""

    gap_category: GapCategory
    canonical_gap_hash: str
    sanitized_labels: tuple[str, ...]
    allowed_field_aliases: tuple[str, ...]
    allowed_operator_families: tuple[str, ...]
    temporal_window_bins: tuple[str, ...]
    structure_cluster_count_band: str
    signal_cluster_count_band: str
    missing_or_failure_risk: tuple[str, ...]


def load_verified_coverage_graph(root: Path) -> VerifiedCoverageGraph:
    """加载并核验显式传入 root 指向的 coverage graph。"""

    snapshot = verify_coverage_graph(root)
    base = Path(root)
    registered_spec = RegisteredCoverageGraphSpec.model_validate_json(
        _read_bytes(base / "build_spec.json", "覆盖图谱 build_spec 缺失")
    )
    if registered_spec.coverage_spec_id != snapshot.manifest.coverage_spec_id:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 coverage_spec_id 不一致")
    if registered_spec.spec.regime_snapshot_id != snapshot.manifest.regime_snapshot_id:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 regime_snapshot_id 不一致")
    if registered_spec.spec.factor_catalog_sha256 != snapshot.manifest.factor_catalog_sha256:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 factor_catalog_sha256 不一致")
    if registered_spec.spec.daily_ic_manifest_sha256 != snapshot.manifest.daily_ic_manifest_sha256:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 daily_ic_manifest_sha256 不一致")
    if registered_spec.spec.algorithm_version != snapshot.manifest.algorithm_version:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 algorithm_version 不一致")
    if registered_spec.spec.visible_start != snapshot.manifest.visible_start:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 visible_start 不一致")
    if registered_spec.spec.visible_end != snapshot.manifest.visible_end:
        raise _graph_error("覆盖图谱 build_spec 与 manifest 的 visible_end 不一致")

    factor_nodes = _read_jsonl_models(base / "factor_nodes.jsonl", CoverageFactorNode, "覆盖图谱 factor_nodes 缺失")
    structural_edges = _read_parquet_models(base / "structural_edges.parquet", StructuralEdge, "覆盖图谱 structural_edges 缺失")
    signal_edges = _read_parquet_models(base / "signal_pattern_edges.parquet", SignalPatternEdge, "覆盖图谱 signal_pattern_edges 缺失")
    factor_performance = _read_parquet_models(base / "factor_performance.parquet", FactorPerformanceSummary, "覆盖图谱 factor_performance 缺失")
    regime_profiles = _read_parquet_models(base / "regime_profiles.parquet", RegimeFactorProfile, "覆盖图谱 regime_profiles 缺失")
    clusters = CoverageClusterAssignments.model_validate_json(
        _read_bytes(base / "cluster_assignments.json", "覆盖图谱 cluster_assignments 缺失")
    )
    coverage_summary = _read_json_dict(base / "coverage_summary.json", "覆盖图谱 coverage_summary 缺失")

    factor_ids = tuple(node.factor_id for node in factor_nodes)
    _require_unique(factor_ids, "覆盖图谱 factor_nodes 的 factor_id")
    expected_pairs = {
        (factor_ids[first], factor_ids[second])
        for first in range(len(factor_ids))
        for second in range(first + 1, len(factor_ids))
    }
    structural_pairs = tuple((edge.factor_a, edge.factor_b) for edge in structural_edges)
    signal_pairs = tuple((edge.factor_a, edge.factor_b) for edge in signal_edges)
    _require_unique(structural_pairs, "覆盖图谱 structural_edges 的因子对")
    _require_unique(signal_pairs, "覆盖图谱 signal_pattern_edges 的因子对")
    if set(structural_pairs) != expected_pairs:
        raise _graph_error("覆盖图谱 structural_edges 因子对集合不完整")
    if set(signal_pairs) != expected_pairs:
        raise _graph_error("覆盖图谱 signal_pattern_edges 因子对集合不完整")

    performance_ids = tuple(item.factor_id for item in factor_performance)
    _require_unique(performance_ids, "覆盖图谱 factor_performance 的 factor_id")
    if performance_ids != factor_ids:
        raise _graph_error("覆盖图谱 factor_performance 未与 factor_nodes 一一对应")

    state_ids = tuple(
        annotation.canonical_state_id
        for annotation in registered_spec.spec.regime_annotations
    )
    expected_profiles = {
        (factor_id, state_id)
        for factor_id in factor_ids
        for state_id in state_ids
    }
    profile_pairs = tuple(
        (profile.factor_id, profile.canonical_state_id) for profile in regime_profiles
    )
    _require_unique(profile_pairs, "覆盖图谱 regime_profiles 的主键")
    if set(profile_pairs) != expected_profiles:
        raise _graph_error("覆盖图谱 regime_profiles 与 regime_annotations 不一致")
    label_by_state = {
        annotation.canonical_state_id: annotation.economic_label
        for annotation in registered_spec.spec.regime_annotations
    }
    for profile in regime_profiles:
        if label_by_state[profile.canonical_state_id] != profile.economic_label:
            raise _graph_error("覆盖图谱 regime_profiles 的状态标签与 build_spec 不一致")

    if clusters.algorithm != registered_spec.spec.cluster_policy.algorithm:
        raise _graph_error("覆盖图谱 cluster_assignments 的 algorithm 与 build_spec 不一致")
    if set(clusters.factor_to_structural_cluster) != set(factor_ids):
        raise _graph_error("覆盖图谱 structural cluster 未覆盖全部 factor_id")
    if set(clusters.factor_to_signal_cluster) != set(factor_ids):
        raise _graph_error("覆盖图谱 signal cluster 未覆盖全部 factor_id")
    _verify_cluster_membership(clusters.structural_clusters, factor_ids, "structural")
    _verify_cluster_membership(clusters.signal_clusters, factor_ids, "signal")

    _verify_coverage_summary(
        coverage_summary=coverage_summary,
        factor_nodes=factor_nodes,
        structural_edges=structural_edges,
        signal_edges=signal_edges,
        factor_performance=factor_performance,
        regime_profiles=regime_profiles,
        clusters=clusters,
    )
    return VerifiedCoverageGraph(
        root=base,
        manifest=snapshot.manifest,
        registered_spec=registered_spec,
        factor_nodes=factor_nodes,
        structural_edges=structural_edges,
        signal_edges=signal_edges,
        factor_performance=factor_performance,
        regime_profiles=regime_profiles,
        clusters=clusters,
        coverage_summary=coverage_summary,
    )


def build_coverage_gap_report(
    graph: VerifiedCoverageGraph,
    memory_snapshot: ResearchMemorySnapshot,
    *,
    policy: GapSelectionPolicy,
) -> CoverageGapReport:
    """基于已核验图谱与冻结记忆快照构建三类 coverage gap report。"""

    build_memory_snapshot_identity(memory_snapshot)
    if memory_snapshot.coverage_graph_id != graph.manifest.coverage_graph_id:
        raise FactorMinerError(
            FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
            "记忆快照 coverage_graph_id 与覆盖图谱不一致",
        )
    if memory_snapshot.coverage_graph_manifest_hash != _manifest_hash(graph.manifest):
        raise FactorMinerError(
            FailureCode.MEMORY_IMMUTABILITY_VIOLATION,
            "记忆快照 coverage_graph_manifest_hash 与覆盖图谱不一致",
        )

    candidates = [
        *list(_structural_gap_candidates(graph)),
        *list(_market_gap_candidates(graph)),
        *list(_data_availability_gap_candidates(graph)),
    ]
    unique_by_hash: dict[str, _GapCandidate] = {}
    for candidate in candidates:
        unique_by_hash.setdefault(candidate.canonical_gap_hash, candidate)
    selected = _select_gap_candidates(tuple(unique_by_hash.values()), policy)
    if not selected:
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            "覆盖图谱不存在可发布的三类缺口",
        )
    truncated_count = max(0, len(unique_by_hash) - len(selected))
    truncation_reason = (
        "未发生截断"
        if truncated_count == 0
        else "候选缺口超过冻结输出上限，按类别、风险与 canonical gap hash 稳定截断"
    )

    cards = tuple(
        sorted(
            (
                _candidate_to_card(candidate)
                for candidate in selected
            ),
            key=lambda item: (item.gap_category.value, item.card_sha256, item.gap_id),
        )
    )
    policy_payload = {
        "version": _SORT_POLICY_VERSION,
        "max_cards": policy.max_cards,
        "max_cards_per_category": policy.max_cards_per_category,
        "risk_rank": _risk_rank_map(),
        "selected_hashes": [candidate.canonical_gap_hash for candidate in selected],
    }
    return CoverageGapReport(
        coverage_graph_id=graph.manifest.coverage_graph_id,
        graph_manifest_hash=_manifest_hash(graph.manifest),
        regime_snapshot_hash=_regime_snapshot_hash(graph),
        memory_snapshot_hash=memory_snapshot.snapshot_sha256,
        gap_cards=cards,
        truncated_count=truncated_count,
        truncation_reason=truncation_reason,
        internal_sort_policy_hash=sha256_json(policy_payload),
    )


def build_sanitized_gap_brief(
    report: CoverageGapReport,
    *,
    field_registry_hash: str,
    design_policy: DesignDiversityPolicy,
) -> dict[str, object]:
    """把 gap report 投影为仅含公开标签的外发 brief。"""

    if not _HASH_PATTERN.fullmatch(field_registry_hash):
        raise FactorMinerError(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "field_registry_hash 必须是 SHA-256",
        )
    required_axes = tuple(sorted(design_policy.allowed_structure_axes))
    payload: dict[str, object] = {
        "brief_version": "1",
        "coverage_graph_id": report.coverage_graph_id,
        "graph_manifest_hash": report.graph_manifest_hash,
        "memory_snapshot_hash": report.memory_snapshot_hash,
        "field_registry_hash": field_registry_hash,
        "design_policy": {
            "hypotheses_per_round": design_policy.hypotheses_per_round,
            "designs_per_hypothesis": design_policy.designs_per_hypothesis,
            "arms_per_round": design_policy.arms_per_round,
            "total_slots": design_policy.total_slots,
            "allowed_structure_axes": required_axes,
            "forbid_temporal_only_change": design_policy.forbid_temporal_only_change,
            "max_single_hypothesis_output_correlation": design_policy.max_single_hypothesis_output_correlation,
            "failure_code_version": design_policy.failure_code_version,
        },
        "gaps": [
            {
                "gap_id": card.gap_id,
                "gap_category": card.gap_category.value,
                "labels": list(card.sanitized_labels),
                "allowed_field_aliases": list(card.allowed_field_aliases),
                "allowed_operator_families": list(card.allowed_operator_families),
                "temporal_window_bins": list(card.temporal_window_bins),
                "required_structure_axes": list(_required_axes_for_card(card, required_axes)),
                "data_availability_labels": list(_data_availability_labels(card)),
                "allowed_failure_risk_labels": list(card.missing_or_failure_risk),
            }
            for card in report.gap_cards
        ],
    }
    _scan_sanitized_payload(payload)
    return payload


def registered_spec_hash(registered_spec: RegisteredCoverageGraphSpec) -> str:
    """返回冻结 build_spec 的内容哈希。"""

    return registered_spec.spec_hash


def _read_bytes(path: Path, missing_message: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise _graph_error(missing_message) from error


def _read_json_dict(path: Path, missing_message: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _graph_error(missing_message) from error
    if not isinstance(payload, dict):
        raise _graph_error(f"{path.name} 必须是 JSON 对象")
    return payload


def _read_jsonl_models[T](path: Path, model: type[T], missing_message: str) -> tuple[T, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise _graph_error(missing_message) from error
    records: list[T] = []
    try:
        for line in lines:
            if line.strip():
                records.append(model.model_validate_json(line))
    except ValueError as error:
        raise _graph_error(f"{path.name} 含非法 JSONL 记录") from error
    return tuple(records)


def _read_parquet_models[T](path: Path, model: type[T], missing_message: str) -> tuple[T, ...]:
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as error:
        raise _graph_error(missing_message) from error
    try:
        return tuple(model.model_validate(row) for row in frame.to_dicts())
    except ValueError as error:
        raise _graph_error(f"{path.name} 含非法 Parquet 记录") from error


def _require_unique(values: Sequence[object], message: str) -> None:
    if len(set(values)) != len(values):
        raise _graph_error(f"{message} 重复")


def _verify_cluster_membership(
    clusters: Mapping[str, tuple[str, ...]],
    factor_ids: Sequence[str],
    layer_name: str,
) -> None:
    seen: list[str] = []
    for cluster_id, members in clusters.items():
        if not members:
            raise _graph_error(f"覆盖图谱 {layer_name} cluster {cluster_id} 不能为空")
        if tuple(sorted(set(members))) != members:
            raise _graph_error(f"覆盖图谱 {layer_name} cluster {cluster_id} 成员必须唯一且排序")
        seen.extend(members)
    if tuple(sorted(seen)) != tuple(sorted(factor_ids)):
        raise _graph_error(f"覆盖图谱 {layer_name} cluster 成员与 factor_nodes 不一致")


def _verify_coverage_summary(
    *,
    coverage_summary: dict[str, object],
    factor_nodes: Sequence[CoverageFactorNode],
    structural_edges: Sequence[StructuralEdge],
    signal_edges: Sequence[SignalPatternEdge],
    factor_performance: Sequence[FactorPerformanceSummary],
    regime_profiles: Sequence[RegimeFactorProfile],
    clusters: CoverageClusterAssignments,
) -> None:
    required = {
        "factor_count",
        "pair_count",
        "category_counts",
        "structure_source_counts",
        "strong_structural_edges",
        "comparable_signal_edges",
        "strong_signal_edges",
        "mean_rank_ic_by_factor",
        "uncomparable_reason_counts",
        "regime_labels",
        "structural_cluster_count",
        "signal_cluster_count",
    }
    if set(coverage_summary) != required:
        raise _graph_error("覆盖图谱 coverage_summary 字段集合不完整")
    if coverage_summary["factor_count"] != len(factor_nodes):
        raise _graph_error("覆盖图谱 coverage_summary.factor_count 不一致")
    if coverage_summary["pair_count"] != len(signal_edges):
        raise _graph_error("覆盖图谱 coverage_summary.pair_count 不一致")
    if coverage_summary["category_counts"] != dict(sorted(Counter(node.category for node in factor_nodes).items())):
        raise _graph_error("覆盖图谱 coverage_summary.category_counts 不一致")
    if coverage_summary["structure_source_counts"] != dict(sorted(Counter(node.structure_source for node in factor_nodes).items())):
        raise _graph_error("覆盖图谱 coverage_summary.structure_source_counts 不一致")
    if coverage_summary["strong_structural_edges"] != sum(edge.strong_structure_edge for edge in structural_edges):
        raise _graph_error("覆盖图谱 coverage_summary.strong_structural_edges 不一致")
    if coverage_summary["comparable_signal_edges"] != sum(edge.comparable for edge in signal_edges):
        raise _graph_error("覆盖图谱 coverage_summary.comparable_signal_edges 不一致")
    if coverage_summary["strong_signal_edges"] != sum(edge.strong_signal_edge for edge in signal_edges):
        raise _graph_error("覆盖图谱 coverage_summary.strong_signal_edges 不一致")
    expected_means = {
        item.factor_id: item.mean_rank_ic for item in factor_performance
    }
    if coverage_summary["mean_rank_ic_by_factor"] != expected_means:
        raise _graph_error("覆盖图谱 coverage_summary.mean_rank_ic_by_factor 不一致")
    expected_reasons = dict(
        sorted(
            Counter(
                edge.reason for edge in signal_edges if not edge.comparable and edge.reason is not None
            ).items()
        )
    )
    if coverage_summary["uncomparable_reason_counts"] != expected_reasons:
        raise _graph_error("覆盖图谱 coverage_summary.uncomparable_reason_counts 不一致")
    labels = {
        profile.canonical_state_id: profile.economic_label for profile in regime_profiles
    }
    expected_labels = [
        {"canonical_state_id": state_id, "economic_label": labels[state_id]}
        for state_id in sorted(labels)
    ]
    if coverage_summary["regime_labels"] != expected_labels:
        raise _graph_error("覆盖图谱 coverage_summary.regime_labels 不一致")
    if coverage_summary["structural_cluster_count"] != len(clusters.structural_clusters):
        raise _graph_error("覆盖图谱 coverage_summary.structural_cluster_count 不一致")
    if coverage_summary["signal_cluster_count"] != len(clusters.signal_clusters):
        raise _graph_error("覆盖图谱 coverage_summary.signal_cluster_count 不一致")


def _structural_gap_candidates(graph: VerifiedCoverageGraph) -> Iterable[_GapCandidate]:
    normalized = tuple(_normalize_node(node, graph) for node in graph.factor_nodes)
    observed = {
        (
            tuple(item["field_aliases"]),
            tuple(item["operator_families"]),
            item["window_bin"],
            item["input_combination"],
        )
        for item in normalized
    }
    aliases = tuple(sorted({alias for item in normalized for alias in item["field_aliases"]}))
    operators = tuple(sorted({family for item in normalized for family in item["operator_families"]}))
    windows = tuple(sorted({str(item["window_bin"]) for item in normalized}))
    combinations = tuple(sorted({str(item["input_combination"]) for item in normalized}))
    for alias in aliases:
        for operator_family in operators:
            for window_bin in windows:
                for input_combination in combinations:
                    signature = ((alias,), (operator_family,), window_bin, input_combination)
                    if signature in observed:
                        continue
                    matching = [
                        item for item in normalized
                        if alias in item["field_aliases"] or operator_family in item["operator_families"]
                    ]
                    yield _GapCandidate(
                        gap_category=GapCategory.STRUCTURAL,
                        canonical_gap_hash=_canonical_gap_hash(
                            gap_category=GapCategory.STRUCTURAL,
                            labels=("coverage", "field_set", "input_combination", "operator_topology", "structural", "temporal_role"),
                            field_aliases=(alias,),
                            operator_families=(operator_family,),
                            temporal_window_bins=(window_bin,),
                            risks=("missing",),
                        ),
                        sanitized_labels=("coverage", "field_set", "input_combination", "operator_topology", "structural", "temporal_role"),
                        allowed_field_aliases=(alias,),
                        allowed_operator_families=(operator_family,),
                        temporal_window_bins=(window_bin,),
                        structure_cluster_count_band=_count_band(_cluster_count(matching, "structural_cluster_id")),
                        signal_cluster_count_band=_count_band(_cluster_count(matching, "signal_cluster_id")),
                        missing_or_failure_risk=("missing",),
                    )


def _market_gap_candidates(graph: VerifiedCoverageGraph) -> Iterable[_GapCandidate]:
    normalized = {item["factor_id"]: item for item in (_normalize_node(node, graph) for node in graph.factor_nodes)}
    grouped: dict[int, list[RegimeFactorProfile]] = defaultdict(list)
    for profile in graph.regime_profiles:
        grouped[profile.canonical_state_id].append(profile)
    for state_id, profiles in sorted(grouped.items()):
        unavailable = [profile for profile in profiles if profile.status != "ok"]
        unstable = [
            profile for profile in profiles
            if profile.status == "ok"
            and (
                profile.mean_normalized_entropy is not None
                and profile.mean_normalized_entropy >= 0.60
            )
        ]
        if not unavailable and not unstable:
            continue
        impacted = unavailable if unavailable else unstable
        node_views = [normalized[profile.factor_id] for profile in impacted]
        risk = ("insufficient_coverage",) if unavailable else ("stale",)
        yield _GapCandidate(
            gap_category=GapCategory.MARKET_REGIME,
            canonical_gap_hash=_canonical_gap_hash(
                gap_category=GapCategory.MARKET_REGIME,
                labels=("coverage", "market_regime", "regime"),
                field_aliases=_union_tuple(item["field_aliases"] for item in node_views),
                operator_families=_union_tuple(item["operator_families"] for item in node_views),
                temporal_window_bins=_union_tuple((item["window_bin"],) for item in node_views),
                risks=risk,
                extra={"state_id": state_id, "profile_statuses": tuple(profile.status for profile in profiles)},
            ),
            sanitized_labels=("coverage", "market_regime", "regime"),
            allowed_field_aliases=_union_tuple(item["field_aliases"] for item in node_views),
            allowed_operator_families=_union_tuple(item["operator_families"] for item in node_views),
            temporal_window_bins=_union_tuple((item["window_bin"],) for item in node_views),
            structure_cluster_count_band=_count_band(_cluster_count(node_views, "structural_cluster_id")),
            signal_cluster_count_band=_count_band(_cluster_count(node_views, "signal_cluster_id")),
            missing_or_failure_risk=risk,
        )


def _data_availability_gap_candidates(graph: VerifiedCoverageGraph) -> Iterable[_GapCandidate]:
    normalized = {item["factor_id"]: item for item in (_normalize_node(node, graph) for node in graph.factor_nodes)}
    for performance in graph.factor_performance:
        if performance.invalid_dates <= 0 and (performance.median_coverage or 0.0) >= 0.80:
            continue
        node_view = normalized[performance.factor_id]
        risk = ("insufficient_coverage",)
        if performance.status == "unavailable":
            risk = ("missing",)
        yield _GapCandidate(
            gap_category=GapCategory.DATA_AVAILABILITY,
            canonical_gap_hash=_canonical_gap_hash(
                gap_category=GapCategory.DATA_AVAILABILITY,
                labels=("coverage", "data_availability"),
                field_aliases=node_view["field_aliases"],
                operator_families=node_view["operator_families"],
                temporal_window_bins=(node_view["window_bin"],),
                risks=risk,
                extra={
                    "factor_id": performance.factor_id,
                    "status": performance.status,
                    "invalid_dates": performance.invalid_dates,
                    "coverage_band": _coverage_band(performance.median_coverage),
                },
            ),
            sanitized_labels=("coverage", "data_availability"),
            allowed_field_aliases=node_view["field_aliases"],
            allowed_operator_families=node_view["operator_families"],
            temporal_window_bins=(node_view["window_bin"],),
            structure_cluster_count_band=_count_band(len({node_view["structural_cluster_id"]})),
            signal_cluster_count_band=_count_band(len({node_view["signal_cluster_id"]})),
            missing_or_failure_risk=risk,
        )
    for edge in graph.signal_edges:
        if edge.comparable:
            continue
        first = normalized[edge.factor_a]
        second = normalized[edge.factor_b]
        aliases = _union_tuple((first["field_aliases"], second["field_aliases"]))
        operators = _union_tuple((first["operator_families"], second["operator_families"]))
        windows = _union_tuple(((first["window_bin"],), (second["window_bin"],)))
        risk = ("missing",) if edge.reason and "重叠日期" in edge.reason else ("insufficient_coverage",)
        yield _GapCandidate(
            gap_category=GapCategory.DATA_AVAILABILITY,
            canonical_gap_hash=_canonical_gap_hash(
                gap_category=GapCategory.DATA_AVAILABILITY,
                labels=("coverage", "data_availability"),
                field_aliases=aliases,
                operator_families=operators,
                temporal_window_bins=windows,
                risks=risk,
                extra={"factor_a": edge.factor_a, "factor_b": edge.factor_b, "reason": edge.reason},
            ),
            sanitized_labels=("coverage", "data_availability"),
            allowed_field_aliases=aliases,
            allowed_operator_families=operators,
            temporal_window_bins=windows,
            structure_cluster_count_band=_count_band(len({first["structural_cluster_id"], second["structural_cluster_id"]})),
            signal_cluster_count_band=_count_band(len({first["signal_cluster_id"], second["signal_cluster_id"]})),
            missing_or_failure_risk=risk,
        )


def _select_gap_candidates(
    candidates: tuple[_GapCandidate, ...],
    policy: GapSelectionPolicy,
) -> tuple[_GapCandidate, ...]:
    categorized: dict[GapCategory, list[_GapCandidate]] = defaultdict(list)
    for candidate in candidates:
        categorized[candidate.gap_category].append(candidate)
    missing_categories = tuple(
        category.value
        for category in GapCategory
        if not categorized.get(category)
    )
    if missing_categories:
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            f"覆盖缺口缺少必需类别：{missing_categories}",
        )
    if policy.max_cards < len(GapCategory):
        raise FactorMinerError(
            FailureCode.COVERAGE_GAP_INVALID,
            f"gap report 预算不足以覆盖三类缺口：max_cards={policy.max_cards}",
        )
    counts: Counter[GapCategory] = Counter()
    selected: list[_GapCandidate] = []
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.gap_category.value,
            _risk_rank(item.missing_or_failure_risk),
            item.canonical_gap_hash,
        ),
    )
    for category in GapCategory:
        chosen = min(
            categorized[category],
            key=lambda item: (
                _risk_rank(item.missing_or_failure_risk),
                item.canonical_gap_hash,
            ),
        )
        selected.append(chosen)
        counts[chosen.gap_category] += 1
    for candidate in ordered:
        if len(selected) >= policy.max_cards:
            break
        if candidate in selected:
            continue
        if counts[candidate.gap_category] >= policy.max_cards_per_category:
            continue
        selected.append(candidate)
        counts[candidate.gap_category] += 1
    return tuple(selected)


def _candidate_to_card(candidate: _GapCandidate) -> CoverageGapCard:
    gap_id = f"gap_{candidate.canonical_gap_hash[:24]}"
    return CoverageGapCard(
        gap_id=gap_id,
        gap_category=candidate.gap_category,
        sanitized_labels=candidate.sanitized_labels,
        allowed_field_aliases=candidate.allowed_field_aliases,
        allowed_operator_families=candidate.allowed_operator_families,
        temporal_window_bins=candidate.temporal_window_bins,
        structure_cluster_count_band=candidate.structure_cluster_count_band,
        signal_cluster_count_band=candidate.signal_cluster_count_band,
        missing_or_failure_risk=candidate.missing_or_failure_risk,
    )


def _canonical_gap_hash(
    *,
    gap_category: GapCategory,
    labels: tuple[str, ...],
    field_aliases: tuple[str, ...],
    operator_families: tuple[str, ...],
    temporal_window_bins: tuple[str, ...],
    risks: tuple[str, ...],
    extra: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "gap_category": gap_category.value,
        "labels": labels,
        "field_aliases": field_aliases,
        "operator_families": operator_families,
        "temporal_window_bins": temporal_window_bins,
        "risks": risks,
    }
    if extra:
        payload["extra"] = extra
    return sha256_json(payload)


def _normalize_node(
    node: CoverageFactorNode,
    graph: VerifiedCoverageGraph,
) -> dict[str, object]:
    aliases = tuple(sorted(_sanitize_field_alias(field) for field in node.input_fields))
    operator_families = tuple(
        sorted(
            {
                _sanitize_operator_family(tag)
                for tag in node.operator_tags
            }
        )
    ) or ("aggregation",)
    window_bin = _window_bin(node.windows, node.lookback_window)
    return {
        "factor_id": node.factor_id,
        "field_aliases": aliases,
        "operator_families": operator_families,
        "window_bin": window_bin,
        "input_combination": "single" if len(aliases) == 1 else "multi",
        "structural_cluster_id": graph.clusters.factor_to_structural_cluster[node.factor_id],
        "signal_cluster_id": graph.clusters.factor_to_signal_cluster[node.factor_id],
    }


def _sanitize_field_alias(field_name: str) -> str:
    alias = _FIELD_ALIAS_MAP.get(field_name)
    if alias is None:
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"覆盖图谱字段无法脱敏公开：{field_name}",
        )
    return alias


def _sanitize_operator_family(operator_tag: str) -> str:
    family = _OPERATOR_FAMILY_MAP.get(operator_tag)
    if family is None:
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"覆盖图谱算子无法脱敏公开：{operator_tag}",
        )
    return family


def _window_bin(windows: Sequence[int], lookback_window: int) -> str:
    if windows:
        reference = max(windows)
    elif lookback_window > 0:
        reference = lookback_window
    else:
        return "unknown"
    if reference <= 5:
        return "short"
    if reference <= 20:
        return "medium"
    return "long"


def _cluster_count(items: Sequence[Mapping[str, object]], field_name: str) -> int:
    return len({str(item[field_name]) for item in items}) if items else 0


def _count_band(count: int) -> str:
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 4:
        return "2_4"
    return "5_plus"


def _coverage_band(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value < 0.50:
        return "low"
    if value < 0.80:
        return "medium"
    return "available"


def _risk_rank_map() -> dict[str, int]:
    return {
        "none": 0,
        "insufficient_coverage": 1,
        "redundant": 1,
        "stale": 2,
        "constant": 2,
        "missing": 3,
        "lookahead": 4,
        "invalid": 4,
    }


def _risk_rank(risks: Sequence[str]) -> int:
    mapping = _risk_rank_map()
    return max(mapping[risk] for risk in risks)


def _union_tuple(groups: Iterable[Sequence[str]]) -> tuple[str, ...]:
    return tuple(sorted({item for group in groups for item in group}))


def _required_axes_for_card(
    card: CoverageGapCard,
    required_axes: tuple[str, ...],
) -> tuple[str, ...]:
    if card.gap_category is GapCategory.STRUCTURAL:
        return tuple(
            axis
            for axis in required_axes
            if axis in {"field_set", "operator_topology", "temporal_role", "input_combination"}
        )
    if card.gap_category is GapCategory.MARKET_REGIME:
        return tuple(
            axis
            for axis in required_axes
            if axis in {"field_set", "operator_topology", "temporal_role", "depth_range"}
        )
    return tuple(
        axis
        for axis in required_axes
        if axis in {"field_set", "input_combination", "node_count"}
    )


def _data_availability_labels(card: CoverageGapCard) -> tuple[str, ...]:
    labels = {"coverage"}
    if card.gap_category is GapCategory.DATA_AVAILABILITY:
        labels.add("data_availability")
    labels.update(card.missing_or_failure_risk)
    return tuple(sorted(labels))


def _scan_sanitized_payload(payload: object) -> None:
    _scan_value(payload, path="brief")


def _scan_value(value: object, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _scan_text(str(key), path=f"{path}.<key>")
            _scan_value(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_value(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        _scan_text(value, path=path)
        return
    if isinstance(value, (bool, int, float)) or value is None:
        return
    raise FactorMinerError(
        FailureCode.LLM_PRIVACY_VIOLATION,
        f"外发 brief 出现未登记类型：{path}",
    )


def _scan_text(value: str, *, path: str) -> None:
    lower = value.lower()
    if any(token in lower for token in _FORBIDDEN_TEXT):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"外发 brief 出现禁止词：{path}",
        )
    if _DATE_PATTERN.search(value):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"外发 brief 出现逐日日期：{path}",
        )
    if _PATH_PATTERN.search(value):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"外发 brief 出现路径：{path}",
        )
    if _TICKER_PATTERN.search(value) and value not in {"low", "high", "close", "open"}:
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"外发 brief 出现 ticker 样式文本：{path}",
        )
    if _HASH_PATTERN.fullmatch(value) or _ID_PATTERN.fullmatch(value):
        return
    safe_values = {
        "1",
        "brief_version",
        "coverage_graph_id",
        "graph_manifest_hash",
        "memory_snapshot_hash",
        "field_registry_hash",
        "design_policy",
        "hypotheses_per_round",
        "designs_per_hypothesis",
        "arms_per_round",
        "total_slots",
        "allowed_structure_axes",
        "forbid_temporal_only_change",
        "max_single_hypothesis_output_correlation",
        "failure_code_version",
        "gaps",
        "gap_id",
        "gap_category",
        "labels",
        "allowed_field_aliases",
        "allowed_operator_families",
        "temporal_window_bins",
        "required_structure_axes",
        "data_availability_labels",
        "allowed_failure_risk_labels",
        "structural",
        "market_regime",
        "data_availability",
        "coverage",
        "field_set",
        "operator_topology",
        "temporal_role",
        "input_combination",
        "regime",
        "missing",
        "stale",
        "constant",
        "redundant",
        "insufficient_coverage",
        "lookahead",
        "invalid",
        "close",
        "price_close",
        "open",
        "price_open",
        "high",
        "price_high",
        "low",
        "price_low",
        "volume",
        "amount",
        "turnover",
        "market_cap",
        "vwap",
        "returns",
        "arithmetic",
        "rolling",
        "temporal",
        "cross_sectional",
        "rank",
        "ranking",
        "interaction",
        "aggregation",
        "conditional",
        "short",
        "medium",
        "long",
        "unknown",
        "node_count",
        "depth_range",
        "evolution-v1",
    }
    if value not in safe_values and not value.startswith("gap_"):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            f"外发 brief 出现未登记公开文本：{path}={value}",
        )


def _manifest_hash(manifest: CoverageGraphManifest) -> str:
    return sha256_json(manifest.model_dump(mode="json"))


def _regime_snapshot_hash(graph: VerifiedCoverageGraph) -> str:
    return sha256_json(
        {
            "regime_snapshot_id": graph.registered_spec.spec.regime_snapshot_id,
            "regime_annotations": [
                item.model_dump(mode="json")
                for item in graph.registered_spec.spec.regime_annotations
            ],
        }
    )


def _graph_error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)
