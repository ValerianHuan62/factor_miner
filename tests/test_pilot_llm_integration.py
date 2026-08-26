"""阶段 B 输出与阶段 A runner 的复用测试。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from factor_miner.llm_online import DEEPSEEK_MODEL
from factor_miner.pilot_llm import (
    ApprovedPilotRequest,
    ExpressionGenerationResponse,
    approve_hypothesis,
    build_expression_generation_request,
    run_approved_pilot,
)
from factor_miner.pilot_schema import PilotRunRequest
from tests.test_pilot_llm import FIXTURE, hypothesis, registry


NOW = datetime(2026, 8, 5, 9, tzinfo=timezone.utc)


class RecordedProvider:
    """提供程序生成的合成三设计，不访问网络。"""

    def generate_three(self, request):
        return ExpressionGenerationResponse(
            request_id=request.request_id,
            provider_call_id="llmcall_" + "b" * 24,
            model=DEEPSEEK_MODEL,
            response_sha256="c" * 64,
            **json.loads(FIXTURE.read_text(encoding="utf-8")),
        )


class PilotLLMIntegrationTest(unittest.TestCase):
    """阶段 B 不复制计算逻辑，只把候选交给阶段 A。"""

    def test_approved_expression_source_delegates_to_stage_a_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved_response = MagicMock()
            approved_response.request_id = "pilotreq_" + "a" * 24
            approved_response.hypothesis = hypothesis()
            approval = approve_hypothesis(
                approved_response,
                approver_role="research_owner",
                approved_at=NOW,
            )
            expression_request = build_expression_generation_request(
                hypothesis=approval.hypothesis,
                registry=registry(),
            )
            run_request = PilotRunRequest(
                visible_start=datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
                visible_end=datetime(2026, 6, 30, tzinfo=timezone.utc).date(),
                candidate_file_sha256="0" * 64,
                evaluation_policy_id="evalpol_synthetic",
                data_release_id="release-stage-b-synthetic-v1",
                code_commit="1" * 40,
                config_hash="2" * 64,
                artifact_root=root,
            )
            request = ApprovedPilotRequest(
                hypothesis=approval.hypothesis,
                approval=approval,
                expression_request=expression_request,
                pilot_request=run_request,
                source_paths=MagicMock(),
                artifact_root=root,
            )
            publication = MagicMock(run_id="run_" + "d" * 24, manifest_path=root / "manifest.json")
            with patch("factor_miner.pilot_llm.run_fixed_pilot", return_value=publication) as runner:
                result = run_approved_pilot(
                    request,
                    RecordedProvider(),
                    registry=registry(),
                    created_at=NOW,
                )
            runner.assert_called_once()
            called = runner.call_args.kwargs
            self.assertEqual(called["candidates"].model_dump()["version"], "pilot-fixed-candidates-v1")
            self.assertNotEqual(called["request"].candidate_file_sha256, "0" * 64)
            self.assertEqual(result.publication.run_id, "run_" + "d" * 24)
            self.assertTrue(
                (root / "state" / "pilot_stage_b" / expression_request.request_id / "generated_candidates.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
