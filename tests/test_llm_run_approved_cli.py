"""批准批次 CLI 的本机边界测试。"""

import platform
import unittest
from typer.testing import CliRunner

from factor_miner.cli import app


class ApprovedBatchCLITest(unittest.TestCase):
    """Mac 只能验证命令边界，不能启动真实批准批次。"""

    @unittest.skipUnless(
        platform.system() == "Darwin",
        "该测试验证 Mac 边界；Linux 服务器应进入真实批次依赖校验",
    )
    def test_run_approved_is_linux_only_before_reading_private_files(self) -> None:
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
        self.assertIn("RUNTIME_BOUNDARY_ERROR", result.output)

    def test_resume_approved_is_exposed_as_a_separate_recovery_command(self) -> None:
        result = CliRunner().invoke(app, ["llm", "resume-approved", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("恢复批准批次", result.output)


if __name__ == "__main__":
    unittest.main()
