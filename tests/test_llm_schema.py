"""V0.5 发现研究族、数据身份与时间留出合同测试。"""

from datetime import date, datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_schema import (
    CandidateEvaluationDataIdentity,
    DiscoveryArmSpec,
    DiscoveryDataIdentity,
    EvaluationDependencyInterval,
    EvaluationEvidenceTier,
    EvaluationRelationship,
    LLMDiscoveryResearchFamilySpec,
    registered_llm_discovery_family,
    validate_evaluation_relationship,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def discovery_identity(
    *,
    visible_end: date = date(2025, 12, 31),
    latest_information: datetime = datetime(2026, 1, 8, tzinfo=SHANGHAI),
) -> DiscoveryDataIdentity:
    """构造包含五日标签信息边界的合成 discovery identity。"""

    return DiscoveryDataIdentity(
        coverage_graph_ids=("covgraph_" + "1" * 24,),
        data_release_id="synthetic-discovery-v1",
        universe_id="synthetic-a-share",
        evaluation_policy_id="evalpol_" + "2" * 24,
        label_id="label_o2o_5d",
        visible_start=date(2020, 1, 2),
        visible_end=visible_end,
        latest_information_timestamp_used=latest_information,
        source_manifest_sha256="3" * 64,
    )


def evaluation_identity() -> CandidateEvaluationDataIdentity:
    """构造独立候选评价身份。"""

    return CandidateEvaluationDataIdentity(
        data_release_id="synthetic-evaluation-v1",
        universe_id="synthetic-a-share",
        evaluation_policy_id="evalpol_" + "2" * 24,
        label_id="label_o2o_5d",
        planned_formation_start=date(2026, 2, 2),
        planned_formation_end=date(2026, 6, 30),
        source_manifest_sha256="4" * 64,
    )


def arm_specs() -> tuple[DiscoveryArmSpec, ...]:
    """构造固定四臂及其确定性顺序。"""

    return (
        DiscoveryArmSpec(
            arm_id="coverage_outcome_llm",
            generator_kind="llm",
            candidate_slots=30,
        ),
        DiscoveryArmSpec(
            arm_id="literature_only_llm",
            generator_kind="llm",
            candidate_slots=30,
        ),
        DiscoveryArmSpec(
            arm_id="mechanical_mutation",
            generator_kind="deterministic",
            candidate_slots=30,
        ),
        DiscoveryArmSpec(
            arm_id="hypothesis_conditioned_grammar",
            generator_kind="deterministic",
            candidate_slots=30,
        ),
    )


def valid_family_spec() -> LLMDiscoveryResearchFamilySpec:
    """构造完整、纯合成的 V0.5 family。"""

    discovery = discovery_identity()
    return LLMDiscoveryResearchFamilySpec(
        discovery_context_data_identity=discovery,
        candidate_evaluation_data_identity=evaluation_identity(),
        evaluation_relationship=EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
        coverage_graph_ids=discovery.coverage_graph_ids,
        arm_specs=arm_specs(),
        maximum_llm_campaigns=2,
        arm_run_count=4,
        candidate_slots_per_arm=30,
        global_statistical_trial_budget=120,
        multiplicity_policy="bonferroni_over_frozen_family_budget",
        outcome_informed_decision_budget=10,
        family_max_api_requests=90,
        family_max_literature_queries=40,
        family_max_total_input_tokens=2_000_000,
        family_max_total_output_tokens=400_000,
        evaluation_policy_id=discovery.evaluation_policy_id,
        created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


def dependency(
    *,
    candidate_slot_id: str = "coverage_outcome_llm:001",
    factor_input_start: date,
    factor_input_end: date = date(2026, 2, 1),
    label_input_start: date = date(2026, 2, 2),
    label_input_end: date = date(2026, 2, 6),
    candidate_generated_at: datetime = datetime(
        2026, 2, 20, tzinfo=SHANGHAI
    ),
) -> EvaluationDependencyInterval:
    """构造一个候选所依赖的原始输入区间。"""

    return EvaluationDependencyInterval(
        candidate_slot_id=candidate_slot_id,
        factor_input_start=factor_input_start,
        factor_input_end=factor_input_end,
        label_input_start=label_input_start,
        label_input_end=label_input_end,
        candidate_generated_at=candidate_generated_at,
    )


class LLMDiscoverySchemaTest(unittest.TestCase):
    """V0.5 family 必须冻结完整预算与真实信息边界。"""

    def test_family_freezes_four_arms_and_120_slots(self) -> None:
        registered = registered_llm_discovery_family(valid_family_spec())
        repeated = registered_llm_discovery_family(valid_family_spec())

        self.assertEqual(registered, repeated)
        self.assertRegex(
            registered.discovery_family_id,
            r"^llmfamily_[0-9a-f]{24}$",
        )
        self.assertEqual(
            sum(arm.candidate_slots for arm in registered.spec.arm_specs),
            120,
        )

    def test_family_rejects_budget_that_does_not_match_slots(self) -> None:
        payload = valid_family_spec().model_dump()
        payload["global_statistical_trial_budget"] = 119

        with self.assertRaises(ValidationError):
            LLMDiscoveryResearchFamilySpec.model_validate(payload)

    def test_family_rejects_missing_arm(self) -> None:
        payload = valid_family_spec().model_dump()
        payload["arm_specs"] = payload["arm_specs"][:-1]

        with self.assertRaises(ValidationError):
            LLMDiscoveryResearchFamilySpec.model_validate(payload)

    def test_family_rejects_reordered_arms(self) -> None:
        payload = valid_family_spec().model_dump()
        reordered = list(payload["arm_specs"])
        reordered[0], reordered[1] = reordered[1], reordered[0]
        payload["arm_specs"] = tuple(reordered)

        with self.assertRaisesRegex(ValidationError, "固定顺序"):
            LLMDiscoveryResearchFamilySpec.model_validate(payload)

    def test_family_rejects_wrong_campaign_and_slot_counts(self) -> None:
        for field, value in (
            ("maximum_llm_campaigns", 4),
            ("arm_run_count", 3),
            ("candidate_slots_per_arm", 29),
        ):
            with self.subTest(field=field):
                payload = valid_family_spec().model_dump()
                payload[field] = value
                with self.assertRaises(ValidationError):
                    LLMDiscoveryResearchFamilySpec.model_validate(payload)

    def test_strict_holdout_uses_earliest_raw_observation_not_generation_time(
        self,
    ) -> None:
        interval = dependency(factor_input_start=date(2026, 1, 8))

        with self.assertRaises(FactorMinerError) as context:
            validate_evaluation_relationship(
                EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
                discovery_identity(),
                (interval,),
            )

        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
        )
        self.assertIn("2026-01-08", context.exception.message)

    def test_discovery_five_day_label_crossing_boundary_rejects_holdout(
        self,
    ) -> None:
        discovery = discovery_identity(
            visible_end=date(2025, 12, 31),
            latest_information=datetime(2026, 1, 8, tzinfo=SHANGHAI),
        )
        interval = dependency(factor_input_start=date(2026, 1, 5))

        with self.assertRaises(FactorMinerError) as context:
            validate_evaluation_relationship(
                EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
                discovery,
                (interval,),
            )

        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
        )
        self.assertIn("2026-01-05", context.exception.message)
        self.assertIn("2026-01-08", context.exception.message)

    def test_strict_holdout_accepts_only_raw_observations_after_information_cutoff(
        self,
    ) -> None:
        tier = validate_evaluation_relationship(
            EvaluationRelationship.STRICT_TEMPORAL_HOLDOUT,
            discovery_identity(),
            (dependency(factor_input_start=date(2026, 1, 9)),),
        )

        self.assertEqual(tier, EvaluationEvidenceTier.FRESH_VISIBLE_VALIDATION)

    def test_reused_discovery_data_is_explicitly_exploratory(self) -> None:
        tier = validate_evaluation_relationship(
            EvaluationRelationship.REUSED_DISCOVERY_DATA,
            discovery_identity(),
            (dependency(factor_input_start=date(2026, 1, 5)),),
        )

        self.assertEqual(tier, EvaluationEvidenceTier.EXPLORATORY_FILTER_ONLY)

    def test_dependency_interval_rejects_reversed_raw_dates(self) -> None:
        with self.assertRaisesRegex(ValidationError, "因子原始输入区间"):
            dependency(
                factor_input_start=date(2026, 2, 2),
                factor_input_end=date(2026, 2, 1),
            )


if __name__ == "__main__":
    unittest.main()
