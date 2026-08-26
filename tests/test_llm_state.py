"""V0.5 发现研究族槽位状态机测试。"""

from datetime import datetime, timezone
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_state import (
    CampaignOperationalState,
    CampaignQualityAssessment,
    CandidateSlotState,
    DiscoveryObjectKind,
    FamilyGenerationState,
    HypothesisSlotState,
    LLMDiscoveryEvent,
    apply_discovery_event,
    expected_candidate_slot_ids,
    initial_discovery_family_state,
    project_discovery_family_state,
)
from tests.test_llm_schema import valid_family_spec
from factor_miner.llm_schema import registered_llm_discovery_family


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def event(
    target_kind: DiscoveryObjectKind,
    target_id: str,
    to_state: str,
    *,
    event_id: str,
) -> LLMDiscoveryEvent:
    """构造显式目标状态的合成事件。"""

    return LLMDiscoveryEvent(
        event_id=event_id,
        discovery_family_id=registered_llm_discovery_family(
            valid_family_spec()
        ).discovery_family_id,
        target_kind=target_kind,
        target_id=target_id,
        to_state=to_state,
        created_at=NOW,
    )


class LLMStateTest(unittest.TestCase):
    """对象终态不可逆，family seal 只能按固定路径推进。"""

    def setUp(self) -> None:
        self.family = registered_llm_discovery_family(valid_family_spec())
        self.state = initial_discovery_family_state(self.family)

    def test_family_initializes_exactly_120_stable_candidate_slots(self) -> None:
        expected = expected_candidate_slot_ids(self.family.spec)

        self.assertEqual(len(expected), 120)
        self.assertEqual(tuple(self.state.candidate_slot_states), expected)
        self.assertTrue(
            all(
                state is CandidateSlotState.RESERVED
                for state in self.state.candidate_slot_states.values()
            )
        )

    def test_candidate_cannot_skip_generation_states(self) -> None:
        with self.assertRaises(FactorMinerError) as context:
            apply_discovery_event(
                self.state,
                event(
                    DiscoveryObjectKind.CANDIDATE_SLOT,
                    "coverage_outcome_llm:001",
                    CandidateSlotState.READY_FOR_REGISTRATION,
                    event_id="event-1",
                ),
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_STATE_TRANSITION_INVALID,
        )

    def test_candidate_terminal_state_cannot_be_reversed(self) -> None:
        state = apply_discovery_event(
            self.state,
            event(
                DiscoveryObjectKind.CANDIDATE_SLOT,
                "coverage_outcome_llm:001",
                CandidateSlotState.GENERATION_IN_PROGRESS,
                event_id="event-1",
            ),
        )
        state = apply_discovery_event(
            state,
            event(
                DiscoveryObjectKind.CANDIDATE_SLOT,
                "coverage_outcome_llm:001",
                CandidateSlotState.GENERATION_FAILED,
                event_id="event-2",
            ),
        )

        with self.assertRaisesRegex(FactorMinerError, "终态"):
            apply_discovery_event(
                state,
                event(
                    DiscoveryObjectKind.CANDIDATE_SLOT,
                    "coverage_outcome_llm:001",
                    CandidateSlotState.GENERATION_IN_PROGRESS,
                    event_id="event-3",
                ),
            )

    def test_rejected_hypothesis_cannot_be_approved(self) -> None:
        state = self.state
        for index, next_state in enumerate(
            (
                HypothesisSlotState.GENERATION_IN_PROGRESS,
                HypothesisSlotState.DRAFT_GENERATED,
                HypothesisSlotState.AWAITING_HUMAN_REVIEW,
                HypothesisSlotState.HUMAN_REJECTED,
            ),
            start=1,
        ):
            state = apply_discovery_event(
                state,
                event(
                    DiscoveryObjectKind.HYPOTHESIS_SLOT,
                    "coverage_outcome_llm:H01",
                    next_state,
                    event_id=f"event-{index}",
                ),
            )

        with self.assertRaisesRegex(FactorMinerError, "终态"):
            apply_discovery_event(
                state,
                event(
                    DiscoveryObjectKind.HYPOTHESIS_SLOT,
                    "coverage_outcome_llm:H01",
                    HypothesisSlotState.HUMAN_APPROVED,
                    event_id="event-5",
                ),
            )

    def test_quality_assessment_is_independent_of_operational_state(self) -> None:
        state = apply_discovery_event(
            self.state,
            event(
                DiscoveryObjectKind.CAMPAIGN_OPERATIONAL,
                "coverage_outcome_llm",
                CampaignOperationalState.BRIEF_VERIFIED,
                event_id="event-1",
            ),
        )
        state = apply_discovery_event(
            state,
            event(
                DiscoveryObjectKind.CAMPAIGN_QUALITY,
                "coverage_outcome_llm",
                CampaignQualityAssessment.FAILED,
                event_id="event-2",
            ),
        )

        self.assertEqual(
            state.campaign_operational_states["coverage_outcome_llm"],
            CampaignOperationalState.BRIEF_VERIFIED,
        )
        self.assertEqual(
            state.campaign_quality_assessments["coverage_outcome_llm"],
            CampaignQualityAssessment.FAILED,
        )

    def test_unknown_slot_and_duplicate_event_are_rejected(self) -> None:
        unknown = event(
            DiscoveryObjectKind.CANDIDATE_SLOT,
            "coverage_outcome_llm:031",
            CandidateSlotState.GENERATION_IN_PROGRESS,
            event_id="event-1",
        )
        with self.assertRaises(FactorMinerError):
            apply_discovery_event(self.state, unknown)

        valid = event(
            DiscoveryObjectKind.FAMILY,
            self.family.discovery_family_id,
            FamilyGenerationState.GENERATING,
            event_id="event-2",
        )
        state = apply_discovery_event(self.state, valid)
        with self.assertRaisesRegex(FactorMinerError, "重复"):
            apply_discovery_event(state, valid)

    def test_superseding_event_cannot_change_research_state(self) -> None:
        first = event(
            DiscoveryObjectKind.CANDIDATE_SLOT,
            "mechanical_mutation:001",
            CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
            event_id="event-1",
        )
        state = apply_discovery_event(self.state, first)
        correction = event(
            DiscoveryObjectKind.CANDIDATE_SLOT,
            "mechanical_mutation:001",
            CandidateSlotState.GENERATION_IN_PROGRESS,
            event_id="event-2",
        ).model_copy(update={"supersedes_event_id": first.event_id})

        with self.assertRaisesRegex(FactorMinerError, "纠错事件"):
            apply_discovery_event(state, correction)

    def test_projection_is_deterministic(self) -> None:
        events = (
            event(
                DiscoveryObjectKind.FAMILY,
                self.family.discovery_family_id,
                FamilyGenerationState.GENERATING,
                event_id="event-1",
            ),
            event(
                DiscoveryObjectKind.CANDIDATE_SLOT,
                "mechanical_mutation:001",
                CandidateSlotState.NOT_EXECUTED_HYPOTHESIS_REJECTED,
                event_id="event-2",
            ),
        )

        first = project_discovery_family_state(self.family, events)
        second = project_discovery_family_state(self.family, events)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
