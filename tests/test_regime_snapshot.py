from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.data_source import InputProvenance
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_research import MonthlyCandidateResult, run_regime_research
from factor_miner.regime_schema import (
    RegimeAnnotation,
    RegimeDeploymentSpec,
    ReturnAggregation,
    registered_regime_deployment,
)
from factor_miner.regime_snapshot import (
    build_filtered_regime_frame,
    publish_regime_snapshot,
    verify_regime_snapshot,
)
from tests.test_regime_research import reduced_research_spec, synthetic_feature_frame


def input_provenance() -> InputProvenance:
    """返回快照身份使用的完整合成数据来源。"""

    return InputProvenance(
        data_origin="synthetic-test",
        resolved_release_id="release-regime-1",
        release_manifest_sha256="d" * 64,
        schema_version="regime-test-v1",
        market_cutoff="2018-08-12",
        adjustment_convention="unadjusted",
        calendar_version="calendar-regime-v1",
        state_table_version="state-regime-v1",
        state_table_cutoff="2018-08-12",
        code_commit="e" * 40,
        config_hash="f" * 64,
    )


def research_fixture():
    """运行一个单配置研究并构造与其严格匹配的正式部署。"""

    features = synthetic_feature_frame()
    spec = reduced_research_spec(features.get_column("date")[-1])
    report = run_regime_research({ReturnAggregation.MEDIAN: features}, spec)
    configuration = report.configurations[0]
    deployment = RegimeDeploymentSpec(
        regime_research_id=report.regime_research_id,
        research_candidate_id=configuration.candidate_id,
        state_count=configuration.state_count,
        return_aggregation=configuration.return_aggregation,
        training_window=configuration.training_window,
        covariance_kind=configuration.covariance_kind,
        seeds=spec.seeds,
        n_iter=spec.n_iter,
        tol=spec.tol,
        min_covar=spec.min_covar,
        min_daily_assets=spec.min_daily_assets,
        feature_policy=spec.feature_policy,
        quality_policy=spec.quality_policy,
        deployment_start=report.monthly_results[0].inference_start,
        visible_cutoff=report.monthly_results[0].training_end,
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    return features, spec, report, registered_regime_deployment(deployment)


class RegimeSnapshotTest(unittest.TestCase):
    """正式状态表、不可变快照和篡改检测测试。"""

    def test_failed_month_is_unavailable_and_never_reuses_probabilities(self) -> None:
        """失败月份必须保留日期但不能回退 K 或沿用上月状态。"""

        features, _, report, deployment = research_fixture()
        successful = next(item for item in report.monthly_results if item.status == "ok")
        next_start = successful.inference_end + timedelta(days=1)
        unavailable = MonthlyCandidateResult(
            candidate_id=successful.candidate_id,
            model_month="2099-01",
            status="unavailable",
            training_start=successful.training_start,
            training_end=successful.training_end,
            inference_start=next_start,
            inference_end=next_start,
            failure_code=FailureCode.REGIME_FIT_NOT_CONVERGED.value,
            failure_message="合成失败",
            fit=None,
            standardizer=None,
            quality=None,
            mapping=None,
            inference_dates=(next_start,),
            raw_probabilities=None,
            canonical_probabilities=None,
            oos_log_likelihood_per_observation=None,
            minimum_bhattacharyya=None,
        )
        calendar = tuple(features.get_column("date").to_list()) + (
            next_start,
            next_start + timedelta(days=1),
        )
        frame = build_filtered_regime_frame(
            (successful, unavailable),
            calendar_dates=calendar,
            state_count=deployment.spec.state_count,
        )
        failed = frame.filter(pl.col("status") == "unavailable").row(0, named=True)
        self.assertIsNone(failed["raw_state_id"])
        self.assertIsNone(failed["canonical_state_id"])
        self.assertIsNone(failed["canonical_state_probabilities"])
        self.assertEqual(
            failed["failure_code"],
            FailureCode.REGIME_FIT_NOT_CONVERGED.value,
        )
        self.assertNotIn("smoothed_state_probability", frame.columns)
        ok = frame.filter(pl.col("status") == "ok")
        self.assertTrue(
            all(
                len(values) == deployment.spec.state_count
                for values in ok.get_column("canonical_state_probabilities").to_list()
            )
        )

    def test_snapshot_is_content_addressed_idempotent_and_complete(self) -> None:
        """相同字节必须得到同一快照，且七个正式文件一次性发布。"""

        features, _, report, deployment = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            second = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            self.assertEqual(first.regime_snapshot_id, second.regime_snapshot_id)
            self.assertEqual(
                {path.name for path in first.snapshot_root.iterdir()},
                {
                    "manifest.json",
                    "deployment_spec.json",
                    "monthly_models.jsonl",
                    "filtered_regimes.parquet",
                    "market_features.parquet",
                    "quality_diagnostics.json",
                    "annotations.jsonl",
                },
            )
            verified = verify_regime_snapshot(first.snapshot_root)
            self.assertEqual(verified.regime_snapshot_id, first.regime_snapshot_id)

    def test_tampering_is_detected_and_annotation_changes_identity(self) -> None:
        """概率文件篡改必须失败；人工解释也属于快照内容身份。"""

        features, _, report, deployment = research_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            annotation = RegimeAnnotation(
                regime_snapshot_id=plain.regime_snapshot_id,
                canonical_state_id=0,
                economic_label="合成低收益状态",
                description="仅用于解释，不声明因果。",
                author="researcher",
                created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            )
            annotated = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=input_provenance(),
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(annotation,),
            )
            self.assertNotEqual(
                plain.regime_snapshot_id,
                annotated.regime_snapshot_id,
            )

            filtered_path = plain.snapshot_root / "filtered_regimes.parquet"
            filtered_path.write_bytes(filtered_path.read_bytes() + b"tampered")
            with self.assertRaises(FactorMinerError) as raised:
                verify_regime_snapshot(plain.snapshot_root)
            self.assertEqual(raised.exception.code, FailureCode.LEDGER_CORRUPT)


if __name__ == "__main__":
    unittest.main()
