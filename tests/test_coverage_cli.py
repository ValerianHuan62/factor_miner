"""V0.4 覆盖图谱正式 CLI 测试。"""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from factor_miner.cli import app
from factor_miner.coverage_snapshot import publish_coverage_graph
from tests.test_coverage_snapshot import (
    _clusters,
    _nodes,
    _performance,
    _profiles,
    _registered,
    _signal,
    _structural,
)


class CoverageCliTest(unittest.TestCase):
    """CLI 只编排正式合同，不暴露跳过核验的开关。"""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_root_help_lists_coverage_command(self) -> None:
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("coverage", result.output)

    def test_coverage_build_reads_spec_and_reports_graph_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "coverage-spec.json"
            spec_path.write_text(
                json.dumps(
                    _registered(_nodes()).spec.model_dump(mode="json"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            expected = SimpleNamespace(
                coverage_graph_id="covgraph_" + "a" * 24,
                snapshot_root=root / "artifacts" / "coverage_graphs" / "expected",
            )
            with (
                patch(
                    "factor_miner.cli.load_runtime_profile",
                    return_value=SimpleNamespace(artifact_root=root),
                ),
                patch("factor_miner.cli.assert_real_data_allowed"),
                patch(
                    "factor_miner.cli.verify_runtime_identity",
                    return_value=SimpleNamespace(
                        code_commit="4" * 40,
                        config_hash="5" * 64,
                        uv_lock_sha256="6" * 64,
                        release_manifest_sha256="7" * 64,
                    ),
                ),
                patch(
                    "factor_miner.cli.build_coverage_graph",
                    return_value=expected,
                ) as build,
            ):
                result = self.runner.invoke(
                    app,
                    [
                        "coverage",
                        "build",
                        str(spec_path),
                        "--catalog",
                        str(root / "factors.yaml"),
                        "--daily-ic",
                        str(root / "daily.parquet"),
                        "--regime-snapshot",
                        str(root / "regime"),
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                json.loads(result.stdout)["coverage_graph_id"],
                expected.coverage_graph_id,
            )
            self.assertEqual(build.call_count, 1)

    def test_coverage_verify_reads_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = _nodes()
            snapshot = publish_coverage_graph(
                artifact_root=root,
                registered_spec=_registered(nodes),
                nodes=nodes,
                structural_edges=_structural(),
                signal_edges=_signal(),
                factor_performance=_performance(),
                regime_profiles=_profiles(),
                clusters=_clusters(),
            )

            result = self.runner.invoke(
                app,
                ["coverage", "verify", str(snapshot.snapshot_root)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["coverage_graph_id"],
                snapshot.coverage_graph_id,
            )
            self.assertEqual(payload["factor_count"], 2)


if __name__ == "__main__":
    unittest.main()
