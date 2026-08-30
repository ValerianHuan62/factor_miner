"""V0.5 两个确定性 shadow arm 的服务器侧生成器。

shadow arm 不编造新的经济假设，也不读取 IC、收益或任何候选结果。
它们只复用已批准的 coverage 假设和已通过固定校验的父 AST，生成可审计的
机械变异与 hypothesis-conditioned grammar 候选；没有合格父候选时保留明确
的未执行终态，继续占用预登记槽位。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.design_diversity import preflight_designs
from factor_miner.dsl import validate_ast
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import FieldAvailabilityRegistry
from factor_miner.ledger import JsonlLedger, _atomic_write_immutable
from factor_miner.llm_hypothesis import RegisteredCoverageGapHypothesis
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_schema import RegisteredLLMDiscoveryResearchFamily
from factor_miner.llm_state import (
    CANDIDATE_TERMINALS,
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    LLMDiscoveryEvent,
    logical_candidate_design_slot_id,
    logical_hypothesis_slot_id,
)
from factor_miner.research_evolution import require_approved_evolution_batch
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    DesignDiversityPolicy,
    ResearchEvolutionContext,
)
from factor_miner.schema import (
    FactorNode,
    RegisteredTrustedCandidate,
    registered_trusted_candidate,
)


_WINDOWS = (5, 10, 20, 40, 60, 120)
_PERIODS = (5, 10, 20, 40, 60)
_SHADOW_ARMS = ("mechanical_mutation", "hypothesis_conditioned_grammar")


class ShadowGenerationSummary(BaseModel):
    """确定性 shadow 生成的非正文摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str
    generated_candidate_ids: tuple[str, ...]
    terminal_slot_ids: tuple[str, ...]
    failed_slot_ids: tuple[str, ...]
    generator_identity_hashes: dict[str, str]
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _shadow_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造不暴露公式正文的 shadow 生成错误。"""

    return FactorMinerError(code, message)


def _families(node: FactorNode) -> frozenset[str]:
    """把 AST 算子映射到冻结的粗粒度算子族。"""

    values: set[str] = set()
    if node.op in {"add", "sub", "mul", "div", "neg", "abs"}:
        values.add("arithmetic")
    elif node.op in {"delay", "delta"}:
        values.add("temporal")
    elif node.op.startswith("rolling_"):
        values.add("rolling")
    for child in node.args:
        values.update(_families(child))
    return frozenset(values)


def _fields(node: FactorNode) -> tuple[str, ...]:
    """返回 AST 中稳定排序的本地字段。"""

    values: set[str] = set()
    if node.op == "field" and node.field is not None:
        values.add(node.field)
    for child in node.args:
        values.update(_fields(child))
    return tuple(sorted(values))


def _replace_window(node: FactorNode, window: int) -> FactorNode:
    """替换根 rolling 窗口或 temporal 周期。"""

    if node.op.startswith("rolling_"):
        return node.model_copy(update={"window": window, "center": False})
    if node.op in {"delay", "delta"}:
        return node.model_copy(update={"period": window, "window": None})
    raise ValueError("根节点不是可替换窗口的算子")


def _mutation_options(
    parent: FactorNode,
    allowed_families: frozenset[str],
) -> tuple[FactorNode, ...]:
    """按冻结顺序枚举单步机械变异。"""

    options: list[FactorNode] = []
    if parent.op.startswith("rolling_") and parent.window is not None:
        options.extend(
            _replace_window(parent, value)
            for value in _WINDOWS
            if value != parent.window
        )
    elif parent.op in {"delay", "delta"}:
        period = parent.period if parent.period is not None else parent.window
        options.extend(
            parent.model_copy(update={"period": value, "window": None})
            for value in _PERIODS
            if value != period
        )
    if "rolling" in allowed_families:
        options.extend(
            FactorNode(op="rolling_mean", args=(parent,), window=value, center=False)
            for value in _WINDOWS
        )
    if "temporal" in allowed_families:
        options.extend(
            FactorNode(op="delta", args=(parent,), period=value)
            for value in _PERIODS
        )
    return tuple(options)


def _grammar_options(
    parent: FactorNode,
    allowed_families: frozenset[str],
) -> tuple[FactorNode, ...]:
    """按固定 seed 和节点顺序枚举受假设约束的 grammar 候选。"""

    fields = _fields(parent)
    if not fields:
        return ()
    field_nodes = tuple(FactorNode(op="field", field=value) for value in fields)
    options: list[FactorNode] = []
    if "rolling" in allowed_families:
        for op in ("rolling_std", "rolling_sum", "rolling_min", "rolling_max"):
            options.extend(
                FactorNode(op=op, args=(field,), window=window, center=False)
                for field in field_nodes
                for window in _WINDOWS
            )
    if "temporal" in allowed_families:
        for op in ("delay", "delta"):
            options.extend(
                FactorNode(op=op, args=(field,), period=period)
                for field in field_nodes
                for period in _PERIODS
            )
    if "arithmetic" in allowed_families:
        options.extend(FactorNode(op="abs", args=(field,)) for field in field_nodes)
    return tuple(options)


def _event_id(slot_id: str, state: CandidateSlotState) -> str:
    """返回 shadow 槽状态事件的稳定 ID。"""

    return f"event-shadow-{slot_id.replace(':', '-')}-{state.value}"


def _set_state(
    ledger: LLMDiscoveryLedger,
    family_id: str,
    slot_id: str,
    target: CandidateSlotState,
    now: datetime,
) -> None:
    """追加确定性槽位状态事件并支持恢复。"""

    state = ledger.project_state(family_id)
    current = state.candidate_slot_states[slot_id]
    if current is target:
        return
    ledger.append_event(
        LLMDiscoveryEvent(
            event_id=_event_id(slot_id, target),
            discovery_family_id=family_id,
            target_kind=DiscoveryObjectKind.CANDIDATE_SLOT,
            target_id=slot_id,
            to_state=target,
            created_at=now,
        )
    )


def _write_slot(
    root: Path,
    family_id: str,
    slot_id: str,
    payload: dict[str, object],
) -> str:
    """以不可变 JSON 写入槽对象并返回其内容哈希。"""

    arm_id, raw_number = slot_id.split(":", 1)
    object_slot_id = (
        slot_id
        if raw_number.startswith("C")
        else f"{arm_id}:C{raw_number}"
    )
    stored_payload = {**payload, "slot_id": object_slot_id}
    encoded = canonical_json_bytes(stored_payload)
    _atomic_write_immutable(_slot_path(root, family_id, slot_id), encoded)
    return sha256_json(stored_payload)


def _slot_path(root: Path, family_id: str, slot_id: str) -> Path:
    """返回候选槽终态对象路径。"""

    arm_id, raw_number = slot_id.split(":", 1)
    object_slot_id = (
        slot_id
        if raw_number.startswith("C")
        else f"{arm_id}:C{raw_number}"
    )
    return (
        root
        / "state"
        / "llm_discovery_families"
        / family_id
        / "candidate_slots"
        / f"{object_slot_id.replace(':', '__')}.json"
    )


def _load_parent(
    root: Path,
    family_id: str,
    slot_id: str,
) -> RegisteredTrustedCandidate | None:
    """从 coverage 槽的不可变对象定位已经登记的父候选。"""

    slot_path = _slot_path(root, family_id, slot_id)
    if not slot_path.is_file():
        return None
    try:
        slot_payload = json.loads(slot_path.read_text(encoding="utf-8"))
        if slot_payload.get("status") != CandidateSlotState.READY_FOR_REGISTRATION.value:
            return None
        candidate_id = slot_payload.get("candidate_id")
        if not isinstance(candidate_id, str):
            return None
        candidate_path = root / "state" / "candidates" / f"{candidate_id}.json"
        return RegisteredTrustedCandidate.model_validate_json(candidate_path.read_bytes())
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _candidate_for_expression(
    parent: RegisteredTrustedCandidate,
    expression: FactorNode,
    *,
    family_id: str,
    slot_id: str,
    generator: str,
    now: datetime,
    registry: FieldAvailabilityRegistry,
) -> RegisteredTrustedCandidate:
    """校验 shadow AST 并构造新的内容寻址候选。"""

    allowed_fields = tuple(
        entry.field_id
        for entry in registry.fields
        if entry.eligible_for_factor and entry.point_in_time_guarantee
    )
    metadata = validate_ast(
        expression,
        allowed_fields=allowed_fields,
        forbidden_fields=("label_o2o_5d", "future_return"),
    )
    spec = parent.spec.model_copy(
        update={
            "expression": expression,
            "required_fields": metadata.required_fields,
            "max_lookback": metadata.lookback,
            "created_at": now,
            "provenance": {
                **parent.spec.provenance,
                "discovery_family_id": family_id,
                "candidate_slot_id": slot_id,
                "generator": generator,
                "parent_candidate_id": parent.candidate_id,
            },
        }
    )
    candidate = registered_trusted_candidate(spec)
    compile_candidate(candidate, allowed_fields=allowed_fields)
    return candidate


def _identity_hashes() -> dict[str, str]:
    """返回 outcome 前冻结的两个 generator 身份。"""

    return {
        "mechanical_mutation": sha256_json(
            {
                "generator": "mechanical_mutation",
                "version": "shadow-v1",
                "windows": _WINDOWS,
                "periods": _PERIODS,
                "order": "root_then_rolling_then_temporal",
            }
        ),
        "hypothesis_conditioned_grammar": sha256_json(
            {
                "generator": "hypothesis_conditioned_grammar",
                "version": "shadow-v1",
                "seed": 20260804,
                "windows": _WINDOWS,
                "periods": _PERIODS,
                "order": "field_then_operator_then_parameter",
            }
        ),
    }


def _strict_evolution_binding(
    *,
    family_id: str,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
) -> ApprovedEvolutionHypothesisBatch | None:
    """核验 shadow 严格演化路径绑定。"""

    if approval_batch is None and evolution_context is None:
        return None
    if approval_batch is None or evolution_context is None:
        raise _shadow_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "严格演化 shadow 必须同时提供 approval_batch 与 evolution_context",
        )
    if evolution_context.discovery_family_id != family_id:
        raise _shadow_error(
            FailureCode.EVOLUTION_HASH_MISMATCH,
            "shadow 演化上下文 family 与当前研究族不一致",
        )
    return require_approved_evolution_batch(
        approval_batch,
        context_sha256=evolution_context.context_sha256,
        discovery_family_id=family_id,
    )


def _evolution_slot_binding(
    slot_id: str,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
) -> dict[str, object]:
    """生成严格演化路径下的槽位绑定元数据。"""

    if approval_batch is None or evolution_context is None:
        return {}
    return {
        "arm_id": slot_id.split(":", 1)[0],
        "logical_hypothesis_id": logical_hypothesis_slot_id(slot_id),
        "candidate_design_slot_id": logical_candidate_design_slot_id(slot_id),
        "context_sha256": evolution_context.context_sha256,
        "approval_batch_sha256": approval_batch.approval_batch_sha256,
    }


def generate_shadow_arms(
    family_id: str,
    family: RegisteredLLMDiscoveryResearchFamily,
    artifact_root: Path,
    approved_coverage_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    registry: FieldAvailabilityRegistry,
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
    now: datetime | None = None,
) -> ShadowGenerationSummary:
    """为已批准 coverage 假设生成两个确定性 shadow arm。

    每个批准假设严格映射到两个 shadow arm 的同编号三个槽位；未批准假设不
    会借用别的父公式。函数只访问服务器账本、候选 Spec 和字段注册表，不访问
    标签、IC、组合收益或 Barra 结果。
    """

    if family.discovery_family_id != family_id:
        raise _shadow_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "shadow family 身份不一致")
    if any(item.decision.decision != "approved" for item in approved_coverage_hypotheses):
        raise _shadow_error(FailureCode.LLM_STATE_TRANSITION_INVALID, "shadow 只接受批准 coverage 假设")
    if any(
        item.draft.slot_id.split(":", 1)[0] != "coverage_outcome_llm"
        for item in approved_coverage_hypotheses
    ):
        raise _shadow_error(FailureCode.LLM_STATE_TRANSITION_INVALID, "shadow 假设必须来自 coverage arm")
    strict_batch = _strict_evolution_binding(
        family_id=family_id,
        approval_batch=approval_batch,
        evolution_context=evolution_context,
    )
    now = now or datetime.now(timezone.utc)
    summary_path = (
        artifact_root
        / "state"
        / "llm_discovery_families"
        / family_id
        / "shadow_generation_summary.json"
    )
    if summary_path.is_file():
        try:
            return ShadowGenerationSummary.model_validate_json(summary_path.read_bytes())
        except (OSError, ValueError):
            raise _shadow_error(FailureCode.LEDGER_CORRUPT, "shadow 摘要损坏") from None
    ledger = LLMDiscoveryLedger(artifact_root)
    state = ledger.project_state(family_id)
    if state.family_generation_state is not FamilyGenerationState.GENERATING:
        raise _shadow_error(FailureCode.LLM_STATE_TRANSITION_INVALID, "shadow 只允许在 generating family 执行")
    approved_by_number = {
        int(item.draft.slot_id.split(":H", 1)[1]): item
        for item in approved_coverage_hypotheses
    }
    generated: list[str] = []
    terminal: list[str] = []
    failed: list[str] = []
    seen_hashes: set[str] = set()
    for arm_id in _SHADOW_ARMS:
        for hypothesis_number in range(1, 11):
            hypothesis = approved_by_number.get(hypothesis_number)
            design_gate = None
            if strict_batch is not None and hypothesis is not None:
                group_expressions: list[FactorNode] = []
                group_hashes: set[str] = set()
                for design_number in range(1, 4):
                    parent = _load_parent(
                        artifact_root,
                        family_id,
                        f"coverage_outcome_llm:C{hypothesis_number * 3 - 3 + design_number:03d}",
                    )
                    if parent is None:
                        group_expressions = []
                        break
                    allowed = frozenset(
                        hypothesis.draft.prediction_proposal.proposed_operator_families
                    )
                    options = (
                        _mutation_options(parent.spec.expression, allowed)
                        if arm_id == "mechanical_mutation"
                        else _grammar_options(parent.spec.expression, allowed)
                    )
                    expression = next(
                        (
                            candidate
                            for candidate in options
                            if (expression_hash := sha256_json(candidate.model_dump(mode="json")))
                            not in seen_hashes | group_hashes
                        ),
                        None,
                    )
                    if expression is None:
                        group_expressions = []
                        break
                    group_expressions.append(expression)
                    group_hashes.add(sha256_json(expression.model_dump(mode="json")))
                if len(group_expressions) == 3:
                    design_gate = preflight_designs(
                        tuple(group_expressions),
                        policy=DesignDiversityPolicy(),
                        registry=registry,
                    ).bind_slot_ids(
                        tuple(
                            f"{arm_id}:C{hypothesis_number * 3 - 3 + design_number:03d}"
                            for design_number in range(1, 4)
                        )
                    )
            for candidate_number in range(1, 4):
                slot_id = f"{arm_id}:{hypothesis_number * 3 - 3 + candidate_number:03d}"
                current = ledger.project_state(family_id).candidate_slot_states[slot_id]
                if current in CANDIDATE_TERMINALS:
                    terminal.append(slot_id)
                    continue
                if hypothesis is None:
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    payload = {
                        "slot_id": slot_id,
                        "status": CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED.value,
                        "generator": arm_id,
                        **slot_binding,
                    }
                    _write_slot(artifact_root, family_id, slot_id, payload)
                    _set_state(
                        ledger,
                        family_id,
                        slot_id,
                        CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
                        now,
                    )
                    terminal.append(slot_id)
                    continue
                if current is CandidateSlotState.RESERVED:
                    _set_state(
                        ledger,
                        family_id,
                        slot_id,
                        CandidateSlotState.GENERATION_IN_PROGRESS,
                        now,
                    )
                parent_slot = (
                    f"coverage_outcome_llm:C"
                    f"{hypothesis_number * 3 - 3 + candidate_number:03d}"
                )
                parent = _load_parent(artifact_root, family_id, parent_slot)
                if parent is None:
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    payload = {
                        "slot_id": slot_id,
                        "status": CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL.value,
                        "generator": arm_id,
                        **slot_binding,
                    }
                    target = CandidateSlotState(payload["status"])
                    _write_slot(artifact_root, family_id, slot_id, payload)
                    _set_state(ledger, family_id, slot_id, target, now)
                    terminal.append(slot_id)
                    if target is not CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED:
                        failed.append(slot_id)
                    continue
                if design_gate is not None and not design_gate.passed:
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    _write_slot(
                        artifact_root,
                        family_id,
                        slot_id,
                        {
                            "slot_id": slot_id,
                            "status": CandidateSlotState.DESIGN_DIVERSITY_FAILED.value,
                            "generator": arm_id,
                            "design_signatures": [
                                {
                                    "canonical_ast_hash": item.canonical_ast_hash,
                                    "ast_without_temporal_parameters_hash": item.ast_without_temporal_parameters_hash,
                                    "field_set": item.field_set,
                                    "operator_topology": item.operator_topology,
                                    "temporal_roles": item.temporal_roles,
                                    "input_combinations": item.input_combinations,
                                    "node_count": item.node_count,
                                    "depth": item.depth,
                                }
                                for item in design_gate.signatures
                            ],
                            "design_diversity_failures": [
                                item.to_dict() for item in design_gate.failures
                            ],
                            "failure_code": FailureCode.DESIGN_DIVERSITY_FAILED.value,
                            "failure_stage": "design_diversity_preflight",
                            "created_at": now.isoformat(),
                            **slot_binding,
                        },
                    )
                    _set_state(
                        ledger,
                        family_id,
                        slot_id,
                        CandidateSlotState.DESIGN_DIVERSITY_FAILED,
                        now,
                    )
                    terminal.append(slot_id)
                    failed.append(slot_id)
                    continue
                try:
                    allowed = frozenset(
                        hypothesis.draft.prediction_proposal.proposed_operator_families
                    )
                    options = (
                        _mutation_options(parent.spec.expression, allowed)
                        if arm_id == "mechanical_mutation"
                        else _grammar_options(parent.spec.expression, allowed)
                    )
                    expression = next(
                        (
                            candidate
                            for candidate in options
                            if sha256_json(candidate.model_dump(mode="json")) not in seen_hashes
                        ),
                        None,
                    )
                    if expression is None or not _families(expression).issubset(allowed):
                        raise _shadow_error(
                            FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                            "没有符合冻结约束的 shadow AST",
                        )
                    candidate = _candidate_for_expression(
                        parent,
                        expression,
                        family_id=family_id,
                        slot_id=slot_id,
                        generator=arm_id,
                        now=now,
                        registry=registry,
                    )
                    expression_hash = sha256_json(expression.model_dump(mode="json"))
                    if expression_hash in seen_hashes:
                        raise _shadow_error(
                            FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED,
                            "shadow AST 重复",
                        )
                    seen_hashes.add(expression_hash)
                    JsonlLedger(artifact_root).register_candidate(candidate)
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    _write_slot(
                        artifact_root,
                        family_id,
                        slot_id,
                        {
                            "slot_id": slot_id,
                            "status": CandidateSlotState.READY_FOR_REGISTRATION.value,
                            "candidate_id": candidate.candidate_id,
                            "candidate_spec_hash": candidate.spec_hash,
                            "generator": arm_id,
                            "parent_candidate_id": parent.candidate_id,
                            **slot_binding,
                        },
                    )
                    _set_state(
                        ledger,
                        family_id,
                        slot_id,
                        CandidateSlotState.DRAFT_GENERATED,
                        now,
                    )
                    _set_state(
                        ledger,
                        family_id,
                        slot_id,
                        CandidateSlotState.READY_FOR_REGISTRATION,
                        now,
                    )
                    generated.append(candidate.candidate_id)
                except FactorMinerError as error:
                    target = (
                        CandidateSlotState.DUPLICATE_FAILED
                        if error.code is FailureCode.REDUNDANCY_THRESHOLD_EXCEEDED
                        else CandidateSlotState.GENERATION_FAILED
                    )
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    _write_slot(
                        artifact_root,
                        family_id,
                        slot_id,
                        {
                            "slot_id": slot_id,
                            "status": target.value,
                            "generator": arm_id,
                            "failure_code": error.code.value,
                            **slot_binding,
                        },
                    )
                    _set_state(ledger, family_id, slot_id, target, now)
                    terminal.append(slot_id)
                    failed.append(slot_id)
                except (OSError, TypeError, ValueError) as error:
                    slot_binding = _evolution_slot_binding(
                        slot_id,
                        approval_batch=strict_batch,
                        evolution_context=evolution_context,
                    )
                    _write_slot(
                        artifact_root,
                        family_id,
                        slot_id,
                        {
                            "slot_id": slot_id,
                            "status": CandidateSlotState.GENERATION_FAILED.value,
                            "generator": arm_id,
                            "failure_code": FailureCode.SPEC_SCHEMA_INVALID.value,
                            "message": str(error),
                            **slot_binding,
                        },
                    )
                    _set_state(ledger, family_id, slot_id, CandidateSlotState.GENERATION_FAILED, now)
                    terminal.append(slot_id)
                    failed.append(slot_id)
    summary_payload = {
        "family_id": family_id,
        "generated_candidate_ids": tuple(sorted(set(generated))),
        "terminal_slot_ids": tuple(sorted(set(terminal))),
        "failed_slot_ids": tuple(sorted(set(failed))),
        "generator_identity_hashes": _identity_hashes(),
    }
    summary = ShadowGenerationSummary(
        **summary_payload,
        summary_sha256=sha256_json(summary_payload),
    )
    _atomic_write_immutable(summary_path, canonical_json_bytes(summary.model_dump(mode="json")))
    return summary


def platform_system() -> str:
    """隔离平台读取，便于 Mac 合成测试显式打桩。"""

    import platform

    return platform.system()
