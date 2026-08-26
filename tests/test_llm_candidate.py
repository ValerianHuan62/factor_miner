"""V0.5 LLM AST 到可信候选的确定性转换测试。"""

from datetime import datetime, timezone
import unittest

from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.llm_candidate import (
    CandidateExpressionDraft,
    SemanticLintDecision,
    convert_candidate,
)
from factor_miner.llm_hypothesis import (
    CoverageGapHypothesisDraft,
    HypothesisDecision,
    PredictionProposal,
    compose_testable_prediction,
    registered_coverage_gap_hypothesis,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.schema import FactorNode, registered_trusted_candidate


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def price_registry() -> FieldAvailabilityRegistry:
    """构造一个可在收盘后使用的价格字段。"""

    return FieldAvailabilityRegistry(
        registry_id="field-registry-candidate-v1",
        data_release_id="release-candidate-v1",
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
        ),
    )


def hypothesis():
    """构造批准的价格延续假设。"""

    proposal = PredictionProposal(
        observable_proxy="过去二十日价格延续",
        expected_sign="positive",
        proposed_field_aliases=("price_close",),
        proposed_operator_families=("arithmetic", "temporal"),
        optional_conditioning_claim=None,
    )
    draft = CoverageGapHypothesisDraft(
        slot_id="coverage_outcome_llm:H01",
        gap_id="G001",
        hypothesis_origin="coverage_outcome_informed",
        claim="价格延续可能反映信息扩散。",
        economic_mechanism="信息进入价格存在时滞。",
        independent_verification="按公开事件密度做独立分组。",
        competing_explanations=("短期流动性冲击",),
        failure_modes=("高波动反转",),
        falsification_path="方向显著反向。",
        source_record_ids=("source-doi-1",),
        prediction_proposal=proposal,
    )
    decision = HypothesisDecision(
        draft_id="draft_" + "2" * 24,
        decision="approved",
        reason="代理、竞争解释和证伪路径明确。",
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


class LLMCandidateTest(unittest.TestCase):
    """候选字段、lookback、availability 和身份只能由程序推导。"""

    def test_valid_public_ast_becomes_content_addressed_trusted_candidate(self) -> None:
        price = FactorNode(op="field", field="price_close")
        draft = CandidateExpressionDraft(
            candidate_slot_id="coverage_outcome_llm:C001",
            expression=FactorNode(
                op="div",
                args=(
                    FactorNode(op="delta", args=(price,), period=20),
                    FactorNode(op="delay", args=(price,), period=20),
                ),
            ),
        )
        lint = SemanticLintDecision(
            candidate_slot_id=draft.candidate_slot_id,
            decision="approved",
            proxy_alignment=True,
            direction_alignment=True,
            availability_alignment=True,
            undeclared_exposure=False,
            reason_codes=(),
            summary="与冻结假设一致。",
        )

        candidate = convert_candidate(
            draft=draft,
            hypothesis=hypothesis(),
            lint=lint,
            registry=price_registry(),
            created_at=NOW,
            provenance={
                "discovery_family_id": "llmfamily_" + "1" * 24,
                "candidate_slot_id": draft.candidate_slot_id,
                "generator_call_id": "llmcall_" + "3" * 24,
                "lint_call_id": "llmcall_" + "4" * 24,
            },
        )
        registered = registered_trusted_candidate(candidate)

        self.assertEqual(candidate.required_fields, ("close",))
        self.assertEqual(candidate.max_lookback, 20)
        self.assertEqual(candidate.availability.earliest_trade, "open_t_plus_1")
        self.assertRegex(registered.candidate_id, r"^cand_[0-9a-f]{24}$")

    def test_lint_rejection_cannot_be_overridden(self) -> None:
        draft = CandidateExpressionDraft(
            candidate_slot_id="coverage_outcome_llm:C001",
            expression=FactorNode(op="field", field="price_close"),
        )
        lint = SemanticLintDecision(
            candidate_slot_id=draft.candidate_slot_id,
            decision="rejected",
            proxy_alignment=False,
            direction_alignment=True,
            availability_alignment=True,
            undeclared_exposure=False,
            reason_codes=("proxy_mismatch",),
            summary="代理不一致。",
        )
        with self.assertRaisesRegex(ValueError, "批准"):
            convert_candidate(
                draft=draft,
                hypothesis=hypothesis(),
                lint=lint,
                registry=price_registry(),
                created_at=NOW,
                provenance={"candidate_slot_id": draft.candidate_slot_id},
            )


if __name__ == "__main__":
    unittest.main()
