"""Task 5 逻辑演化假设请求、解析与批准测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_online import (
    AgentRole,
    registered_llm_evolution_request_authorization,
    verify_evolution_export_authorization,
)
from factor_miner.llm_privacy import (
    CorporateExternalResearchPolicy,
    registered_corporate_external_research_policy,
)
from factor_miner.research_evolution import (
    approve_evolution_hypotheses,
    build_evolution_hypothesis_request,
    evolution_gap_brief_sha256,
    parse_evolution_hypothesis_response,
)
from factor_miner.research_evolution_schema import (
    EvolutionHypothesisApproval,
    build_evolution_context,
)
from factor_miner.semantic_coverage import build_semantic_coverage, build_semantic_quota


NOW = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)
SAFE_FAMILY = "llmfamily_" + "1" * 24


def context():
    """构造一个合法的演化上下文。"""

    return build_evolution_context(
        discovery_family_id=SAFE_FAMILY,
        coverage_graph_manifest_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        gap_report_hash="c" * 64,
        field_registry_hash="d" * 64,
        evaluation_policy_hash="e" * 64,
        design_policy_hash="f" * 64,
        external_model_redaction_policy="evolution-gap-brief-v1",
    )


def policy() -> CorporateExternalResearchPolicy:
    """构造只允许演化 brief 的公司外发政策。"""

    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint="https://api.deepseek.com/chat/completions",
        allowed_information_classes=("evolution_gap_brief",),
        forbidden_information_classes=("raw_market_data",),
        allowed_models=("deepseek-v4-pro",),
        maximum_authorized_campaigns=1,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        approver_role="research_owner",
        approval_reference="task-5-evolution-policy",
    )


def gap_brief() -> dict[str, object]:
    """构造带额外脏字段的合成 gap brief。"""

    return {
        "gap_cards": [
            {
                "gap_id": "G001",
                "gap_category": "structural",
                "sanitized_labels": ("coverage", "structural"),
                "allowed_field_aliases": ("close", "price_close"),
                "allowed_operator_families": ("ranking", "rolling"),
                "temporal_window_bins": ("long", "short"),
                "structure_cluster_count_band": "2_4",
                "signal_cluster_count_band": "none",
                "missing_or_failure_risk": ("missing",),
                "historical_rank_ic": [0.12, 0.05],
                "server_path": "/data/quantlake/private",
            },
            {
                "gap_id": "G002",
                "gap_category": "market_regime",
                "sanitized_labels": ("coverage", "market_regime"),
                "allowed_field_aliases": ("returns",),
                "allowed_operator_families": ("interaction",),
                "temporal_window_bins": ("medium",),
                "structure_cluster_count_band": "1",
                "signal_cluster_count_band": "1",
                "missing_or_failure_risk": ("stale",),
            },
        ],
        "raw_market_data_preview": {"rank_ic": 0.1234},
    }


def gap_brief_with_memory() -> dict[str, object]:
    brief = gap_brief()
    brief["memory_feedback"] = {
        "candidate_history_band": "high",
        "evaluated_share_band": "low",
        "ic_quality_band": "low",
        "rank_ic_quality_band": "medium",
        "portfolio_quality_band": "low",
        "direction_reversal_band": "high",
        "dominant_failure_patterns": (
            "invalid_complexity",
            "unit_mismatch",
        ),
        "generation_guidance": (
            "avoid_previous_ast_duplicates",
            "prioritize_positive_excess_information_ratio",
            "respect_unit_compatibility",
        ),
    }
    return brief


def response_payload() -> dict[str, object]:
    """构造正好十个逻辑槽的合成响应。"""

    hypotheses = []
    for index in range(1, 11):
        hypotheses.append(
            {
                "logical_slot_id": f"H{index:02d}",
                "mechanism_unverified": True,
                "prior_claim": f"主张 {index}",
                "mechanism": f"机制 {index}",
                "expected_direction": "positive"
                if index % 2
                else "negative",
                "observable_proxy": f"代理 {index}",
                "independent_verification": f"独立验证 {index}",
                "competing_explanations": [f"竞争解释 {index}"],
                "failure_modes": [f"失效方式 {index}"],
                "falsification_path": f"证伪路径 {index}",
                "gap_ids": [f"G{index:03d}"],
                "source_records": [
                    {
                        "source_record_id": f"src-{index:02d}",
                        "claim_fragment": f"来源片段 {index}",
                        "rationale": f"检索理由 {index}",
                        "query_terms": ["price", f"signal{index}"],
                        "year_start": 2000,
                        "year_end": 2026,
                        "result_limit": 3,
                    }
                ],
            }
        )
    return {"hypotheses": hypotheses}


def response_payload_with_semantic_tags() -> dict[str, object]:
    """构造带完整但非必填 E/C/Q/D/O 标签的合成响应。"""

    payload = response_payload()
    for index, hypothesis in enumerate(payload["hypotheses"], start=1):
        hypothesis["semantic_plan"] = {
            "event_tag": "breakout" if index % 2 else "volume_change",
            "context_tag": "recent_extreme" if index % 2 else "volume_regime",
            "quality_tags": ["volume_confirmation"],
            "direction_tag": "continuation" if index % 2 else "reversal",
            "output_tag": "continuous_score",
        }
    return payload


def approvals_for(drafts, *, role="research_reviewer"):
    """为十个草案构造同一审批角色的批准记录。"""

    return tuple(
        EvolutionHypothesisApproval(
            logical_slot_id=item.logical_slot_id,
            context_sha256=context().context_sha256,
            draft_sha256=item.draft_sha256,
            discovery_family_id=context().discovery_family_id,
            approval_role=role,
            approved_at=NOW,
            decision="approved",
        )
        for item in drafts
    )


def approval_authorization(ctx, *, role="research_reviewer"):
    """构造与审批角色和上下文绑定的演化授权。"""

    return registered_llm_evolution_request_authorization(
        discovery_family_id=ctx.discovery_family_id,
        context_sha256=ctx.context_sha256,
        gap_report_hash=ctx.gap_report_hash,
        memory_snapshot_hash=ctx.memory_snapshot_hash,
        brief_sha256="0" * 64,
        corporate_policy_id=policy().policy_id,
        approver_role=role,
        allowed_agent_role=AgentRole.HYPOTHESIS,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


class ResearchEvolutionTest(unittest.TestCase):
    """验证 Task 5 的十槽请求、解析和批准边界。"""

    def test_request_redacts_history_and_binds_hashes(self) -> None:
        draft_context = context()
        brief = gap_brief()
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=draft_context.discovery_family_id,
            context_sha256=draft_context.context_sha256,
            gap_report_hash=draft_context.gap_report_hash,
            memory_snapshot_hash=draft_context.memory_snapshot_hash,
            brief_sha256=evolution_gap_brief_sha256(brief),
            corporate_policy_id=policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )

        request = build_evolution_hypothesis_request(
            draft_context,
            brief,
            authorization=authorization,
        )
        same_request = build_evolution_hypothesis_request(
            draft_context,
            brief,
            authorization=authorization,
        )

        request_bytes = verify_evolution_export_authorization(
            request,
            policy(),
            authorization,
            NOW,
        )
        self.assertGreater(len(request_bytes), 100)
        self.assertEqual(request.request_sha256, same_request.request_sha256)
        self.assertEqual(
            request.export_payload["evolution_binding"]["context_sha256"],
            draft_context.context_sha256,
        )
        prompt = request.body["messages"][1]["content"]
        self.assertNotIn("0.1234", prompt)
        self.assertNotIn("/data/quantlake/private", prompt)
        self.assertNotIn("historical_rank_ic", prompt)
        for internal_key in (
            "discovery_family_id",
            "context_sha256",
            "gap_report_hash",
            "memory_snapshot_hash",
            "brief_sha256",
            "authorization_scope",
            "evolution_binding",
            "model_scope",
            "redaction_policy",
        ):
            self.assertNotIn(f'"{internal_key}"', prompt)
        for internal_value in (
            draft_context.discovery_family_id,
            draft_context.context_sha256,
            draft_context.gap_report_hash,
            draft_context.memory_snapshot_hash,
            evolution_gap_brief_sha256(brief),
            authorization.authorization_id,
            authorization.authorization_sha256,
            draft_context.external_model_redaction_policy,
        ):
            self.assertNotIn(internal_value, prompt)
        self.assertIsNotNone(request.audit_payload_sha256)
        self.assertIn("evolution_binding", request.export_payload)

    def test_request_declares_logical_hypothesis_output_schema(self) -> None:
        """系统提示必须明确十槽逻辑假设的根对象和关键字段。"""

        draft_context = context()
        brief = gap_brief()
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=draft_context.discovery_family_id,
            context_sha256=draft_context.context_sha256,
            gap_report_hash=draft_context.gap_report_hash,
            memory_snapshot_hash=draft_context.memory_snapshot_hash,
            brief_sha256=evolution_gap_brief_sha256(brief),
            corporate_policy_id=policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )

        request = build_evolution_hypothesis_request(
            draft_context,
            brief,
            authorization=authorization,
        )
        system_prompt = request.body["messages"][0]["content"]
        self.assertIn('"hypotheses"', system_prompt)
        self.assertIn('"logical_slot_id"', system_prompt)
        self.assertIn('"source_records"', system_prompt)
        self.assertIn("JSON 数组", system_prompt)
        self.assertIn("result_limit", system_prompt)
        self.assertIn("1-5", system_prompt)
        self.assertIn("简体中文", system_prompt)
        self.assertIn("source_records 恰好一项", system_prompt)
        self.assertIn("competing_explanations 和 failure_modes 各恰好一项", system_prompt)
        self.assertIn("每个自然语言字符串不超过 60 个汉字", system_prompt)
        self.assertIn("expected_direction 必须以", system_prompt)
        self.assertIn("正向：", system_prompt)
        self.assertIn("负向：", system_prompt)
        self.assertIn('"semantic_plan"', system_prompt)
        self.assertIn("E/C/Q/D/O", system_prompt)
        self.assertEqual(request.body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", request.body)

    def test_parser_accepts_optional_semantic_plan_and_preserves_legacy_payload(self) -> None:
        tagged = parse_evolution_hypothesis_response(
            response_payload_with_semantic_tags(),
            context=context(),
        )
        legacy = parse_evolution_hypothesis_response(
            response_payload(),
            context=context(),
        )

        self.assertEqual(tagged[0].semantic_plan.event_tag, "breakout")
        self.assertEqual(tagged[0].semantic_plan.context_tag, "recent_extreme")
        self.assertIsNone(legacy[0].semantic_plan)

    def test_request_exposes_fixed_semantic_quota_without_outcomes(self) -> None:
        draft_context = context()
        brief = gap_brief_with_memory()
        empty_coverage = build_semantic_coverage(())
        brief["memory_feedback"]["semantic_coverage"] = empty_coverage.model_dump(mode="json")
        brief["memory_feedback"]["semantic_quota"] = build_semantic_quota(
            empty_coverage
        ).model_dump(mode="json")
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=draft_context.discovery_family_id,
            context_sha256=draft_context.context_sha256,
            gap_report_hash=draft_context.gap_report_hash,
            memory_snapshot_hash=draft_context.memory_snapshot_hash,
            brief_sha256=evolution_gap_brief_sha256(brief),
            corporate_policy_id=policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )

        request = build_evolution_hypothesis_request(
            draft_context,
            brief,
            authorization=authorization,
        )
        prompt = request.body["messages"][1]["content"]
        self.assertIn("semantic_quota", prompt)
        self.assertIn("semantic-quota-6-3-1-v1", prompt)
        self.assertNotIn("rank_ic_mean", prompt)

    def test_request_exposes_only_discrete_memory_feedback(self) -> None:
        draft_context = context()
        brief = gap_brief_with_memory()
        authorization = registered_llm_evolution_request_authorization(
            discovery_family_id=draft_context.discovery_family_id,
            context_sha256=draft_context.context_sha256,
            gap_report_hash=draft_context.gap_report_hash,
            memory_snapshot_hash=draft_context.memory_snapshot_hash,
            brief_sha256=evolution_gap_brief_sha256(brief),
            corporate_policy_id=policy().policy_id,
            approver_role="research_owner",
            allowed_agent_role=AgentRole.HYPOTHESIS,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )

        request = build_evolution_hypothesis_request(
            draft_context,
            brief,
            authorization=authorization,
        )
        prompt = request.body["messages"][1]["content"]

        self.assertIn("memory_feedback", prompt)
        self.assertIn("unit_mismatch", prompt)
        self.assertNotIn("0.015", prompt)

    def test_parser_allows_distinct_hypotheses_to_reuse_a_gap(self) -> None:
        """同一缺口可以承载多个不同机制的假设。"""

        response = response_payload()
        for item in response["hypotheses"]:
            item["gap_ids"] = ["G001"]

        parsed = parse_evolution_hypothesis_response(
            response,
            context=context(),
        )

        self.assertEqual(len(parsed), 10)

    def test_parser_normalizes_explicit_chinese_unverified_marker(self) -> None:
        """模型用中文表达“未验证”时仍应落为不可绕过的布尔 true。"""

        response = response_payload()
        for hypothesis in response["hypotheses"]:
            hypothesis["mechanism_unverified"] = "未验证"

        parsed = parse_evolution_hypothesis_response(response, context=context())

        self.assertEqual(len(parsed), 10)
        self.assertTrue(all(item.mechanism_unverified is True for item in parsed))

    def test_parser_forces_mechanism_status_to_unverified(self) -> None:
        """机制状态由本地治理固定，不采信模型写入的文本或 false。"""

        response = response_payload()
        response["hypotheses"][0]["mechanism_unverified"] = "模型误填的机制说明"
        response["hypotheses"][1]["mechanism_unverified"] = False

        parsed = parse_evolution_hypothesis_response(response, context=context())

        self.assertTrue(all(item.mechanism_unverified is True for item in parsed))

    def test_parser_wraps_single_competing_and_failure_text(self) -> None:
        """恰好一项的中文字段即使被模型省略数组壳也可确定性恢复。"""

        response = response_payload()
        for hypothesis in response["hypotheses"]:
            hypothesis["competing_explanations"] = hypothesis["competing_explanations"][0]
            hypothesis["failure_modes"] = hypothesis["failure_modes"][0]

        parsed = parse_evolution_hypothesis_response(response, context=context())

        self.assertTrue(all(len(item.competing_explanations) == 1 for item in parsed))
        self.assertTrue(all(len(item.failure_modes) == 1 for item in parsed))

    def test_parser_requires_chinese_narrative(self) -> None:
        """假设叙事必须使用中文，检索关键词可以保留英文。"""

        response = response_payload()
        response["hypotheses"][0]["prior_claim"] = "This is an English claim."

        with self.assertRaises(FactorMinerError):
            parse_evolution_hypothesis_response(
                response,
                context=context(),
            )

    def test_parse_requires_exactly_ten_complete_slots(self) -> None:
        draft_context = context()
        drafts = parse_evolution_hypothesis_response(
            response_payload(),
            context=draft_context,
        )
        self.assertEqual(len(drafts), 10)
        self.assertEqual(drafts[0].logical_slot_id, "H01")

        nine = response_payload()
        nine["hypotheses"] = nine["hypotheses"][:-1]
        with self.assertRaises(FactorMinerError) as caught_nine:
            parse_evolution_hypothesis_response(nine, context=draft_context)
        self.assertIs(caught_nine.exception.code, FailureCode.LLM_RESPONSE_INVALID)

        eleven = response_payload()
        eleven["hypotheses"] = [
            *eleven["hypotheses"],
            dict(eleven["hypotheses"][0], logical_slot_id="H11"),
        ]
        with self.assertRaises(FactorMinerError) as caught_eleven:
            parse_evolution_hypothesis_response(eleven, context=draft_context)
        self.assertIs(
            caught_eleven.exception.code,
            FailureCode.LLM_RESPONSE_INVALID,
        )

    def test_parse_rejects_duplicate_slot_duplicate_content_and_policy_override(
        self,
    ) -> None:
        draft_context = context()
        duplicate_slot = response_payload()
        duplicate_slot["hypotheses"][1]["logical_slot_id"] = "H01"
        with self.assertRaises(FactorMinerError) as duplicate_slot_error:
            parse_evolution_hypothesis_response(
                duplicate_slot,
                context=draft_context,
            )
        self.assertIs(
            duplicate_slot_error.exception.code,
            FailureCode.LLM_RESPONSE_INVALID,
        )

        duplicate_content = response_payload()
        duplicate_content["hypotheses"][1] = dict(
            duplicate_content["hypotheses"][0],
            logical_slot_id="H02",
        )
        with self.assertRaises(FactorMinerError) as duplicate_content_error:
            parse_evolution_hypothesis_response(
                duplicate_content,
                context=draft_context,
            )
        self.assertIs(
            duplicate_content_error.exception.code,
            FailureCode.LLM_RESPONSE_INVALID,
        )

        override = response_payload()
        override["hypotheses"][0]["alpha"] = 0.01
        with self.assertRaises(FactorMinerError) as override_error:
            parse_evolution_hypothesis_response(
                override,
                context=draft_context,
            )
        self.assertIs(
            override_error.exception.code,
            FailureCode.LLM_RESPONSE_INVALID,
        )

    def test_approval_requires_ten_approved_hash_bound_decisions(self) -> None:
        draft_context = context()
        drafts = parse_evolution_hypothesis_response(
            response_payload(),
            context=draft_context,
        )
        batch = approve_evolution_hypotheses(
            drafts,
            approvals_for(drafts),
            context=draft_context,
            authorization=approval_authorization(draft_context),
        )
        self.assertEqual(len(batch.approvals), 10)
        self.assertEqual(batch.discovery_family_id, SAFE_FAMILY)

        rejected = list(approvals_for(drafts))
        rejected[0] = EvolutionHypothesisApproval(
            logical_slot_id="H01",
            context_sha256=draft_context.context_sha256,
            draft_sha256=drafts[0].draft_sha256,
            discovery_family_id=draft_context.discovery_family_id,
            approval_role="research_reviewer",
            approved_at=NOW,
            decision="rejected",
        )
        with self.assertRaises(FactorMinerError) as rejected_error:
            approve_evolution_hypotheses(
                drafts,
                tuple(rejected),
                context=draft_context,
            )
        self.assertIs(
            rejected_error.exception.code,
            FailureCode.APPROVAL_COUNT_INVALID,
        )
        self.assertEqual(rejected_error.exception.message, "approval_required")

        with self.assertRaises(FactorMinerError) as short_error:
            approve_evolution_hypotheses(
                drafts[:-1],
                approvals_for(drafts[:-1]),
                context=draft_context,
                authorization=approval_authorization(draft_context),
            )
        self.assertIs(
            short_error.exception.code,
            FailureCode.APPROVAL_COUNT_INVALID,
        )
        self.assertEqual(short_error.exception.message, "hypothesis_budget_invalid")

    def test_approval_role_is_nonblank_and_must_match_authorization(self) -> None:
        draft_context = context()
        drafts = parse_evolution_hypothesis_response(
            response_payload(),
            context=draft_context,
        )
        with self.assertRaises(FactorMinerError) as blank_role:
            approve_evolution_hypotheses(
                drafts,
                approvals_for(drafts, role="  "),
                context=draft_context,
                authorization=approval_authorization(draft_context),
            )
        self.assertEqual(blank_role.exception.message, "approval_required")

        batch = approve_evolution_hypotheses(
            drafts,
            approvals_for(drafts, role=" research_reviewer "),
            context=draft_context,
            authorization=approval_authorization(
                draft_context,
                role="research_reviewer",
            ),
        )
        self.assertEqual(batch.approvals[0].approval_role, "research_reviewer")
        with self.assertRaises(FactorMinerError) as wrong_role:
            approve_evolution_hypotheses(
                drafts,
                approvals_for(drafts, role="research_reviewer"),
                context=draft_context,
                authorization=approval_authorization(
                    draft_context,
                    role="research_owner",
                ),
            )
        self.assertEqual(wrong_role.exception.message, "approval_required")

        with self.assertRaises(FactorMinerError) as missing_authorization:
            approve_evolution_hypotheses(
                drafts,
                approvals_for(drafts),
                context=draft_context,
            )
        self.assertEqual(
            missing_authorization.exception.message,
            "approval_required",
        )


if __name__ == "__main__":
    unittest.main()
