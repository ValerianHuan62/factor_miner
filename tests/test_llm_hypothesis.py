"""V0.5 假设、预测合成与文献用途合同测试。"""

from datetime import date, datetime, timezone
import unittest

from pydantic import ValidationError

from factor_miner.llm_hypothesis import (
    ArmScopedApprovedHypothesis,
    CandidatePredictionOutcome,
    CitationSupportRecord,
    CoverageGapHypothesisDraft,
    HypothesisDecision,
    HypothesisDiscoverySummary,
    HypothesisLiteratureStatus,
    PredictionProposal,
    adapt_approved_evolution_hypotheses_for_arm,
    compose_testable_prediction,
    registered_coverage_gap_hypothesis,
    summarize_hypothesis_candidates,
    validate_novel_unverified_quota,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapCard,
    EvolutionHypothesisApproval,
)
from factor_miner.schema import evaluation_policy_id


def proposal() -> PredictionProposal:
    """构造只包含模型可建议字段的预测提案。"""

    return PredictionProposal(
        observable_proxy="过去二十日价格延续",
        expected_sign="positive",
        proposed_field_aliases=("price_close",),
        proposed_operator_families=("rolling",),
        optional_conditioning_claim=None,
    )


def citation(**updates: object) -> CitationSupportRecord:
    """构造完成反证检索记录的合成引用。"""

    payload: dict[str, object] = {
        "source_record_id": "source-doi-1",
        "identity_status": "claim_support_verified",
        "passage_locator": "第 3 节",
        "minimal_excerpt": "价格延续与后续收益存在关联。",
        "support_kind": "proxy_choice_supported",
        "supported_claim_fragment": "价格延续可作为代理",
        "applicable_market": "公开股票市场",
        "applicable_sample": "日频股票",
        "counterevidence_search_performed": True,
        "counterevidence_queries": ("price continuation reversal",),
        "counterevidence_source_ids": ("source-doi-2",),
        "counterevidence_cutoff": date(2026, 7, 30),
        "counterevidence_summary": "存在短期反转研究。",
        "counterevidence_limitations": "仅检索批准的元数据来源。",
        "reviewer_role": "research_reviewer",
        "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
    }
    payload.update(updates)
    return CitationSupportRecord.model_validate(payload)


class LLMHypothesisTest(unittest.TestCase):
    """模型建议与固定评价规则必须严格分层。"""

    def test_prediction_proposal_rejects_locked_evaluation_fields(self) -> None:
        for field, value in (
            ("alpha", 0.5),
            ("universe_id", "changed"),
            ("horizon_sessions", 20),
            ("multiplicity_family_id", "changed"),
        ):
            with self.subTest(field=field):
                payload = proposal().model_dump()
                payload[field] = value
                with self.assertRaises(ValidationError):
                    PredictionProposal.model_validate(payload)

    def test_fixed_program_composes_evaluation_policy_fields(self) -> None:
        policy = company_a_share_visible_policy()
        prediction = compose_testable_prediction(
            proposal(),
            policy,
            discovery_family_id="llmfamily_" + "1" * 24,
        )

        self.assertEqual(prediction.evaluation_policy_id, evaluation_policy_id(policy))
        self.assertEqual(prediction.universe_id, policy.universe.universe_id)
        self.assertEqual(prediction.horizon_sessions, 5)
        self.assertEqual(prediction.alpha, 0.05)
        self.assertEqual(
            prediction.multiplicity_family_id,
            "llmfamily_" + "1" * 24,
        )
        self.assertEqual(prediction.expected_sign, "positive")

    def test_counterevidence_search_requires_query_cutoff_and_limitations(self) -> None:
        for field, value in (
            ("counterevidence_queries", ()),
            ("counterevidence_limitations", ""),
            ("counterevidence_cutoff", None),
        ):
            with self.subTest(field=field):
                payload = citation().model_dump()
                payload[field] = value
                with self.assertRaises(ValidationError):
                    CitationSupportRecord.model_validate(payload)

    def test_novel_unverified_quota_is_two_per_ten_slots(self) -> None:
        statuses = (
            HypothesisLiteratureStatus.NOVEL_UNVERIFIED,
            HypothesisLiteratureStatus.NOVEL_UNVERIFIED,
        ) + (HypothesisLiteratureStatus.PROXY_CHOICE_SUPPORTED,) * 8
        validate_novel_unverified_quota(statuses)

        with self.assertRaisesRegex(ValueError, "最多 2"):
            validate_novel_unverified_quota(
                statuses[:-1]
                + (HypothesisLiteratureStatus.NOVEL_UNVERIFIED,)
            )

    def test_strict_candidate_outcomes_have_deterministic_hypothesis_summary(
        self,
    ) -> None:
        cases = (
            ((), HypothesisDiscoverySummary.NO_VALID_CANDIDATE),
            (
                (CandidatePredictionOutcome.INCONCLUSIVE,),
                HypothesisDiscoverySummary.ALL_CANDIDATES_INCONCLUSIVE,
            ),
            (
                (CandidatePredictionOutcome.REVERSE,),
                HypothesisDiscoverySummary.ALL_VALID_CANDIDATES_REVERSE,
            ),
            (
                (
                    CandidatePredictionOutcome.SUPPORTED,
                    CandidatePredictionOutcome.REVERSE,
                ),
                HypothesisDiscoverySummary.MIXED_CANDIDATE_EVIDENCE,
            ),
            (
                (
                    CandidatePredictionOutcome.SUPPORTED,
                    CandidatePredictionOutcome.INCONCLUSIVE,
                ),
                HypothesisDiscoverySummary.HAS_SUPPORTED_CANDIDATE,
            ),
        )
        for outcomes, expected in cases:
            with self.subTest(outcomes=outcomes):
                self.assertEqual(
                    summarize_hypothesis_candidates(outcomes),
                    expected,
                )

    def test_exploratory_filter_cannot_be_summarized_as_support(self) -> None:
        with self.assertRaisesRegex(ValueError, "探索性"):
            summarize_hypothesis_candidates(
                (CandidatePredictionOutcome.PASSES_EXPLORATORY_FILTER,)
            )

    def test_approved_hypothesis_is_content_addressed_and_mechanism_unverified(
        self,
    ) -> None:
        draft = CoverageGapHypothesisDraft(
            slot_id="coverage_outcome_llm:H01",
            gap_id="G001",
            hypothesis_origin="coverage_outcome_informed",
            claim="价格延续可能反映信息扩散。",
            economic_mechanism="信息进入价格存在时滞。",
            independent_verification="比较公告密度分组。",
            competing_explanations=("短期流动性冲击",),
            failure_modes=("高波动反转",),
            falsification_path="方向显著反向。",
            source_record_ids=("source-doi-1",),
            prediction_proposal=proposal(),
        )
        prediction = compose_testable_prediction(
            proposal(),
            company_a_share_visible_policy(),
            discovery_family_id="llmfamily_" + "1" * 24,
        )
        decision = HypothesisDecision(
            draft_id="draft_" + "2" * 24,
            decision="approved",
            reason="代理和证伪路径明确。",
            verified_source_record_ids=("source-doi-1",),
            literature_status="proxy_choice_supported",
            reviewer_role="research_reviewer",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        first = registered_coverage_gap_hypothesis(draft, prediction, decision)
        second = registered_coverage_gap_hypothesis(draft, prediction, decision)

        self.assertEqual(first, second)
        self.assertRegex(first.hypothesis_id, r"^llmhyp_[0-9a-f]{24}$")
        self.assertEqual(first.mechanism_status, "mechanism_unverified")

    def test_rejected_draft_cannot_be_registered(self) -> None:
        decision = HypothesisDecision(
            draft_id="draft_" + "2" * 24,
            decision="rejected",
            reason="代理不足。",
            verified_source_record_ids=("source-doi-1",),
            literature_status="background_only",
            reviewer_role="research_reviewer",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        draft = CoverageGapHypothesisDraft(
            slot_id="coverage_outcome_llm:H01",
            gap_id="G001",
            hypothesis_origin="coverage_outcome_informed",
            claim="待拒绝主张。",
            economic_mechanism="机制不充分。",
            independent_verification="独立分组。",
            competing_explanations=("竞争解释",),
            failure_modes=("失效方式",),
            falsification_path="方向反向。",
            source_record_ids=("source-doi-1",),
            prediction_proposal=proposal(),
        )

        with self.assertRaisesRegex(ValueError, "批准"):
            registered_coverage_gap_hypothesis(
                draft,
                compose_testable_prediction(
                    proposal(),
                    company_a_share_visible_policy(),
                    discovery_family_id="llmfamily_" + "1" * 24,
                ),
                decision,
            )

    def test_two_llm_arms_share_same_logical_batch_and_hypothesis_ids(self) -> None:
        logical_draft = LogicalEvolutionHypothesisDraft(
            logical_slot_id="H01",
            mechanism_unverified=True,
            prior_claim="价格延续可能反映信息扩散。",
            mechanism="公开信息进入价格存在时滞。",
            expected_direction="positive",
            observable_proxy="过去二十日价格延续",
            independent_verification="比较公告密度分组。",
            competing_explanations=("短期流动性冲击",),
            failure_modes=("高波动反转",),
            falsification_path="方向显著反向。",
            gap_ids=("G001",),
            source_records=(
                {
                    "source_record_id": "src-01",
                    "claim_fragment": "价格延续与扩散相关",
                    "rationale": "验证信息扩散线索",
                    "query_terms": ("price", "continuation"),
                    "year_start": 1990,
                    "year_end": 2026,
                    "result_limit": 3,
                },
            ),
        )
        approvals = []
        for index in range(1, 11):
            slot = f"H{index:02d}"
            draft = (
                logical_draft
                if slot == "H01"
                else LogicalEvolutionHypothesisDraft(
                    **{
                        **logical_draft.model_dump(
                            mode="json",
                            exclude={"draft_sha256"},
                        ),
                        "logical_slot_id": slot,
                        "prior_claim": f"主张 {index}",
                        "mechanism": f"机制 {index}",
                        "observable_proxy": f"代理 {index}",
                        "independent_verification": f"独立验证 {index}",
                        "competing_explanations": (f"竞争解释 {index}",),
                        "failure_modes": (f"失效方式 {index}",),
                        "falsification_path": f"证伪路径 {index}",
                        "gap_ids": (f"G{index:03d}",),
                        "source_records": (
                            {
                                "source_record_id": f"src-{index:02d}",
                                "claim_fragment": f"来源片段 {index}",
                                "rationale": f"检索理由 {index}",
                                "query_terms": ("price", f"signal{index}"),
                                "year_start": 1990,
                                "year_end": 2026,
                                "result_limit": 3,
                            },
                        ),
                    }
                )
            )
            approvals.append(
                (
                    draft,
                    EvolutionHypothesisApproval(
                        logical_slot_id=slot,
                        context_sha256="a" * 64,
                        draft_sha256=draft.draft_sha256,
                        discovery_family_id="llmfamily_" + "1" * 24,
                        approval_role="research_reviewer",
                        approved_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
                        decision="approved",
                    ),
                )
            )
        drafts = tuple(item[0] for item in approvals)
        batch = ApprovedEvolutionHypothesisBatch(
            context_sha256="a" * 64,
            discovery_family_id="llmfamily_" + "1" * 24,
            approvals=tuple(item[1] for item in approvals),
        )
        gap_cards_by_id = {
            f"G{index:03d}": CoverageGapCard(
                gap_id=f"G{index:03d}",
                gap_category="structural",
                sanitized_labels=("coverage",),
                allowed_field_aliases=("close",),
                allowed_operator_families=("rolling",),
                temporal_window_bins=("short",),
                structure_cluster_count_band="low",
                signal_cluster_count_band="none",
                missing_or_failure_risk=("missing",),
            )
            for index in range(1, 11)
        }

        coverage_arm = adapt_approved_evolution_hypotheses_for_arm(
            approval_batch=batch,
            drafts=drafts,
            gap_cards_by_id=gap_cards_by_id,
            arm_id="coverage_outcome_llm",
            generation_route="coverage_outcome_informed",
        )
        literature_arm = adapt_approved_evolution_hypotheses_for_arm(
            approval_batch=batch,
            drafts=drafts,
            gap_cards_by_id=gap_cards_by_id,
            arm_id="literature_only_llm",
            generation_route="literature_only_informed",
        )

        self.assertEqual(len(coverage_arm), 10)
        self.assertEqual(len(literature_arm), 10)
        self.assertIsInstance(coverage_arm[0], ArmScopedApprovedHypothesis)
        self.assertEqual(
            coverage_arm[0].logical_hypothesis_id,
            literature_arm[0].logical_hypothesis_id,
        )
        self.assertEqual(
            coverage_arm[0].approval_batch_sha256,
            literature_arm[0].approval_batch_sha256,
        )
        self.assertEqual(
            coverage_arm[0].context_sha256,
            literature_arm[0].context_sha256,
        )
        self.assertEqual(coverage_arm[0].draft.slot_id, "coverage_outcome_llm:H01")
        self.assertEqual(literature_arm[0].draft.slot_id, "literature_only_llm:H01")


if __name__ == "__main__":
    unittest.main()
