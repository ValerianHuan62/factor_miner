"""阶段 A 固定候选运行产物测试。"""

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.canonical import canonical_json_bytes
from factor_miner.ic_diagnostics import ICDiagnostics
from factor_miner.pilot_runner import (
    FixedBarraEvaluation,
    FixedPilotCandidateResult,
    FixedPortfolioEvaluation,
    LongOnlyWindowEvaluation,
    publish_fixed_pilot_run,
    _evaluation_record_provenance,
    _freeze_confirmation_diagnostics,
    _freeze_direction_decision,
)
from factor_miner.pilot_schema import (
    BarraAvailability,
    BenchmarkIdentity,
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotInputManifest,
    PilotRunRequest,
)
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.portfolio_artifacts import publish_run_artifacts
from factor_miner.pilot_artifacts import (
    canonical_pilot_config_hash,
    load_published_candidate_specs,
    reevaluate_existing_pilot_run,
    resolve_pilot_code_commit,
    verify_reevaluation_request_identity,
)
from factor_miner.portfolio_evaluation import PortfolioBacktestResult
from factor_miner.portfolio_evaluation import (
    ExtremeSpreadDiagnostic,
    PortfolioGovernanceSummary,
)
from factor_miner.portfolio_schema import (
    PortfolioEvaluationPolicy,
    TradingCalendarIdentity,
    portfolio_policy_id,
)
from factor_miner.portfolio_statistics import PortfolioMetrics, PortfolioSeriesMetrics
from factor_miner.pilot_sources import load_fixed_candidates
from factor_miner.schema import evaluation_policy_id
from factor_miner.long_only_protocol import (
    LongOnlyResearchProtocol,
)


FIXTURE = Path(__file__).parent / "fixtures" / "pilot" / "fixed_candidates.json"


def _input_manifest(root: Path) -> PilotInputManifest:
    """构造不读取真实数据的输入身份夹具。"""

    quantlake = root / "quantlake"
    return PilotInputManifest(
        quantlake_root=quantlake,
        resolved_release_id="release-1",
        release_manifest_uri=quantlake / "metadata" / "release.json",
        release_manifest_sha256="1" * 64,
        state_manifest_uri=quantlake / "metadata" / "state.json",
        state_manifest_sha256="2" * 64,
        schema_version="l2_state_v1",
        market_cutoff=date(2026, 7, 31),
        state_table_version="state-1",
        state_table_cutoff=date(2026, 7, 31),
        adjustment_convention="forward_adjusted_market; state_flags_unadjusted",
        market_uri=quantlake / "processed" / "market.parquet",
        state_uri=quantlake / "state" / "state.parquet",
        field_registry_uri=quantlake / "config" / "fields.csv",
        field_registry_sha256="3" * 64,
        canonical_field_map={"close": "adj_close", "volume": "volume"},
        calendar_uri=quantlake / "calendar" / "sse_szse.parquet",
        calendar=TradingCalendarIdentity(
            calendar_version="sse-szse-2026-v1",
            calendar_sha256="4" * 64,
        ),
        benchmark=BenchmarkIdentity(
            source_uri=root / "csi300" / "index_daily.parquet",
            source_sha256="5" * 64,
            schema_version="csi300-open-v1",
            cutoff=date(2026, 6, 30),
        ),
        barra=BarraAvailability(
            status="not_available",
            missing_inputs=("industry_exposures", "benchmark_weights", "factor_returns"),
        ),
    )


def _ic(
    value: float,
    observed_date: date = date(2026, 7, 1),
) -> ICDiagnostics:
    """构造最小合法 IC 诊断。"""

    return ICDiagnostics(
        daily=(
            {
                "date": observed_date,
                "horizon": 5,
                "ic": value,
                "rank_ic": value,
                "eligible_count": 20,
            },
        ),
        ic_sequence=(value,),
        rank_ic_sequence=(value,),
        ic_mean=value,
        rank_ic_mean=value,
        ic_std=0.0,
        rank_ic_std=0.0,
        ic_ir=0.0,
        rank_ic_ir=0.0,
        ir=0.0,
        p_ic_lt_neg_002=1.0 if value < -0.02 else 0.0,
        p_ic_gt_pos_002=1.0 if value > 0.02 else 0.0,
        ic_hac_t=0.0,
        rank_ic_hac_t=0.0,
        ic_distribution={"q05": value, "q25": value, "q50": value, "q75": value, "q95": value},
        rank_ic_distribution={"q05": value, "q25": value, "q50": value, "q75": value, "q95": value},
        decay=tuple(
            {
                "horizon": horizon,
                "ic_mean": value,
                "rank_ic_mean": value,
                "valid_dates": 1,
            }
            for horizon in (1, 3, 5, 10, 20)
        ),
        autocorrelation=(),
        annual_summary={"2026": {"valid_dates": 1, "ic_mean": value, "rank_ic_mean": value}},
    )


def _portfolio(direction: str = "positive") -> FixedPortfolioEvaluation:
    """构造最小合法组合结果。"""

    policy = PortfolioEvaluationPolicy()
    backtest = PortfolioBacktestResult.build(
        policy_id=portfolio_policy_id(policy),
        direction=direction,
        daily_returns=(
            {
                "entry_date": date(2026, 7, 2),
                "exit_date": date(2026, 7, 3),
                "target_long_gross_return": 0.01,
                "target_long_turnover": 1.0,
                "target_long_cost": 0.0014,
                "target_long_net_return": 0.0086,
                "benchmark_return": 0.002,
            },
        ),
        weights=(
            {
                "signal_date": date(2026, 7, 1),
                "entry_date": date(2026, 7, 2),
                "exit_date": date(2026, 7, 3),
                "security_id": "A",
                "portfolio": "target_long",
                "weight": 1.0,
            },
        ),
        governance=PortfolioGovernanceSummary.build(
            (
                ExtremeSpreadDiagnostic(
                    entry_date=date(2026, 7, 2),
                    exit_date=date(2026, 7, 3),
                    extreme_spread_net_return=0.01,
                ),
            )
        ),
    )
    series = PortfolioSeriesMetrics(
        observations=2,
        period_return=0.01,
        annualized_return=0.01,
        excess_return=0.008,
        excess_annualized_return=0.008,
        annualized_volatility=0.1,
        excess_annualized_volatility=0.1,
        sharpe=0.1,
        information_ratio=0.1,
        max_drawdown=0.0,
        excess_max_drawdown=0.0,
        annualization_factor=1.0,
    )
    metrics = PortfolioMetrics.build(
        calendar_version="sse-szse-2026-v1",
        calendar_sha256="4" * 64,
        series={
            "target_long_gross_return": series,
            "target_long_net_return": series,
            "CSI300": series,
        },
    )
    return FixedPortfolioEvaluation(backtest=backtest, metrics=metrics)


def _barra() -> FixedBarraEvaluation:
    """构造可选 Barra 不可用状态。"""

    return FixedBarraEvaluation(
        availability=BarraAvailability(
            status="not_available",
            missing_inputs=("industry_exposures",),
        ),
        attribution=None,
        identity=None,
        reason="合成夹具未提供 Barra 行业暴露",
    )


def _request(root: Path) -> PilotRunRequest:
    """构造固定运行请求。"""

    policy = company_a_share_visible_policy()
    return PilotRunRequest(
        visible_start=date(2026, 1, 1),
        visible_end=date(2026, 6, 30),
        candidate_file_sha256="6" * 64,
        evaluation_policy_id=evaluation_policy_id(policy),
        data_release_id="release-1",
        code_commit="a" * 40,
        config_hash="7" * 64,
        artifact_root=root,
    )


def _results(
    root: Path,
    bad: bool = False,
    candidates=None,
    family_size: int | None = None,
) -> dict[str, FixedPilotCandidateResult]:
    """构造三个候选的完整结果。"""

    candidates = candidates or load_fixed_candidates(FIXTURE)
    value = -0.5 if bad else 0.1
    protocol = LongOnlyResearchProtocol()
    frozen_family_size = family_size or len(candidates.candidates)
    record_provenance = _evaluation_record_provenance(
        input_manifest=_input_manifest(root),
        evaluation_policy=company_a_share_visible_policy(),
        family_size=frozen_family_size,
        request=_request(root),
        source_run_id=None,
    )
    results: dict[str, FixedPilotCandidateResult] = {}
    for candidate in candidates.candidates:
        suffix = "_bad" if bad else ""
        direction_path = (
            root
            / "state"
            / "pilot_direction"
            / f"{candidate.candidate_id}{suffix}.json"
        )
        direction, direction_sha256 = _freeze_direction_decision(
            rank_ic=value,
            hypothesis_direction=candidate.spec.hypothesis.expected_sign.value,
            direction_record_path=direction_path,
            protocol=protocol,
            identity={
                "candidate_id": candidate.candidate_id,
                "spec_sha256": candidate.spec_hash,
            },
            provenance=record_provenance,
        )
        confirmation = _ic(value, observed_date=date(2026, 6, 1))
        confirmation_path = direction_path.with_name(
            f"confirmation_{candidate.candidate_id}{suffix}.json"
        )
        confirmation_sha256 = _freeze_confirmation_diagnostics(
            candidate_id=candidate.candidate_id,
            spec_sha256=candidate.spec_hash,
            diagnostics=confirmation,
            confirmation_record_path=confirmation_path,
            protocol=protocol,
            provenance=record_provenance,
            direction_record_sha256=direction_sha256,
        )
        results[candidate.candidate_id] = FixedPilotCandidateResult(
            candidate_id=candidate.candidate_id,
            ic=confirmation,
            portfolio=_portfolio("negative" if bad else "positive"),
            barra=_barra(),
            windows=LongOnlyWindowEvaluation(
                direction=direction,
                confirmation=confirmation,
                recent=_ic(value),
                confirmation_passed=False,
                stress_test_eligible=False,
                direction_record_sha256=direction_sha256,
                direction_record_path=direction_path,
                confirmation_record_sha256=confirmation_sha256,
                confirmation_record_path=confirmation_path,
                family_size=frozen_family_size,
            ),
            recent_portfolio_metrics=_portfolio(
                "negative" if bad else "positive"
            ).metrics,
        )
    return results


def _with_frozen_confirmation(
    root: Path,
    candidate,
    result: FixedPilotCandidateResult,
    diagnostics: ICDiagnostics,
    suffix: str,
) -> FixedPilotCandidateResult:
    """为测试替换确认诊断时同步生成新的不可变确认记录。"""

    assert result.windows is not None
    path = (
        root
        / "state"
        / "pilot_direction"
        / f"confirmation_{candidate.candidate_id}_{suffix}.json"
    )
    sha256 = _freeze_confirmation_diagnostics(
        candidate_id=candidate.candidate_id,
        spec_sha256=candidate.spec_hash,
        diagnostics=diagnostics,
        confirmation_record_path=path,
        protocol=LongOnlyResearchProtocol(),
        provenance=json.loads(
            result.windows.direction_record_path.read_text("utf-8")
        )["provenance"],
        direction_record_sha256=result.windows.direction_record_sha256,
    )
    return replace(
        result,
        ic=diagnostics,
        windows=replace(
            result.windows,
            confirmation=diagnostics,
            confirmation_record_path=path,
            confirmation_record_sha256=sha256,
        ),
    )


class PilotArtifactsTest(unittest.TestCase):
    """Pilot 运行身份和产物必须不可变。"""

    def test_publish_is_idempotent_and_contains_all_three_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            manifest = _input_manifest(root)
            request = _request(root)
            first = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=_results(root),
                input_manifest=manifest,
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=request,
            )
            second = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=_results(root),
                input_manifest=manifest,
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=request,
            )
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(first.manifest, second.manifest)
            verified = verify_published_run(root, first.run_id)
            paths = {item.relative_path for item in verified.artifacts}
            self.assertIn("run/input_manifest.json", paths)
            self.assertIn("ic/diagnostics.json", paths)
            self.assertIn("portfolio/metrics.json", paths)
            self.assertIn("barra/attribution.json", paths)
            self.assertIn("direction/decisions.json", paths)
            self.assertIn("ic/recent.json", paths)
            self.assertIn("run/eligibility.json", paths)
            self.assertIn("portfolio/recent_metrics.json", paths)
            self.assertEqual(sum(path.endswith("/spec.json") for path in paths), 3)
            recent = json.loads(
                (
                    root
                    / "artifacts"
                    / "runs"
                    / first.run_id
                    / "portfolio"
                    / "recent_metrics.json"
                ).read_text("utf-8")
            )
            series = recent["candidates"]["pilot_fixed_001"]["series"]
            for portfolio_name in (
                "target_long_gross_return",
                "target_long_net_return",
            ):
                self.assertIn("annualized_return", series[portfolio_name])
                self.assertIn("max_drawdown", series[portfolio_name])
                self.assertIn("sharpe", series[portfolio_name])

    def test_publish_rejects_spoofed_stress_eligibility(self) -> None:
        """发布器必须重算确认资格，不能相信调用者传入的布尔值。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            original = results["pilot_fixed_001"]
            assert original.windows is not None
            results["pilot_fixed_001"] = replace(
                original,
                windows=replace(
                    original.windows,
                    confirmation_passed=True,
                    stress_test_eligible=True,
                ),
            )

            with self.assertRaisesRegex(FactorMinerError, "资格"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )

    def test_publish_rejects_fake_direction_record_hash(self) -> None:
        """发布器必须重读方向原子文件，不能只相信传入的 SHA。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            original = results["pilot_fixed_001"]
            assert original.windows is not None
            results["pilot_fixed_001"] = replace(
                original,
                windows=replace(
                    original.windows,
                    direction_record_sha256="9" * 64,
                ),
            )

            with self.assertRaisesRegex(FactorMinerError, "方向"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )

    def test_publish_rejects_confirmation_from_other_window(self) -> None:
        """高 t 值也不能把发现期或其他窗口的诊断替换成正式确认。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            original = results["pilot_fixed_001"]
            assert original.windows is not None
            other_window = _ic(0.1, observed_date=date(2023, 12, 29))
            other_window = other_window.model_copy(
                update={"rank_ic_hac_t": 99.0}
            )
            results["pilot_fixed_001"] = replace(
                original,
                ic=other_window,
                windows=replace(original.windows, confirmation=other_window),
            )

            with self.assertRaisesRegex(FactorMinerError, "确认"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )

    def test_publish_rejects_direction_record_outside_run_state_root(self) -> None:
        """即使 SHA 相同，也不能把任意外部文件冒充本次方向记录。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            original = results["pilot_fixed_001"]
            assert original.windows is not None
            outside = root / "outside-direction.json"
            outside.write_bytes(original.windows.direction_record_path.read_bytes())
            results["pilot_fixed_001"] = replace(
                original,
                windows=replace(
                    original.windows,
                    direction_record_path=outside,
                ),
            )

            with self.assertRaisesRegex(FactorMinerError, "方向"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )

    def test_publish_rejects_records_from_different_run_provenance(self) -> None:
        """同候选与窗口的旧 release/config/policy/source run 记录也不能替换。"""

        for drift in ("release", "config", "policy", "source_run"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidates = load_fixed_candidates(FIXTURE)
                manifest = _input_manifest(root)
                policy = company_a_share_visible_policy()
                request = _request(root)
                source_run_id = None
                if drift == "release":
                    manifest = manifest.model_copy(
                        update={"resolved_release_id": "release-2"}
                    )
                    request = request.model_copy(
                        update={"data_release_id": "release-2"}
                    )
                elif drift == "config":
                    request = request.model_copy(update={"config_hash": "9" * 64})
                elif drift == "policy":
                    policy = policy.model_copy(update={"alpha": 0.01})
                    request = request.model_copy(
                        update={"evaluation_policy_id": evaluation_policy_id(policy)}
                    )
                else:
                    source_run_id = "run_" + "4" * 24

                with self.assertRaisesRegex(FactorMinerError, "provenance"):
                    publish_fixed_pilot_run(
                        candidates=candidates,
                        candidate_results=_results(root),
                        input_manifest=manifest,
                        evaluation_policy=policy,
                        portfolio_policy=PortfolioEvaluationPolicy(),
                        barra_policy=None,
                        request=request,
                        source_run_id=source_run_id,
                    )

    def test_publish_rejects_confirmation_bound_to_other_direction_record(self) -> None:
        """确认记录必须显式绑定同一候选实际方向文件的 SHA。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            candidate = candidates.candidates[0]
            original = results[candidate.candidate_id]
            assert original.windows is not None
            path = root / "state" / "pilot_direction" / "confirmation_other_direction.json"
            provenance = json.loads(
                original.windows.direction_record_path.read_text("utf-8")
            )["provenance"]
            digest = _freeze_confirmation_diagnostics(
                candidate_id=candidate.candidate_id,
                spec_sha256=candidate.spec_hash,
                diagnostics=original.windows.confirmation,
                confirmation_record_path=path,
                protocol=LongOnlyResearchProtocol(),
                provenance=provenance,
                direction_record_sha256="f" * 64,
            )
            results[candidate.candidate_id] = replace(
                original,
                windows=replace(
                    original.windows,
                    confirmation_record_path=path,
                    confirmation_record_sha256=digest,
                ),
            )

            with self.assertRaisesRegex(FactorMinerError, "确认"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )
    def test_publish_rejects_result_ic_different_from_confirmation(self) -> None:
        """公开 IC 与资格使用的确认诊断必须是同一不可变内容。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root)
            original = results["pilot_fixed_001"]
            results["pilot_fixed_001"] = replace(original, ic=_ic(-0.1))

            with self.assertRaisesRegex(FactorMinerError, "确认"):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=results,
                    input_manifest=_input_manifest(root),
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=_request(root),
                )

    def test_publish_uses_full_frozen_family_for_bonferroni(self) -> None:
        """三候选批次也必须使用完整冻结研究族的 Bonferroni 分母。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            results = _results(root, family_size=29)
            candidate_by_id = {
                item.candidate_id: item for item in candidates.candidates
            }
            for candidate_id, original in tuple(results.items()):
                assert original.windows is not None
                confirmation = original.windows.confirmation.model_copy(
                    update={"rank_ic_hac_t": 2.7},
                )
                frozen = _with_frozen_confirmation(
                    root,
                    candidate_by_id[candidate_id],
                    original,
                    confirmation,
                    "family29",
                )
                assert frozen.windows is not None
                results[candidate_id] = replace(
                    frozen,
                    windows=replace(
                        frozen.windows,
                        family_size=29,
                        confirmation_passed=False,
                        stress_test_eligible=False,
                    ),
                )

            publication = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=results,
                input_manifest=_input_manifest(root),
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=_request(root),
                frozen_family_size=29,
            )

            self.assertEqual(publication.manifest.status, "published")

    def test_reevaluation_rejects_source_run_without_exactly_29_specs(self) -> None:
        """正式现有运行复算必须拒绝 1 个或 30 个 Spec 的来源 run。"""

        for count in (1, 30):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_run_id = f"run_{count:024x}"
                source_spec = load_fixed_candidates(FIXTURE).candidates[0].spec
                artifacts = {
                    f"candidates/cand_{index:024x}/spec.json": canonical_json_bytes(
                        source_spec.model_dump(mode="json")
                    )
                    for index in range(count)
                }
                artifacts["run/metrics.json"] = canonical_json_bytes(
                    {
                        "candidate_count": count,
                        "candidates": {
                            f"cand_{index:024x}": {} for index in range(count)
                        },
                    }
                )
                publish_run_artifacts(root, source_run_id, artifacts)

                with self.assertRaisesRegex(FactorMinerError, "29"):
                    reevaluate_existing_pilot_run(
                        artifact_root=root,
                        source_run_id=source_run_id,
                        paths=object(),
                        request=_request(root),
                        runner=lambda **_: object(),
                    )

    def test_reevaluation_rejects_code_commit_and_config_hash_drift(self) -> None:
        """复算请求不能继承与实际部署代码或配置字节不一致的身份。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pilot.json"
            actual_commit = "b" * 40
            request = _request(root).model_copy(
                update={"code_commit": actual_commit}
            )
            payload = {
                "paths": {"deployment": "synthetic"},
                "request": request.model_dump(mode="json"),
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            actual_hash = canonical_pilot_config_hash(config_path)
            payload["request"]["config_hash"] = actual_hash
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            verified_request = PilotRunRequest.model_validate(payload["request"])

            verified = verify_reevaluation_request_identity(
                config_path=config_path,
                request=verified_request,
                actual_code_commit=actual_commit,
            )
            self.assertEqual(verified.code_commit, actual_commit)
            self.assertEqual(verified.config_hash, actual_hash)

            with self.assertRaisesRegex(FactorMinerError, "code_commit"):
                verify_reevaluation_request_identity(
                    config_path=config_path,
                    request=verified_request,
                    actual_code_commit="c" * 40,
                )

            changed = json.loads(config_path.read_text("utf-8"))
            changed["request"]["data_release_id"] = "changed-release"
            config_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(FactorMinerError, "config_hash"):
                verify_reevaluation_request_identity(
                    config_path=config_path,
                    request=verified_request,
                    actual_code_commit=actual_commit,
                )

    def test_deployment_without_git_is_hard_failure(self) -> None:
        """正式复算必须由真实 Git metadata 核验 HEAD。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(FactorMinerError, r"\.git"):
                resolve_pilot_code_commit(root)

    def test_git_mode_rejects_dirty_relevant_source_tree(self) -> None:
        """Git 模式在复算前拒绝相关源码的 tracked 或 untracked 漂移。"""

        for dirty_kind in ("tracked", "untracked"):
            with self.subTest(dirty_kind=dirty_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "src").mkdir()
                (root / "src" / "factor.py").write_text("VALUE = 1\n", encoding="utf-8")
                (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
                (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
                subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
                subprocess.run(["git", "add", "."], cwd=root, check=True)
                subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
                if dirty_kind == "tracked":
                    (root / "src" / "factor.py").write_text("VALUE = 2\n", encoding="utf-8")
                else:
                    (root / "src" / "new.py").write_text("VALUE = 2\n", encoding="utf-8")

                with self.assertRaisesRegex(FactorMinerError, "dirty"):
                    resolve_pilot_code_commit(root)

    def test_loads_all_published_specs_without_changing_source_identity(self) -> None:
        """现有运行的任意数量 Spec 必须按 source_candidate_id 原样进入复算集合。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run_id = "run_" + "9" * 24
            source_spec = load_fixed_candidates(FIXTURE).candidates[0].spec
            source_payload = source_spec.model_dump(mode="json")
            artifacts = {
                f"candidates/cand_{index:024x}/spec.json": canonical_json_bytes(
                    source_payload
                )
                for index in range(29)
            }
            publication = publish_run_artifacts(root, source_run_id, artifacts)
            before = {
                ref.relative_path: ref.sha256
                for ref in publication.artifacts
            }

            candidates = load_published_candidate_specs(root, source_run_id)

            self.assertEqual(len(candidates.candidates), 29)
            self.assertEqual(
                tuple(item.candidate_id for item in candidates.candidates),
                tuple(f"cand_{index:024x}" for index in range(29)),
            )
            self.assertTrue(
                all(
                    item.spec_hash == candidates.candidates[0].spec_hash
                    for item in candidates.candidates
                )
            )
            after = verify_published_run(root, source_run_id)
            self.assertEqual(
                {ref.relative_path: ref.sha256 for ref in after.artifacts},
                before,
            )

    def test_generic_published_spec_loader_accepts_arbitrary_count(self) -> None:
        """通用只读 loader 不承担正式 strict-29 CLI 限制。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run_id = "run_" + "6" * 24
            source_spec = load_fixed_candidates(FIXTURE).candidates[0].spec
            publish_run_artifacts(
                root,
                source_run_id,
                {
                    "candidates/only_one/spec.json": canonical_json_bytes(
                        source_spec.model_dump(mode="json")
                    )
                },
            )

            loaded = load_published_candidate_specs(root, source_run_id)

            self.assertEqual(
                tuple(item.candidate_id for item in loaded.candidates),
                ("only_one",),
            )

    def test_reevaluation_builds_one_new_run_for_all_source_candidates(self) -> None:
        """复算入口不得分裂发布，也不得改写来源候选身份或读取 2012。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run_id = "run_" + "7" * 24
            source_spec = load_fixed_candidates(FIXTURE).candidates[0].spec
            artifacts = {
                f"candidates/cand_{index:024x}/spec.json": canonical_json_bytes(
                    source_spec.model_dump(mode="json")
                )
                for index in range(29)
            }
            artifacts["run/metrics.json"] = canonical_json_bytes(
                {
                    "candidate_count": 29,
                    "candidates": {
                        f"cand_{index:024x}": {} for index in range(29)
                    },
                }
            )
            publish_run_artifacts(root, source_run_id, artifacts)
            calls = []

            def runner(**kwargs):
                calls.append(kwargs)
                return type("Publication", (), {"run_id": "run_" + "8" * 24})()

            publication = reevaluate_existing_pilot_run(
                artifact_root=root,
                source_run_id=source_run_id,
                paths=object(),
                request=_request(root),
                runner=runner,
            )

            self.assertEqual(publication.run_id, "run_" + "8" * 24)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]["candidates"].candidates), 29)
            self.assertEqual(calls[0]["source_run_id"], source_run_id)
            self.assertEqual(calls[0]["request"].visible_start, date(2021, 1, 1))
            self.assertEqual(calls[0]["request"].visible_end, date(2026, 6, 30))

    def test_real_publisher_output_is_valid_strict_29_reevaluation_source(self) -> None:
        """正式发布器生成的 29 候选 run 必须能直接进入复算入口。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_spec = load_fixed_candidates(FIXTURE).candidates[0].spec
            candidates = PilotFixedCandidateFile(
                version="pilot-candidates-v2",
                candidates=tuple(
                    PilotFixedCandidate(
                        candidate_id=f"source_{index:03d}",
                        spec=source_spec,
                    )
                    for index in range(29)
                ),
            )
            source = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=_results(root, candidates=candidates),
                input_manifest=_input_manifest(root),
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=_request(root),
                frozen_family_size=29,
            )
            calls: list[dict[str, object]] = []

            def runner(**kwargs):
                calls.append(kwargs)
                return type(
                    "Publication",
                    (),
                    {"run_id": "run_" + "5" * 24},
                )()

            reevaluated = reevaluate_existing_pilot_run(
                artifact_root=root,
                source_run_id=source.run_id,
                paths=object(),
                request=_request(root),
                runner=runner,
            )

            self.assertEqual(reevaluated.run_id, "run_" + "5" * 24)
            self.assertEqual(len(calls[0]["candidates"].candidates), 29)

    def test_same_run_id_with_changed_content_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            manifest = _input_manifest(root)
            request = _request(root)
            first = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=_results(root),
                input_manifest=manifest,
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=request,
            )
            with self.assertRaises(FactorMinerError):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=_results(root, bad=True),
                    input_manifest=manifest,
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=request,
                    pilot_run_id=first.run_id,
                )

    def test_missing_inputs_fail_and_bad_candidates_still_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = load_fixed_candidates(FIXTURE)
            manifest = _input_manifest(root)
            request = _request(root)
            base = _results(root)
            with self.assertRaises(FactorMinerError) as missing_candidate:
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results={key: value for key, value in base.items() if key != "pilot_fixed_003"},
                    input_manifest=manifest,
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=request,
                )
            self.assertEqual(missing_candidate.exception.code, FailureCode.PILOT_INPUT_CONTRACT_INVALID)

            incomplete = dict(base)
            incomplete["pilot_fixed_001"] = FixedPilotCandidateResult(
                candidate_id="pilot_fixed_001",
                ic=base["pilot_fixed_001"].ic,
                portfolio=None,
                barra=_barra(),
            )
            with self.assertRaises(FactorMinerError):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=incomplete,
                    input_manifest=manifest,
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=request,
                )

            with self.assertRaises(FactorMinerError):
                publish_fixed_pilot_run(
                    candidates=candidates,
                    candidate_results=base,
                    input_manifest=None,
                    evaluation_policy=company_a_share_visible_policy(),
                    portfolio_policy=PortfolioEvaluationPolicy(),
                    barra_policy=None,
                    request=request,
                )

            published = publish_fixed_pilot_run(
                candidates=candidates,
                candidate_results=_results(root, bad=True),
                input_manifest=manifest,
                evaluation_policy=company_a_share_visible_policy(),
                portfolio_policy=PortfolioEvaluationPolicy(),
                barra_policy=None,
                request=request,
            )
            self.assertEqual(published.manifest.status, "published")


if __name__ == "__main__":
    unittest.main()
