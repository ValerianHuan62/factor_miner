"""分别对结构强边和信号强边构建稳定连通分量。"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib

from pydantic import BaseModel, ConfigDict

from factor_miner.coverage_schema import (
    CoverageClusterPolicy,
    CoverageFactorNode,
)
from factor_miner.coverage_signal import SignalPatternEdge
from factor_miner.coverage_structure import StructuralEdge
from factor_miner.errors import FactorMinerError, FailureCode


class CoverageClusterAssignments(BaseModel):
    """数学结构层与实际信号层彼此独立的簇分配。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: str
    factor_to_structural_cluster: dict[str, str]
    factor_to_signal_cluster: dict[str, str]
    structural_clusters: dict[str, tuple[str, ...]]
    signal_clusters: dict[str, tuple[str, ...]]


def build_coverage_clusters(
    nodes: Sequence[CoverageFactorNode],
    structural_edges: Sequence[StructuralEdge],
    signal_edges: Sequence[SignalPatternEdge],
    policy: CoverageClusterPolicy,
) -> CoverageClusterAssignments:
    """只使用各层自己的强边，生成与输入顺序无关的稳定簇。"""

    factor_ids = tuple(sorted(node.factor_id for node in nodes))
    if len(set(factor_ids)) != len(factor_ids):
        raise FactorMinerError(
            FailureCode.COVERAGE_INPUT_MISMATCH,
            "覆盖图谱聚类节点 ID 重复",
        )
    valid = set(factor_ids)
    _validate_edge_endpoints(
        valid,
        ((edge.factor_a, edge.factor_b) for edge in structural_edges),
    )
    _validate_edge_endpoints(
        valid,
        ((edge.factor_a, edge.factor_b) for edge in signal_edges),
    )
    structural_components = _components(
        factor_ids,
        (
            (edge.factor_a, edge.factor_b)
            for edge in structural_edges
            if edge.strong_structure_edge
        ),
    )
    signal_components = _components(
        factor_ids,
        (
            (edge.factor_a, edge.factor_b)
            for edge in signal_edges
            if edge.comparable and edge.strong_signal_edge
        ),
    )
    structural_clusters = _named_clusters("struct", structural_components)
    signal_clusters = _named_clusters("signal", signal_components)
    return CoverageClusterAssignments(
        algorithm=policy.algorithm,
        factor_to_structural_cluster=_reverse(structural_clusters),
        factor_to_signal_cluster=_reverse(signal_clusters),
        structural_clusters=structural_clusters,
        signal_clusters=signal_clusters,
    )


def _components(
    factor_ids: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    """以排序遍历实现确定性无向图连通分量。"""

    adjacency = {factor_id: set() for factor_id in factor_ids}
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    remaining = set(factor_ids)
    result: list[tuple[str, ...]] = []
    while remaining:
        start = min(remaining)
        stack = [start]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(sorted(adjacency[current] - members, reverse=True))
        remaining -= members
        result.append(tuple(sorted(members)))
    return tuple(sorted(result))


def _named_clusters(
    prefix: str,
    components: Sequence[tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """由层名和成员集合生成稳定、可读的簇 ID。"""

    result: dict[str, tuple[str, ...]] = {}
    for members in components:
        digest = hashlib.sha256("\n".join(members).encode("utf-8")).hexdigest()
        result[f"{prefix}_{digest[:12]}"] = members
    return dict(sorted(result.items()))


def _reverse(clusters: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """将簇成员表转换为每因子唯一簇 ID。"""

    return {
        factor_id: cluster_id
        for cluster_id, members in clusters.items()
        for factor_id in members
    }


def _validate_edge_endpoints(
    valid: set[str],
    edges: Sequence[tuple[str, str]],
) -> None:
    """拒绝引用图外因子的边。"""

    for first, second in edges:
        if first not in valid or second not in valid or first == second:
            raise FactorMinerError(
                FailureCode.COVERAGE_INPUT_MISMATCH,
                f"覆盖图谱边端点非法：{first}, {second}",
            )
