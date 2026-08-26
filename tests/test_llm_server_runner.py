"""服务器端 LLM 隔离运行器的边界测试。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.canonical import canonical_json_bytes
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_online import (
    AgentRole,
    DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    DEEPSEEK_MODEL,
    LLMExportAuthorization,
    build_deepseek_request,
)
from factor_miner.llm_privacy import (
    registered_corporate_external_research_policy,
)
from factor_miner.llm_server_runner import run_server_side_llm_pipeline


NOW = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)
FAMILY_ID = "llmfamily_" + "1" * 24


def _request(mode: str):
    return build_deepseek_request(
        campaign_id="llmcampaign_server_runner_001",
        agent_role=AgentRole.EXPRESSION,
        slot_ids=("coverage_outcome_llm:C001",),
        system_prompt="只输出 JSON object。",
        user_payload={
            "information_class": mode,
            "public_question": "合成表达式能力测试",
        },
    )


def _bundle(root: Path, mode: str) -> Path:
    bundle_root = root / "state" / "llm_server_runs" / FAMILY_ID
    request = _request(mode)
    policy = registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
        allowed_information_classes=(mode,),
        forbidden_information_classes=("raw_market_data",),
        allowed_models=(DEEPSEEK_MODEL,),
        maximum_authorized_campaigns=1,
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        approver_role="research_owner",
        approval_reference="server-runner-synthetic-policy",
    )
    authorization = LLMExportAuthorization(
        authorization_id="authorization-server-runner-001",
        campaign_id=request.campaign_id,
        request_sha256=request.request_sha256,
        corporate_policy_id=policy.policy_id,
        approver_role=policy.approver_role,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )
    bundle_root.mkdir(parents=True)
    (bundle_root / "approved_request.json").write_bytes(
        canonical_json_bytes(request.model_dump(mode="json"))
    )
    (bundle_root / "corporate_policy.json").write_bytes(
        canonical_json_bytes(policy.model_dump(mode="json"))
    )
    (bundle_root / "export_authorization.json").write_bytes(
        canonical_json_bytes(authorization.model_dump(mode="json"))
    )
    return bundle_root


class FakeResponse:
    """不访问网络的最小成功响应。"""

    def __init__(self, record_root: Path, request_hash: str) -> None:
        self.record = type(
            "Record",
            (),
            {
                "call_id": "llmcall_" + "2" * 24,
                "response_sha256": "3" * 64,
                "request_sha256": request_hash,
                "finish_reason": "stop",
            },
        )()
        self.content_json = {"status": "ok"}
        self.tool_calls = ()
        self.record_directory = record_root / "calls" / self.record.call_id


class ServerLLMRunnerTest(unittest.TestCase):
    """服务器运行器不得把真实内容带入 Codex 或绕过审批。"""

    def test_real_server_run_is_rejected_on_mac(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _bundle(root, "public_capability_only")
            with patch.object(platform, "system", return_value="Darwin"):
                with self.assertRaises(FactorMinerError) as context:
                    run_server_side_llm_pipeline(
                        FAMILY_ID,
                        "public_capability_only",
                        root,
                    )
        self.assertEqual(
            context.exception.code,
            FailureCode.RUNTIME_BOUNDARY_ERROR,
        )

    def test_sanitized_mode_requires_an_approved_policy_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_root = _bundle(root, "sanitized_coverage_brief")
            (bundle_root / "corporate_policy.json").unlink()
            with patch.object(platform, "system", return_value="Linux"):
                with self.assertRaises(FactorMinerError) as context:
                    run_server_side_llm_pipeline(
                        FAMILY_ID,
                        "sanitized_coverage_brief",
                        root,
                    )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_POLICY_NOT_AUTHORIZED,
        )

    def test_public_mode_returns_only_a_hash_bound_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_root = _bundle(root, "public_capability_only")
            with patch.object(platform, "system", return_value="Linux"):
                with patch(
                    "factor_miner.llm_server_runner.execute_recorded_call",
                    return_value=FakeResponse(bundle_root, _request("public_capability_only").request_sha256),
                ):
                    summary = run_server_side_llm_pipeline(
                        FAMILY_ID,
                        "public_capability_only",
                        root,
                    )

            self.assertEqual(summary.status, "completed")
            self.assertEqual(summary.terminal_slot_count, 1)
            self.assertEqual(len(summary.request_hashes), 1)
            self.assertEqual(len(summary.artifact_manifest_sha256), 64)
            stdout_like = canonical_json_bytes(summary.model_dump(mode="json"))
            self.assertNotIn(b"public_question", stdout_like)
            self.assertNotIn(b"DEEPSEEK_API_KEY", stdout_like)


if __name__ == "__main__":
    unittest.main()
