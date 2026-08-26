"""Dashboard 假设草案只读加载测试。"""

import json
from pathlib import Path
import tempfile
import unittest

from factor_miner.canonical import sha256_json
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft
from dashboard.hypotheses import load_hypothesis_draft_batch


def _draft(index: int) -> dict[str, object]:
    """构造一条合法的合成中文假设。"""

    return LogicalEvolutionHypothesisDraft(
        logical_slot_id=f"H{index:02d}",
        mechanism_unverified=True,
        prior_claim=f"主张 {index}",
        mechanism=f"机制 {index}",
        expected_direction="正向",
        observable_proxy=f"可观测代理 {index}",
        independent_verification=f"独立验证 {index}",
        competing_explanations=(f"竞争解释 {index}",),
        failure_modes=(f"失效方式 {index}",),
        falsification_path=f"证伪路径 {index}",
        gap_ids=(f"G{index:03d}",),
        source_records=(
            {
                "source_record_id": f"source-{index:02d}",
                "claim_fragment": f"来源片段 {index}",
                "rationale": f"检索理由 {index}",
                "query_terms": ("price", f"signal-{index}"),
                "year_start": 2000,
                "year_end": 2026,
                "result_limit": 3,
            },
        ),
    ).model_dump(mode="json")


def _batch() -> dict[str, object]:
    """构造正式草案批次的最小合成载荷。"""

    drafts = [_draft(index) for index in range(1, 11)]
    identity = {
        "request_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "gap_card_sha256": {f"G{index:03d}": "c" * 64 for index in range(1, 11)},
        "context_sha256": "d" * 64,
        "discovery_family_id": "llmfamily_" + "e" * 24,
        "draft_sha256": [item["draft_sha256"] for item in drafts],
    }
    return {
        **identity,
        "draft_batch_sha256": sha256_json(identity),
        "drafts": drafts,
    }


class DashboardHypothesesTest(unittest.TestCase):
    """Dashboard 只读取完整、可核验的十槽草案批次。"""

    def test_loads_ten_drafts_without_exposing_raw_response(self) -> None:
        """加载器返回可展示草案，不返回模型原始响应字段。"""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hypothesis_drafts.json"
            path.write_text(json.dumps(_batch(), ensure_ascii=False), encoding="utf-8")

            batch = load_hypothesis_draft_batch(path)

        self.assertEqual(batch.status, "待审批")
        self.assertEqual(tuple(item.logical_slot_id for item in batch.drafts), tuple(f"H{i:02d}" for i in range(1, 11)))
        self.assertEqual(batch.drafts[0].prior_claim, "主张 1")
        self.assertFalse(hasattr(batch, "response"))

    def test_rejects_changed_draft_batch_hash(self) -> None:
        """草案批次身份被改写时不能显示。"""

        payload = _batch()
        payload["draft_batch_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hypothesis_drafts.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(Exception):
                load_hypothesis_draft_batch(path)
