"""轻量十假设生成和 Dashboard 逐条审批测试。"""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from factor_miner.llm_online import AgentRole, build_deepseek_request
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisDecision,
    generate_lightweight_hypotheses,
    freeze_lightweight_review,
)
from tests.test_research_evolution import context, response_payload


NOW = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
RUN_ID = "autrun_" + "1" * 24


class FakeProvider:
    """返回已校验 JSON 的合成录制 provider。"""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.call_count = 0

    def generate(self, request):
        self.call_count += 1
        return SimpleNamespace(
            record=SimpleNamespace(
                call_id="llmcall_" + "2" * 24,
                request_sha256=request.request_sha256,
                response_sha256="3" * 64,
            ),
            content_json=self.payload,
        )


def request():
    """构造不含研究结果的十槽合成请求。"""

    return build_deepseek_request(
        campaign_id=f"{RUN_ID}:hypothesis",
        agent_role=AgentRole.HYPOTHESIS,
        slot_ids=tuple(f"H{i:02d}" for i in range(1, 11)),
        system_prompt="只输出包含十条中文假设的 JSON object。",
        user_payload={"information_class": "evolution_gap_brief", "gap_cards": []},
        audit_payload={"information_class": "evolution_gap_brief"},
    )


def decisions(batch, *, approved_count: int = 7):
    """构造前若干批准、其余拒绝的十条决定。"""

    return tuple(
        LightweightHypothesisDecision.build(
            run_id=RUN_ID,
            context_sha256=batch.context_sha256,
            draft=item,
            decision="approved" if index < approved_count else "rejected",
            approval_role="research_owner",
            decided_at=NOW,
        )
        for index, item in enumerate(batch.hypotheses)
    )


class LightweightHypothesesTest(unittest.TestCase):
    """审批发生在表达式和结果之前，拒绝记录不得消失。"""

    def test_hypothesis_batch_requires_exactly_h01_to_h10(self) -> None:
        provider = FakeProvider(response_payload())
        batch = generate_lightweight_hypotheses(
            run_id=RUN_ID,
            context=context(),
            prepared=request(),
            provider=provider,
        )
        self.assertEqual(
            tuple(item.logical_slot_id for item in batch.hypotheses),
            tuple(f"H{i:02d}" for i in range(1, 11)),
        )
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(batch.provider_call_id, "llmcall_" + "2" * 24)

    def test_review_freezes_all_decisions_and_keeps_rejections(self) -> None:
        batch = generate_lightweight_hypotheses(
            run_id=RUN_ID,
            context=context(),
            prepared=request(),
            provider=FakeProvider(response_payload()),
        )
        review = freeze_lightweight_review(batch, decisions(batch))
        self.assertEqual(review.decision_count, 10)
        self.assertEqual(review.approved_hypothesis_count, 7)
        self.assertEqual(review.rejected_hypothesis_count, 3)
        self.assertEqual(review.candidate_family_size, 21)
        self.assertEqual(review.rejected_slot_ids, ("H08", "H09", "H10"))

    def test_review_rejects_missing_decision(self) -> None:
        batch = generate_lightweight_hypotheses(
            run_id=RUN_ID,
            context=context(),
            prepared=request(),
            provider=FakeProvider(response_payload()),
        )
        with self.assertRaisesRegex(ValueError, "完整覆盖 H01-H10"):
            freeze_lightweight_review(batch, decisions(batch)[:-1])

    def test_decision_must_bind_exact_draft(self) -> None:
        batch = generate_lightweight_hypotheses(
            run_id=RUN_ID,
            context=context(),
            prepared=request(),
            provider=FakeProvider(response_payload()),
        )
        payload = decisions(batch)[0].model_dump(mode="json")
        payload["draft_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "决定内容身份"):
            LightweightHypothesisDecision.model_validate(payload)

    def test_provider_request_and_response_identity_must_match(self) -> None:
        provider = FakeProvider(response_payload())
        prepared = request()
        original = provider.generate

        def mismatched(request_value):
            result = original(request_value)
            result.record.request_sha256 = "0" * 64
            return result

        provider.generate = mismatched  # type: ignore[method-assign]
        with self.assertRaisesRegex(ValueError, "请求身份不一致"):
            generate_lightweight_hypotheses(
                run_id=RUN_ID,
                context=context(),
                prepared=prepared,
                provider=provider,
            )


if __name__ == "__main__":
    unittest.main()
