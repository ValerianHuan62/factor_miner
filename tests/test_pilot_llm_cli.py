"""阶段 B CLI 的无网络请求准备和人工批准边界测试。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from typer.testing import CliRunner

from factor_miner.llm_online import DEEPSEEK_MODEL
from factor_miner.pilot_llm import HypothesisGenerationResponse
from factor_miner.cli import app
from tests.test_pilot_llm import hypothesis


class PilotLLMCLITest(unittest.TestCase):
    """CLI 只写阶段 B 独立控制产物，不调用旧 family。"""

    def test_prepare_and_approve_hypothesis_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brief_path = root / "brief.json"
            request_path = root / "hypothesis_request.json"
            response_path = root / "hypothesis_response.json"
            approval_path = root / "approval.json"
            brief_path.write_text(
                json.dumps(
                    {
                        "public_brief": "研究信息扩散与交易活动的公开简报。",
                        "verified_source_refs": ["public-source-synthetic-001"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runner = CliRunner()
            prepared = runner.invoke(
                app,
                [
                    "pilot",
                    "prepare-hypothesis",
                    str(brief_path),
                    "--artifact-root",
                    str(root / "artifacts"),
                    "--output",
                    str(request_path),
                ],
            )
            self.assertEqual(prepared.exit_code, 0, prepared.output)
            self.assertTrue(request_path.is_file())
            response = HypothesisGenerationResponse(
                request_id=json.loads(prepared.output)["request_id"],
                request_sha256=json.loads(prepared.output)["request_sha256"],
                provider_call_id="llmcall_" + "e" * 24,
                model=DEEPSEEK_MODEL,
                response_sha256="f" * 64,
                hypothesis=hypothesis(),
            )
            response_path.write_text(
                json.dumps(response.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            approved = runner.invoke(
                app,
                [
                    "pilot",
                    "approve-hypothesis",
                    str(response_path),
                    "--artifact-root",
                    str(root / "artifacts"),
                    "--output",
                    str(approval_path),
                    "--approver-role",
                    "research_owner",
                ],
            )
            self.assertEqual(approved.exit_code, 0, approved.output)
            self.assertTrue(approval_path.is_file())
            state_root = root / "artifacts" / "state" / "pilot_stage_b"
            self.assertTrue(list(state_root.rglob("snapshots/*.json")))


if __name__ == "__main__":
    unittest.main()
