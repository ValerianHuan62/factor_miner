"""批准批次 CLI 的跨平台边界测试。"""

import unittest
from typer.testing import CliRunner

from factor_miner.cli import app


class ApprovedBatchCLITest(unittest.TestCase):
    """所有平台都必须经过相同的显式输入校验。"""

    def test_run_approved_is_not_blocked_by_operating_system(self) -> None:
        result = CliRunner().invoke(
            app,
            [
                "llm",
                "run-approved",
                "llmfamily_" + "0" * 24,
                "/tmp/does-not-exist-hypotheses.json",
                "/tmp/does-not-exist-policy.json",
                "/tmp/does-not-exist-payloads.json",
                "/tmp/does-not-exist-registry.json",
                "/tmp/does-not-exist-authorizations.json",
                "--artifact-root",
                "/tmp/factor-miner-synthetic-artifacts",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("只能在 Linux", result.output)

    def test_resume_approved_is_exposed_as_a_separate_recovery_command(self) -> None:
        result = CliRunner().invoke(app, ["llm", "resume-approved", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("恢复批准批次", result.output)


if __name__ == "__main__":
    unittest.main()
