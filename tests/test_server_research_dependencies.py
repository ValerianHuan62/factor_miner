"""真实自主研究依赖的 Linux 与显式配置边界测试。"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.long_only_protocol import (
    DiscoveryRankICSummary,
    LongOnlyResearchProtocol,
    select_direction,
)
from factor_miner.pilot_schema import PilotRunRequest, PilotSourcePaths
from factor_miner.portfolio_artifacts import publish_run_artifacts
from factor_miner.llm_privacy import registered_corporate_external_research_policy
from factor_miner.server_research_dependencies import (
    ServerResearchDependencies,
    ServerResearchConfig,
    _aggregate_direction_decisions,
    _candidate_dashboard_dimensions,
    _dashboard_barra_summary,
    _dashboard_portfolio_daily,
    _evaluate_ready_with_isolation,
    _load_frozen_direction_decision,
    _run_pilot_with_frozen_family,
    load_server_research_config,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.lightweight_schema import LightweightCandidateBinding
from factor_miner.llm_state import CandidateSlotState
from factor_miner.research_campaign_runner import CampaignSlotEvaluation


class ServerResearchDependenciesTest(unittest.TestCase):
    """缺少任何冻结输入时都不能回落到旧目录或录制数据。"""

    def test_dashboard_publication_keeps_returns_but_not_security_weights(self) -> None:
        """主 Dashboard 产物只保留画图收益，完整权重留在来源 Pilot。"""

        payload = _dashboard_portfolio_daily({
            "daily": [{"exit_date": "2026-01-02", "target_long_net_return": 0.01}],
            "weights": [{"security_id": "000001.XSHE", "weight": 0.1}],
        })

        self.assertEqual(payload, {
            "daily": [{"exit_date": "2026-01-02", "target_long_net_return": 0.01}]
        })

    def test_invalid_provider_response_is_retried_without_hiding_other_errors(self) -> None:
        calls = 0

        def transient():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise FactorMinerError(
                    FailureCode.LLM_RESPONSE_INVALID,
                    "合成响应被截断",
                )
            return "ok"

        self.assertEqual(
            ServerResearchDependencies._retry_invalid_provider_response(transient),
            "ok",
        )
        self.assertEqual(calls, 3)

        def permanent():
            raise FactorMinerError(
                FailureCode.LLM_PROVIDER_UNAVAILABLE,
                "合成供应商不可用",
            )

        with self.assertRaises(FactorMinerError) as caught:
            ServerResearchDependencies._retry_invalid_provider_response(permanent)
        self.assertIs(caught.exception.code, FailureCode.LLM_PROVIDER_UNAVAILABLE)

    def test_hypothesis_chinese_validation_failure_retries_complete_generation(self) -> None:
        """中文叙事校验位于供应商调用之后，也必须进入自动重试边界。"""

        dependencies = object.__new__(ServerResearchDependencies)
        dependencies.context = object()
        invalid = FactorMinerError(
            FailureCode.LLM_RESPONSE_INVALID,
            "H01 的 observable_proxy 必须使用中文",
        )
        with patch(
            "factor_miner.server_research_dependencies.generate_lightweight_hypotheses",
            side_effect=(invalid, "中文假设批次"),
        ) as generate:
            result = dependencies._generate_hypothesis_batch_with_retry(
                run_id="autrun_0123456789abcdef01234567",
                prepared=object(),
                provider=object(),
            )

        self.assertEqual(result, "中文假设批次")
        self.assertEqual(generate.call_count, 2)

    def test_worker_passes_complete_manifest_family_size_to_pilot(self) -> None:
        """Worker 即使分三候选运行也必须使用完整冻结研究族分母。"""

        calls: list[dict[str, object]] = []

        def runner(**kwargs: object) -> object:
            calls.append(kwargs)
            return object()

        _run_pilot_with_frozen_family(
            runner,
            frozen_family_size=29,
            candidates="three-candidate-batch",
        )

        self.assertEqual(calls[0]["frozen_family_size"], 29)
        self.assertEqual(calls[0]["candidates"], "three-candidate-batch")

    def test_candidate_dimensions_preserve_barra_attribution(self) -> None:
        """自主研究合并 Pilot 时必须把 Barra 归因带入主发布运行。"""

        snapshot = {
            "candidate_metrics": {"candidates": {"pilot_fixed_001": {"ic": 0.01}}},
            "ic_diagnostics": {"candidates": {"pilot_fixed_001": {"ic_mean": 0.01}}},
            "recent_ic_diagnostics": {"candidates": {"pilot_fixed_001": {"ic_mean": 0.02}}},
            "portfolio_metrics": {"candidates": {"pilot_fixed_001": {"series": {}}}},
            "portfolio_governance": {"candidates": {"pilot_fixed_001": {"win_rate": 0.6}}},
            "recent_portfolio_metrics": {"candidates": {"pilot_fixed_001": {"series": {"target_long_net_return": {"annualized_return": 0.2}}}}},
            "portfolio_daily": {"candidates": {"pilot_fixed_001": {"daily": [], "weights": [1]}}},
            "barra_attribution": {"candidates": {"pilot_fixed_001": {"status": "complete"}}},
        }

        result = _candidate_dashboard_dimensions(snapshot, "pilot_fixed_001")

        self.assertEqual(result["barra_attribution"], {"status": "complete"})
        self.assertEqual(result["portfolio_daily"], {"daily": []})
        self.assertEqual(result["portfolio_governance"], {"win_rate": 0.6})
        self.assertEqual(result["recent_ic_diagnostics"], {"ic_mean": 0.02})
        self.assertEqual(
            result["recent_portfolio_metrics"]["series"]
            ["target_long_net_return"]["annualized_return"],
            0.2,
        )

    def test_aggregate_publication_keeps_each_frozen_discovery_direction(self) -> None:
        """主运行不能丢失子 Pilot 已冻结的方向，否则数据库投影会误报。"""

        protocol = LongOnlyResearchProtocol()
        decision = select_direction(
            DiscoveryRankICSummary(
                protocol=protocol,
                window_start=protocol.discovery_start,
                window_end=protocol.discovery_end,
                rank_ic=-0.02,
            ),
            hypothesis_direction="positive",
        )
        slot = CampaignSlotEvaluation(
            slot_id="H01:C001",
            slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
            status="evaluated",
            candidate_id="cand_" + "1" * 24,
            candidate_spec_hash="2" * 64,
            output={
                "direction_decision": decision.model_dump(mode="json"),
                "direction_record_sha256": "3" * 64,
            },
        )
        evaluation = type(
            "SyntheticEvaluation",
            (),
            {"slot_evaluations": (slot,)},
        )()

        payload = _aggregate_direction_decisions(evaluation)

        candidate = payload["candidates"]["cand_" + "1" * 24]
        self.assertEqual(candidate["decision"]["selected_direction"], "negative")
        self.assertEqual(candidate["direction_record_sha256"], "3" * 64)

    def _publish_direction_artifacts(
        self,
        root: Path,
        *,
        run_id: str,
        decisions_version: str = "pilot-direction-decisions-v1",
        decision_record_ref: str | None = None,
        decision_record_sha256: str | None = None,
    ) -> tuple[str, str]:
        """发布由正式清单保护的最小方向产物及其冻结记录。"""

        candidate_id = "pilot_fixed_001"
        candidate_spec_hash = "a" * 64
        protocol = LongOnlyResearchProtocol()
        decision = select_direction(
            DiscoveryRankICSummary(
                protocol=protocol,
                window_start=protocol.discovery_start,
                window_end=protocol.discovery_end,
                rank_ic=0.01,
            ),
            hypothesis_direction="positive",
        )
        provenance = {
            "data_release_id": "release-1",
            "input_manifest_sha256": "b" * 64,
            "evaluation_policy_id": "eval-policy",
            "family_size": 3,
            "code_commit": "c" * 40,
            "config_hash": "d" * 64,
            "source_run_id": None,
        }
        record_ref = "state/pilot_direction/direction_test.json"
        record = {
            "version": "pilot-direction-freeze-v1",
            "protocol": protocol.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "identity": {
                "candidate_id": candidate_id,
                "spec_sha256": candidate_spec_hash,
            },
            "provenance": provenance,
        }
        record_path = root / record_ref
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(canonical_json_bytes(record))
        record_sha256 = sha256_json(record)
        metrics = {
            "data_release_id": provenance["data_release_id"],
            "input_manifest_sha256": provenance["input_manifest_sha256"],
            "evaluation_policy_id": provenance["evaluation_policy_id"],
            "frozen_family_size": provenance["family_size"],
            "code_commit": provenance["code_commit"],
            "config_hash": provenance["config_hash"],
            "source_run_id": provenance["source_run_id"],
            "candidates": {
                candidate_id: {
                    "candidate_id": candidate_id,
                    "spec_sha256": candidate_spec_hash,
                    "direction_record_ref": record_ref,
                    "direction_record_sha256": record_sha256,
                }
            },
        }
        decisions = {
            "version": decisions_version,
            "candidates": {
                candidate_id: {
                    "decision": decision.model_dump(mode="json"),
                    "direction_record_ref": decision_record_ref or record_ref,
                    "direction_record_sha256": decision_record_sha256 or record_sha256,
                }
            },
        }
        publish_run_artifacts(
            root,
            run_id,
            {
                "direction/decisions.json": canonical_json_bytes(decisions),
                "run/metrics.json": canonical_json_bytes(metrics),
            },
        )
        return candidate_id, candidate_spec_hash

    def test_worker_reloads_and_binds_frozen_direction_from_verified_disk_manifest(self) -> None:
        """若改信任内存 Pilot manifest、旧版本或外部记录引用，Worker 必须失败。"""

        cases = (
            ("run_" + "1" * 24, {}, None),
            ("run_" + "2" * 24, {"decisions_version": "pilot-direction-decisions-v0"}, "版本"),
            ("run_" + "3" * 24, {"decision_record_ref": "state/pilot_direction/other.json"}, "引用"),
            ("run_" + "4" * 24, {"decision_record_sha256": "f" * 64}, "哈希"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for run_id, overrides, error in cases:
                candidate_id, spec_hash = self._publish_direction_artifacts(
                    root, run_id=run_id, **overrides
                )
                with self.subTest(run_id=run_id):
                    if error is None:
                        decision, record_sha256 = _load_frozen_direction_decision(
                            artifact_root=root,
                            run_id=run_id,
                            candidate_id=candidate_id,
                            candidate_spec_hash=spec_hash,
                        )
                        self.assertEqual(decision.selected_direction, "positive")
                        self.assertEqual(len(record_sha256), 64)
                    else:
                        with self.assertRaisesRegex(ValueError, error):
                            _load_frozen_direction_decision(
                                artifact_root=root,
                                run_id=run_id,
                                candidate_id=candidate_id,
                                candidate_spec_hash=spec_hash,
                            )

    def test_worker_rejects_tampered_or_pre_direction_disk_manifest(self) -> None:
        """磁盘产物篡改和旧清单都不能被内存 Pilot 元数据掩盖。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "run_" + "5" * 24
            candidate_id, spec_hash = self._publish_direction_artifacts(
                root, run_id=run_id
            )
            direction_path = (
                root / "artifacts" / "runs" / run_id / "direction" / "decisions.json"
            )
            direction_path.write_bytes(b'{"version":"tampered"}')
            with self.assertRaisesRegex(FactorMinerError, "哈希"):
                _load_frozen_direction_decision(
                    artifact_root=root,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    candidate_spec_hash=spec_hash,
                )

            old_run_id = "run_" + "6" * 24
            publish_run_artifacts(
                root,
                old_run_id,
                {"run/metrics.json": canonical_json_bytes({"candidates": {}})},
            )
            with self.assertRaisesRegex(ValueError, "缺少冻结方向决定"):
                _load_frozen_direction_decision(
                    artifact_root=root,
                    run_id=old_run_id,
                    candidate_id=candidate_id,
                    candidate_spec_hash=spec_hash,
                )

    def test_dashboard_barra_summary_keeps_only_latest_cross_section(self) -> None:
        """主运行只保留最新截面摘要，完整历史留在来源 Pilot。"""

        wrapper = {
            "status": "available",
            "attribution": {
                "risk_decomposition_status": "complete",
                "max_abs_reconciliation_error": 0.0,
                "exposure_summary": [
                    {"signal_date": "2026-01-01", "portfolio": "Q1", "Size": 0.1},
                    {"signal_date": "2026-01-08", "portfolio": "Q1", "Size": 0.2},
                ],
                "attribution": [
                    {"signal_date": "2026-01-01", "factor": "Size", "contribution": 0.01},
                    {"signal_date": "2026-01-08", "factor": "Size", "contribution": 0.02},
                ],
                "risk_decomposition": [
                    {"signal_date": "2026-01-01", "portfolio": "Q1", "total_variance": 0.1},
                    {"signal_date": "2026-01-08", "portfolio": "Q1", "total_variance": 0.2},
                ],
            },
        }

        summary = _dashboard_barra_summary(wrapper)

        detail = summary["attribution"]
        self.assertEqual(detail["exposure_summary"][0]["signal_date"], "2026-01-08")
        self.assertEqual(detail["attribution"][0]["contribution"], 0.02)
        self.assertEqual(detail["risk_decomposition"][0]["total_variance"], 0.2)

    def _write_policy(self, path: Path, *classes: str) -> None:
        """写入内容身份完整的合成外发政策。"""

        policy = registered_corporate_external_research_policy(
            provider="deepseek",
            allowed_endpoint="https://api.deepseek.com/chat/completions",
            allowed_information_classes=tuple(sorted(classes)),
            forbidden_information_classes=("raw_market_data",),
            allowed_models=("deepseek-v4-pro",),
            maximum_authorized_campaigns=10,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
            approver_role="research_owner",
            approval_reference="synthetic-autonomous-policy",
        )
        path.write_text(policy.model_dump_json(), encoding="utf-8")

    def _config(self, root: Path, *, mode: str = "live") -> Path:
        artifact_root = root / "artifacts"
        artifact_root.mkdir()
        minute = root / "equities_final_2005_20260630"
        minute.mkdir()
        quantlake = root / "quantlake"
        quantlake.mkdir()
        files = {}
        for name in (
            "registry.json", "context.json", "gap.json", "policy.json",
            "scope.json", "evaluation.json",
        ):
            path = root / name
            path.write_text("{}", encoding="utf-8")
            files[name] = path
        self._write_policy(
            files["policy.json"],
            "evolution_gap_brief",
            "public_capability_only",
        )
        pilot_paths = PilotSourcePaths(
            quantlake_root=quantlake,
            release_manifest_uri=quantlake / "release.json",
            state_manifest_uri=quantlake / "state-manifest.json",
            market_uri=quantlake / "market.parquet",
            state_uri=quantlake / "state.parquet",
            field_registry_uri=quantlake / "registry.json",
            benchmark_root=quantlake,
            calendar_root=quantlake,
            calendar_uri=quantlake / "calendar.parquet",
            calendar_version="calendar-1",
            benchmark_uri=quantlake / "benchmark.parquet",
            benchmark_schema_version="benchmark-1",
        )
        request = PilotRunRequest(
            visible_start=date(2021, 1, 1),
            visible_end=date(2026, 6, 30),
            candidate_file_sha256="0" * 64,
            evaluation_policy_id="eval-policy",
            data_release_id="release-1",
            code_commit="1" * 40,
            config_hash="2" * 64,
            artifact_root=artifact_root,
        )
        kwargs = {
            "artifact_root": artifact_root,
            "minute_aggregate_root": minute,
            "field_registry_path": files["registry.json"],
            "evolution_context_path": files["context.json"],
            "gap_brief_path": files["gap.json"],
            "corporate_policy_path": files["policy.json"],
            "scope_authorization_path": files["scope.json"],
            "evaluation_policy_path": files["evaluation.json"],
            "pilot_paths": pilot_paths,
            "pilot_request": request,
            "provider_mode": mode,
        }
        if mode == "recorded":
            for key in ("recorded_hypothesis_call", "recorded_expression_call"):
                path = root / key
                path.mkdir()
                kwargs[key] = path
        config = ServerResearchConfig(**kwargs)
        path = root / "worker.json"
        path.write_text(
            json.dumps(config.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_real_worker_rejects_darwin_before_reading_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(Path(directory))
            with self.assertRaisesRegex(ValueError, "Linux"):
                load_server_research_config(
                    path,
                    environment={},
                    system_name="Darwin",
                )

    def test_live_mode_requires_database_and_deepseek_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(Path(directory))
            with self.assertRaisesRegex(ValueError, "PostgreSQL"):
                load_server_research_config(path, environment={}, system_name="Linux")
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                load_server_research_config(
                    path,
                    environment={"FM_DASHBOARD_DSN": "postgresql://synthetic"},
                    system_name="Linux",
                )

    def test_recorded_mode_does_not_require_network_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(Path(directory), mode="recorded")
            config = load_server_research_config(
                path,
                environment={"FM_DASHBOARD_DSN": "postgresql://synthetic"},
                system_name="Linux",
            )
            self.assertEqual(config.provider_mode, "recorded")

    def test_live_mode_rejects_policy_missing_expression_information_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._config(root)
            self._write_policy(root / "policy.json", "evolution_gap_brief")
            with self.assertRaisesRegex(ValueError, "public_capability_only"):
                load_server_research_config(
                    path,
                    environment={
                        "FM_DASHBOARD_DSN": "postgresql://synthetic",
                        "DEEPSEEK_API_KEY": "synthetic",
                    },
                    system_name="Linux",
                )

    def test_missing_or_wrong_minute_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._config(root)
            payload = json.loads(path.read_text("utf-8"))
            wrong = root / "old_minute_data"
            wrong.mkdir()
            payload["minute_aggregate_root"] = str(wrong)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "equities_final"):
                load_server_research_config(
                    path,
                    environment={
                        "FM_DASHBOARD_DSN": "postgresql://synthetic",
                        "DEEPSEEK_API_KEY": "synthetic",
                    },
                    system_name="Linux",
                )

    def test_enabled_barra_sources_require_explicit_policy_path(self) -> None:
        """Worker 启用三类 Barra URI 时必须同时冻结归因政策文件。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._config(root)
            payload = json.loads(path.read_text("utf-8"))
            derived = root / "factor_miner_derived"
            derived.mkdir()
            payload["pilot_paths"].update(
                {
                    "barra_root": str(derived),
                    "barra_manifest_uri": str(derived / "manifest.json"),
                    "barra_max_exposure_staleness_days": 7,
                    "barra_exposure_uri": str(derived / "exposures.parquet"),
                    "barra_factor_returns_uri": str(derived / "factor_returns.parquet"),
                    "barra_benchmark_weights_uri": str(derived / "weights.parquet"),
                }
            )
            with self.assertRaisesRegex(ValueError, "barra_policy_path"):
                ServerResearchConfig.model_validate(payload)

    def test_candidate_local_failure_isolated_to_original_slot(self) -> None:
        """组评价失败后逐槽重试，常数候选不能中止其余候选。"""

        bindings = tuple(
            LightweightCandidateBinding(
                slot_id=f"H01:C{index:03d}",
                source_candidate_id=f"candidate_{index}",
                candidate_spec_hash=f"{index:064x}",
            )
            for index in range(1, 4)
        )
        calls: list[tuple[str, ...]] = []

        def evaluator(group):
            calls.append(tuple(item.slot_id for item in group))
            if len(group) > 1 or group[0].slot_id == "H01:C002":
                raise FactorMinerError(
                    FailureCode.STAT_FAMILY_NOT_FROZEN,
                    "IC 截面存在常数输入",
                )
            item = group[0]
            return [
                CampaignSlotEvaluation(
                    slot_id=item.slot_id,
                    slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                    status="evaluated",
                )
            ]

        results = _evaluate_ready_with_isolation(bindings, evaluator)

        self.assertEqual([item.status for item in results], ["evaluated", "failed", "evaluated"])
        self.assertEqual(results[1].slot_id, "H01:C002")
        self.assertIn("IC 截面存在常数输入", results[1].failure_reason)
        self.assertEqual(len(calls), 4)

    def test_infrastructure_failure_is_not_downgraded_to_candidate_failure(self) -> None:
        binding = LightweightCandidateBinding(
            slot_id="H01:C001",
            source_candidate_id="candidate_1",
            candidate_spec_hash="1" * 64,
        )

        def evaluator(group):
            raise FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, "发布版本不一致")

        with self.assertRaisesRegex(FactorMinerError, "发布版本不一致"):
            _evaluate_ready_with_isolation((binding,), evaluator)


if __name__ == "__main__":
    unittest.main()
