"""正式研究族 seal 后评价、统计汇总和发布测试。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest

from factor_miner.dashboard_projection import project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.llm_seal import build_generation_seal
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_state import (
    CandidateSlotState,
    FamilyGenerationState,
    initial_discovery_family_state,
)
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.research_campaign_runner import (
    MappingCampaignPilotRunner,
    evaluate_sealed_campaign,
    publish_campaign_evaluation,
)
from factor_miner.errors import FactorMinerError
from tests.test_llm_schema import dependency, valid_family_spec
from tests.test_llm_seal import seal_inputs


class ResearchCampaignEvaluationTest(unittest.TestCase):
    """正式发布保留 family、seal、完整分母和 Dashboard 元数据。"""

    def test_publish_and_project_preserve_campaign_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            result = evaluate_sealed_campaign(
                seal,
                family,
                MappingCampaignPilotRunner(
                    {
                        "coverage_outcome_llm:001": {
                            "candidate_id": "cand_" + "1" * 24,
                            "candidate_spec_hash": "2" * 64,
                            "expression": {"op": "field", "field": "close"},
                            "rank_ic_sequence": [0.01] * 60,
                            "ic": {
                                "ic_mean": 0.01,
                                "rank_ic_mean": 0.01,
                                "ic_std": 0.02,
                                "rank_ic_std": 0.02,
                                "ic_ir": 0.5,
                                "rank_ic_ir": 0.5,
                                "ic_hac_t": 1.0,
                                "rank_ic_hac_t": 1.0,
                            },
                            "portfolio_daily": [
                                {"Q10_Q1_net_return": 0.01},
                                {"Q10_Q1_net_return": -0.01},
                            ],
                        }
                    }
                ),
            )
            publication = publish_campaign_evaluation(root, family, seal, result)
            manifest = verify_published_run(root, publication.run_id)
            self.assertEqual(manifest.run_id, publication.run_id)
            snapshot = project_run_artifacts(
                root,
                publication.run_id,
                InMemoryDashboardStore(),
            )
            self.assertEqual(
                snapshot.campaign_metadata["registered_slot_count"],
                120,
            )
            self.assertEqual(
                snapshot.campaign_metadata["generation_seal_id"],
                seal.generation_seal_id,
            )

    def test_publish_rejects_evaluated_slot_without_complete_dashboard_metrics(self) -> None:
        """正式研究族缺少 IC、RankIC 或逐期收益时不得发布半空读模型。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family = registered_llm_discovery_family(valid_family_spec())
            base = initial_discovery_family_state(family)
            states = {
                slot_id: CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
                for slot_id in base.candidate_slot_states
            }
            states["coverage_outcome_llm:001"] = CandidateSlotState.READY_FOR_REGISTRATION
            generating = base.model_copy(update={
                "family_generation_state": FamilyGenerationState.GENERATING,
                "candidate_slot_states": states,
            })
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
            result = evaluate_sealed_campaign(
                seal,
                family,
                MappingCampaignPilotRunner({
                    "coverage_outcome_llm:001": {
                        "candidate_id": "cand_" + "1" * 24,
                        "candidate_spec_hash": "2" * 64,
                        "expression": {"op": "field", "field": "close"},
                        "rank_ic_sequence": [0.01] * 60,
                        "ic": {"rank_ic_mean": 0.01},
                    }
                }),
            )
            with self.assertRaises(FactorMinerError):
                publish_campaign_evaluation(root, family, seal, result)


if __name__ == "__main__":
    unittest.main()
