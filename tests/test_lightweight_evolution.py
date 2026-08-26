"""轻量记忆与因子图谱只供下一批使用的测试。"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from factor_miner.canonical import sha256_json
from factor_miner.lightweight_evolution import (
    LightweightEvolutionContext,
    LightweightMemorySnapshot,
    LightweightMemoryStore,
    PublishedLightweightCoverageGraph,
    PublishedLightweightGapBrief,
    PublishedLightweightRun,
    build_lightweight_coverage_catalog,
    build_lightweight_memory_feedback,
    build_lightweight_memory_entries,
    refresh_lightweight_evolution,
)
from factor_miner.long_only_protocol import (
    DiscoveryRankICSummary,
    LongOnlyResearchProtocol,
    select_direction,
)
from factor_miner.lightweight_expressions import (
    build_lightweight_expression_request,
    candidate_bindings_from_expression_batch,
    parse_lightweight_expression_response,
)
from factor_miner.lightweight_schema import (
    LightweightResearchConfig,
    build_lightweight_batch_manifest,
)
from factor_miner.llm_state import CandidateSlotState
from factor_miner.research_campaign_runner import (
    CampaignSlotEvaluation,
    LightweightCampaignEvaluationResult,
)
from tests.test_design_diversity import _registry
from tests.test_lightweight_expressions import response, reviewed
from tests.test_research_evolution import response_payload_with_semantic_tags


NOW = datetime(2026, 8, 14, 13, tzinfo=timezone.utc)


def source_context() -> LightweightEvolutionContext:
    return LightweightEvolutionContext(
        memory_snapshot_id="memory_source",
        memory_snapshot_hash="a" * 64,
        coverage_graph_id="graph_source",
        coverage_graph_hash="b" * 64,
    )


def publication(*, fail_last: bool = False, tagged: bool = False) -> PublishedLightweightRun:
    hypotheses, review = reviewed(
        approved_count=2,
        hypothesis_response=(response_payload_with_semantic_tags() if tagged else None),
    )
    prepared = build_lightweight_expression_request(
        hypotheses=hypotheses,
        review=review,
        registry=_registry(),
    )
    expressions = parse_lightweight_expression_response(
        prepared=prepared,
        response=response(invalid_last=fail_last),
        hypotheses=hypotheses,
        review=review,
        registry=_registry(),
        created_at=NOW,
    )
    manifest = build_lightweight_batch_manifest(
        config=LightweightResearchConfig(hypothesis_count=2, candidates_per_hypothesis=3),
        candidate_bindings=candidate_bindings_from_expression_batch(expressions),
        evaluation_policy_hash="c" * 64,
        memory_snapshot_hash="a" * 64,
        coverage_graph_hash="b" * 64,
    )
    evaluations = tuple(
        CampaignSlotEvaluation(
            slot_id=item.slot_id,
            slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
            status="evaluated" if item.terminal_status == "ready" else "failed",
            failure_reason=item.failure_reason,
            output=(
                {
                    "outcome_band": "可见通过",
                    "daily_ic_reference": {
                        "artifact_relative_path": f"daily_ic/{item.slot_id}.parquet",
                        "artifact_sha256": "d" * 64,
                    },
                }
                if item.terminal_status == "ready"
                else {}
            ),
        )
        for item in manifest.candidate_bindings
    )
    frozen_evaluations = []
    result_by_slot = {item.slot_id: item for item in expressions.slot_results}
    protocol = LongOnlyResearchProtocol()
    for index, item in enumerate(evaluations, start=1):
        candidate = result_by_slot[item.slot_id].candidate
        if candidate is not None and item.status == "evaluated":
            expected = candidate.spec.hypothesis.expected_sign.value
            decision = select_direction(
                DiscoveryRankICSummary(
                    protocol=protocol,
                    window_start=protocol.discovery_start,
                    window_end=protocol.discovery_end,
                    rank_ic=0.01 if expected == "positive" else -0.01,
                ),
                hypothesis_direction=expected,
            )
            item = item.model_copy(update={
                "output": {
                    **item.output,
                    "direction_decision": decision.model_dump(mode="json"),
                    "direction_record_sha256": f"{index:x}".zfill(64),
                }
            })
        frozen_evaluations.append(item)
    evaluation = LightweightCampaignEvaluationResult.build(
        manifest=manifest,
        slot_evaluations=tuple(frozen_evaluations),
    )
    return PublishedLightweightRun(
        run_id=hypotheses.run_id,
        hypotheses=hypotheses,
        review=review,
        expressions=expressions,
        manifest=manifest,
        evaluation=evaluation,
        intraday_field_ids=("close",),
    )


def publication_with_reversed_first_direction() -> PublishedLightweightRun:
    """为首个候选注入已冻结方向，模拟真实 Pilot 的不可变发现记录。"""

    current = publication()
    protocol = LongOnlyResearchProtocol()
    decision = select_direction(
        DiscoveryRankICSummary(
            protocol=protocol,
            window_start=protocol.discovery_start,
            window_end=protocol.discovery_end,
            rank_ic=0.01,
        ),
        hypothesis_direction="negative",
    )
    updated = []
    for index, item in enumerate(current.evaluation.slot_evaluations):
        if index == 0:
            output = {
                **item.output,
                "direction_decision": decision.model_dump(mode="json"),
                "direction_record_sha256": "e" * 64,
            }
            item = item.model_copy(update={"output": output})
        updated.append(item)
    evaluation = LightweightCampaignEvaluationResult.build(
        manifest=current.manifest,
        slot_evaluations=tuple(updated),
    )
    return current.model_copy(update={"evaluation": evaluation})


class FakeDependencies:
    """返回三项内容身份的幂等合成发布端口。"""

    def __init__(self, root: Path) -> None:
        self.store = LightweightMemoryStore(root)
        self.calls = {"memory": 0, "graph": 0, "brief": 0}

    def publish_memory(self, entries, context, run_id):
        self.calls["memory"] += 1
        return self.store.publish(
            entries,
            source_context=context,
            run_id=run_id,
            written_at=NOW,
        )

    def publish_coverage_graph(self, catalog):
        self.calls["graph"] += 1
        return PublishedLightweightCoverageGraph(
            graph_id="graph_next",
            graph_sha256=sha256_json(catalog.model_dump(mode="json")),
        )

    def publish_gap_brief(self, memory, graph):
        self.calls["brief"] += 1
        return PublishedLightweightGapBrief(
            brief_id="gap_next",
            brief_sha256=sha256_json(
                {"memory": memory.snapshot_sha256, "graph": graph.graph_sha256}
            ),
            description_cn="下一轮优先补充尚未覆盖的结构与失效状态。",
        )


class LightweightEvolutionTest(unittest.TestCase):
    """拒绝和失败不能消失，新状态不得污染当前批次。"""

    def test_rejected_failed_and_evaluated_items_enter_next_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = publication(fail_last=True)
            result = refresh_lightweight_evolution(
                current,
                source_context(),
                FakeDependencies(Path(directory)),
            )
            self.assertEqual(result.hypothesis_entry_count, 10)
            self.assertEqual(result.candidate_entry_count, current.manifest.family_size)
            self.assertEqual(result.source_memory_snapshot_id, "memory_source")
            self.assertNotEqual(result.next_memory_snapshot_id, result.source_memory_snapshot_id)
            self.assertEqual(current.manifest.memory_snapshot_hash, "a" * 64)
            self.assertEqual(current.manifest.coverage_graph_hash, "b" * 64)

    def test_semantic_plan_is_recorded_once_per_hypothesis_and_builds_quota(self) -> None:
        entries = build_lightweight_memory_entries(publication(tagged=True))
        hypothesis_entries = tuple(
            item for item in entries if item.entry_kind == "hypothesis"
        )
        candidate_entries = tuple(
            item for item in entries if item.entry_kind == "candidate"
        )

        self.assertTrue(all(item.semantic_plan is not None for item in hypothesis_entries))
        self.assertTrue(all(item.semantic_plan is None for item in candidate_entries))
        feedback = build_lightweight_memory_feedback(entries)
        self.assertEqual(feedback["semantic_coverage"]["tagged_hypothesis_count"], 10)
        self.assertEqual(
            tuple(item["quota_kind"] for item in feedback["semantic_quota"]["slots"]),
            ("uncovered",) * 6 + ("sparse",) * 3 + ("free",),
        )

    def test_intraday_candidate_is_tagged_in_catalog(self) -> None:
        catalog = build_lightweight_coverage_catalog(publication(), source_context())
        self.assertEqual(len(catalog.entries), 6)
        self.assertTrue(
            all("intraday_aggregate" in item.data_source_tags for item in catalog.entries)
        )
        self.assertTrue(all(item.node.lag_days == 1 for item in catalog.entries))

    def test_reversed_discovery_direction_is_immutable_memory_and_graph_orientation(self) -> None:
        """若错误地回读 HypothesisSpec 方向，此测试会失败。"""

        current = publication_with_reversed_first_direction()
        entry = next(
            item
            for item in build_lightweight_memory_entries(current)
            if item.candidate_slot_id == "H01:C001"
        )
        catalog = build_lightweight_coverage_catalog(current, source_context())
        node = next(item.node for item in catalog.entries if item.node.factor_id == entry.candidate_id)

        self.assertEqual(entry.hypothesis_direction, "negative")
        self.assertEqual(entry.selected_direction, "positive")
        self.assertEqual(entry.direction_relation, "reversed")
        self.assertEqual(
            entry.failure_reason,
            "事前负向假设在方向发现区间被反转，确认结果另行记录",
        )
        self.assertEqual(node.orientation_sign, 1)
        self.assertEqual(node.orientation_source, "discovery_window_frozen")

    def test_unresolved_direction_is_explicit_memory_without_graph_orientation(self) -> None:
        """未取得冻结决定时若留下全 None，下一轮会误以为该候选从未登记。"""

        current = publication(fail_last=True)
        entry = next(
            item
            for item in build_lightweight_memory_entries(current)
            if item.candidate_slot_id == "H02:C003"
        )
        catalog = build_lightweight_coverage_catalog(current, source_context())

        self.assertEqual(entry.hypothesis_direction, "positive")
        self.assertIsNone(entry.selected_direction)
        self.assertEqual(entry.direction_relation, "unresolved")
        self.assertEqual(entry.direction_source, "discovery_window_unresolved")
        self.assertIn("方向发现未决，确认结果另行记录", entry.failure_reason)
        self.assertNotIn(entry.candidate_id, {item.node.factor_id for item in catalog.entries})

    def test_memory_store_is_idempotent_and_separate_from_formal_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependencies = FakeDependencies(root)
            first = refresh_lightweight_evolution(publication(), source_context(), dependencies)
            second = refresh_lightweight_evolution(publication(), source_context(), dependencies)
            self.assertEqual(first, second)
            events = (root / "research_memory" / "lightweight" / "events.jsonl").read_text("utf-8").splitlines()
            self.assertEqual(len(events), 16)
            self.assertFalse((root / "research_memory" / "entries.jsonl").exists())

    def test_memory_snapshots_accumulate_previous_round_entries(self) -> None:
        """第二轮快照必须包含第一轮条目，而不是重新只保存本轮。"""

        with tempfile.TemporaryDirectory() as directory:
            store = LightweightMemoryStore(Path(directory))
            first_entries = build_lightweight_memory_entries(publication())
            first = store.publish(
                first_entries,
                source_context=source_context(),
                run_id="run_first",
                written_at=NOW,
            )
            additional = first_entries[0].model_copy(
                update={
                    "run_id": "run_second",
                    "entry_id": "lwmem_" + "e" * 24,
                    "entry_sha256": "e" * 64,
                }
            )
            # 使用 build 保持新条目的内容身份有效。
            additional = type(additional).build(
                **additional.model_dump(
                    mode="json", exclude={"entry_id", "entry_sha256"}
                )
            )
            second = store.publish(
                (additional,),
                source_context=LightweightEvolutionContext(
                    memory_snapshot_id=first.snapshot_id,
                    memory_snapshot_hash=first.snapshot_sha256,
                    coverage_graph_id="graph_next",
                    coverage_graph_hash="f" * 64,
                ),
                run_id="run_second",
                written_at=NOW,
            )

            loaded = store.load_snapshot_entries(second.snapshot_id)

        self.assertEqual(len(loaded), len(first_entries) + 1)
        self.assertEqual(second.hypothesis_entry_count, 11)
        self.assertEqual(second.candidate_entry_count, 6)

    def test_feedback_summarizes_failure_and_portfolio_quality_without_numbers(self) -> None:
        current = publication(fail_last=True)
        entries = list(build_lightweight_memory_entries(current))
        failed_index = next(
            index
            for index, item in enumerate(entries)
            if item.entry_kind == "candidate" and item.terminal_status == "failed"
        )
        payload = entries[failed_index].model_dump(
            mode="json", exclude={"entry_id", "entry_sha256"}
        )
        payload["failure_reason"] = "表达式过于简单：至少需要 5 个 AST 节点"
        entries[failed_index] = type(entries[failed_index]).build(**payload)

        feedback = build_lightweight_memory_feedback(tuple(entries))

        self.assertIn("invalid_complexity", feedback["dominant_failure_patterns"])
        self.assertIn(
            "prioritize_positive_excess_information_ratio",
            feedback["generation_guidance"],
        )
        self.assertNotIn("rank_ic_mean", str(feedback))

    def test_three_products_are_required_for_refresh_result(self) -> None:
        payload = {
            "source_memory_snapshot_id": "memory_source",
            "source_memory_snapshot_hash": "a" * 64,
            "next_memory_snapshot_id": "memory_next",
            "next_memory_snapshot_hash": "b" * 64,
            "source_coverage_graph_id": "graph_source",
            "source_coverage_graph_hash": "c" * 64,
            "next_coverage_graph_id": "graph_next",
            "next_coverage_graph_hash": "d" * 64,
            "gap_brief_id": "",
            "gap_brief_hash": "e" * 64,
            "hypothesis_entry_count": 10,
            "candidate_entry_count": 6,
            "result_sha256": "f" * 64,
        }
        from factor_miner.lightweight_evolution import EvolutionRefreshResult
        with self.assertRaises(ValidationError):
            EvolutionRefreshResult.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
