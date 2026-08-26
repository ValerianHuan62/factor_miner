"""V0.5 离线 CLI 的安全表面测试。"""

import unittest

from typer.testing import CliRunner

from factor_miner.cli import app


class LLMCLITest(unittest.TestCase):
    """命令表面不得暴露密钥、端点覆盖或研究防火墙绕过。"""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_help_lists_only_approved_offline_commands(self) -> None:
        result = self.runner.invoke(app, ["llm", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for command in (
            "request-prepare",
            "export-preview",
            "policy-build",
            "authorize-export",
            "authorize-scope",
            "provider-call",
            "literature-resolve",
            "response-validate",
            "format-repair-prepare",
            "candidate-convert",
            "prepare-approved",
            "run-approved",
            "resume-approved",
            "brief-build-synthetic",
            "family-register",
            "event-append",
            "family-seal",
            "family-seal-auto",
            "family-verify",
        ):
            self.assertIn(command, result.output)
        for forbidden in (
            "call-deepseek",
            "search-web",
            "skip-privacy",
            "skip-seal",
            "force-strict",
            "read-outcome",
            "api-key",
            "base-url",
        ):
            self.assertNotIn(forbidden, result.output.lower())


if __name__ == "__main__":
    unittest.main()
