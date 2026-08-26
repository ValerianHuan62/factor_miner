"""阶段 A Pilot Dashboard 投影测试。"""

from pathlib import Path
import tempfile
import unittest

from typer.testing import CliRunner

from factor_miner.cli import app
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.errors import FactorMinerError
from factor_miner.pilot_projection import project_pilot_run
from factor_miner.portfolio_artifacts import publish_run_artifacts, verify_published_run


RUN_ID = "run_" + "3" * 24


def _publish(root: Path) -> None:
    """发布最小 Pilot JSON 集合。"""

    publish_run_artifacts(
        root,
        RUN_ID,
        {
            "run/metrics.json": b'{"candidate_summaries":[{"candidate_id":"pilot_fixed_001","spec_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}',
            "run/input_manifest.json": b'{"input_manifest_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
            "ic/diagnostics.json": b'{"candidates":{"pilot_fixed_001":{"ic_mean":0.01}}}',
            "portfolio/daily.json": b'{"candidates":{"pilot_fixed_001":{"daily":[{"exit_date":"2026-01-13","Q10_net_return":0.01}]}}}',
            "portfolio/metrics.json": b'{"candidates":{"pilot_fixed_001":{"series":{"Q10_net_return":{"sharpe":1.0}}}}}',
            "barra/attribution.json": b'{"candidates":{"pilot_fixed_001":{"status":"not_available","missing_inputs":["industry_exposures"]}}}',
            "visualization/pilot.json": b'{"candidates":{"pilot_fixed_001":{"ic_sequence":[0.01]}}}',
            "candidates/pilot_fixed_001/spec.json": '{"spec_version":"1","hypothesis":{"claim":"合成测试主张。","mechanism":"合成测试机制。","observable_proxy":"合成测试代理。","independent_verification":"合成测试验证。","competing_explanations":["合成竞争解释。"],"failure_modes":["合成失效方式。"],"falsification_path":"合成证伪路径。","source_refs":["合成测试来源。"]}}'.encode(),
            "candidates/pilot_fixed_002/spec.json": '{"spec_version":"1","hypothesis":{"claim":"合成测试主张。","mechanism":"合成测试机制。","observable_proxy":"合成测试代理。","independent_verification":"合成测试验证。","competing_explanations":["合成竞争解释。"],"failure_modes":["合成失效方式。"],"falsification_path":"合成证伪路径。","source_refs":["合成测试来源。"]}}'.encode(),
            "candidates/pilot_fixed_003/spec.json": '{"spec_version":"1","hypothesis":{"claim":"合成测试主张。","mechanism":"合成测试机制。","observable_proxy":"合成测试代理。","independent_verification":"合成测试验证。","competing_explanations":["合成竞争解释。"],"failure_modes":["合成失效方式。"],"falsification_path":"合成证伪路径。","source_refs":["合成测试来源。"]}}'.encode(),
        },
    )


class FailingStore:
    """模拟 PostgreSQL 连接失败。"""

    def replace_run_snapshot(self, run_id: str, snapshot: dict[str, object]) -> None:
        raise RuntimeError("connection refused")

    def load_run_snapshot(self, run_id: str) -> dict[str, object] | None:
        return None


class PilotProjectionTest(unittest.TestCase):
    """投影必须只依赖已发布本地产物并保持幂等。"""

    def test_projection_is_idempotent_and_normalizes_candidate_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _publish(root)
            store = InMemoryDashboardStore()
            first = project_pilot_run(root, RUN_ID, store)
            second = project_pilot_run(root, RUN_ID, store)
            self.assertEqual(first, second)
            self.assertEqual(
                first.candidate_metrics["pilot_fixed_001"]["candidate_id"],
                "pilot_fixed_001",
            )
            self.assertEqual(
                first.portfolio_daily["pilot_fixed_001"][0]["Q10_net_return"],
                0.01,
            )
            self.assertEqual(
                first.barra_attribution["pilot_fixed_001"]["status"],
                "not_available",
            )

    def test_database_failure_does_not_destroy_verifiable_local_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _publish(root)
            with self.assertRaises(RuntimeError):
                project_pilot_run(root, RUN_ID, FailingStore())
            self.assertEqual(verify_published_run(root, RUN_ID).run_id, RUN_ID)

    def test_different_snapshot_for_same_run_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _publish(root)
            store = InMemoryDashboardStore()
            project_pilot_run(root, RUN_ID, store)
            store.snapshots[RUN_ID]["snapshot_sha256"] = "0" * 64
            with self.assertRaises(FactorMinerError):
                project_pilot_run(root, RUN_ID, store)

    def test_unpublished_or_missing_reference_fails_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FactorMinerError):
                project_pilot_run(root, RUN_ID, InMemoryDashboardStore())
            publish_run_artifacts(root, RUN_ID, {"run/metrics.json": b"{}"})
            with self.assertRaises(FactorMinerError):
                project_pilot_run(root, RUN_ID, InMemoryDashboardStore())

    def test_cli_exposes_local_run_and_projection_commands(self) -> None:
        result = CliRunner().invoke(app, ["pilot", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("run-fixed", result.output)
        self.assertIn("project", result.output)


if __name__ == "__main__":
    unittest.main()
