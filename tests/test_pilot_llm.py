"""阶段 B DeepSeek 请求、硬校验和人工批准测试。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.field_registry import (
    FieldAvailabilityEntry,
    FieldAvailabilityRegistry,
)
from factor_miner.llm_online import (
    DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    DEEPSEEK_MODEL,
    LLMExportAuthorization,
)
from factor_miner.llm_privacy import registered_corporate_external_research_policy
from factor_miner.pilot_llm import (
    ExpressionGenerationResponse,
    HypothesisGenerationResponse,
    PilotHypothesisApproval,
    RecordedDeepSeekExpressionProvider,
    build_expression_generation_request,
    build_hypothesis_generation_request,
    build_prepared_expression_request,
    build_prepared_hypothesis_request,
    normalize_expression_response,
    request_from_prepared_expression,
    approve_hypothesis,
)
from factor_miner.schema import HypothesisSpec


NOW = datetime(2026, 8, 5, 9, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "pilot" / "deepseek_expression_response.json"


def registry() -> FieldAvailabilityRegistry:
    """构造只含合成收盘价和成交量能力的字段注册表。"""

    return FieldAvailabilityRegistry(
        registry_id="field-registry-stage-b-synthetic-v1",
        data_release_id="release-stage-b-synthetic-v1",
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
                economic_type="trading_activity",
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


def hypothesis() -> HypothesisSpec:
    """构造已通过来源约束的合成假设。"""

    return HypothesisSpec(
        claim="价格变化可能反映信息扩散速度差异。",
        mechanism="信息进入价格存在时滞。",
        expected_sign="positive",
        observable_proxy="二十日收盘价变化。",
        independent_verification="使用独立公告与成交量信息检验。",
        competing_explanations=("行业暴露", "流动性暴露"),
        baseline_reference="合成价格变化基线。",
        failure_modes=("价格缺失", "机制在不同市场状态反转"),
        falsification_path="若独立检验失败或方向反转则否定。",
        source_refs=("public-source-synthetic-001",),
    )


def policy():
    """构造仅允许公开能力摘要的合成外发政策。"""

    from datetime import timedelta

    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=("public_capability_only",),
        forbidden_information_classes=(
            "factor_outcome",
            "individual_security_data",
            "raw_market_data",
        ),
        allowed_models=(DEEPSEEK_MODEL,),
        maximum_authorized_campaigns=2,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        approver_role="research_owner",
        approval_reference="stage-b-synthetic",
    )


def _authorization(prepared, external_policy):
    """创建只绑定精确请求哈希的合成授权。"""

    return LLMExportAuthorization(
        authorization_id="authorization_" + prepared.request_sha256[:24],
        campaign_id=prepared.campaign_id,
        request_sha256=prepared.request_sha256,
        corporate_policy_id=external_policy.policy_id,
        approver_role=external_policy.approver_role,
        authorized_at=NOW,
        expires_at=NOW.replace(hour=10),
    )


class RecordedTransport:
    """只返回本地录制夹具的合成传输。"""

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        del api_key
        body = json.loads(request_bytes)
        assert body["response_format"] == {"type": "json_object"}
        content = FIXTURE.read_text(encoding="utf-8")
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        ).encode("utf-8")


class PilotLLMContractTest(unittest.TestCase):
    """阶段 B 合同必须先硬失败，再得到确定性三个候选。"""

    def test_hypothesis_prompt_freezes_exact_output_shape(self) -> None:
        request = build_hypothesis_generation_request(
            public_brief="研究公开简报：信息扩散与交易活动。",
            verified_source_refs=("public-source-synthetic-001",),
        )
        prepared = build_prepared_hypothesis_request(request)
        prompt = prepared.body["messages"][0]["content"]
        for field in (
            '"hypothesis"',
            '"claim"',
            '"expected_sign"',
            '"baseline_reference"',
            '"mechanism_status"',
        ):
            self.assertIn(field, prompt)
        self.assertNotIn('"expected_direction"', prompt)
        self.assertNotIn('"freeze_operator"', prompt)

    def test_expression_request_contains_only_public_capabilities(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        prepared = build_prepared_expression_request(request)
        payload = json.dumps(prepared.export_payload, ensure_ascii=False)
        self.assertNotIn('"field_id"', payload)
        self.assertNotIn("IC", payload)
        self.assertNotIn("Sharpe", payload)
        self.assertNotIn("回撤", payload)
        self.assertNotIn("individual_security_data", payload)
        self.assertEqual(
            tuple(item.public_alias for item in request.field_capabilities),
            ("price_close", "volume"),
        )

    def test_expression_prompt_freezes_exact_output_shape(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        prepared = build_prepared_expression_request(request)
        prompt = prepared.body["messages"][0]["content"]
        for field in (
            '"designs"',
            '"candidate_slot_id"',
            '"expression"',
            '"required_fields"',
            '"max_lookback"',
            '"lint_diagnostics"',
            '"op"',
            '"args"',
            '"field"',
        ):
            self.assertIn(field, prompt)
        self.assertIn("max_depth", prompt)
        self.assertIn("15", prompt)
        self.assertIn("5", prompt)
        self.assertIn("父节点深度为 1 + max(子节点深度)", prompt)
        self.assertIn("不要把 rolling_corr 放在 neg、mul 或 div 的深层子树中", prompt)
        self.assertNotIn('"pilot_fixed_001":', prompt)
        self.assertNotIn('"operands"', prompt)

    def test_expression_response_normalizes_three_candidates(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        response = ExpressionGenerationResponse(
            request_id=request.request_id,
            provider_call_id="llmcall_" + "1" * 24,
            model=DEEPSEEK_MODEL,
            response_sha256="2" * 64,
            **json.loads(FIXTURE.read_text(encoding="utf-8")),
        )
        candidates = normalize_expression_response(
            response,
            hypothesis(),
            registry(),
            request,
            created_at=NOW,
        )
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0].required_fields, ("close",))
        self.assertEqual(candidates[0].max_lookback, 20)
        self.assertEqual(candidates[1].required_fields, ("volume",))
        self.assertEqual(candidates[1].max_lookback, 19)
        self.assertEqual(candidates[0].availability, "next_open")
        self.assertEqual(candidates[0].provenance["source"], "deepseek_pilot_stage_b")

    def test_invalid_centered_rolling_is_hard_rejected(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["designs"][0]["expression"]["op"] = "rolling_mean"
        payload["designs"][0]["expression"]["window"] = 20
        payload["designs"][0]["expression"]["center"] = True
        payload["designs"][0]["expression"].pop("period", None)
        payload["designs"][0]["max_lookback"] = 19
        response = ExpressionGenerationResponse(
            request_id=request.request_id,
            provider_call_id="llmcall_" + "3" * 24,
            model=DEEPSEEK_MODEL,
            response_sha256="4" * 64,
            **payload,
        )
        with self.assertRaises(FactorMinerError) as context:
            normalize_expression_response(response, hypothesis(), registry(), request)
        self.assertEqual(context.exception.code, FailureCode.LOOKAHEAD_DETECTED)

    def test_required_fields_and_lookback_are_not_model_controlled(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["designs"][1]["required_fields"] = ["price_close"]
        response = ExpressionGenerationResponse(
            request_id=request.request_id,
            provider_call_id="llmcall_" + "5" * 24,
            model=DEEPSEEK_MODEL,
            response_sha256="6" * 64,
            **payload,
        )
        with self.assertRaises(FactorMinerError) as context:
            normalize_expression_response(response, hypothesis(), registry(), request)
        self.assertEqual(context.exception.code, FailureCode.FIELD_MISSING)

    def test_prepared_expression_round_trips_without_internal_fields(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        prepared = build_prepared_expression_request(request)
        restored = request_from_prepared_expression(prepared)
        self.assertEqual(restored, request)
        self.assertNotIn('"field_id"', json.dumps(prepared.export_payload))

    def test_provider_records_response_and_never_reads_outcomes(self) -> None:
        request = build_expression_generation_request(
            hypothesis=hypothesis(),
            registry=registry(),
        )
        external_policy = policy()
        prepared = build_prepared_expression_request(request)
        with tempfile.TemporaryDirectory() as directory:
            import os

            provider = RecordedDeepSeekExpressionProvider(
                policy=external_policy,
                authorization=_authorization(prepared, external_policy),
                record_root=Path(directory),
                transport=RecordedTransport(),
                now=NOW,
            )
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic"}):
                response = provider.generate_three(request)
            self.assertEqual(len(response.designs), 3)
            request_payload = json.loads(
                (Path(directory) / "calls" / response.provider_call_id / "request.json").read_bytes()
            )
            user_payload = request_payload["messages"][1]["content"]
            self.assertNotIn("raw_market_data", user_payload)
            self.assertNotIn("individual_security_data", user_payload)

    def test_hypothesis_approval_binds_exact_content(self) -> None:
        request = build_hypothesis_generation_request(
            public_brief="研究公开简报：信息扩散与交易活动。",
            verified_source_refs=("public-source-synthetic-001",),
        )
        response = HypothesisGenerationResponse(
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            provider_call_id="llmcall_" + "7" * 24,
            model=DEEPSEEK_MODEL,
            response_sha256="8" * 64,
            hypothesis=hypothesis(),
        )
        approval = approve_hypothesis(response, approver_role="research_owner", approved_at=NOW)
        payload = approval.model_dump(mode="json")
        payload["hypothesis_sha256"] = "0" * 64
        # 修改假设哈希后不得重新构造成合法批准记录。
        with self.assertRaises(ValueError):
            PilotHypothesisApproval.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
