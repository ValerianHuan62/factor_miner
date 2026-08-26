"""阶段 C 研究族评价编排的合成测试。"""

from datetime import date
import unittest

from factor_miner.llm_seal import build_generation_seal
from factor_miner.llm_state import (
    CandidateSlotState,
    FamilyGenerationState,
    initial_discovery_family_state,
)
from factor_miner.research_campaign_runner import (
    MappingCampaignPilotRunner,
    evaluate_sealed_campaign,
)
from factor_miner.statistics import FrozenStatisticalPolicy
from tests.test_llm_schema import dependency, valid_family_spec
from factor_miner.llm_schema import registered_llm_discovery_family
from tests.test_llm_seal import seal_inputs


class ResearchCampaignRunnerTest(unittest.TestCase):
    """正式评价必须保留完整分母并先通过 seal。"""

    def _sealed_family(self):
        family = registered_llm_discovery_family(valid_family_spec())
        base = initial_discovery_family_state(family)
        states = {
            slot_id: CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
            for slot_id in base.candidate_slot_states
        }
        states["coverage_outcome_llm:001"] = CandidateSlotState.READY_FOR_REGISTRATION
        generating = base.model_copy(
            update={
                "family_generation_state": FamilyGenerationState.GENERATING,
                "candidate_slot_states": states,
            }
        )
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes={
                slot_id: f"{index:064x}"
                for index, slot_id in enumerate(states, start=1)
            },
            evaluation_dependency_intervals=(
                dependency(
                    candidate_slot_id="coverage_outcome_llm:001",
                    factor_input_start=date(2026, 1, 9),
                ),
            ),
            **seal_inputs(),
        )
        return family, seal

    def test_all_120_slots_remain_in_denominator_when_only_one_is_evaluated(self) -> None:
        family, seal = self._sealed_family()
        values = tuple(0.01 + (index % 3) * 0.001 for index in range(60))
        result = evaluate_sealed_campaign(
            seal,
            family,
            MappingCampaignPilotRunner(
                {
                    "coverage_outcome_llm:001": {
                        "candidate_id": "cand_" + "1" * 24,
                        "candidate_spec_hash": "2" * 64,
                        "expression": {"op": "field", "field": "close"},
                        "rank_ic_sequence": values,
                    }
                }
            ),
            statistical_policy=FrozenStatisticalPolicy.formal_120(
                policy_id=family.spec.evaluation_policy_id,
                min_valid_dates=60,
            ),
        )

        self.assertEqual(result.registered_slot_count, 120)
        self.assertEqual(result.bonferroni_denominator, 120)
        self.assertEqual(result.evaluated_slot_count, 1)
        self.assertEqual(result.failed_slot_count, 119)
        self.assertEqual(len(result.slot_evaluations), 120)
        self.assertIsNotNone(result.slot_evaluations[0].hac)

    def test_duplicate_ast_is_retained_as_failed_slot(self) -> None:
        family, seal = self._sealed_family()
        states = dict(seal.manifest.candidate_slot_states)
        states["coverage_outcome_llm:002"] = CandidateSlotState.READY_FOR_REGISTRATION
        # 重新构造一个两个 ready 槽的 seal，确保重复检查在正式端口执行前发生。
        generating = initial_discovery_family_state(family).model_copy(
            update={
                "family_generation_state": FamilyGenerationState.GENERATING,
                "candidate_slot_states": states,
            }
        )
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=seal.manifest.slot_object_hashes,
            evaluation_dependency_intervals=(
                dependency(
                    candidate_slot_id="coverage_outcome_llm:001",
                    factor_input_start=date(2026, 1, 9),
                ),
                dependency(
                    candidate_slot_id="coverage_outcome_llm:002",
                    factor_input_start=date(2026, 1, 9),
                ),
            ),
            **seal_inputs(),
        )
        values = tuple(0.01 for _ in range(60))
        results = {
            slot_id: {
                "candidate_id": "cand_" + str(index) * 24,
                "expression": {"op": "field", "field": "close"},
                "rank_ic_sequence": values,
            }
            for index, slot_id in enumerate(
                ("coverage_outcome_llm:001", "coverage_outcome_llm:002"),
                start=1,
            )
        }
        result = evaluate_sealed_campaign(seal, family, MappingCampaignPilotRunner(results))
        duplicate = result.slot_evaluations[1]
        self.assertEqual(duplicate.status, "redundant")
        self.assertIn("规范 AST", duplicate.failure_reason or "")
        self.assertEqual(result.bonferroni_denominator, 120)


if __name__ == "__main__":
    unittest.main()
