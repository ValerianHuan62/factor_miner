"""任务四三因子设计差异闸门测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from factor_miner.design_diversity import preflight_designs
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.llm_candidate import (
    CandidateExpressionBatch,
    CandidateExpressionDraft,
    SemanticLintBatch,
    SemanticLintDecision,
    preflight_candidate_batch,
)
from factor_miner.llm_hypothesis import (
    CoverageGapHypothesisDraft,
    HypothesisDecision,
    PredictionProposal,
    compose_testable_prediction,
    registered_coverage_gap_hypothesis,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.research_evolution_schema import DesignDiversityPolicy
from factor_miner.schema import FactorNode

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)
POLICY = DesignDiversityPolicy()


def _field(name: str) -> FactorNode:
    return FactorNode(op="field", field=name)


def _registry() -> FieldAvailabilityRegistry:
    """构造只含脱敏公开别名的 synthetic 字段注册表。"""

    return FieldAvailabilityRegistry(
        registry_id="field-registry-design-diversity-v1",
        data_release_id="release-design-diversity-v1",
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
                field_id="volume",
                public_alias="volume",
                economic_type="market_volume",
                unit_dimension="shares",
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


def _hypothesis() -> object:
    """构造允许价格与成交量 DSL 的 synthetic 假设。"""

    proposal = PredictionProposal(
        observable_proxy="价格与成交量共同刻画行为扩散",
        expected_sign="positive",
        proposed_field_aliases=("price_close", "volume"),
        proposed_operator_families=("arithmetic", "rolling", "temporal"),
        optional_conditioning_claim=None,
    )
    draft = CoverageGapHypothesisDraft(
        slot_id="coverage_outcome_llm:H01",
        gap_id="G001",
        hypothesis_origin="coverage_outcome_informed",
        claim="价格与成交量的不同组合可能反映扩散强弱。",
        economic_mechanism="不同交易活跃度下的信息扩散路径不同。",
        independent_verification="按成交拥挤度做独立分组。",
        competing_explanations=("短期流动性冲击",),
        failure_modes=("高波动下的反转压制",),
        falsification_path="方向显著反向。",
        source_record_ids=("source-doi-1",),
        prediction_proposal=proposal,
    )
    decision = HypothesisDecision(
        draft_id="draft_" + "2" * 24,
        decision="approved",
        reason="synthetic 假设只用于脱敏结构测试。",
        verified_source_record_ids=("source-doi-1",),
        literature_status="proxy_choice_supported",
        reviewer_role="research_owner",
        created_at=NOW,
    )
    return registered_coverage_gap_hypothesis(
        draft,
        compose_testable_prediction(
            proposal,
            company_a_share_visible_policy(),
            discovery_family_id="llmfamily_" + "1" * 24,
        ),
        decision,
    )


def _hypothesis_with_extra_alias(alias: str) -> object:
    """构造额外允许一个公开别名的 synthetic 假设。"""

    proposal = PredictionProposal(
        observable_proxy="价格与成交量共同刻画行为扩散",
        expected_sign="positive",
        proposed_field_aliases=tuple(sorted(("price_close", "volume", alias))),
        proposed_operator_families=("arithmetic", "rolling", "temporal"),
        optional_conditioning_claim=None,
    )
    draft = CoverageGapHypothesisDraft(
        slot_id="coverage_outcome_llm:H01",
        gap_id="G001",
        hypothesis_origin="coverage_outcome_informed",
        claim="价格与成交量的不同组合可能反映扩散强弱。",
        economic_mechanism="不同交易活跃度下的信息扩散路径不同。",
        independent_verification="按成交拥挤度做独立分组。",
        competing_explanations=("短期流动性冲击",),
        failure_modes=("高波动下的反转压制",),
        falsification_path="方向显著反向。",
        source_record_ids=("source-doi-1",),
        prediction_proposal=proposal,
    )
    decision = HypothesisDecision(
        draft_id="draft_" + "2" * 24,
        decision="approved",
        reason="synthetic 假设只用于脱敏结构测试。",
        verified_source_record_ids=("source-doi-1",),
        literature_status="proxy_choice_supported",
        reviewer_role="research_owner",
        created_at=NOW,
    )
    return registered_coverage_gap_hypothesis(
        draft,
        compose_testable_prediction(
            proposal,
            company_a_share_visible_policy(),
            discovery_family_id="llmfamily_" + "1" * 24,
        ),
        decision,
    )


def _approved_lint_batch() -> SemanticLintBatch:
    """构造三个槽位全批准 lint。"""

    return SemanticLintBatch(
        decisions=(
            SemanticLintDecision(
                candidate_slot_id="coverage_outcome_llm:C001",
                decision="approved",
                proxy_alignment=True,
                direction_alignment=True,
                availability_alignment=True,
                undeclared_exposure=False,
                reason_codes=("ok_001",),
                summary="synthetic 通过。",
            ),
            SemanticLintDecision(
                candidate_slot_id="coverage_outcome_llm:C002",
                decision="approved",
                proxy_alignment=True,
                direction_alignment=True,
                availability_alignment=True,
                undeclared_exposure=False,
                reason_codes=("ok_002",),
                summary="synthetic 通过。",
            ),
            SemanticLintDecision(
                candidate_slot_id="coverage_outcome_llm:C003",
                decision="approved",
                proxy_alignment=True,
                direction_alignment=True,
                availability_alignment=True,
                undeclared_exposure=False,
                reason_codes=("ok_003",),
                summary="synthetic 通过。",
            ),
        )
    )


def _batch(*expressions: FactorNode) -> CandidateExpressionBatch:
    """把三个表达式包装为固定槽位批次。"""

    slot_ids = (
        "coverage_outcome_llm:C001",
        "coverage_outcome_llm:C002",
        "coverage_outcome_llm:C003",
    )
    return CandidateExpressionBatch(
        candidates=tuple(
            CandidateExpressionDraft(
                candidate_slot_id=slot_id,
                expression=expression,
            )
            for slot_id, expression in zip(slot_ids, expressions, strict=True)
        )
    )


def _provenance() -> dict[str, dict[str, str]]:
    """构造按槽位区分的 synthetic provenance。"""

    return {
        "coverage_outcome_llm:C001": {
            "discovery_family_id": "llmfamily_" + "1" * 24,
            "expression_call_id": "llmcall_" + "3" * 24,
            "semantic_lint_call_id": "llmcall_" + "4" * 24,
        },
        "coverage_outcome_llm:C002": {
            "discovery_family_id": "llmfamily_" + "1" * 24,
            "expression_call_id": "llmcall_" + "5" * 24,
            "semantic_lint_call_id": "llmcall_" + "6" * 24,
        },
        "coverage_outcome_llm:C003": {
            "discovery_family_id": "llmfamily_" + "1" * 24,
            "expression_call_id": "llmcall_" + "7" * 24,
            "semantic_lint_call_id": "llmcall_" + "8" * 24,
        },
    }


class DesignDiversityGateTest(unittest.TestCase):
    """三因子结构闸门必须拒绝只改时间参数的重复设计。"""

    def test_exact_duplicate_designs_fail_with_serializable_slot_pairs(self) -> None:
        shared = FactorNode(
            op="div",
            args=(
                FactorNode(op="delta", args=(_field("close"),), period=20),
                FactorNode(op="delay", args=(_field("close"),), period=20),
            ),
        )
        third = FactorNode(
            op="div",
            args=(
                FactorNode(op="delta", args=(_field("volume"),), period=20),
                FactorNode(op="delay", args=(_field("volume"),), period=20),
            ),
        )
        result = preflight_designs(
            (shared, shared, third),
            policy=POLICY,
            registry=_registry(),
        ).bind_slot_ids(
            (
                "coverage_outcome_llm:C001",
                "coverage_outcome_llm:C002",
                "coverage_outcome_llm:C003",
            )
        )

        self.assertFalse(result.passed)
        self.assertEqual(
            result.failure_code,
            FailureCode.DESIGN_DIVERSITY_FAILED.value,
        )
        self.assertEqual(
            [item.axis_name for item in result.failures],
            [
                "canonical_ast_hash",
                "ast_without_temporal_parameters_hash",
            ],
        )
        self.assertEqual(result.failures[0].left_slot_id, "coverage_outcome_llm:C001")
        self.assertEqual(result.failures[0].right_slot_id, "coverage_outcome_llm:C002")
        json.dumps(result.to_dict(), ensure_ascii=False)

    def test_temporal_window_only_change_fails(self) -> None:
        result = preflight_candidate_batch(
            candidate_batch=_batch(
                FactorNode(op="rolling_mean", args=(_field("price_close"),), window=20),
                FactorNode(op="rolling_mean", args=(_field("price_close"),), window=60),
                FactorNode(op="div", args=(_field("price_close"), _field("volume"))),
            ),
            lint_batch=_approved_lint_batch(),
            hypothesis=_hypothesis(),
            registry=_registry(),
            created_at=NOW,
            provenance_by_slot=_provenance(),
            policy=POLICY,
        )

        self.assertFalse(result.design_result.passed)
        self.assertEqual(
            [item.axis_name for item in result.design_result.failures],
            ["ast_without_temporal_parameters_hash"],
        )
        self.assertEqual(
            result.design_result.failures[0].left_hash,
            result.design_result.signatures[0].ast_without_temporal_parameters_hash,
        )
        self.assertEqual(
            result.design_result.failures[0].right_hash,
            result.design_result.signatures[1].ast_without_temporal_parameters_hash,
        )

    def test_delta_period_only_change_fails(self) -> None:
        result = preflight_candidate_batch(
            candidate_batch=_batch(
                FactorNode(op="delta", args=(_field("price_close"),), period=5),
                FactorNode(op="delta", args=(_field("price_close"),), period=20),
                FactorNode(op="rolling_mean", args=(_field("volume"),), window=20),
            ),
            lint_batch=_approved_lint_batch(),
            hypothesis=_hypothesis(),
            registry=_registry(),
            created_at=NOW,
            provenance_by_slot=_provenance(),
            policy=POLICY,
        )

        self.assertFalse(result.design_result.passed)
        self.assertEqual(
            [item.axis_name for item in result.design_result.failures],
            ["ast_without_temporal_parameters_hash"],
        )

    def test_field_set_change_passes(self) -> None:
        result = preflight_designs(
            (
                FactorNode(op="delay", args=(_field("close"),), period=20),
                FactorNode(op="delay", args=(_field("volume"),), period=20),
                FactorNode(
                    op="div",
                    args=(_field("close"), FactorNode(op="delay", args=(_field("close"),), period=20)),
                ),
            ),
            policy=POLICY,
            registry=_registry(),
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            result.signatures[0].field_set,
            ("close",),
        )
        self.assertEqual(
            result.signatures[1].field_set,
            ("volume",),
        )

    def test_operator_topology_change_passes(self) -> None:
        result = preflight_designs(
            (
                FactorNode(op="delay", args=(_field("close"),), period=20),
                FactorNode(op="delta", args=(_field("close"),), period=20),
                FactorNode(op="rolling_mean", args=(_field("close"),), window=20),
            ),
            policy=POLICY,
            registry=_registry(),
        )

        self.assertTrue(result.passed)
        self.assertNotEqual(
            result.signatures[0].operator_topology,
            result.signatures[1].operator_topology,
        )

    def test_added_input_combination_passes(self) -> None:
        result = preflight_designs(
            (
                FactorNode(op="rolling_mean", args=(_field("close"),), window=20),
                FactorNode(op="div", args=(_field("close"), _field("volume"))),
                FactorNode(
                    op="mul",
                    args=(
                        FactorNode(op="rolling_mean", args=(_field("close"),), window=20),
                        FactorNode(op="delay", args=(_field("volume"),), period=5),
                    ),
                ),
            ),
            policy=POLICY,
            registry=_registry(),
        )

        self.assertTrue(result.passed)
        self.assertIn(("close", "volume"), result.signatures[2].input_combinations)

    def test_direct_preflight_designs_requires_real_registry(self) -> None:
        """验证公开入口缺少真实注册表时必须硬失败。"""

        with self.assertRaises(FactorMinerError) as caught:
            preflight_designs(
                (
                    FactorNode(op="delay", args=(_field("close"),), period=20),
                    FactorNode(op="delay", args=(_field("volume"),), period=20),
                    FactorNode(op="rolling_mean", args=(_field("close"),), window=20),
                ),
                policy=POLICY,
            )
        self.assertIs(caught.exception.code, FailureCode.FIELD_MISSING)

    def test_direct_preflight_designs_rejects_unknown_field(self) -> None:
        """验证公开入口不能用注册表之外的字段生成签名。"""

        with self.assertRaises(FactorMinerError) as caught:
            preflight_designs(
                (
                    FactorNode(op="delay", args=(_field("close"),), period=20),
                    FactorNode(op="delay", args=(_field("mystery"),), period=20),
                    FactorNode(op="rolling_mean", args=(_field("volume"),), window=20),
                ),
                policy=POLICY,
                registry=_registry(),
            )
        self.assertIs(caught.exception.code, FailureCode.FIELD_MISSING)

    def test_direct_preflight_designs_rejects_incompatible_units(self) -> None:
        """验证公开入口保留真实字段单位不相容的语义失败。"""

        with self.assertRaises(FactorMinerError) as caught:
            preflight_designs(
                (
                    FactorNode(
                        op="add",
                        args=(_field("close"), _field("volume")),
                    ),
                    FactorNode(op="delay", args=(_field("close"),), period=20),
                    FactorNode(op="rolling_mean", args=(_field("volume"),), window=20),
                ),
                policy=POLICY,
                registry=_registry(),
            )
        self.assertIs(caught.exception.code, FailureCode.DSL_TYPE_ERROR)

    def test_centered_rolling_is_rejected_before_signature(self) -> None:
        with self.assertRaises(FactorMinerError) as caught:
            preflight_candidate_batch(
                candidate_batch=_batch(
                    FactorNode(
                        op="rolling_mean",
                        args=(_field("price_close"),),
                        window=20,
                        center=True,
                    ),
                    FactorNode(op="delay", args=(_field("price_close"),), period=20),
                    FactorNode(op="delay", args=(_field("volume"),), period=20),
                ),
                lint_batch=_approved_lint_batch(),
                hypothesis=_hypothesis(),
                registry=_registry(),
                created_at=NOW,
                provenance_by_slot=_provenance(),
                policy=POLICY,
            )
        self.assertIs(caught.exception.code, FailureCode.LOOKAHEAD_DETECTED)

    def test_direct_preflight_designs_rejects_centered_rolling(self) -> None:
        with self.assertRaises(FactorMinerError) as caught:
            preflight_designs(
                (
                    FactorNode(
                        op="rolling_mean",
                        args=(_field("close"),),
                        window=20,
                        center=True,
                    ),
                    FactorNode(op="delay", args=(_field("close"),), period=20),
                    FactorNode(op="delay", args=(_field("volume"),), period=20),
                ),
                policy=POLICY,
                registry=_registry(),
            )
        self.assertIs(caught.exception.code, FailureCode.LOOKAHEAD_DETECTED)

    def test_unknown_field_is_rejected_before_signature(self) -> None:
        with self.assertRaises(FactorMinerError) as caught:
            preflight_candidate_batch(
                candidate_batch=_batch(
                    FactorNode(op="delay", args=(_field("price_close"),), period=20),
                    FactorNode(op="delay", args=(FactorNode(op="field", field="mystery"),), period=20),
                    FactorNode(op="delay", args=(_field("volume"),), period=20),
                ),
                lint_batch=_approved_lint_batch(),
                hypothesis=_hypothesis_with_extra_alias("mystery"),
                registry=_registry(),
                created_at=NOW,
                provenance_by_slot=_provenance(),
                policy=POLICY,
            )
        self.assertIs(caught.exception.code, FailureCode.FIELD_MISSING)

    def test_lookback_over_limit_is_rejected_before_signature(self) -> None:
        with self.assertRaises(FactorMinerError) as caught:
            preflight_candidate_batch(
                candidate_batch=_batch(
                    FactorNode(op="delay", args=(_field("price_close"),), period=20),
                    FactorNode(
                        op="rolling_mean",
                        args=(
                            FactorNode(op="delay", args=(_field("price_close"),), period=20),
                        ),
                        window=120,
                    ),
                    FactorNode(op="delay", args=(_field("volume"),), period=20),
                ),
                lint_batch=_approved_lint_batch(),
                hypothesis=_hypothesis(),
                registry=_registry(),
                created_at=NOW,
                provenance_by_slot=_provenance(),
                policy=POLICY,
            )
        self.assertIs(caught.exception.code, FailureCode.DSL_TYPE_ERROR)


if __name__ == "__main__":
    unittest.main()
