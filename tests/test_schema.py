from datetime import datetime
import unittest

from pydantic import ValidationError

from factor_miner.canonical import sha256_json
from factor_miner.schema import (
    EvaluationPolicySpec,
    IncrementalEvaluationPolicySpec,
    HypothesisSlot,
    CandidateFactorSpec,
    MechanismStatus,
    ReferenceFactorLibrarySpec,
    ResearchFamilySpec,
    TrustedCandidateFactorSpec,
    TrustedVisibleCampaignSpec,
    campaign_id,
    evaluation_policy_id,
    reference_factor_library_id,
    registered_research_family,
    registered_reference_factor_library,
    registered_candidate,
    validate_incremental_policy_library,
    validate_trusted_campaign,
)
from factor_miner.policy import company_a_share_visible_policy
from tests.helpers import (
    valid_campaign,
    valid_candidate,
    valid_registered_candidate,
    valid_registered_trusted_candidate,
    valid_research_family,
    valid_reference_factor_library,
    valid_registered_reference_factor_library,
    valid_incremental_policy,
    valid_trusted_candidate,
    valid_trusted_campaign,
)


class SchemaTest(unittest.TestCase):
    """假设、候选和 campaign schema 测试。"""

    def test_candidate_id_is_content_addressed(self) -> None:
        """验证相同 spec 生成相同 candidate ID 和 spec hash。"""
        left = registered_candidate(valid_candidate())
        right = registered_candidate(valid_candidate())
        self.assertEqual(left.candidate_id, right.candidate_id)
        self.assertEqual(left.spec_hash, right.spec_hash)
        self.assertTrue(left.candidate_id.startswith("cand_"))
        self.assertEqual(len(left.candidate_id), len("cand_") + 24)

    def test_missing_independent_verification_is_rejected(self) -> None:
        """验证缺少独立机制验证时拒绝候选。"""
        payload = valid_candidate().model_dump(mode="json")
        payload["hypothesis"]["independent_verification"] = ""
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_campaign_budget_covers_registered_candidates(self) -> None:
        """验证 max_hypotheses 必须覆盖所有候选 slot。"""
        payload = valid_campaign().model_dump(mode="json")
        payload["max_hypotheses"] = 0
        with self.assertRaises(ValidationError):
            type(valid_campaign()).model_validate(payload)

    def test_extra_fields_are_forbidden(self) -> None:
        """验证 schema 不接受未声明字段。"""
        payload = valid_candidate().model_dump(mode="json")
        payload["unexpected"] = "禁止"
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_naive_created_at_is_rejected(self) -> None:
        """验证 created_at 必须包含时区。"""
        payload = valid_candidate().model_dump(mode="json")
        payload["created_at"] = datetime(2026, 7, 15, 9, 0).isoformat()
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_candidate_id_is_not_accepted_from_input(self) -> None:
        """验证 CandidateFactorSpec 不接受人工指定的 candidate ID。"""
        payload = valid_candidate().model_dump(mode="json")
        payload["candidate_id"] = "cand_人工指定"
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_invalid_alpha_is_rejected(self) -> None:
        """验证 alpha 必须位于开区间零到一之间。"""
        payload = valid_campaign().model_dump(mode="json")
        payload["alpha"] = 1.5
        with self.assertRaises(ValidationError):
            type(valid_campaign()).model_validate(payload)

    def test_duplicate_candidate_ids_are_rejected(self) -> None:
        """验证 campaign 不接受重复候选 ID。"""
        candidate_id = valid_registered_candidate().candidate_id
        payload = valid_campaign().model_dump(mode="json")
        payload["candidate_ids"] = [candidate_id, candidate_id]
        payload["max_hypotheses"] = 2
        with self.assertRaises(ValidationError):
            type(valid_campaign()).model_validate(payload)

    def test_post_hoc_text_cannot_be_added_to_hypothesis(self) -> None:
        """验证结果后解释字段不能覆盖或混入事前假设。"""
        payload = valid_candidate().model_dump(mode="json")
        payload["hypothesis"]["post_hoc_explanation"] = "结果出来后的解释"
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_hypothesis_is_frozen(self) -> None:
        """验证候选登记后不能原地改写假设。"""
        candidate = valid_candidate()
        with self.assertRaises(ValidationError):
            candidate.hypothesis = candidate.hypothesis.model_copy(
                update={"claim": "结果后改写"}
            )

    def test_mechanism_status_is_unverified(self) -> None:
        """验证 V0 只允许机制未验证状态。"""
        candidate = valid_candidate()
        self.assertEqual(
            candidate.hypothesis.mechanism_status,
            MechanismStatus.MECHANISM_UNVERIFIED,
        )
        payload = candidate.model_dump(mode="json")
        payload["hypothesis"]["mechanism_status"] = "supported"
        with self.assertRaises(ValidationError):
            CandidateFactorSpec.model_validate(payload)

    def test_round_trip_has_no_hash_drift(self) -> None:
        """验证规范化 JSON 往返序列化不改变候选哈希。"""
        candidate = valid_candidate()
        restored = CandidateFactorSpec.model_validate(candidate.model_dump(mode="json"))
        self.assertEqual(
            sha256_json(candidate.model_dump(mode="json")),
            sha256_json(restored.model_dump(mode="json")),
        )

    def test_campaign_order_is_preserved_and_hash_is_stable(self) -> None:
        """验证 campaign 候选顺序保留且 campaign ID 稳定。"""
        campaign = valid_campaign()
        restored = type(campaign).model_validate(campaign.model_dump(mode="json"))
        self.assertEqual(campaign.candidate_ids, restored.candidate_ids)
        self.assertEqual(campaign_id(campaign), campaign_id(restored))

    def test_trusted_candidate_rejects_free_text_availability(self) -> None:
        """V0.1 候选必须使用 typed availability。"""

        payload = valid_trusted_candidate().model_dump(mode="json")
        payload["availability"] = "next_open"
        with self.assertRaises(ValidationError):
            TrustedCandidateFactorSpec.model_validate(payload)

    def test_trusted_campaign_cannot_override_policy_fields(self) -> None:
        """trusted campaign 不能自行提供 label、mask 或 alpha。"""

        for key, value in (
            ("label_column", "close"),
            ("rank_mask_column", "valid_for_trading"),
            ("alpha", 1.0),
        ):
            payload = valid_trusted_campaign().model_dump(mode="json")
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                TrustedVisibleCampaignSpec.model_validate(payload)

    def test_builtin_policy_freezes_company_visible_semantics(self) -> None:
        """代码内置 policy 必须冻结标签、时点和推断口径。"""

        policy = company_a_share_visible_policy()
        self.assertEqual(policy.label_column, "label_o2o_5d")
        self.assertEqual(policy.rank_mask_column, "valid_for_factor_rank")
        self.assertEqual(policy.alpha, 0.05)
        self.assertEqual(policy.min_abs_mean_rank_ic, 0.01)
        self.assertEqual(policy.neutralization, "none")
        self.assertEqual(policy.availability.earliest_trade, "open_t_plus_1")
        self.assertTrue(policy.universe.point_in_time)
        self.assertTrue(evaluation_policy_id(policy).startswith("evalpol_"))

    def test_policy_rejects_non_point_in_time_universe(self) -> None:
        """评价 policy 不能接受期末成分股式 universe。"""

        payload = company_a_share_visible_policy().model_dump(mode="json")
        payload["universe"]["point_in_time"] = False
        with self.assertRaises(ValidationError):
            EvaluationPolicySpec.model_validate(payload)

    def test_family_budget_and_slots_are_fail_closed(self) -> None:
        """全局预算必须覆盖 slot，slot 编号和候选都不能重复。"""

        candidate_id = valid_registered_trusted_candidate().candidate_id
        base = valid_research_family().model_dump(mode="json")
        base["global_hypothesis_budget"] = 1
        base["slots"] = [
            {"slot_number": 1, "candidate_id": candidate_id},
            {"slot_number": 2, "candidate_id": f"cand_{'f' * 24}"},
        ]
        with self.assertRaises(ValidationError):
            ResearchFamilySpec.model_validate(base)

        duplicate = valid_research_family().model_dump(mode="json")
        duplicate["slots"] = [
            {"slot_number": 1, "candidate_id": candidate_id},
            {"slot_number": 1, "candidate_id": f"cand_{'f' * 24}"},
        ]
        with self.assertRaises(ValidationError):
            ResearchFamilySpec.model_validate(duplicate)

    def test_registered_family_is_content_addressed(self) -> None:
        """research family ID 和 hash 必须由 canonical spec 派生。"""

        left = registered_research_family(valid_research_family())
        right = registered_research_family(valid_research_family())
        self.assertEqual(left, right)
        self.assertTrue(left.research_family_id.startswith("family_"))

    def test_trusted_campaign_requires_matching_policy_family_and_candidates(self) -> None:
        """跨文档校验必须拒绝错 policy、漏 slot 和 legacy candidate。"""

        campaign = valid_trusted_campaign()
        family = registered_research_family(valid_research_family())
        policy = company_a_share_visible_policy()
        trusted = valid_registered_trusted_candidate()
        self.assertIs(
            validate_trusted_campaign(
                campaign,
                family,
                policy,
                {trusted.candidate_id: trusted},
            ),
            campaign,
        )

        wrong_policy = EvaluationPolicySpec.model_validate(
            policy.model_copy(update={"label_formula_version": "wrong"}).model_dump(mode="json")
        )
        with self.assertRaises(ValueError):
            validate_trusted_campaign(
                campaign,
                family,
                wrong_policy,
                {trusted.candidate_id: trusted},
            )

        legacy = valid_registered_candidate()
        with self.assertRaises(ValueError):
            validate_trusted_campaign(
                campaign,
                family,
                policy,
                {campaign.candidate_ids[0]: legacy},
            )

    def test_reference_factor_library_is_content_addressed_and_ordered(self) -> None:
        """参考库身份必须绑定有序、唯一且非空的参考因子集合。"""

        left = valid_registered_reference_factor_library()
        right = registered_reference_factor_library(valid_reference_factor_library())
        self.assertEqual(left, right)
        self.assertEqual(
            left.reference_factor_library_id,
            reference_factor_library_id(left.spec),
        )
        self.assertTrue(left.reference_factor_library_id.startswith("reflib_"))

        duplicate = valid_reference_factor_library().model_dump(mode="json")
        duplicate["reference_factor_ids"] = ["reference_value", "reference_value"]
        with self.assertRaises(ValidationError):
            ReferenceFactorLibrarySpec.model_validate(duplicate)

        empty = valid_reference_factor_library().model_dump(mode="json")
        empty["reference_factor_ids"] = []
        with self.assertRaises(ValidationError):
            ReferenceFactorLibrarySpec.model_validate(empty)

        missing_manifest_hash = valid_reference_factor_library().model_dump(
            mode="json"
        )
        missing_manifest_hash.pop("reference_manifest_sha256", None)
        with self.assertRaises(ValidationError):
            ReferenceFactorLibrarySpec.model_validate(missing_manifest_hash)

    def test_incremental_policy_requires_matching_frozen_library(self) -> None:
        """V0.2 政策的清单和参考顺序必须与内容寻址参考库一致。"""

        policy = valid_incremental_policy()
        library = valid_registered_reference_factor_library()
        self.assertEqual(policy.policy_version, "2")
        self.assertIsNone(validate_incremental_policy_library(policy, library))

        wrong_order = library.spec.model_copy(
            update={
                "reference_factor_ids": tuple(
                    reversed(library.spec.reference_factor_ids)
                )
            }
        )
        wrong_library = registered_reference_factor_library(wrong_order)
        with self.assertRaisesRegex(ValueError, "参考因子"):
            validate_incremental_policy_library(policy, wrong_library)

        payload = policy.model_dump(mode="json")
        payload.pop("orthogonalization")
        with self.assertRaises(ValidationError):
            IncrementalEvaluationPolicySpec.model_validate(payload)

    def test_v01_policy_content_identity_is_unchanged(self) -> None:
        """新增 V0.2 类型不能改变已经冻结的 V0.1 policy ID。"""

        self.assertEqual(
            evaluation_policy_id(company_a_share_visible_policy()),
            "evalpol_cd9a83fb7dcaece5139e5353",
        )
