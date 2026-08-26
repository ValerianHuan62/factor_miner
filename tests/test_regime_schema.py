from datetime import date, datetime, timezone
import unittest

from pydantic import ValidationError

from factor_miner.regime_schema import (
    RegimeAnnotation,
    RegimeDeploymentSpec,
    RegimeDiagnosticSeriesManifest,
    RegimeFeaturePolicy,
    RegimeQualityPolicy,
    RegimeResearchSpec,
    RegisteredRegimeResearch,
    TrainingWindow,
    regime_deployment_id,
    regime_research_id,
    registered_regime_deployment,
    registered_regime_research,
)


def valid_research_spec() -> RegimeResearchSpec:
    """返回测试使用的完整冻结研究合同。"""

    return RegimeResearchSpec(
        candidate_state_counts=(2, 3, 4),
        return_aggregations=("median", "equal_weight"),
        training_windows=("expanding", "rolling_5y", "rolling_8y"),
        covariance_kinds=("diag", "full"),
        seeds=(11, 23, 47, 71, 101),
        n_iter=500,
        tol=1e-4,
        min_covar=1e-6,
        min_daily_assets=500,
        min_oos_months=24,
        min_successful_month_ratio=0.90,
        feature_policy=RegimeFeaturePolicy(),
        quality_policy=RegimeQualityPolicy(),
        research_start=date(2010, 1, 1),
        research_end=date(2026, 6, 30),
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


def valid_deployment_spec() -> RegimeDeploymentSpec:
    """返回引用合法研究空间的固定三状态部署合同。"""

    research = registered_regime_research(valid_research_spec())
    return RegimeDeploymentSpec(
        regime_research_id=research.regime_research_id,
        research_candidate_id="regcand_" + "a" * 24,
        state_count=3,
        return_aggregation="median",
        training_window="rolling_5y",
        covariance_kind="diag",
        seeds=research.spec.seeds,
        n_iter=research.spec.n_iter,
        tol=research.spec.tol,
        min_covar=research.spec.min_covar,
        min_daily_assets=research.spec.min_daily_assets,
        feature_policy=research.spec.feature_policy,
        quality_policy=research.spec.quality_policy,
        deployment_start=date(2026, 7, 1),
        visible_cutoff=date(2026, 6, 30),
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


class RegimeSchemaTest(unittest.TestCase):
    """V0.3 市场状态不可变合同测试。"""

    def test_research_identity_changes_with_frozen_decisions(self) -> None:
        """修改任何模型选择或可见截止日都必须改变研究身份。"""

        base = valid_research_spec()
        restored = RegimeResearchSpec.model_validate(base.model_dump(mode="json"))
        self.assertEqual(regime_research_id(base), regime_research_id(restored))
        self.assertEqual(registered_regime_research(base), registered_regime_research(restored))
        self.assertTrue(regime_research_id(base).startswith("regresearch_"))

        changes = (
            {"candidate_state_counts": (2, 3)},
            {"seeds": (11, 23, 47)},
            {"training_windows": (TrainingWindow.ROLLING_5Y,)},
            {"min_oos_months": 36},
            {"research_end": date(2026, 5, 31)},
        )
        for change in changes:
            with self.subTest(change=change):
                modified = base.model_copy(update=change)
                self.assertNotEqual(regime_research_id(base), regime_research_id(modified))

    def test_research_rejects_unordered_or_duplicate_search_space(self) -> None:
        """研究空间必须有序、唯一且只能比较 K=2、3、4。"""

        base = valid_research_spec().model_dump(mode="json")
        for key, value in (
            ("candidate_state_counts", [3, 2]),
            ("candidate_state_counts", [2, 2, 3]),
            ("seeds", [11, 11]),
            ("return_aggregations", ["median", "median"]),
            ("covariance_kinds", ["spherical"]),
        ):
            payload = dict(base)
            payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                RegimeResearchSpec.model_validate(payload)

    def test_registered_research_rejects_content_address_mismatch(self) -> None:
        """登记记录中的完整哈希和短 ID 不能与 Spec 内容脱节。"""

        registered = registered_regime_research(valid_research_spec())
        payload = registered.model_dump(mode="json")
        payload["spec_hash"] = "f" * 64
        with self.assertRaises(ValidationError):
            RegisteredRegimeResearch.model_validate(payload)

    def test_naive_time_and_reverse_dates_are_rejected(self) -> None:
        """研究和部署都必须具备时区及正向日期边界。"""

        research = valid_research_spec().model_dump(mode="json")
        research["created_at"] = "2026-07-29T00:00:00"
        with self.assertRaises(ValidationError):
            RegimeResearchSpec.model_validate(research)

        deployment = valid_deployment_spec().model_dump(mode="json")
        deployment["visible_cutoff"] = "2026-07-31"
        deployment["deployment_start"] = "2026-07-01"
        with self.assertRaises(ValidationError):
            RegimeDeploymentSpec.model_validate(deployment)

    def test_deployment_has_one_fixed_configuration(self) -> None:
        """正式合同只能冻结一个 K、收益、窗口和 covariance。"""

        deployment = valid_deployment_spec()
        restored = RegimeDeploymentSpec.model_validate(
            deployment.model_dump(mode="json")
        )
        self.assertEqual(regime_deployment_id(deployment), regime_deployment_id(restored))
        self.assertEqual(
            registered_regime_deployment(deployment),
            registered_regime_deployment(restored),
        )
        self.assertTrue(regime_deployment_id(deployment).startswith("regdeploy_"))

        payload = deployment.model_dump(mode="json")
        payload["state_count"] = [2, 3, 4]
        with self.assertRaises(ValidationError):
            RegimeDeploymentSpec.model_validate(payload)

    def test_quality_policy_freezes_reviewed_numeric_rules(self) -> None:
        """默认质量政策必须表达已批准的数值拒绝和警告边界。"""

        policy = RegimeQualityPolicy()
        self.assertEqual(policy.occupancy_hard_min, 0.03)
        self.assertEqual(policy.occupancy_warning_min, 0.08)
        self.assertEqual(policy.core_duration_hard_min, 3.0)
        self.assertEqual(policy.weighted_duration_warning_min, 5.0)
        self.assertEqual(policy.similarity_warning_distance, 0.15)
        self.assertEqual(policy.redundancy_distance_max, 0.05)
        self.assertEqual(
            sum(policy.mapping_weights.model_dump(mode="python").values()),
            1.0,
        )

    def test_diagnostic_manifest_and_annotation_are_content_strict(self) -> None:
        """外部诊断和人工解释必须带版本、截止日和明确引用。"""

        manifest = RegimeDiagnosticSeriesManifest(
            series_id="ridge3w_return",
            source_system="approved_external_research",
            data_release_id="release_202607",
            algorithm_version="ridge3w_v1",
            cutoff=date(2026, 6, 30),
            availability="realized_during_t",
            file_sha256="b" * 64,
            created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(manifest.availability, "realized_during_t")

        annotation = RegimeAnnotation(
            regime_snapshot_id="regsnap_" + "c" * 24,
            canonical_state_id=1,
            economic_label="高波动压力",
            description="波动率高、市场宽度弱，但不声明因果。",
            author="researcher",
            created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(annotation.canonical_state_id, 1)

        invalid = manifest.model_dump(mode="json")
        invalid["file_sha256"] = "not-a-hash"
        with self.assertRaises(ValidationError):
            RegimeDiagnosticSeriesManifest.model_validate(invalid)


if __name__ == "__main__":
    unittest.main()
