"""不可变运行产物发布测试。"""

from pathlib import Path
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_artifacts import (
    publish_run_artifacts,
    verify_published_run,
)


RUN_ID = "run_" + "1" * 24


class PortfolioArtifactsTest(unittest.TestCase):
    """运行产物必须完整、原子且可重建。"""

    def test_publish_and_verify_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = publish_run_artifacts(
                root,
                RUN_ID,
                {
                    "portfolio/daily_returns.parquet": b"synthetic parquet",
                    "portfolio/metrics.json": b"{\"status\":\"ok\"}",
                },
            )
            second = publish_run_artifacts(
                root,
                RUN_ID,
                {
                    "portfolio/daily_returns.parquet": b"synthetic parquet",
                    "portfolio/metrics.json": b"{\"status\":\"ok\"}",
                },
            )
            self.assertEqual(first, second)
            self.assertEqual(verify_published_run(root, RUN_ID), first)

    def test_tamper_and_forbidden_names_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FactorMinerError) as context:
                publish_run_artifacts(root, RUN_ID, {"Evidence/result.json": b"x"})
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)
            publish_run_artifacts(root, RUN_ID, {"portfolio/metrics.json": b"x"})
            path = root / "artifacts" / "runs" / RUN_ID / "portfolio/metrics.json"
            path.write_bytes(b"tampered")
            with self.assertRaises(FactorMinerError):
                verify_published_run(root, RUN_ID)


if __name__ == "__main__":
    unittest.main()
