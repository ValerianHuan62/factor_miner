"""V0.5 generation seal 与结果端口防火墙测试。"""

from datetime import date
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_schema import (
    EvaluationDependencyInterval,
    EvaluationEvidenceTier,
    EvaluationRelationship,
    registered_llm_discovery_family,
)
from factor_miner.llm_seal import (
    VerifiedDiscoveryProjection,
    authorize_evaluation_open,
    build_generation_seal,
    verify_generation_seal,
)
from factor_miner.llm_state import (
    CandidateSlotState,
    FamilyGenerationState,
    initial_discovery_family_state,
)
from tests.test_llm_schema import SHANGHAI, dependency, valid_family_spec


class OutcomeProbe:
    """记录结果端口是否真正被打开。"""

    def __init__(self) -> None:
        self.open_count = 0

    def open(self) -> str:
        self.open_count += 1
        return "opened"


def terminal_state(*, sealed: bool, incomplete: bool = False):
    """构造 120 槽终态投影，可选择留下一个非终态槽。"""

    family = registered_llm_discovery_family(valid_family_spec())
    state = initial_discovery_family_state(family)
    slots = {
        slot_id: CandidateSlotState.NOT_EXECUTED_INFRASTRUCTURE_TERMINAL
        for slot_id in state.candidate_slot_states
    }
    slots["coverage_outcome_llm:001"] = (
        CandidateSlotState.GENERATION_IN_PROGRESS
        if incomplete
        else CandidateSlotState.READY_FOR_REGISTRATION
    )
    return family, state.model_copy(
        update={
            "family_generation_state": (
                FamilyGenerationState.GENERATION_SEALED
                if sealed
                else FamilyGenerationState.GENERATING
            ),
            "candidate_slot_states": slots,
        }
    )


def hashes(state) -> dict[str, str]:
    """为每个终态槽构造显式 terminal/object hash。"""

    return {
        slot_id: f"{index:064x}"
        for index, slot_id in enumerate(state.candidate_slot_states, start=1)
    }


def seal_inputs() -> dict[str, object]:
    """模拟 outcome 前已经登记的真实 campaign/prompt/generator 身份。"""

    return {
        "llm_campaign_spec_hashes": ("c" * 64, "d" * 64),
        "prompt_bundle_hashes": ("e" * 64, "f" * 64, "1" * 64),
        "model_identity": "deepseek-v4-pro:thinking-high:recorded",
        "generator_identity_hashes": {
            "coverage_outcome_llm": "2" * 64,
            "literature_only_llm": "3" * 64,
            "mechanical_mutation": "4" * 64,
            "hypothesis_conditioned_grammar": "5" * 64,
        },
    }


def interval(start: date) -> EvaluationDependencyInterval:
    """为唯一成功候选构造原始依赖区间。"""

    return dependency(
        factor_input_start=start,
        factor_input_end=date(2026, 2, 1),
        label_input_start=date(2026, 2, 2),
        label_input_end=date(2026, 2, 6),
    )


class LLMSealTest(unittest.TestCase):
    """任何缺口都必须发生在结果端口调用之前。"""

    def test_incomplete_slot_prevents_seal_and_outcome_access(self) -> None:
        family, state = terminal_state(sealed=False, incomplete=True)
        probe = OutcomeProbe()

        with self.assertRaises(FactorMinerError) as context:
            build_generation_seal(
                family,
                state,
                slot_object_hashes=hashes(state),
                evaluation_dependency_intervals=(interval(date(2026, 1, 9)),),
                **seal_inputs(),
            )
        self.assertEqual(context.exception.code, FailureCode.LLM_FAMILY_NOT_SEALED)
        self.assertEqual(probe.open_count, 0)

    def test_missing_slot_object_hash_prevents_seal(self) -> None:
        family, state = terminal_state(sealed=False)
        object_hashes = hashes(state)
        object_hashes.pop("literature_only_llm:030")

        with self.assertRaisesRegex(FactorMinerError, "120"):
            build_generation_seal(
                family,
                state,
                slot_object_hashes=object_hashes,
                evaluation_dependency_intervals=(interval(date(2026, 1, 9)),),
                **seal_inputs(),
            )

    def test_unverified_projection_cannot_open_outcome(self) -> None:
        family, generating = terminal_state(sealed=False)
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(interval(date(2026, 1, 9)),),
            **seal_inputs(),
        )
        _, sealed = terminal_state(sealed=True)
        probe = OutcomeProbe()

        with self.assertRaisesRegex(TypeError, "VerifiedDiscoveryProjection"):
            authorize_evaluation_open(family, sealed, seal, probe.open)
        self.assertEqual(probe.open_count, 0)

    def test_strict_holdout_checks_raw_lookback_before_opening(self) -> None:
        family, generating = terminal_state(sealed=False)
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(interval(date(2026, 1, 8)),),
            **seal_inputs(),
        )
        _, sealed = terminal_state(sealed=True)
        projection = VerifiedDiscoveryProjection(
            state=sealed,
            ledger_tip_hash="a" * 64,
        )
        probe = OutcomeProbe()

        with self.assertRaises(FactorMinerError) as context:
            authorize_evaluation_open(
                family,
                projection,
                seal,
                probe.open,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
        )
        self.assertEqual(probe.open_count, 0)

    def test_valid_strict_seal_opens_outcome_once(self) -> None:
        family, generating = terminal_state(sealed=False)
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(interval(date(2026, 1, 9)),),
            **seal_inputs(),
        )
        self.assertEqual(
            seal.manifest.llm_campaign_spec_hashes,
            ("c" * 64, "d" * 64),
        )
        _, sealed = terminal_state(sealed=True)
        projection = VerifiedDiscoveryProjection(
            state=sealed,
            ledger_tip_hash="a" * 64,
        )
        probe = OutcomeProbe()

        authorization = authorize_evaluation_open(
            family,
            projection,
            seal,
            probe.open,
        )

        self.assertEqual(
            authorization.evidence_tier,
            EvaluationEvidenceTier.FRESH_VISIBLE_VALIDATION,
        )
        self.assertEqual(authorization.outcome_result, "opened")
        self.assertEqual(probe.open_count, 1)

    def test_tampered_slot_state_invalidates_seal(self) -> None:
        family, generating = terminal_state(sealed=False)
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(interval(date(2026, 1, 9)),),
            **seal_inputs(),
        )
        changed = dict(generating.candidate_slot_states)
        changed["coverage_outcome_llm:002"] = CandidateSlotState.GENERATION_FAILED
        tampered = generating.model_copy(update={"candidate_slot_states": changed})

        with self.assertRaisesRegex(FactorMinerError, "seal"):
            verify_generation_seal(family, tampered, seal)

    def test_reused_data_can_only_open_exploratory_tier(self) -> None:
        spec = valid_family_spec().model_copy(
            update={
                "evaluation_relationship": EvaluationRelationship.REUSED_DISCOVERY_DATA
            }
        )
        family = registered_llm_discovery_family(spec)
        _, base = terminal_state(sealed=False)
        generating = base.model_copy(
            update={"discovery_family_id": family.discovery_family_id}
        )
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(interval(date(2026, 1, 5)),),
            **seal_inputs(),
        )
        sealed = generating.model_copy(
            update={"family_generation_state": FamilyGenerationState.GENERATION_SEALED}
        )
        probe = OutcomeProbe()
        authorization = authorize_evaluation_open(
            family,
            VerifiedDiscoveryProjection(
                state=sealed,
                ledger_tip_hash="b" * 64,
            ),
            seal,
            probe.open,
        )

        self.assertEqual(
            authorization.evidence_tier,
            EvaluationEvidenceTier.EXPLORATORY_FILTER_ONLY,
        )


if __name__ == "__main__":
    unittest.main()
