"""V0.5 外发信息分类与规范字节扫描测试。"""

from datetime import datetime, timedelta, timezone
import unittest
from pydantic import ValidationError

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_privacy import (
    CorporateExternalResearchPolicy,
    build_export_preview,
    registered_corporate_external_research_policy,
    scan_export_payload,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def policy(*classes: str) -> CorporateExternalResearchPolicy:
    """构造不含个人身份的合成公司外发政策。"""

    return registered_corporate_external_research_policy(
        provider="deepseek",
        allowed_endpoint="https://api.deepseek.com/chat/completions",
        allowed_information_classes=tuple(sorted(classes)),
        forbidden_information_classes=("raw_market_data", "real_factor_identity"),
        allowed_models=("deepseek-v4-pro",),
        maximum_authorized_campaigns=2,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        approver_role="research_owner",
        approval_reference="synthetic-policy-test-reference",
    )


class LLMPrivacyTest(unittest.TestCase):
    """绝对禁止信息不能被公司政策升级为可外发。"""

    def test_policy_cannot_authorize_absolutely_forbidden_values(self) -> None:
        # 以下均为纯字符串夹具，用来证明公司路径、内网地址、身份、密钥和
        # 真实因子标识会在外发前被拒绝；测试不会访问对应路径或网络。
        cases = (
            "/data/quantlake/private.parquet",
            "192.0.2.10",
            "researcher@example.com",
            "Bearer synthetic-test-token-never-persist",
            "factor_internal_000123",
        )
        permissive = policy(
            "coverage_gap_bins",
            "field_capabilities",
            "failure_pattern_bins",
            "performance_bins",
        )

        for forbidden in cases:
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(FactorMinerError) as context:
                    scan_export_payload(
                        {
                            "information_class": "coverage_gap_bins",
                            "summary": forbidden,
                        },
                        permissive,
                    )
                self.assertEqual(
                    context.exception.code,
                    FailureCode.LLM_PRIVACY_VIOLATION,
                )

    def test_conditionally_exportable_class_requires_explicit_policy(self) -> None:
        payload = {
            "information_class": "coverage_gap_bins",
            "summary": "结构覆盖为零",
        }

        with self.assertRaises(FactorMinerError) as context:
            scan_export_payload(payload, policy("field_capabilities"))

        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_POLICY_NOT_AUTHORIZED,
        )

    def test_control_and_zero_width_characters_are_rejected_before_serialization(
        self,
    ) -> None:
        for unsafe in ("正常\u0000文本", "正常\u200b文本"):
            with self.subTest(unsafe=repr(unsafe)):
                with self.assertRaises(FactorMinerError) as context:
                    scan_export_payload(
                        {
                            "information_class": "coverage_gap_bins",
                            "summary": unsafe,
                        },
                        policy("coverage_gap_bins"),
                    )
                self.assertEqual(
                    context.exception.code,
                    FailureCode.LLM_PRIVACY_VIOLATION,
                )

    def test_content_addressed_audit_id_is_not_mistaken_for_a_credential(self) -> None:
        """固定格式的本地审计 ID 可以高熵，但普通正文中的同值仍必须拒绝。"""

        identity = "llmfamily_28de56db50a1c9310f4eb478"
        allowed = policy("evolution_gap_brief")
        payload = {
            "information_class": "evolution_gap_brief",
            "discovery_family_id": identity,
            "summary": "仅包含脱敏聚合摘要",
        }
        self.assertGreater(len(scan_export_payload(payload, allowed)), 0)
        with self.assertRaises(FactorMinerError) as context:
            scan_export_payload(
                {
                    "information_class": "evolution_gap_brief",
                    "summary": identity,
                },
                allowed,
            )
        self.assertEqual(
            context.exception.code,
            FailureCode.LLM_PRIVACY_VIOLATION,
        )

    def test_preview_hashes_exact_scanned_bytes(self) -> None:
        payload = {
            "information_class": "coverage_gap_bins",
            "summary": "结构覆盖为零",
        }
        preview = build_export_preview(
            payload,
            policy("coverage_gap_bins"),
        )

        self.assertEqual(preview.information_classes, ("coverage_gap_bins",))
        self.assertEqual(len(preview.payload_sha256), 64)
        self.assertEqual(preview.field_count, 2)

    def test_policy_content_address_detects_scope_tampering(self) -> None:
        payload = policy("coverage_gap_bins").model_dump(mode="json")
        payload["maximum_authorized_campaigns"] = 99
        with self.assertRaises(ValidationError):
            CorporateExternalResearchPolicy.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
