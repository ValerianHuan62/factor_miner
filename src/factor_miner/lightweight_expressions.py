"""一次请求生成批准假设的全部表达式并保留失败槽。"""

from __future__ import annotations

import ast
from datetime import datetime
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.design_diversity import preflight_designs
from factor_miner.dsl import AstMetadata, iter_ast_nodes, validate_ast
from factor_miner.field_registry import FieldAvailabilityRegistry, resolve_public_field_aliases
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightReviewBatch,
)
from factor_miner.lightweight_schema import LightweightCandidateBinding
from factor_miner.llm_online import AgentRole, PreparedDeepSeekRequest, build_deepseek_request
from factor_miner.pilot_llm import public_field_capabilities
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft
from factor_miner.research_evolution_schema import DesignDiversityPolicy
from factor_miner.schema import (
    AvailabilitySpec,
    ExpectedSign,
    FactorNode,
    HypothesisSpec,
    MechanismStatus,
    RegisteredTrustedCandidate,
    TrustedCandidateFactorSpec,
    registered_trusted_candidate,
)


_HASH = r"^[0-9a-f]{64}$"
_MINIMUM_NODE_COUNT = 5
_MINIMUM_DISTINCT_OPERATOR_COUNT = 3
_PUBLIC_OPERATOR_ALIASES = {
    "constant": "const",
    "diff": "delta",
    "roll_mean": "rolling_mean",
}


def _parse_public_expression_text(value: str) -> dict[str, object]:
    """把模型偶发返回的函数式 DSL 安全转换为 typed AST。"""

    allowed = {
        "abs", "add", "const", "delta", "delay", "div", "field", "mul",
        "neg", "rolling_corr", "rolling_max", "rolling_mean", "rolling_min",
        "rolling_std", "rolling_sum", "sub",
    }
    rolling = {
        "rolling_corr", "rolling_max", "rolling_mean", "rolling_min",
        "rolling_std", "rolling_sum",
    }

    def convert(node: ast.AST) -> dict[str, object]:
        if isinstance(node, ast.Name):
            return {"op": "field", "field": node.id}
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return {"op": "const", "value": node.value}
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            raise ValueError("函数字符串只能包含白名单调用、字段名和有限数字")
        op = _PUBLIC_OPERATOR_ALIASES.get(node.func.id, node.func.id)
        if op not in allowed or node.keywords:
            raise ValueError("函数字符串包含未授权算子或关键字参数")
        if op == "field":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise ValueError("field 必须使用一个字符串字段名")
            return {"op": "field", "field": node.args[0].value}
        if op == "const":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, (int, float)):
                raise ValueError("const 必须使用一个有限数字")
            return {"op": "const", "value": node.args[0].value}
        raw_args = list(node.args)
        result: dict[str, object] = {"op": op}
        if op in rolling or op in {"delta", "delay"}:
            if not raw_args or not isinstance(raw_args[-1], ast.Constant) or not isinstance(raw_args[-1].value, int):
                raise ValueError(f"{op} 的最后一个参数必须是整数周期")
            result["window" if op in rolling else "period"] = raw_args.pop().value
        result["args"] = [convert(item) for item in raw_args]
        return result

    try:
        parsed = ast.parse(value.strip(), mode="eval")
    except (SyntaxError, ValueError) as error:
        raise ValueError("函数字符串不是合法表达式") from error
    return convert(parsed.body)


class LightweightExpressionSlotResult(BaseModel):
    """一个预留槽的表达式终态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_id: str = Field(pattern=r"^H(0[1-9]|10):C00[1-3]$")
    status: Literal["ready", "failed"]
    candidate: RegisteredTrustedCandidate | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> LightweightExpressionSlotResult:
        """ready 和 failed 的载荷不能混用。"""

        if self.status == "ready" and (self.candidate is None or self.failure_reason is not None):
            raise ValueError("ready 表达式槽必须且只能包含候选")
        if self.status == "failed" and (self.candidate is not None or not self.failure_reason):
            raise ValueError("failed 表达式槽必须且只能包含失败原因")
        return self


class LightweightExpressionBatch(BaseModel):
    """与冻结审批集合绑定的完整表达式槽结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "lightweight-expression-batch-v1"
    review_sha256: str = Field(pattern=_HASH)
    request_sha256: str = Field(pattern=_HASH)
    family_size: int = Field(gt=0)
    ready_slot_count: int = Field(ge=0)
    failed_slot_count: int = Field(ge=0)
    slot_results: tuple[LightweightExpressionSlotResult, ...] = Field(min_length=1)
    failed_slot_ids: tuple[str, ...]
    batch_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_batch(self) -> LightweightExpressionBatch:
        """槽位、计数和哈希必须完整一致。"""

        slots = tuple(item.slot_id for item in self.slot_results)
        if len(slots) != self.family_size or len(set(slots)) != self.family_size:
            raise ValueError("表达式批次没有完整覆盖冻结槽位")
        ready = sum(item.status == "ready" for item in self.slot_results)
        failed_ids = tuple(item.slot_id for item in self.slot_results if item.status == "failed")
        if (
            self.ready_slot_count != ready
            or self.failed_slot_count != len(failed_ids)
            or ready + len(failed_ids) != self.family_size
            or self.failed_slot_ids != failed_ids
        ):
            raise ValueError("表达式批次终态计数不一致")
        if self.batch_sha256 != sha256_json(
            self.model_dump(mode="json", exclude={"batch_sha256"})
        ):
            raise ValueError("表达式批次内容身份不一致")
        return self


def _approved_drafts(
    hypotheses: LightweightHypothesisBatch,
    review: LightweightReviewBatch,
) -> tuple[LogicalEvolutionHypothesisDraft, ...]:
    """按 H 槽顺序读取已批准且哈希绑定的草案。"""

    if (
        review.hypothesis_batch_sha256 != hypotheses.batch_sha256
        or review.context_sha256 != hypotheses.context_sha256
    ):
        raise ValueError("表达式请求的假设批次与审批集合不一致")
    approved = set(review.approved_slot_ids)
    return tuple(item for item in hypotheses.hypotheses if item.logical_slot_id in approved)


def build_lightweight_expression_request(
    *,
    hypotheses: LightweightHypothesisBatch,
    review: LightweightReviewBatch,
    registry: FieldAvailabilityRegistry,
    memory_feedback: Mapping[str, object] | None = None,
) -> PreparedDeepSeekRequest:
    """为全部批准假设构造唯一脱敏表达式请求。"""

    drafts = _approved_drafts(hypotheses, review)
    if not drafts:
        raise ValueError("没有批准假设，不能生成表达式请求")
    slots = tuple(
        f"{draft.logical_slot_id}:C{candidate:03d}"
        for draft in drafts
        for candidate in range(1, 4)
    )
    public_hypotheses = [
        draft.model_dump(mode="json", exclude={"draft_sha256", "source_records"})
        for draft in drafts
    ]
    capabilities = [
        item.model_dump(mode="json") for item in public_field_capabilities(registry)
    ]
    public_payload = {
        "information_class": "public_capability_only",
        "review_sha256": review.review_sha256,
        "hypotheses": public_hypotheses,
        "field_capabilities": capabilities,
        "candidate_slot_ids": slots,
        "allowed_operators": (
            "abs", "add", "const", "delta", "delay", "div", "mul", "neg",
            "rolling_corr", "rolling_max", "rolling_mean", "rolling_min",
            "rolling_std", "rolling_sum", "sub",
        ),
        "max_lookback": 130,
        "allowed_windows": (5, 10, 20, 40, 60, 120),
        "minimum_node_count": _MINIMUM_NODE_COUNT,
        "minimum_distinct_operator_count": _MINIMUM_DISTINCT_OPERATOR_COUNT,
        "design_roles": {
            "C001": "标准化变化：差分或延迟变化与滚动波动、均值或区间组合",
            "C002": "跨字段交互：价格、成交量、成交额或高低价至少两类输入组合",
            "C003": "状态或稳定性：相关性、区间位置、极值或波动结构组合",
        },
        "forbidden_templates": (
            "单字段原值",
            "单一滚动均值、滚动标准差、差分或相关性",
            "同一字段两条移动均线之差（MACD 类）",
            "只改变 window 或 period 的三个设计",
        ),
    }
    if memory_feedback:
        public_payload["memory_feedback"] = dict(memory_feedback)
    return build_deepseek_request(
        campaign_id=f"{review.run_id}:expression",
        agent_role=AgentRole.EXPRESSION,
        slot_ids=slots,
        system_prompt=(
            "你是因子表达式代理。只输出 JSON object，根对象必须是 hypotheses 数组。"
            "每个批准假设对象必须使用 logical_slot_id 和 designs 两个字段；"
            "每个 design 对象必须使用 candidate_slot_id 和 expression 两个字段，"
            "并精确覆盖三个预留槽。不得把这两个槽位字段简写成 slot_id。"
            "为避免批量响应被截断，expression 必须使用紧凑函数式 DSL 字符串，"
            "例如 div(delta(price_close,20),rolling_std(price_close,20))；"
            "字段直接写字段名，滚动窗口或差分周期写作最后一个整数参数。"
            "不得为每个节点展开 op、args、field、window 等 JSON object。"
            "不得使用 allowed_operators 列表以外的算子，尤其不得使用 rolling_rank。"
            "const 必须精确写成 const，rolling_mean 必须精确写成 rolling_mean。"
            "rolling 算子的 window 只能是 5、10、20、40、60、120；"
            "delta 和 delay 必须包含 period，且 period 只能是这些允许值。"
            "每个表达式至少包含 5 个 AST 节点和 3 种不同的非叶子算子；"
            "禁止输出单一 rolling_mean、rolling_std、delta、rolling_corr，"
            "禁止输出移动均线差、MACD 或其他教科书技术指标模板。"
            "同一假设的 C001、C002、C003 必须严格采用 user payload 的三种 design_roles；"
            "三者至少在根算子、字段组合和算子拓扑中的两项不同，不能复制表达式，"
            "也不能只改变 window 或 period。优先构造有经济含义的标准化变化、"
            "跨字段交互和状态条件结构，不得直接相乘两个价格水平。"
            "expression 只能使用给定公开字段别名和白名单 typed AST；不得输出解释、"
            "回测、代码、标签、未来字段或修改候选预算。"
            "如果 user payload 含 memory_feedback，必须针对其中的失败模式修正结构，"
            "避开历史重复 AST，并优先满足正向超额信息比率这一组合目标；不得编造"
            "历史精确指标。"
        ),
        user_payload=public_payload,
        audit_payload={
            "information_class": "public_capability_only",
            "review_sha256": review.review_sha256,
        },
        thinking="disabled",
    )


def _slot_value(
    item: Mapping[str, object],
    *,
    canonical_key: str,
) -> object:
    """兼容模型唯一已知的 slot_id 简写，但拒绝含义冲突。"""

    canonical = item.get(canonical_key)
    alias = item.get("slot_id")
    if canonical is not None and alias is not None and canonical != alias:
        raise ValueError("表达式响应槽位字段冲突")
    return canonical if canonical is not None else alias


def _normalize_public_ast(node: object) -> object:
    """把一种常见模型 AST 方言确定性转换为项目 typed AST。"""

    if isinstance(node, str):
        node = _parse_public_expression_text(node)
    if not isinstance(node, Mapping):
        return node
    normalized = dict(node)
    canonical_op = normalized.get("op")
    alias_op = normalized.pop("type", None)
    if canonical_op is not None and alias_op is not None and canonical_op != alias_op:
        raise ValueError("表达式算子字段冲突")
    if canonical_op is None and alias_op is not None:
        normalized["op"] = alias_op
    if normalized.get("op") in _PUBLIC_OPERATOR_ALIASES:
        normalized["op"] = _PUBLIC_OPERATOR_ALIASES[str(normalized["op"])]

    canonical_period = normalized.get("period")
    alias_period = normalized.pop("periods", None)
    if (
        canonical_period is not None
        and alias_period is not None
        and canonical_period != alias_period
    ):
        raise ValueError("表达式周期字段冲突")
    if canonical_period is None and alias_period is not None:
        normalized["period"] = alias_period

    has_args = "args" in normalized
    has_input = "input" in normalized
    has_left = "left" in normalized
    has_right = "right" in normalized
    if has_args and (has_input or has_left or has_right):
        raise ValueError("表达式子节点字段冲突")
    if has_input and (has_left or has_right):
        raise ValueError("表达式子节点字段冲突")
    if has_left != has_right:
        raise ValueError("二元表达式必须同时包含 left 和 right")
    if has_input:
        normalized["args"] = [normalized.pop("input")]
    elif has_left:
        normalized["args"] = [normalized.pop("left"), normalized.pop("right")]

    raw_args = normalized.get("args")
    if isinstance(raw_args, Sequence) and not isinstance(raw_args, (str, bytes)):
        normalized["args"] = [_normalize_public_ast(item) for item in raw_args]
    return normalized


def _contains_moving_average_difference(node: FactorNode) -> bool:
    """识别 MACD 类的同层移动均线差模板。"""

    if (
        node.op == "sub"
        and len(node.args) == 2
        and node.args[0].op == "rolling_mean"
        and node.args[1].op == "rolling_mean"
    ):
        return True
    return any(_contains_moving_average_difference(child) for child in node.args)


def _validate_research_complexity(
    expression: FactorNode,
    metadata: AstMetadata,
) -> None:
    """拒绝低信息量教科书模板，保留有限且可解释的组合复杂度。"""

    operators = {
        node.op for node in iter_ast_nodes(expression)
        if node.op not in {"field", "const"}
    }
    if metadata.node_count < _MINIMUM_NODE_COUNT:
        raise ValueError(
            f"表达式过于简单：至少需要 {_MINIMUM_NODE_COUNT} 个 AST 节点"
        )
    if _contains_moving_average_difference(expression):
        raise ValueError("禁止使用移动均线差或 MACD 类教科书模板")
    if len(operators) < _MINIMUM_DISTINCT_OPERATOR_COUNT:
        raise ValueError(
            "表达式过于简单：至少需要 "
            f"{_MINIMUM_DISTINCT_OPERATOR_COUNT} 种不同的非叶子算子"
        )


def _expected_sign(expected_direction: str) -> ExpectedSign:
    """从明确的机器值或中文方向前缀读取冻结收益方向。"""

    value = expected_direction.strip()
    if value == ExpectedSign.POSITIVE.value or value.startswith("正向："):
        return ExpectedSign.POSITIVE
    if value == ExpectedSign.NEGATIVE.value or value.startswith("负向："):
        return ExpectedSign.NEGATIVE
    raise ValueError("预期方向必须以正向或负向开头")


def _hypothesis_spec(draft: LogicalEvolutionHypothesisDraft) -> HypothesisSpec:
    """把已批准逻辑假设确定性转换为候选事前假设。"""

    return HypothesisSpec(
        claim=draft.prior_claim,
        mechanism=draft.mechanism,
        expected_sign=_expected_sign(draft.expected_direction),
        observable_proxy=draft.observable_proxy,
        independent_verification=draft.independent_verification,
        competing_explanations=draft.competing_explanations,
        baseline_reference=f"覆盖图谱缺口：{','.join(draft.gap_ids)}",
        failure_modes=draft.failure_modes,
        falsification_path=draft.falsification_path,
        source_refs=tuple(item.source_record_id for item in draft.source_records),
        mechanism_status=MechanismStatus.MECHANISM_UNVERIFIED,
    )


def _candidate(
    *,
    slot_id: str,
    draft: LogicalEvolutionHypothesisDraft,
    public_expression: FactorNode,
    registry: FieldAvailabilityRegistry,
    review: LightweightReviewBatch,
    created_at: datetime,
) -> RegisteredTrustedCandidate:
    """执行本地字段、AST 和 compiler 硬校验并登记候选。"""

    local = resolve_public_field_aliases(public_expression, registry)
    allowed = tuple(item.field_id for item in registry.fields if item.eligible_for_factor)
    metadata = validate_ast(
        local,
        allowed_fields=allowed,
        forbidden_fields=("label", "future_return", "label_o2o_5d"),
    )
    _validate_research_complexity(local, metadata)
    spec = TrustedCandidateFactorSpec(
        hypothesis=_hypothesis_spec(draft),
        expression=local,
        required_fields=metadata.required_fields,
        max_lookback=metadata.lookback,
        availability=AvailabilitySpec(
            observation="close_t",
            decision="after_close_t",
            earliest_trade="open_t_plus_1",
        ),
        created_at=created_at,
        provenance={
            "source": "deepseek_lightweight_dashboard",
            "candidate_slot_id": slot_id,
            "review_sha256": review.review_sha256,
            "field_registry_id": registry.registry_id,
            "data_release_id": registry.data_release_id,
        },
    )
    registered = registered_trusted_candidate(spec)
    compile_candidate(registered, allowed_fields=allowed)
    return registered


def parse_lightweight_expression_response(
    *,
    prepared: PreparedDeepSeekRequest,
    response: Mapping[str, object],
    hypotheses: LightweightHypothesisBatch,
    review: LightweightReviewBatch,
    registry: FieldAvailabilityRegistry,
    created_at: datetime,
) -> LightweightExpressionBatch:
    """逐槽解析响应；无效槽失败但不从冻结 family 消失。"""

    if prepared.agent_role is not AgentRole.EXPRESSION:
        raise ValueError("轻量表达式请求角色必须是 expression")
    drafts = _approved_drafts(hypotheses, review)
    draft_by_slot = {item.logical_slot_id: item for item in drafts}
    expected_slots = prepared.slot_ids
    raw_groups = response.get("hypotheses")
    if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
        raise ValueError("表达式响应缺少 hypotheses 数组")
    raw_by_slot: dict[str, object] = {}
    for group in raw_groups:
        if not isinstance(group, Mapping):
            continue
        logical_slot = _slot_value(group, canonical_key="logical_slot_id")
        if logical_slot not in draft_by_slot or logical_slot in raw_by_slot:
            raise ValueError("表达式响应包含未批准或重复的假设槽")
        raw_by_slot[str(logical_slot)] = group.get("designs")

    results: list[LightweightExpressionSlotResult] = []
    local_by_hypothesis: dict[str, list[tuple[str, FactorNode]]] = {}
    for slot_id in expected_slots:
        logical_slot = slot_id.split(":", 1)[0]
        raw_designs = raw_by_slot.get(logical_slot)
        raw_design = None
        if isinstance(raw_designs, Sequence) and not isinstance(raw_designs, (str, bytes)):
            raw_design = next(
                (
                    item for item in raw_designs
                    if isinstance(item, Mapping)
                    and _slot_value(item, canonical_key="candidate_slot_id") == slot_id
                ),
                None,
            )
        try:
            if not isinstance(raw_design, Mapping):
                raise ValueError("模型响应缺少预留槽")
            expression = FactorNode.model_validate(
                _normalize_public_ast(raw_design.get("expression"))
            )
            registered = _candidate(
                slot_id=slot_id,
                draft=draft_by_slot[logical_slot],
                public_expression=expression,
                registry=registry,
                review=review,
                created_at=created_at,
            )
            local_by_hypothesis.setdefault(logical_slot, []).append(
                (slot_id, registered.spec.expression)
            )
            results.append(
                LightweightExpressionSlotResult(
                    slot_id=slot_id,
                    status="ready",
                    candidate=registered,
                )
            )
        except Exception as error:
            results.append(
                LightweightExpressionSlotResult(
                    slot_id=slot_id,
                    status="failed",
                    failure_reason=f"表达式硬校验失败：{error}",
                )
            )

    by_slot = {item.slot_id: item for item in results}
    for logical_slot, values in local_by_hypothesis.items():
        if len(values) != 3:
            continue
        try:
            diversity = preflight_designs(
                tuple(item[1] for item in values),  # type: ignore[arg-type]
                policy=DesignDiversityPolicy(),
                registry=registry,
            )
        except Exception as error:
            for slot_id, _ in values:
                by_slot[slot_id] = LightweightExpressionSlotResult(
                    slot_id=slot_id,
                    status="failed",
                    failure_reason=f"三个候选设计结构或单位校验失败：{error}",
                )
            continue
        if diversity.passed:
            continue
        for slot_id, _ in values:
            by_slot[slot_id] = LightweightExpressionSlotResult(
                slot_id=slot_id,
                status="failed",
                failure_reason="三个候选设计缺少实质结构差异",
            )
    ordered = tuple(by_slot[slot_id] for slot_id in expected_slots)
    failed_ids = tuple(item.slot_id for item in ordered if item.status == "failed")
    payload = {
        "version": "lightweight-expression-batch-v1",
        "review_sha256": review.review_sha256,
        "request_sha256": prepared.request_sha256,
        "family_size": len(expected_slots),
        "ready_slot_count": len(expected_slots) - len(failed_ids),
        "failed_slot_count": len(failed_ids),
        "slot_results": ordered,
        "failed_slot_ids": failed_ids,
    }
    draft_batch = LightweightExpressionBatch.model_construct(
        batch_sha256="0" * 64,
        **payload,
    )
    digest = sha256_json(
        draft_batch.model_dump(mode="json", exclude={"batch_sha256"})
    )
    return LightweightExpressionBatch(**payload, batch_sha256=digest)


def candidate_bindings_from_expression_batch(
    batch: LightweightExpressionBatch,
) -> tuple[LightweightCandidateBinding, ...]:
    """把 ready/failed 槽完整投影为 manifest binding。"""

    bindings = []
    for result in batch.slot_results:
        if result.status == "ready":
            assert result.candidate is not None
            bindings.append(
                LightweightCandidateBinding(
                    slot_id=result.slot_id,
                    terminal_status="ready",
                    source_candidate_id=result.candidate.candidate_id,
                    candidate_spec_hash=result.candidate.spec_hash,
                )
            )
        else:
            bindings.append(
                LightweightCandidateBinding(
                    slot_id=result.slot_id,
                    terminal_status="failed",
                    failure_reason=result.failure_reason,
                )
            )
    return tuple(bindings)
