"""V0.5 DeepSeek 固定请求和精确哈希授权测试。"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest

from factor_miner.canonical import canonical_json_bytes
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_online import (
    AgentRole,
    DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    DEEPSEEK_MODEL,
    LLMCampaignScopeAuthorization,
    LLMEvolutionRequestAuthorization,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    build_deepseek_request,
    registered_llm_evolution_request_authorization,
    registered_llm_campaign_scope_authorization,
    verify_evolution_export_authorization,
    verify_export_authorization,
    verify_scope_export_authorization,
)
from factor_miner.llm_privacy import CorporateExternalResearchPolicy
from factor_miner.llm_privacy import (
    registered_corporate_external_research_policy,
)


NOW = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)


def policy() -> CorporateExternalResearchPolicy:
    """构造只允许合成覆盖空白信息的测试政策。"""

    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=("coverage_gap_bins",),
        forbidden_information_classes=("raw_market_data",),
        allowed_models=(DEEPSEEK_MODEL,),
        maximum_authorized_campaigns=1,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        approver_role="research_owner",
        approval_reference="synthetic-policy-test-reference",
    )


def evolution_policy() -> CorporateExternalResearchPolicy:
    """构造只允许演化 brief 的测试政策。"""

    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=("evolution_gap_brief",),
        forbidden_information_classes=("raw_market_data",),
        allowed_models=(DEEPSEEK_MODEL,),
        maximum_authorized_campaigns=1,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        approver_role="research_owner",
        approval_reference="evolution-scope-test",
    )


def prepared_request():
    """构造固定的代理 1 测试请求。"""

    return build_deepseek_request(
        campaign_id="llmcampaign_synthetic_001",
        agent_role=AgentRole.HYPOTHESIS,
        slot_ids=("coverage_outcome_llm:H01",),
        system_prompt="请只输出 JSON object。",
        user_payload={
            "information_class": "coverage_gap_bins",
            "gap": "成交与价格交互覆盖较少",
        },
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "search_literature",
                    "description": "检索公开文献元数据",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
        ),
    )


def authorization(request):
    """为精确请求哈希构造测试授权。"""

    return LLMExportAuthorization(
        authorization_id="authorization-synthetic-001",
        campaign_id=request.campaign_id,
        request_sha256=request.request_sha256,
        corporate_policy_id=policy().policy_id,
        approver_role="research_owner",
        authorized_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


class LLMOnlineTest(unittest.TestCase):
    """在线请求不得允许模型、端点、预算或导出字节漂移。"""

    def test_request_freezes_official_endpoint_model_and_json_mode(self) -> None:
        request = prepared_request()

        self.assertEqual(
            request.endpoint,
            DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        )
        self.assertEqual(request.body["model"], DEEPSEEK_MODEL)
        self.assertEqual(request.body["thinking"], {"type": "enabled"})
        self.assertEqual(request.body["reasoning_effort"], "high")
        self.assertFalse(request.body["stream"])
        self.assertEqual(
            request.body["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(request.body["max_tokens"], 8000)
        self.assertNotIn("temperature", request.body)
        self.assertNotIn("top_p", request.body)
        self.assertNotIn("api_key", type(request).model_fields)
        self.assertNotIn("base_url", type(request).model_fields)

    def test_expression_and_lint_agents_cannot_receive_tools(self) -> None:
        for role in (AgentRole.EXPRESSION, AgentRole.SEMANTIC_LINT):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    build_deepseek_request(
                        campaign_id="llmcampaign_synthetic_001",
                        agent_role=role,
                        slot_ids=("coverage_outcome_llm:C001",),
                        system_prompt="请输出 JSON object。",
                        user_payload={
                            "information_class": "coverage_gap_bins",
                            "gap": "合成空白",
                        },
                        tools=prepared_request().body["tools"],
                    )

    def test_expression_role_has_batch_sized_output_budget(self) -> None:
        request = build_deepseek_request(
            campaign_id="llmcampaign_synthetic_expression",
            agent_role=AgentRole.EXPRESSION,
            slot_ids=("H01:C001",),
            system_prompt="只输出 JSON object。",
            user_payload={"information_class": "public_capability_only"},
            thinking="disabled",
        )
        self.assertEqual(request.body["max_tokens"], 12000)

    def test_authorization_binds_exact_request_and_expiry(self) -> None:
        request = prepared_request()
        approved = authorization(request)

        request_bytes = verify_export_authorization(
            request,
            policy(),
            approved,
            NOW + timedelta(minutes=1),
        )
        self.assertGreater(len(request_bytes), 100)

        changed = build_deepseek_request(
            campaign_id=request.campaign_id,
            agent_role=AgentRole.HYPOTHESIS,
            slot_ids=request.slot_ids,
            system_prompt="请只输出 JSON object。",
            user_payload={
                "information_class": "coverage_gap_bins",
                "gap": "成交与价格交互覆盖较少，且要求稳健",
            },
            tools=request.body["tools"],
        )
        with self.assertRaises(FactorMinerError) as changed_context:
            verify_export_authorization(changed, policy(), approved, NOW)
        self.assertEqual(
            changed_context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )

        with self.assertRaises(FactorMinerError) as expired_context:
            verify_export_authorization(
                request,
                policy(),
                approved,
                NOW + timedelta(hours=2),
            )
        self.assertEqual(
            expired_context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )

    def test_expired_corporate_policy_cannot_be_replaced_by_fresh_click(self) -> None:
        request = prepared_request()
        expired_policy = registered_corporate_external_research_policy(
            provider="deepseek",
            allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
            allowed_information_classes=("coverage_gap_bins",),
            forbidden_information_classes=("raw_market_data",),
            allowed_models=(DEEPSEEK_MODEL,),
            maximum_authorized_campaigns=1,
            valid_from=NOW - timedelta(days=2),
            valid_until=NOW - timedelta(days=1),
            approver_role="research_owner",
            approval_reference="expired-synthetic-policy",
        )
        clicked = LLMExportAuthorization(
            authorization_id="authorization-fresh-click",
            campaign_id=request.campaign_id,
            request_sha256=request.request_sha256,
            corporate_policy_id=expired_policy.policy_id,
            approver_role="research_owner",
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )

        with self.assertRaises(FactorMinerError) as context:
            verify_export_authorization(
                request,
                expired_policy,
                clicked,
                NOW,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )

    def test_evolution_authorization_binds_context_gap_memory_and_brief_hashes(
        self,
    ) -> None:
        family_id = "llmfamily_" + "2" * 24
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=family_id,
            context_sha256="a" * 64,
            gap_report_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            brief_sha256="d" * 64,
            corporate_policy_id=evolution_policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        request = build_deepseek_request(
            campaign_id=f"{family_id}:evolution_hypothesis",
            agent_role=AgentRole.HYPOTHESIS,
            slot_ids=tuple(f"H{index:02d}" for index in range(1, 11)),
            system_prompt="请只输出 JSON object。",
            user_payload={
                "information_class": "evolution_gap_brief",
                "evolution_binding": {
                    "discovery_family_id": family_id,
                    "context_sha256": "a" * 64,
                    "gap_report_hash": "b" * 64,
                    "memory_snapshot_hash": "c" * 64,
                    "brief_sha256": "d" * 64,
                    "authorization_scope_sha256": authorization.authorization_sha256,
                },
                "model_scope": {
                    "agent_role": "hypothesis",
                    "tools_allowed": False,
                },
            },
        )

        request_bytes = verify_evolution_export_authorization(
            request,
            evolution_policy(),
            authorization,
            NOW,
        )
        self.assertGreater(len(request_bytes), 100)
        self.assertIsInstance(authorization, LLMEvolutionRequestAuthorization)

        changed = build_deepseek_request(
            campaign_id=f"{family_id}:evolution_hypothesis",
            agent_role=AgentRole.HYPOTHESIS,
            slot_ids=request.slot_ids,
            system_prompt="请只输出 JSON object。",
            user_payload={
                "information_class": "evolution_gap_brief",
                "evolution_binding": {
                    "discovery_family_id": family_id,
                    "context_sha256": "0" * 64,
                    "gap_report_hash": "b" * 64,
                    "memory_snapshot_hash": "c" * 64,
                    "brief_sha256": "d" * 64,
                    "authorization_scope_sha256": authorization.authorization_sha256,
                },
                "model_scope": {
                    "agent_role": "hypothesis",
                    "tools_allowed": False,
                },
            },
        )
        with self.assertRaises(FactorMinerError) as context:
            verify_evolution_export_authorization(
                changed,
                evolution_policy(),
                authorization,
                NOW,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )

    def test_evolution_rejects_replaced_public_content_with_self_consistent_audit(
        self,
    ) -> None:
        family_id = "llmfamily_" + "3" * 24
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=family_id,
            context_sha256="a" * 64,
            gap_report_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            brief_sha256="d" * 64,
            corporate_policy_id=evolution_policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        public_payload = {
            "information_class": "evolution_gap_brief",
            "gap_brief": {"summary": "原始公开简报"},
        }
        audit_payload = {
            "information_class": "evolution_gap_brief",
            "public_payload_sha256": sha256(
                canonical_json_bytes(public_payload)
            ).hexdigest(),
            "evolution_binding": {
                "discovery_family_id": family_id,
                "context_sha256": "a" * 64,
                "gap_report_hash": "b" * 64,
                "memory_snapshot_hash": "c" * 64,
                "brief_sha256": "d" * 64,
                "authorization_scope_sha256": authorization.authorization_sha256,
            },
            "model_scope": {
                "agent_role": "hypothesis",
                "tools_allowed": False,
            },
        }
        request = build_deepseek_request(
            campaign_id=f"{family_id}:evolution_hypothesis",
            agent_role=AgentRole.HYPOTHESIS,
            slot_ids=tuple(f"H{index:02d}" for index in range(1, 11)),
            system_prompt="请只输出 JSON object。",
            user_payload=public_payload,
            audit_payload=audit_payload,
        )

        forged_body = dict(request.body)
        forged_messages = list(request.body["messages"])
        forged_user_message = dict(forged_messages[1])
        forged_user_message["content"] = canonical_json_bytes(
            {
                "information_class": "evolution_gap_brief",
                "gap_brief": {"summary": "被替换的公开简报"},
            }
        ).decode("utf-8")
        forged_messages[1] = forged_user_message
        forged_body["messages"] = forged_messages
        forged_request_sha256 = sha256(
            canonical_json_bytes(forged_body)
        ).hexdigest()

        with self.assertRaises(ValueError):
            PreparedDeepSeekRequest(
                campaign_id=request.campaign_id,
                agent_role=request.agent_role,
                slot_ids=request.slot_ids,
                endpoint=request.endpoint,
                body=forged_body,
                export_payload=request.export_payload,
                audit_payload_sha256=request.audit_payload_sha256,
                request_sha256=forged_request_sha256,
            )

        forged_request = PreparedDeepSeekRequest.model_construct(
            campaign_id=request.campaign_id,
            agent_role=request.agent_role,
            slot_ids=request.slot_ids,
            endpoint=request.endpoint,
            body=forged_body,
            export_payload=request.export_payload,
            audit_payload_sha256=request.audit_payload_sha256,
            request_sha256=forged_request_sha256,
        )
        with self.assertRaises(FactorMinerError) as context:
            verify_evolution_export_authorization(
                forged_request,
                evolution_policy(),
                authorization,
                NOW,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )

    def test_personal_scope_authorizes_bounded_batch_without_exact_hash(self) -> None:
        family_id = "llmfamily_0123456789abcdef01234567"
        request = build_deepseek_request(
            campaign_id=f"{family_id}:coverage_outcome_llm",
            agent_role=AgentRole.EXPRESSION,
            slot_ids=("coverage_outcome_llm:C001",),
            system_prompt="请输出 JSON object。",
            user_payload={
                "information_class": "coverage_gap_bins",
                "gap": "新的合成覆盖空白",
            },
        )
        scope = registered_llm_campaign_scope_authorization(
            family_id=family_id,
            corporate_policy_id=policy().policy_id,
            approver_role="research_owner",
            allowed_agent_roles=(AgentRole.EXPRESSION,),
            allowed_information_classes=("coverage_gap_bins",),
            max_hypotheses=10,
            max_requests=2,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )

        request_bytes = verify_scope_export_authorization(
            request,
            policy(),
            scope,
            NOW,
            request_count=2,
        )
        self.assertGreater(len(request_bytes), 100)
        self.assertIsInstance(scope, LLMCampaignScopeAuthorization)

        with self.assertRaises(FactorMinerError) as context:
            verify_scope_export_authorization(
                request,
                policy(),
                scope,
                NOW,
                request_count=3,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_EXPORT_NOT_AUTHORIZED,
        )


if __name__ == "__main__":
    unittest.main()
