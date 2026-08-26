"""V0.4 因子数学形式层的确定性两两关系。"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.coverage_schema import (
    CoverageFactorNode,
    CoverageStructuralPolicy,
)


class StructuralEdge(BaseModel):
    """一个因子对各结构维度及冻结加权总分。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_a: str
    factor_b: str
    exact_ast_match: bool
    exact_formula_match: bool
    field_similarity: FiniteFloat = Field(ge=0, le=1)
    operator_similarity: FiniteFloat = Field(ge=0, le=1)
    window_similarity: FiniteFloat = Field(ge=0, le=1)
    category_similarity: FiniteFloat = Field(ge=0, le=1)
    structural_similarity: FiniteFloat = Field(ge=0, le=1)
    strong_structure_edge: bool


def compare_factor_structure(
    node_a: CoverageFactorNode,
    node_b: CoverageFactorNode,
    policy: CoverageStructuralPolicy,
) -> StructuralEdge:
    """分别计算字段、算子、窗口和人工类别关系。"""

    factor_a, factor_b = sorted((node_a.factor_id, node_b.factor_id))
    exact_ast = (
        node_a.ast_hash is not None
        and node_b.ast_hash is not None
        and node_a.ast_hash == node_b.ast_hash
    )
    exact_formula = node_a.formula_hash == node_b.formula_hash
    field_similarity = _jaccard(node_a.input_fields, node_b.input_fields)
    operator_similarity = _jaccard(node_a.operator_tags, node_b.operator_tags)
    window_similarity = _window_similarity(
        node_a.windows,
        node_b.windows,
        policy.window_scale,
    )
    category_similarity = (
        1.0
        if node_a.subcategory == node_b.subcategory
        else 0.5
        if node_a.category == node_b.category
        else 0.0
    )
    structural_similarity = (
        policy.field_weight * field_similarity
        + policy.operator_weight * operator_similarity
        + policy.window_weight * window_similarity
        + policy.category_weight * category_similarity
    )
    return StructuralEdge(
        factor_a=factor_a,
        factor_b=factor_b,
        exact_ast_match=exact_ast,
        exact_formula_match=exact_formula,
        field_similarity=field_similarity,
        operator_similarity=operator_similarity,
        window_similarity=window_similarity,
        category_similarity=category_similarity,
        structural_similarity=structural_similarity,
        strong_structure_edge=(
            exact_ast
            or exact_formula
            or structural_similarity >= policy.strong_structure_similarity
        ),
    )


def build_structural_edges(
    nodes: Sequence[CoverageFactorNode],
    policy: CoverageStructuralPolicy,
) -> tuple[StructuralEdge, ...]:
    """按因子 ID 对每个无序因子对生成一条结构边。"""

    ordered = tuple(sorted(nodes, key=lambda item: item.factor_id))
    return tuple(
        compare_factor_structure(first, second, policy)
        for first, second in combinations(ordered, 2)
    )


def _jaccard(first: Sequence[str], second: Sequence[str]) -> float:
    """计算集合 Jaccard；两个空集合视为同样无该类算子。"""

    left = set(first)
    right = set(second)
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _window_similarity(
    first: Sequence[int],
    second: Sequence[int],
    scale: float,
) -> float:
    """用双向最近窗口距离计算对参数个数稳健的邻近度。"""

    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    distances = [
        min(abs(value - other) for other in second)
        for value in first
    ] + [
        min(abs(value - other) for other in first)
        for value in second
    ]
    mean_distance = sum(distances) / len(distances)
    return 1.0 / (1.0 + mean_distance / scale)
