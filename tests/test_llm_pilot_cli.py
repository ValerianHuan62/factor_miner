"""V0.5 在线试运行 CLI 的无网络准备与授权测试。"""

import json
from pathlib import Path
import tempfile
import unittest

from typer.testing import CliRunner

from factor_miner.cli import app
from tests.test_llm_online import policy


class LLMPilotCLITest(unittest.TestCase):
    """请求准备、预览和授权都不得读取密钥或访问网络。"""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_prepare_preview_and_exact_hash_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_path = root / "payload.json"
            slots_path = root / "slots.json"
            request_path = root / "request.json"
            policy_path = root / "policy.json"
            authorization_path = root / "authorization.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "information_class": "coverage_gap_bins",
                        "gap_id": "G001",
                        "summary": "价格延续覆盖较少",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            slots_path.write_text(
                '["coverage_outcome_llm:H01"]',
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(policy().model_dump(mode="json")),
                encoding="utf-8",
            )

            prepared = self.runner.invoke(
                app,
                [
                    "llm",
                    "request-prepare",
                    "hypothesis",
                    str(payload_path),
                    "--campaign-id",
                    "llmcampaign_synthetic_001",
                    "--slot-ids",
                    str(slots_path),
                    "--output",
                    str(request_path),
                ],
            )
            self.assertEqual(prepared.exit_code, 0, prepared.output)
            request_hash = json.loads(prepared.output)["request_sha256"]

            preview = self.runner.invoke(
                app,
                [
                    "llm",
                    "export-preview",
                    str(request_path),
                    str(policy_path),
                ],
            )
            self.assertEqual(preview.exit_code, 0, preview.output)
            self.assertEqual(
                json.loads(preview.output)["request_sha256"],
                request_hash,
            )

            authorized = self.runner.invoke(
                app,
                [
                    "llm",
                    "authorize-export",
                    str(request_path),
                    str(policy_path),
                    "--approved-request-sha256",
                    request_hash,
                    "--approver-role",
                    "research_owner",
                    "--output",
                    str(authorization_path),
                ],
            )
            self.assertEqual(authorized.exit_code, 0, authorized.output)
            self.assertTrue(authorization_path.is_file())
            persisted = authorization_path.read_text(encoding="utf-8")
            self.assertNotIn("DEEPSEEK_API_KEY", persisted)
            self.assertNotIn("api_key", persisted.casefold())

    def test_wrong_hash_cannot_be_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_path = root / "payload.json"
            slots_path = root / "slots.json"
            policy_path = root / "policy.json"
            request_path = root / "request.json"
            authorization_path = root / "authorization.json"
            payload_path.write_text(
                '{"information_class":"coverage_gap_bins","gap":"合成"}',
                encoding="utf-8",
            )
            slots_path.write_text(
                '["coverage_outcome_llm:H01"]',
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(policy().model_dump(mode="json")),
                encoding="utf-8",
            )
            self.runner.invoke(
                app,
                [
                    "llm",
                    "request-prepare",
                    "hypothesis",
                    str(payload_path),
                    "--campaign-id",
                    "llmcampaign_synthetic_001",
                    "--slot-ids",
                    str(slots_path),
                    "--output",
                    str(request_path),
                ],
            )
            result = self.runner.invoke(
                app,
                [
                    "llm",
                    "authorize-export",
                    str(request_path),
                    str(policy_path),
                    "--approved-request-sha256",
                    "0" * 64,
                    "--approver-role",
                    "research_owner",
                    "--output",
                    str(authorization_path),
                ],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("LLM_EXPORT_NOT_AUTHORIZED", result.output)
            self.assertFalse(authorization_path.exists())

    def test_policy_build_creates_content_addressed_private_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = policy().model_dump(mode="json")
            source.pop("policy_id")
            source.pop("policy_sha256")
            spec_path = root / "policy_spec.json"
            output_path = root / "policy.json"
            spec_path.write_text(
                json.dumps(source),
                encoding="utf-8",
            )

            result = self.runner.invoke(
                app,
                [
                    "llm",
                    "policy-build",
                    str(spec_path),
                    "--output",
                    str(output_path),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            built = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertRegex(
                built["policy_id"],
                r"^extpolicy_[0-9a-f]{24}$",
            )
            self.assertEqual(len(built["policy_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
