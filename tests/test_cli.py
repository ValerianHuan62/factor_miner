"""CLI 正式入口测试。"""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from factor_miner.cli import (
    _load_reference_resources,
    _validate_reference_manifest_binding,
    app,
)
from factor_miner.compiler import compile_candidate
from factor_miner.errors import FactorMinerError
from factor_miner.policy import (
    company_a_share_incremental_policy,
    company_a_share_visible_policy,
)
from factor_miner.regime_research import regime_research_report_payload
from factor_miner.regime_snapshot import publish_regime_snapshot
from tests.test_regime_snapshot import input_provenance, research_fixture
from tests.helpers import (
    strip_ansi,
    valid_candidate,
    valid_incremental_policy,
    valid_reference_factor_library,
    valid_registered_candidate,
    valid_registered_reference_factor_library,
)


class CliTest(unittest.TestCase):
    """验证 CLI 的成功、失败和危险参数防护。"""

    def setUp(self) -> None:
        """创建 Typer 测试运行器。"""

        self.runner = CliRunner()

    def test_help_lists_formal_commands_without_forbidden_switches(self) -> None:
        """帮助必须列出正式命令且不暴露绕过参数。"""
        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.stdout)
        for command in (
            "doctor",
            "validate-spec",
            "register-candidate",
            "register-policy",
            "register-reference-library",
            "register-family",
            "register-campaign",
            "compile-spec",
            "run-smoke",
            "run-visible",
            "ledger-verify",
            "recover-interrupted",
            "regime",
            "evolution",
            "research",
        ):
            self.assertIn(command, result.stdout)
        for forbidden in (
            "ignore-state",
            "use-local-data",
            "skip-ledger",
            "force-test",
            "dolphindb",
            "postgres",
        ):
            self.assertNotIn(forbidden, result.stdout.lower())

    def test_research_help_lists_worker_and_status(self) -> None:
        """自主研究 CLI 只暴露单 Worker 和只读状态命令。"""

        result = self.runner.invoke(app, ["research", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("worker", result.stdout)
        self.assertIn("status", result.stdout)

    def test_pilot_help_lists_existing_run_reevaluation(self) -> None:
        """正式 Pilot CLI 必须暴露不调用 LLM 的现有 Spec 复算入口。"""

        result = self.runner.invoke(app, ["pilot", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("reevaluate-existing", result.stdout)
        detail = self.runner.invoke(app, ["pilot", "reevaluate-existing", "--help"])
        self.assertEqual(detail.exit_code, 0, detail.output)
        plain_help = strip_ansi(detail.stdout)
        self.assertIn("--source-run-id", plain_help)
        self.assertIn("--config", plain_help)
        self.assertIn("--artifact-root", plain_help)
        self.assertNotIn("--build-identity", plain_help)

    def test_research_worker_rejects_darwin(self) -> None:
        """Mac 不得进入真实自主研究依赖装配。"""

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "worker.json"
            config.write_text("{}", encoding="utf-8")
            with patch("factor_miner.server_research_dependencies.platform.system", return_value="Darwin"):
                result = self.runner.invoke(
                    app,
                    ["research", "worker", "--config", str(config), "--once"],
                )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Linux", result.output)

    def test_regime_research_writes_registered_summary_from_input_only_source(
        self,
    ) -> None:
        """研究命令必须登记 Spec，并输出双轨建议字段和报告路径。"""

        features, spec, report, _ = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "regime-research.json"
            spec_path.write_text(
                json.dumps(spec.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            source = unittest.mock.MagicMock()
            source.inspect_inputs.return_value = input_provenance()
            source.scan_inputs.return_value.collect.return_value = object()
            feature_result = SimpleNamespace(frame=features)
            identity = SimpleNamespace(
                code_commit="a" * 40,
                config_hash="b" * 64,
                release_manifest_sha256="c" * 64,
                uv_lock_sha256="d" * 64,
            )
            with (
                patch(
                    "factor_miner.cli.load_runtime_profile",
                    return_value=SimpleNamespace(artifact_root=root),
                ),
                patch("factor_miner.cli.assert_real_data_allowed"),
                patch(
                    "factor_miner.cli.verify_runtime_identity",
                    return_value=identity,
                ),
                patch(
                    "factor_miner.cli.CompanyAShareRegimeDataSource",
                    return_value=source,
                ),
                patch(
                    "factor_miner.cli.build_market_features",
                    return_value=feature_result,
                ),
                patch(
                    "factor_miner.cli.run_regime_research",
                    return_value=report,
                ),
            ):
                result = self.runner.invoke(
                    app,
                    ["regime", "research", str(spec_path)],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["regime_research_id"], report.regime_research_id)
            self.assertIn("median", payload["recommendations"])
            self.assertTrue(Path(payload["report_path"]).is_file())
            report_payload = json.loads(
                Path(payload["report_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                report_payload["input_provenance"]["resolved_release_id"],
                "release-regime-1",
            )
            self.assertEqual(
                report_payload["runtime_identity"]["code_commit"],
                "a" * 40,
            )
            source.inspect_inputs.assert_called_once_with()
            source.scan_inputs.assert_called_once()

    def test_regime_register_deployment_rejects_unregistered_research(self) -> None:
        """部署引用的研究不存在时必须失败关闭。"""

        _, _, report, deployment = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment_path = root / "deployment.json"
            report_path = root / "report.json"
            deployment_path.write_text(
                json.dumps(deployment.spec.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(
                    regime_research_report_payload(report),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = self.runner.invoke(
                app,
                [
                    "regime",
                    "register-deployment",
                    str(deployment_path),
                    "--report-path",
                    str(report_path),
                    "--artifact-root",
                    str(root),
                ],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("REGIME_DOWNSTREAM_MANIFEST_MISMATCH", result.output)

    def test_regime_build_rejects_real_mode_on_mac(self) -> None:
        """真实状态构建必须先经过授权 Linux 运行边界。"""

        environment = {
            "FM_MODE": "visible",
            "FM_ARTIFACT_ROOT": "/data/factor_miner_artifacts",
            "FM_QUANTLAKE_ROOT": "/data/quantlake",
            "FM_MARKET_URI": "/data/market.parquet",
            "FM_STATE_URI": "/data/state.parquet",
            "FM_LABEL_URI": "/data/label.parquet",
            "FM_DATA_ORIGIN": "server_quantlake",
            "FM_RESOLVED_RELEASE_ID": "release-1",
            "FM_RELEASE_MANIFEST_SHA256": "a" * 64,
            "FM_SCHEMA_VERSION": "schema-1",
            "FM_MARKET_CUTOFF": "2026-07-01",
            "FM_ADJUSTMENT_CONVENTION": "unadjusted",
            "FM_CALENDAR_VERSION": "calendar-1",
            "FM_STATE_TABLE_VERSION": "state-1",
            "FM_STATE_TABLE_CUTOFF": "2026-07-01",
        }
        with patch("factor_miner.runtime.platform_module.system", return_value="Darwin"):
            result = self.runner.invoke(
                app,
                [
                    "regime",
                    "build",
                    "--deployment-id",
                    "regdeploy_" + "1" * 24,
                    "--start",
                    "2020-01-01",
                    "--end",
                    "2026-07-01",
                ],
                env=environment,
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("RUNTIME_BOUNDARY_ERROR", result.output)

    def test_regime_verify_outputs_snapshot_provenance(self) -> None:
        """只读 verify 必须返回绑定的数据来源而不触发重训。"""

        features, _, report, deployment = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = publish_regime_snapshot(
                artifact_root=Path(directory),
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            result = self.runner.invoke(
                app,
                ["regime", "verify", str(snapshot.snapshot_root)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["provenance"]["resolved_release_id"],
                "release-regime-1",
            )
            forbidden = ("alpha", "evidence", "认证状态", "生产结论")
            self.assertTrue(
                all(text not in result.stdout.lower() for text in forbidden)
            )

    def test_register_reference_library_outputs_content_identity(self) -> None:
        """CLI 必须把参考库登记到独立内容寻址目录。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library_path = root / "reference-library.json"
            library = valid_reference_factor_library()
            library_path.write_text(
                json.dumps(library.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            result = self.runner.invoke(
                app,
                [
                    "register-reference-library",
                    str(library_path),
                    "--artifact-root",
                    str(root),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["reference_factor_library_id"],
                valid_registered_reference_factor_library().reference_factor_library_id,
            )
            self.assertTrue(Path(payload["path"]).is_file())

    def test_incremental_policy_requires_registered_library(self) -> None:
        """V0.2 政策在参考库尚未登记时必须拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "incremental-policy.json"
            policy_path.write_text(
                json.dumps(
                    valid_incremental_policy().model_dump(mode="json"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = self.runner.invoke(
                app,
                [
                    "register-policy",
                    str(policy_path),
                    "--artifact-root",
                    str(root),
                ],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("参考因子库", result.output)

    def test_incremental_policy_rejects_custom_orthogonalization(self) -> None:
        """正式 CLI 不允许通过 JSON 自定义正交化门槛。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = valid_registered_reference_factor_library()
            library_path = root / "reference-library.json"
            library_path.write_text(
                json.dumps(library.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            registered = self.runner.invoke(
                app,
                [
                    "register-reference-library",
                    str(library_path),
                    "--artifact-root",
                    str(root),
                ],
            )
            self.assertEqual(registered.exit_code, 0, registered.output)
            policy = company_a_share_incremental_policy(library)
            custom = policy.model_copy(
                update={
                    "orthogonalization": policy.orthogonalization.model_copy(
                        update={"min_median_residual_variance_ratio": 0.06}
                    )
                }
            )
            policy_path = root / "custom-policy.json"
            policy_path.write_text(
                json.dumps(custom.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            result = self.runner.invoke(
                app,
                [
                    "register-policy",
                    str(policy_path),
                    "--artifact-root",
                    str(root),
                ],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("代码内置", result.output)

    def test_incremental_library_rejects_different_runtime_manifest(self) -> None:
        """运行环境清单不得悄悄替换已登记参考因子库对应的清单。"""

        library = valid_registered_reference_factor_library()
        with self.assertRaisesRegex(FactorMinerError, "参考清单 SHA-256"):
            _validate_reference_manifest_binding(
                {"FM_REFERENCE_MANIFEST_SHA256": "d" * 64},
                library,
            )

    def test_validate_spec_outputs_canonical_json(self) -> None:
        """合法 spec 应通过 CLI 并输出规范化 JSON。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(
                json.dumps(valid_candidate().model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            result = self.runner.invoke(app, ["validate-spec", str(path)])
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout)["spec_version"], "1")

    def test_invalid_spec_returns_nonzero(self) -> None:
        """非法 spec 必须返回非零退出码。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("{}", encoding="utf-8")
            result = self.runner.invoke(app, ["validate-spec", str(path)])
            self.assertNotEqual(result.exit_code, 0)

    def test_visible_doctor_rejects_darwin_profile(self) -> None:
        """Darwin 平台不能执行 visible doctor。"""
        environment = {
            "FM_MODE": "visible",
            "FM_ARTIFACT_ROOT": "/data/factor_miner_artifacts",
            "FM_QUANTLAKE_ROOT": "/data/quantlake",
            "FM_MARKET_URI": "/data/market.parquet",
            "FM_STATE_URI": "/data/state.parquet",
            "FM_LABEL_URI": "/data/label.parquet",
            "FM_DATA_ORIGIN": "server_quantlake",
            "FM_RESOLVED_RELEASE_ID": "release-1",
            "FM_RELEASE_MANIFEST_SHA256": "a" * 64,
            "FM_SCHEMA_VERSION": "schema-1",
            "FM_MARKET_CUTOFF": "2020-01-03",
            "FM_ADJUSTMENT_CONVENTION": "unadjusted",
            "FM_CALENDAR_VERSION": "calendar-1",
            "FM_STATE_TABLE_VERSION": "state-1",
            "FM_STATE_TABLE_CUTOFF": "2020-01-03",
        }
        with patch("factor_miner.runtime.platform_module.system", return_value="Darwin"):
            result = self.runner.invoke(app, ["doctor"], env=environment)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("RUNTIME_BOUNDARY_ERROR", result.output)

    def test_damaged_ledger_returns_nonzero(self) -> None:
        """损坏账本必须让 ledger-verify 返回非零。"""
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "state" / "ledger"
            ledger_path.mkdir(parents=True)
            (ledger_path / "trials.jsonl").write_bytes(b"{bad}\n")
            result = self.runner.invoke(app, ["ledger-verify", "--artifact-root", directory])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("LEDGER_CORRUPT", result.output)

    def test_reference_metadata_check_does_not_open_parquet(self) -> None:
        """冒烟只核验参考编译元数据，不要求参考输出文件存在。"""

        policy = company_a_share_visible_policy()
        plan = compile_candidate(valid_registered_candidate(), {"close"}).model_copy(
            update={"candidate_id": policy.reference_factor_ids[0]}
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "references.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_id": policy.reference_manifest_id,
                        "data_cutoff": "2020-01-30",
                        "factors": [
                            {
                                "factor_id": policy.reference_factor_ids[0],
                                "parquet_path": str(root / "does-not-exist.parquet"),
                                "parquet_sha256": "f" * 64,
                                "compiled_plan": plan.model_dump(mode="json"),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            environment = {
                "FM_REFERENCE_MANIFEST_PATH": str(manifest_path),
                "FM_REFERENCE_MANIFEST_SHA256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
            }
            source, plans = _load_reference_resources(
                environment, policy, metadata_only=True
            )
            self.assertIsNone(source)
            self.assertEqual(tuple(plans), policy.reference_factor_ids)
            with self.assertRaises(FactorMinerError):
                _load_reference_resources(environment, policy, metadata_only=False)


if __name__ == "__main__":
    unittest.main()
