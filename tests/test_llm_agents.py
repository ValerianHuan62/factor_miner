"""V0.5 三个运行时研究角色的最小权限测试。"""

import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_agents import (
    HypothesisAgentOutput,
    build_expression_agent_request,
    build_format_repair_request,
    build_hypothesis_agent_request,
    build_hypothesis_tool_continuation,
    build_semantic_lint_agent_request,
)
from factor_miner.llm_literature import (
    LiteratureResult,
    LiteratureSearchRecord,
)
from factor_miner.llm_provider import ProviderToolCall, RecordedLLMResponse
from factor_miner.llm_provider import LLMCallRecord
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    EvolutionHypothesisApproval,
    build_evolution_context,
)
from datetime import datetime, timezone
from pathlib import Path


PUBLIC_PAYLOAD = {
    "information_class": "coverage_gap_bins",
    "gap_id": "G001",
    "summary": "价格延续覆盖较少",
}


def evolution_context_for_expression():
    """构造表达式闸门使用的纯合成上下文。"""

    return build_evolution_context(
        discovery_family_id="llmfamily_" + "9" * 24,
        coverage_graph_manifest_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        gap_report_hash="c" * 64,
        field_registry_hash="d" * 64,
        evaluation_policy_hash="e" * 64,
        design_policy_hash="f" * 64,
        external_model_redaction_policy="synthetic-redaction-v1",
    )


def approved_batch_for_expression(context):
    """构造十个已批准槽的最小合成批次。"""

    approvals = tuple(
        EvolutionHypothesisApproval(
            logical_slot_id=f"H{index:02d}",
            context_sha256=context.context_sha256,
            draft_sha256="a" * 64,
            discovery_family_id=context.discovery_family_id,
            approval_role="research_reviewer",
            approved_at=datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
            decision="approved",
        )
        for index in range(1, 11)
    )
    return ApprovedEvolutionHypothesisBatch(
        context_sha256=context.context_sha256,
        discovery_family_id=context.discovery_family_id,
        approvals=approvals,
    )


class LLMAgentsTest(unittest.TestCase):
    """代理 1 独占文献工具，代理 2/3 不能获得工具。"""

    def test_only_hypothesis_agent_has_literature_tool(self) -> None:
        hypothesis = build_hypothesis_agent_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("coverage_outcome_llm:H01",),
            public_payload=PUBLIC_PAYLOAD,
        )
        expression = build_expression_agent_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("coverage_outcome_llm:C001",),
            public_payload=PUBLIC_PAYLOAD,
        )
        lint = build_semantic_lint_agent_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("coverage_outcome_llm:C001",),
            public_payload=PUBLIC_PAYLOAD,
        )

        self.assertEqual(
            hypothesis.body["tools"][0]["function"]["name"],
            "search_literature",
        )
        self.assertNotIn("tools", expression.body)
        self.assertNotIn("tools", lint.body)
        self.assertEqual(hypothesis.agent_role, "hypothesis")
        self.assertEqual(expression.agent_role, "expression")
        self.assertEqual(lint.agent_role, "semantic_lint")

    def test_evolution_expression_requires_approved_batch(self) -> None:
        context = evolution_context_for_expression()
        with self.assertRaises(FactorMinerError) as missing:
            build_expression_agent_request(
                campaign_id="llmcampaign_synthetic_001",
                slot_ids=("coverage_outcome_llm:C001",),
                public_payload=PUBLIC_PAYLOAD,
                evolution_context=context,
            )
        self.assertIs(missing.exception.code, FailureCode.APPROVAL_COUNT_INVALID)
        self.assertEqual(missing.exception.message, "approval_required")

        batch = approved_batch_for_expression(context)
        request = build_expression_agent_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("coverage_outcome_llm:C001",),
            public_payload=PUBLIC_PAYLOAD,
            approval_batch=batch,
            evolution_context=context,
            discovery_family_id=context.discovery_family_id,
            approval_batch_sha256=batch.approval_batch_sha256,
        )
        self.assertEqual(request.agent_role, "expression")

        with self.assertRaises(FactorMinerError) as wrong_hash:
            build_expression_agent_request(
                campaign_id="llmcampaign_synthetic_001",
                slot_ids=("coverage_outcome_llm:C001",),
                public_payload=PUBLIC_PAYLOAD,
                approval_batch=batch,
                evolution_context=context,
                approval_batch_sha256="b" * 64,
            )
        self.assertIs(wrong_hash.exception.code, FailureCode.EVOLUTION_HASH_MISMATCH)

    def test_format_repair_request_has_no_tools_and_keeps_original_payload(self) -> None:
        request = build_format_repair_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("literature_only_llm:H01",),
            public_payload=PUBLIC_PAYLOAD,
            invalid_content={"hypotheses": []},
            validation_errors=("hypotheses 至少需要一项",),
        )

        self.assertEqual(request.agent_role, "format_repair")
        self.assertNotIn("tools", request.body)
        self.assertEqual(
            request.export_payload["invalid_content"],
            {"hypotheses": []},
        )
        self.assertEqual(
            request.export_payload["validation_errors"],
            ["hypotheses 至少需要一项"],
        )

    def test_hypothesis_output_rejects_extra_or_duplicate_slots(self) -> None:
        draft = {
            "slot_id": "coverage_outcome_llm:H01",
            "gap_id": "G001",
            "hypothesis_origin": "coverage_outcome_informed",
            "claim": "价格延续可能反映信息扩散。",
            "economic_mechanism": "信息进入价格存在时滞。",
            "independent_verification": "比较公开事件密度分组。",
            "competing_explanations": ["短期流动性冲击"],
            "failure_modes": ["高波动反转"],
            "falsification_path": "方向显著反向。",
            "source_record_ids": ["source-doi-1"],
            "prediction_proposal": {
                "observable_proxy": "过去二十日价格延续",
                "expected_sign": "positive",
                "proposed_field_aliases": ["price_close"],
                "proposed_operator_families": ["rolling"],
                "optional_conditioning_claim": None,
            },
        }
        parsed = HypothesisAgentOutput.model_validate(
            {"hypotheses": [draft]}
        )
        self.assertEqual(len(parsed.hypotheses), 1)

        with self.assertRaises(ValueError):
            HypothesisAgentOutput.model_validate(
                {"hypotheses": [draft, draft]}
            )
        with self.assertRaises(ValueError):
            HypothesisAgentOutput.model_validate(
                {"hypotheses": [draft], "evaluation_alpha": 0.5}
            )

    def test_tool_continuation_binds_sanitized_public_metadata(self) -> None:
        root = build_hypothesis_agent_request(
            campaign_id="llmcampaign_synthetic_001",
            slot_ids=("coverage_outcome_llm:H01",),
            public_payload=PUBLIC_PAYLOAD,
        )
        tool_call = ProviderToolCall(
            tool_call_id="call_crossref_1",
            name="search_literature",
            arguments={
                "query_terms": ["price", "continuation"],
                "year_start": 1990,
                "year_end": 2026,
                "result_limit": 3,
            },
        )
        response = RecordedLLMResponse(
            record=LLMCallRecord(
                call_id="llmcall_" + "1" * 24,
                campaign_id=root.campaign_id,
                agent_role="hypothesis",
                slot_ids=root.slot_ids,
                request_sha256=root.request_sha256,
                response_sha256="2" * 64,
                model="deepseek-v4-pro",
                system_fingerprint=None,
                finish_reason="tool_calls",
                usage={},
                completed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            ),
            content_json=None,
            tool_calls=(tool_call,),
            assistant_message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "synthetic reasoning",
                "tool_calls": [
                    {
                        "id": "call_crossref_1",
                        "type": "function",
                        "function": {
                            "name": "search_literature",
                            "arguments": (
                                '{"query_terms":["price","continuation"],'
                                '"year_start":1990,"year_end":2026,'
                                '"result_limit":3}'
                            ),
                        },
                    }
                ],
            },
            record_directory=Path("/tmp/synthetic-call"),
        )
        result = LiteratureResult(
            public_identifier="10.1/synthetic",
            title="Synthetic Price Continuation",
            authors=("A Researcher",),
            publication_year=2020,
            abstract_excerpt="Public metadata excerpt.",
            source_name="Synthetic Journal",
            canonical_url="https://doi.org/10.1/synthetic",
            metadata_sha256="3" * 64,
            verification_status="identity_verified",
        )
        literature = LiteratureSearchRecord(
            provider="crossref",
            normalized_query="price continuation",
            retrieved_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            results=(result,),
            response_sha256="4" * 64,
        )

        continuation = build_hypothesis_tool_continuation(
            root_request=root,
            response=response,
            literature_records=(literature,),
        )

        self.assertEqual(continuation.agent_role, "hypothesis")
        self.assertEqual(len(continuation.body["messages"]), 4)
        self.assertNotEqual(continuation.request_sha256, root.request_sha256)
        self.assertEqual(
            continuation.export_payload["information_class"],
            "public_literature_metadata",
        )


if __name__ == "__main__":
    unittest.main()
