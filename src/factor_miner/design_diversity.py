"""三因子设计实质差异闸门。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

from factor_miner.canonical import sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.dsl import (
    canonical_ast,
    canonical_ast_hash,
    canonical_ast_without_temporal_parameters,
    validate_ast,
)
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.field_registry import FieldAvailabilityEntry, FieldAvailabilityRegistry
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.research_evolution_schema import DesignDiversityPolicy
from factor_miner.schema import (
    AvailabilitySpec,
    FactorNode,
    HypothesisSpec,
    MechanismStatus,
    TrustedCandidateFactorSpec,
    registered_trusted_candidate,
)

FactorExpression = FactorNode

_TEMPORAL_ROLE_OPERATORS = frozenset(
    {
        "delay",
        "delta",
        "rolling_sum",
        "rolling_mean",
        "rolling_std",
        "rolling_min",
        "rolling_max",
        "rolling_corr",
    }
)
_DEFAULT_FORBIDDEN_FIELDS = ("label", "label_o2o_5d", "target", "forward_return", "future_return")
_SYNTHETIC_CREATED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class DesignSignature:
    canonical_ast_hash: str
    ast_without_temporal_parameters_hash: str
    field_set: tuple[str, ...]
    operator_topology: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    input_combinations: tuple[tuple[str, ...], ...]
    node_count: int
    depth: int


@dataclass(frozen=True, slots=True)
class DesignDiversityFailure:
    """一对设计在指定结构轴上失败的可序列化记录。"""

    left_index: int
    right_index: int
    axis_name: str
    left_hash: str
    right_hash: str
    failure_code: str
    left_slot_id: str | None = None
    right_slot_id: str | None = None

    def with_slot_ids(self, slot_ids: tuple[str, str, str]) -> DesignDiversityFailure:
        """把槽位顺序映射回失败记录。"""

        return replace(
            self,
            left_slot_id=slot_ids[self.left_index],
            right_slot_id=slot_ids[self.right_index],
        )

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class DesignDiversityResult:
    """设计差异闸门结果。"""

    passed: bool
    failure_code: str | None
    signatures: tuple[DesignSignature, ...]
    failures: tuple[DesignDiversityFailure, ...]

    def bind_slot_ids(self, slot_ids: tuple[str, str, str]) -> DesignDiversityResult:
        """把槽位顺序绑定到全部失败记录。"""

        return replace(
            self,
            failures=tuple(item.with_slot_ids(slot_ids) for item in self.failures),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容字典。"""

        return {
            "passed": self.passed,
            "failure_code": self.failure_code,
            "signatures": [asdict(item) for item in self.signatures],
            "failures": [item.to_dict() for item in self.failures],
        }


def _local_registry_view(
    registry: FieldAvailabilityRegistry | None,
) -> FieldAvailabilityRegistry:
    """把真实字段注册表转换为 local field_id 视角。"""

    if registry is None:
        raise FactorMinerError(
            FailureCode.FIELD_MISSING,
            "设计差异闸门缺少真实字段可用性注册表",
        )
    return FieldAvailabilityRegistry(
        registry_id=registry.registry_id,
        data_release_id=registry.data_release_id,
        fields=tuple(
            FieldAvailabilityEntry(
                field_id=item.field_id,
                public_alias=item.field_id,
                economic_type=item.economic_type,
                unit_dimension=item.unit_dimension,
                panel_shape=item.panel_shape,
                event_time=item.event_time,
                source_publish_time=item.source_publish_time,
                vendor_available_time=item.vendor_available_time,
                revision_policy=item.revision_policy,
                point_in_time_guarantee=item.point_in_time_guarantee,
                earliest_decision_time=item.earliest_decision_time,
                eligible_for_factor=item.eligible_for_factor,
            )
            for item in registry.fields
        ),
    )


def _build_validation_candidate(
    expression: FactorExpression,
    *,
    required_fields: tuple[str, ...],
    max_lookback: int,
) -> TrustedCandidateFactorSpec:
    """构造仅用于 compiler 硬校验的 synthetic trusted candidate。"""

    return TrustedCandidateFactorSpec(
        hypothesis=HypothesisSpec(
            claim="design_diversity_validation",
            mechanism="design_diversity_validation",
            expected_sign="positive",
            observable_proxy="design_diversity_validation",
            independent_verification="design_diversity_validation",
            competing_explanations=("design_diversity_validation",),
            baseline_reference="design_diversity_validation",
            failure_modes=("design_diversity_validation",),
            falsification_path="design_diversity_validation",
            source_refs=("design_diversity_validation",),
            mechanism_status=MechanismStatus.MECHANISM_UNVERIFIED,
        ),
        expression=expression,
        required_fields=required_fields,
        max_lookback=max_lookback,
        availability=AvailabilitySpec(
            observation="close_t",
            decision="after_close_t",
            earliest_trade="open_t_plus_1",
        ),
        created_at=_SYNTHETIC_CREATED_AT,
        provenance={
            "source": "design_diversity_preflight",
            "validation_mode": "structural",
        },
    )


def _validated_signature(
    expression: FactorExpression,
    *,
    registry: FieldAvailabilityRegistry,
) -> DesignSignature:
    """先执行硬校验，再返回设计签名。"""

    analyse_semantic_type(expression, registry)
    allowed_fields = tuple(
        item.field_id
        for item in registry.fields
        if item.eligible_for_factor and item.point_in_time_guarantee
    )
    metadata = validate_ast(
        expression,
        allowed_fields=allowed_fields,
        forbidden_fields=_DEFAULT_FORBIDDEN_FIELDS,
    )
    compile_candidate(
        registered_trusted_candidate(
            _build_validation_candidate(
                expression,
                required_fields=metadata.required_fields,
                max_lookback=metadata.lookback,
            )
        ),
        allowed_fields=allowed_fields,
    )
    return _build_signature(expression)


def _canonical_nodes(
    node: dict[str, Any],
    *,
    path: str = "root",
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """返回规范化 AST 的稳定前序遍历。"""

    nodes: list[tuple[str, dict[str, Any]]] = [(path, node)]
    for index, child in enumerate(node.get("args", ())):
        nodes.extend(_canonical_nodes(child, path=f"{path}.{index}"))
    return tuple(nodes)


def _subtree_field_set(node: dict[str, Any]) -> tuple[str, ...]:
    """提取子树依赖字段集合。"""

    if node["op"] == "field":
        return (str(node["field"]),)
    fields: set[str] = set()
    for child in node.get("args", ()):
        fields.update(_subtree_field_set(child))
    return tuple(sorted(fields))


def _operator_topology(canonical: dict[str, Any]) -> tuple[str, ...]:
    """提取规范化拓扑算子签名。"""

    return tuple(node["op"] for _, node in _canonical_nodes(canonical))


def _temporal_roles(canonical: dict[str, Any]) -> tuple[str, ...]:
    """提取不依赖 period/window 的时间角色签名。"""

    roles: list[str] = []
    for path, node in _canonical_nodes(canonical):
        if node["op"] in _TEMPORAL_ROLE_OPERATORS:
            roles.append(f"{path}:{node['op']}")
    return tuple(roles)


def _input_combinations(canonical: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """提取每个内部节点聚合的字段组合。"""

    combinations: list[tuple[str, ...]] = []
    for _, node in _canonical_nodes(canonical):
        if node["op"] in {"field", "const"}:
            continue
        combinations.append(_subtree_field_set(node))
    return tuple(combinations)


def _node_depth(canonical: dict[str, Any]) -> int:
    """计算规范化 AST 深度。"""

    children = tuple(canonical.get("args", ()))
    if not children:
        return 1
    return 1 + max(_node_depth(child) for child in children)


def _build_signature(expression: FactorExpression) -> DesignSignature:
    """基于规范化 AST 构造设计签名。"""

    canonical = canonical_ast(expression)
    without_temporal_parameters = canonical_ast_without_temporal_parameters(
        expression
    )
    field_set = _subtree_field_set(canonical)
    return DesignSignature(
        canonical_ast_hash=canonical_ast_hash(expression),
        ast_without_temporal_parameters_hash=sha256_json(
            without_temporal_parameters
        ),
        field_set=field_set,
        operator_topology=_operator_topology(canonical),
        temporal_roles=_temporal_roles(canonical),
        input_combinations=_input_combinations(canonical),
        node_count=len(_canonical_nodes(canonical)),
        depth=_node_depth(canonical),
    )


def _duplicate_failures(
    signatures: tuple[DesignSignature, ...],
    *,
    axis_name: str,
    attribute: str,
) -> tuple[DesignDiversityFailure, ...]:
    """生成某一结构轴的两两重复失败。"""

    failures: list[DesignDiversityFailure] = []
    for left_index in range(len(signatures)):
        left_value = getattr(signatures[left_index], attribute)
        for right_index in range(left_index + 1, len(signatures)):
            right_value = getattr(signatures[right_index], attribute)
            if left_value != right_value:
                continue
            failures.append(
                DesignDiversityFailure(
                    left_index=left_index,
                    right_index=right_index,
                    axis_name=axis_name,
                    left_hash=str(left_value),
                    right_hash=str(right_value),
                    failure_code=FailureCode.DESIGN_DIVERSITY_FAILED.value,
                )
            )
    return tuple(failures)


def preflight_designs(
    expressions: tuple[FactorExpression, FactorExpression, FactorExpression],
    *,
    policy: DesignDiversityPolicy,
    registry: FieldAvailabilityRegistry | None = None,
) -> DesignDiversityResult:
    """对三个设计先执行硬校验，再执行结构差异闸门。

    参数：
        expressions: 同一假设下的三个 local typed AST。
        policy: 冻结的结构差异政策。
        registry: 真实字段可用性注册表；缺失时硬失败。

    返回：
        包含签名、失败对和稳定失败码的闸门结果。
    """

    if len(expressions) != policy.designs_per_hypothesis:
        raise ValueError(
            f"每个假设必须提供 {policy.designs_per_hypothesis} 个设计"
        )
    local_registry = _local_registry_view(registry)
    signatures = tuple(
        _validated_signature(expression, registry=local_registry)
        for expression in expressions
    )
    failures = _duplicate_failures(
        signatures,
        axis_name="canonical_ast_hash",
        attribute="canonical_ast_hash",
    ) + _duplicate_failures(
        signatures,
        axis_name="ast_without_temporal_parameters_hash",
        attribute="ast_without_temporal_parameters_hash",
    )
    return DesignDiversityResult(
        passed=not failures,
        failure_code=(
            None
            if not failures
            else FailureCode.DESIGN_DIVERSITY_FAILED.value
        ),
        signatures=signatures,
        failures=failures,
    )
