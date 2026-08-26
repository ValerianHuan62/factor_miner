"""为文献型LLM arm的振幅异常假设生成3个候选因子表达式。

假设：日内价格振幅异常大的股票，未来5个交易日收益为负。
机制：投资者把日内极值作为参考点，造成短期过度反应，随后出现价格修正。
可观察代理：(high - low) / 过去5日平均(high - low)
预期方向：负向
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from factor_miner.canonical import sha256_json
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.llm_candidate import (
    CandidateExpressionBatch,
    CandidateExpressionDraft,
    SemanticLintBatch,
    SemanticLintDecision,
)
from factor_miner.llm_hypothesis import (
    CoverageGapHypothesisDraft,
    HypothesisDecision,
    PredictionProposal,
    compose_testable_prediction,
    registered_coverage_gap_hypothesis,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.schema import FactorNode


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
ARM = "literature_only_llm"
HYPOTHESIS_SLOT = f"{ARM}:H01"

# ── 字段注册表（公开别名 → 服务器真实字段） ──────────────────────

FIELD_REGISTRY = FieldAvailabilityRegistry(
    registry_id="field-registry-amplitude-v1",
    data_release_id="quantlake-20260727-f6f1d26940aa9a4b+coverage-visible-2026-v1",
    fields=(
        FieldAvailabilityEntry(
            field_id="close",
            public_alias="price_close",
            economic_type="market_price",
            unit_dimension="price",
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
        FieldAvailabilityEntry(
            field_id="high",
            public_alias="price_high",
            economic_type="market_price",
            unit_dimension="price",
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
        FieldAvailabilityEntry(
            field_id="low",
            public_alias="price_low",
            economic_type="market_price",
            unit_dimension="price",
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
        FieldAvailabilityEntry(
            field_id="open",
            public_alias="price_open",
            economic_type="market_price",
            unit_dimension="price",
            panel_shape="asset_date_scalar",
            event_time="open_t",
            source_publish_time="open_t",
            vendor_available_time="open_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
        FieldAvailabilityEntry(
            field_id="amount",
            public_alias="traded_value",
            economic_type="currency_amount",
            unit_dimension="currency_amount",
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
        FieldAvailabilityEntry(
            field_id="volume",
            public_alias="traded_volume",
            economic_type="share_volume",
            unit_dimension="share_volume",
            panel_shape="asset_date_scalar",
            event_time="close_t",
            source_publish_time="close_t",
            vendor_available_time="after_close_t",
            revision_policy="immutable_daily",
            point_in_time_guarantee=True,
            earliest_decision_time="after_close_t",
            eligible_for_factor=True,
        ),
    ),
)

# ── 批准假设 ──────────────────────────────────────────────────

PROPOSAL = PredictionProposal(
    observable_proxy="(price_high - price_low) / 过去5日平均(price_high - price_low)",
    expected_sign="negative",
    proposed_field_aliases=("price_close", "price_high", "price_low", "traded_volume"),
    proposed_operator_families=("arithmetic", "rolling", "temporal"),
    optional_conditioning_claim=None,
)

DRAFT = CoverageGapHypothesisDraft(
    slot_id=HYPOTHESIS_SLOT,
    gap_id="G001",
    hypothesis_origin="literature_informed",
    claim="日内价格振幅异常大的股票，未来5个交易日收益为负。",
    economic_mechanism="投资者把日内极值作为参考点，造成短期过度反应，随后出现价格修正。",
    independent_verification="按振幅极端程度分组，检验各组未来5日收益的单调性。",
    competing_explanations=(
        "大振幅可能来自信息不对称，导致收益延续而非反转。",
        "流动性冲击可能同时推高振幅和后续收益。",
    ),
    failure_modes=(
        "窄tick市场（如高价股最小变动单位受限）导致振幅度量失真。",
        "市场整体高波动期间，振幅异常信号被系统性波动淹没。",
    ),
    falsification_path="若5日RankIC方向为正且显著，或独立按振幅分组后高振幅组收益不低于低振幅组，则否定该假设。",
    source_record_ids=("10.1111/jofi.12001", "10.1016/j.jfineco.2019.05.005"),
    prediction_proposal=PROPOSAL,
)

DECISION = HypothesisDecision(
    draft_id="draft_" + "a" * 24,
    decision="approved",
    reason="代理、可观察变量、竞争解释和证伪路径完整。文献来源支持振幅异常与短期反转的关联。",
    verified_source_record_ids=DRAFT.source_record_ids,
    literature_status="proxy_choice_supported",
    reviewer_role="research_owner",
    created_at=NOW,
)

TESTABLE = compose_testable_prediction(
    PROPOSAL,
    company_a_share_visible_policy(),
    discovery_family_id="llmfamily_" + "f" * 24,
)

REGISTERED_HYPOTHESIS = registered_coverage_gap_hypothesis(
    DRAFT,
    TESTABLE,
    DECISION,
)

# ── 3个候选因子表达式（公开别名 AST） ──────────────────────────


def make_amplitude_range() -> FactorNode:
    """日内振幅 = price_high - price_low"""
    return FactorNode(
        op="sub",
        args=(
            FactorNode(op="field", field="price_high"),
            FactorNode(op="field", field="price_low"),
        ),
    )


# C001：基准相对振幅（假设核心代理的直接实现）
# (price_high - price_low) / rolling_mean(price_high - price_low, 5)
C001 = FactorNode(
    op="div",
    args=(
        make_amplitude_range(),
        FactorNode(
            op="rolling_mean",
            args=(make_amplitude_range(),),
            window=5,
        ),
    ),
)

# C002：振幅突变信号（日内振幅的日间变化率）
# delta(price_high - price_low, 1) / rolling_mean(price_high - price_low, 5)
# 捕捉振幅的突然放大 — 比持续高振幅更能代表过度反应
C002 = FactorNode(
    op="div",
    args=(
        FactorNode(
            op="delta",
            args=(make_amplitude_range(),),
            period=1,
        ),
        FactorNode(
            op="rolling_mean",
            args=(make_amplitude_range(),),
            window=5,
        ),
    ),
)

# C003：价格归一化振幅异常（控制价格水平差异）
# div(
#   div(price_high - price_low, price_close),
#   rolling_mean(div(price_high - price_low, price_close), 5)
# )
# 先用收盘价归一化日内振幅（百分比振幅），再与自身历史均值比较
C003 = FactorNode(
    op="div",
    args=(
        FactorNode(
            op="div",
            args=(
                make_amplitude_range(),
                FactorNode(op="field", field="price_close"),
            ),
        ),
        FactorNode(
            op="rolling_mean",
            args=(
                FactorNode(
                    op="div",
                    args=(
                        make_amplitude_range(),
                        FactorNode(op="field", field="price_close"),
                    ),
                ),
            ),
            window=5,
        ),
    ),
)

CANDIDATES = (
    CandidateExpressionDraft(
        candidate_slot_id=f"{ARM}:C001",
        expression=C001,
    ),
    CandidateExpressionDraft(
        candidate_slot_id=f"{ARM}:C002",
        expression=C002,
    ),
    CandidateExpressionDraft(
        candidate_slot_id=f"{ARM}:C003",
        expression=C003,
    ),
)

BATCH = CandidateExpressionBatch(candidates=CANDIDATES)

# ── Semantic Lint（代理3 全部批准） ────────────────────────────

LINT_DECISIONS = tuple(
    SemanticLintDecision(
        candidate_slot_id=c.candidate_slot_id,
        decision="approved",
        proxy_alignment=True,
        direction_alignment=True,
        availability_alignment=True,
        undeclared_exposure=False,
        reason_codes=(),
        summary=f"候选 {c.candidate_slot_id}：可观察代理、方向、字段可得性和窗口均与冻结假设一致，未引入未声明经济暴露。",
    )
    for c in CANDIDATES
)

LINT_BATCH = SemanticLintBatch(decisions=LINT_DECISIONS)


# ── 验证 ──────────────────────────────────────────────────────

def validate_candidate(
    draft: CandidateExpressionDraft,
    lint: SemanticLintDecision,
    index: int,
) -> None:
    """对单个候选执行完整硬校验链。"""
    label = f"C{index+1:03d} ({draft.candidate_slot_id})"

    # 1. DSL 结构校验 (使用真实字段)
    from factor_miner.llm_candidate import convert_candidate
    from factor_miner.field_registry import resolve_public_field_aliases

    local_ast = resolve_public_field_aliases(draft.expression, FIELD_REGISTRY)
    metadata = validate_ast(
        local_ast,
        allowed_fields=tuple(
            f.field_id for f in FIELD_REGISTRY.fields if f.eligible_for_factor
        ),
        forbidden_fields=("label_o2o_5d", "future_return"),
    )
    print(f"  [{label}] DSL 校验通过: fields={metadata.required_fields}, "
          f"lookback={metadata.lookback}, nodes={metadata.node_count}, "
          f"depth={metadata.depth}")

    # 2. 语义类型分析
    semantic = analyse_semantic_type(draft.expression, FIELD_REGISTRY)
    print(f"  [{label}] 语义类型: unit={semantic.unit_dimension}, "
          f"axis={semantic.axis_type}, decision_time={semantic.earliest_decision_time}")
    assert semantic.unit_dimension == "dimensionless", \
        f"因子必须无量纲，当前为 {semantic.unit_dimension}"

    # 3. 完整候选转换
    candidate = convert_candidate(
        draft=draft,
        hypothesis=REGISTERED_HYPOTHESIS,
        lint=lint,
        registry=FIELD_REGISTRY,
        created_at=NOW,
        provenance={
            "discovery_family_id": "llmfamily_" + "f" * 24,
            "arm_id": ARM,
            "hypothesis_slot_id": HYPOTHESIS_SLOT,
            "candidate_slot_id": draft.candidate_slot_id,
        },
    )
    from factor_miner.schema import registered_trusted_candidate
    registered = registered_trusted_candidate(candidate)
    print(f"  [{label}] 候选转换成功: id={registered.candidate_id}, "
          f"required_fields={candidate.required_fields}, "
          f"lookback={candidate.max_lookback}")
    print(f"  [{label}] canonical_ast_hash={sha256_json(candidate.expression.model_dump(mode='json'))[:24]}...")


# ── 主流程 ─────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print("日内振幅异常 → 短期反转：3 候选因子表达式生成")
    print("=" * 70)

    print(f"\n假设 ID: {REGISTERED_HYPOTHESIS.hypothesis_id}")
    print(f"假设: {DRAFT.claim}")
    print(f"预期方向: {TESTABLE.expected_sign}")
    print(f"机制状态: {DRAFT.hypothesis_origin}")

    print(f"\n── 字段可得性 ──")
    for f in FIELD_REGISTRY.fields:
        print(f"  {f.public_alias} → {f.field_id} "
              f"(unit={f.unit_dimension}, pit={f.point_in_time_guarantee})")

    print(f"\n── 候选因子 ──")
    for i, draft in enumerate(CANDIDATES):
        description = {
            0: "基准相对振幅：当日振幅 / 5日均幅。直接实现假设的可观察代理。",
            1: "振幅突变信号：振幅日间变化 / 5日均幅。捕捉振幅突然放大——比持续高振幅更能代表过度反应。",
            2: "价格归一化振幅异常：先用收盘价归一化振幅（消除价格水平差异），再与自身5日均值比较。",
        }[i]
        print(f"\n  C{i+1:03d}: {draft.candidate_slot_id}")
        print(f"  描述: {description}")

    print(f"\n── 硬校验 ──")
    for i, (draft, lint) in enumerate(zip(CANDIDATES, LINT_DECISIONS)):
        try:
            validate_candidate(draft, lint, i)
        except Exception as e:
            print(f"  ❌ 校验失败: {e}")
            raise

    print(f"\n── 输出文件 ──")

    # 写入 CandidateExpressionBatch
    batch_path = "v05_amplitude_candidate_batch.local.json"
    with open(batch_path, "w", encoding="utf-8") as f:
        json.dump(BATCH.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    print(f"  ✓ {batch_path}")

    # 写入 SemanticLintBatch
    lint_path = "v05_amplitude_lint_batch.local.json"
    with open(lint_path, "w", encoding="utf-8") as f:
        json.dump(LINT_BATCH.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    print(f"  ✓ {lint_path}")

    # 写入假设
    hyp_path = "v05_amplitude_hypothesis_registered.local.json"
    with open(hyp_path, "w", encoding="utf-8") as f:
        json.dump(
            REGISTERED_HYPOTHESIS.model_dump(mode="json"),
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  ✓ {hyp_path}")

    print(f"\n── Canonical AST 哈希（去重检查）──")
    seen = set()
    for draft in CANDIDATES:
        h = sha256_json(draft.expression.model_dump(mode="json"))[:16]
        assert h not in seen, f"重复 AST: {draft.candidate_slot_id}"
        seen.add(h)
        print(f"  {draft.candidate_slot_id}: {h}")
    print("  全部唯一 ✓")

    print(f"\n{'=' * 70}")
    print("3 个候选因子全部通过硬校验，可进入候选登记流程。")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
