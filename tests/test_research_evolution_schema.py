"""演化合同的脱敏、内容寻址和失败语义测试。"""

import unittest
from datetime import datetime, timezone

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapCard,
    CoverageGapReport,
    DataIdentitySummary,
    DesignDiversityPolicy,
    EvaluationSummary,
    EvolutionHypothesisApproval,
    ResearchEvolutionContext,
    ResearchMemoryEntry,
    ResearchMemorySnapshot,
    build_evolution_context,
)

SAFE_FAMILY = "llmfamily_" + "1" * 24
POLLUTED_FAMILY = "llmfamily_1bae19965638a6ac9620e0b0"
NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def card(**overrides: object) -> CoverageGapCard:
    """创建不手工提供身份 hash 的合法缺口卡片。"""

    values: dict[str, object] = {
        "gap_id": "G001",
        "gap_category": "structural",
        "sanitized_labels": ("coverage",),
        "allowed_field_aliases": ("close",),
        "allowed_operator_families": ("rank",),
        "temporal_window_bins": ("short",),
        "structure_cluster_count_band": "low",
        "signal_cluster_count_band": "none",
        "missing_or_failure_risk": ("missing",),
    }
    values.update(overrides)
    return CoverageGapCard.model_validate(values)


def approval(slot: str = "H01", family_id: str = SAFE_FAMILY) -> EvolutionHypothesisApproval:
    """创建不手工提供决定 hash 的审批记录。"""

    return EvolutionHypothesisApproval(
        logical_slot_id=slot,
        context_sha256="a" * 64,
        draft_sha256="b" * 64,
        discovery_family_id=family_id,
        approval_role="reviewer",
        approved_at=NOW,
        decision="approved",
    )


class ResearchEvolutionSchemaTests(unittest.TestCase):
    """证明治理合同的正向和硬失败边界。"""

    def test_policy_budget_and_failure_code(self) -> None:
        self.assertEqual(DesignDiversityPolicy().total_slots, 120)
        with self.assertRaises(FactorMinerError) as caught:
            DesignDiversityPolicy(total_slots=121)
        self.assertIs(caught.exception.code, FailureCode.DESIGN_DIVERSITY_FAILED)

    def test_gap_redaction_rejects_numbers_paths_and_raw_terms(self) -> None:
        fields = (
            "sanitized_labels", "allowed_field_aliases", "allowed_operator_families",
            "temporal_window_bins", "missing_or_failure_risk",
        )
        for field in fields:
            with self.subTest(field=field):
                with self.assertRaises(FactorMinerError) as caught:
                    card(**{field: ("2026-01-01",)})
                self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
                with self.assertRaises(FactorMinerError):
                    card(**{field: ("/data/raw",)})
                with self.assertRaises(FactorMinerError):
                    card(**{field: ("ticker",)})
                with self.assertRaises(FactorMinerError):
                    card(**{field: ("AAPL",)})
                with self.assertRaises(FactorMinerError):
                    card(**{field: ("unregistered_word",)})

    def test_registered_discrete_labels_are_allowed(self) -> None:
        item = card(
            sanitized_labels=("coverage", "structural"),
            allowed_field_aliases=("close", "price_close"),
            allowed_operator_families=("ranking", "rolling"),
            temporal_window_bins=("long", "short"),
            missing_or_failure_risk=("missing", "stale"),
            structure_cluster_count_band="2_4",
            signal_cluster_count_band="5_plus",
        )
        self.assertIsNotNone(item.card_sha256)
        summary = EvaluationSummary(
            outcome_band="supported", rank_ic_band="high", hac_significance_band="stable",
            portfolio_band="medium", redundancy_band="low",
        )
        self.assertEqual(summary.outcome_band, "supported")
        with self.assertRaises(FactorMinerError) as caught:
            EvaluationSummary(
                outcome_band="0.12", rank_ic_band="high", hac_significance_band="stable",
                portfolio_band="medium", redundancy_band="low",
            )
        self.assertIs(caught.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_gap_category_band_cutoff_and_sorting_fail_with_codes(self) -> None:
        with self.assertRaises(FactorMinerError) as caught:
            card(gap_category="unknown")
        self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
        with self.assertRaises(FactorMinerError):
            card(sanitized_labels=("zeta", "alpha"))
        with self.assertRaises(FactorMinerError):
            DataIdentitySummary(release_hash="a" * 64, manifest_hash="b" * 64, field_registry_hash="c" * 64, cutoff_band="2026")
        first = card(gap_id="G001")
        second = card(gap_id="G002", sanitized_labels=("regime",))
        ordered = tuple(sorted((first, second), key=lambda item: (item.gap_category.value, item.card_sha256, item.gap_id)))
        with self.assertRaises(FactorMinerError) as caught:
            CoverageGapReport(
                coverage_graph_id="covgraph_" + "1" * 24,
                graph_manifest_hash="a" * 64,
                regime_snapshot_hash="b" * 64,
                memory_snapshot_hash="c" * 64,
                gap_cards=ordered[::-1],
                truncated_count=0,
                truncation_reason="未发生截断",
                internal_sort_policy_hash="d" * 64,
            )
        self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
        with self.assertRaises(FactorMinerError) as caught:
            CoverageGapReport(
                coverage_graph_id="covgraph_" + "1" * 24,
                graph_manifest_hash="a" * 64,
                regime_snapshot_hash="b" * 64,
                memory_snapshot_hash="c" * 64,
                gap_cards=ordered,
                truncated_count=0,
                truncation_reason="  ",
                internal_sort_policy_hash="d" * 64,
            )
        self.assertIs(caught.exception.code, FailureCode.COVERAGE_GAP_INVALID)
        report = CoverageGapReport(
            coverage_graph_id="covgraph_" + "1" * 24,
            graph_manifest_hash="a" * 64,
            regime_snapshot_hash="b" * 64,
            memory_snapshot_hash="c" * 64,
            gap_cards=ordered,
            truncated_count=0,
            truncation_reason="未发生截断",
            internal_sort_policy_hash="d" * 64,
        )
        self.assertEqual(report.truncated_count, 0)
        self.assertEqual(report.truncation_reason, "未发生截断")

    def test_hashes_are_derived_and_wrong_hash_is_rejected(self) -> None:
        item = card()
        self.assertIsNotNone(item.card_sha256)
        with self.assertRaises(FactorMinerError) as caught:
            card(card_sha256="0" * 64)
        self.assertIs(caught.exception.code, FailureCode.EVOLUTION_HASH_MISMATCH)
        context = build_evolution_context(
            discovery_family_id=SAFE_FAMILY,
            coverage_graph_manifest_hash="a" * 64,
            memory_snapshot_hash="b" * 64,
            gap_report_hash="c" * 64,
            field_registry_hash="d" * 64,
            evaluation_policy_hash="e" * 64,
            design_policy_hash="f" * 64,
            external_model_redaction_policy="sanitized-bands-v1",
        )
        self.assertIsNotNone(context.context_sha256)

    def test_polluted_family_rejected_by_context_approval_and_batch(self) -> None:
        kwargs = dict(
            discovery_family_id=POLLUTED_FAMILY,
            coverage_graph_manifest_hash="a" * 64,
            memory_snapshot_hash="b" * 64,
            gap_report_hash="c" * 64,
            field_registry_hash="d" * 64,
            evaluation_policy_hash="e" * 64,
            design_policy_hash="f" * 64,
            external_model_redaction_policy="sanitized-bands-v1",
        )
        with self.assertRaises(FactorMinerError) as caught:
            build_evolution_context(**kwargs)
        self.assertIs(caught.exception.code, FailureCode.POLLUTED_FAMILY_REJECTED)
        with self.assertRaises(FactorMinerError):
            approval(family_id=POLLUTED_FAMILY)
        polluted_approval = EvolutionHypothesisApproval.model_construct(
            logical_slot_id="H01", context_sha256="a" * 64, draft_sha256="b" * 64,
            discovery_family_id=POLLUTED_FAMILY, approval_role="reviewer",
            approved_at=NOW, decision="approved", decision_sha256="c" * 64,
        )
        with self.assertRaises(FactorMinerError) as caught:
            ApprovedEvolutionHypothesisBatch(
                context_sha256="a" * 64, discovery_family_id=POLLUTED_FAMILY,
                approvals=(polluted_approval,) * 10,
            )
        self.assertIs(caught.exception.code, FailureCode.POLLUTED_FAMILY_REJECTED)

    def test_memory_entry_snapshot_hash_order_and_pollution_fail(self) -> None:
        common = dict(
            memory_entry_id="memoryentry_" + "1" * 24, entry_kind="terminal",
            discovery_family_id=SAFE_FAMILY, generation_seal_id="seal_" + "2" * 24,
            run_id="run_" + "3" * 24, hypothesis_slot_id="H01", candidate_slot_id="C01",
            hypothesis_spec_hash="a" * 64, candidate_spec_hash="b" * 64, ast_hash="c" * 64,
            coverage_graph_id="covgraph_" + "4" * 24, memory_snapshot_id="memorysnap_" + "5" * 24,
            field_signature=("close",), operator_signature=("rank",), temporal_signature=("level",),
            structure_signature=("field_set",), gap_labels=("coverage",), terminal_state="failed",
            data_identity_summary=DataIdentitySummary(release_hash="d" * 64, manifest_hash="e" * 64, field_registry_hash="f" * 64, cutoff_band="high"),
            created_at=NOW,
        )
        entry = ResearchMemoryEntry(**common)
        self.assertIsNotNone(entry.entry_sha256)
        unresolved = ResearchMemoryEntry(
            **{
                **common,
                "hypothesis_direction": "negative",
                "selected_direction": None,
                "direction_relation": "unresolved",
                "direction_source": "discovery_window_unresolved",
                "direction_record_sha256": None,
                "failure_reason": "方向发现未决，确认结果另行记录",
            }
        )
        self.assertEqual(unresolved.direction_relation, "unresolved")
        self.assertIsNotNone(unresolved.entry_sha256)
        self.assertNotEqual(unresolved.entry_sha256, entry.entry_sha256)
        with self.assertRaises(FactorMinerError) as caught:
            ResearchMemoryEntry(**{**common, "entry_sha256": "0" * 64})
        self.assertIs(caught.exception.code, FailureCode.EVOLUTION_HASH_MISMATCH)
        snapshot = ResearchMemorySnapshot(
            memory_snapshot_id="memorysnap_" + "6" * 24, created_at=NOW, cutoff_at=NOW,
            source_family_ids=(SAFE_FAMILY,), source_seal_ids=("seal_" + "2" * 24,),
            source_run_ids=("run_" + "3" * 24,), entry_ids=(entry.memory_entry_id,),
            entry_hashes=(entry.entry_sha256,),
            entry_bindings=(
                ResearchMemorySnapshot.EntryBinding(
                    memory_entry_id=entry.memory_entry_id,
                    entry_sha256=entry.entry_sha256,
                ),
            ),
            exclusion_rules=("polluted_family",), entry_count=1,
            coverage_graph_id="covgraph_" + "4" * 24, coverage_graph_manifest_hash="a" * 64,
        )
        self.assertIsNotNone(snapshot.snapshot_sha256)
        with self.assertRaises(FactorMinerError) as caught:
            ResearchMemorySnapshot(**{**snapshot.model_dump(), "entry_ids": ("memoryentry_" + "9" * 24, entry.memory_entry_id)})
        self.assertIs(caught.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)
        with self.assertRaises(FactorMinerError) as caught:
            ResearchMemorySnapshot(
                **{
                    **snapshot.model_dump(),
                    "entry_bindings": (
                        {
                            "memory_entry_id": entry.memory_entry_id,
                            "entry_sha256": "0" * 64,
                        },
                    ),
                }
            )
        self.assertIs(caught.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)
        with self.assertRaises(FactorMinerError) as caught:
            ResearchMemoryEntry(**{**common, "discovery_family_id": POLLUTED_FAMILY})
        self.assertIs(caught.exception.code, FailureCode.POLLUTED_FAMILY_REJECTED)

    def test_duplicate_hypothesis_slots_fail_with_approval_count_code(self) -> None:
        approvals = tuple(approval(slot=f"H{i:02d}") for i in range(1, 10)) + (approval(slot="H01"),)
        with self.assertRaises(FactorMinerError) as caught:
            ApprovedEvolutionHypothesisBatch(context_sha256="a" * 64, discovery_family_id=SAFE_FAMILY, approvals=approvals)
        self.assertIs(caught.exception.code, FailureCode.APPROVAL_COUNT_INVALID)


if __name__ == "__main__":
    unittest.main()
