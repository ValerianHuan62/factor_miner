"""Task7 研究记忆发布与投影合同测试。"""

from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.research_memory import (
    ResearchMemoryStore,
    build_campaign_memory_entries,
    publish_campaign_memory,
)
from factor_miner.research_evolution_schema import DataIdentitySummary, EvaluationSummary, ResearchMemoryEntry
from factor_miner.long_only_protocol import (
    DiscoveryRankICSummary,
    LongOnlyResearchProtocol,
    select_direction,
)
from dashboard.pg_store import PostgresDashboardStore
from tests.test_llm_orchestrator import NOW
from tests.test_llm_schema import valid_family_spec
from tests.test_research_evolution import response_payload
from tests.test_research_evolution_synthetic_e2e import _approvals, _context, _memory_snapshot, _publish_graph, _synthetic_slot_objects
from factor_miner.canonical import sha256_json
from factor_miner.dashboard_projection import project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.llm_orchestrator import GenerationSealDependencies, seal_completed_generation
from factor_miner.llm_brief import GapSelectionPolicy
from factor_miner.llm_schema import registered_llm_discovery_family
from factor_miner.llm_state import expected_candidate_slot_ids
from factor_miner.research_evolution import approve_evolution_hypotheses, parse_evolution_hypothesis_response
from factor_miner.research_gap import build_coverage_gap_report, load_verified_coverage_graph
from factor_miner.portfolio_artifacts import publish_run_artifacts


class ResearchMemoryProjectionTest(unittest.TestCase):
    """验证脱敏、完整槽位和不可变重复语义。"""

    def _slots(self):
        return tuple({"slot_id": f"coverage_outcome_llm:{index:03d}", "status": "not_executed"} for index in range(1, 121))

    def test_publishes_120_entries_and_redacts_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = publish_campaign_memory(
                root, run_id="run_" + "1" * 24, family_id="llmfamily_" + "2" * 24,
                generation_seal_id="llmseal_" + "3" * 24, source_manifest_sha256="a" * 64,
                data_contract_identity_hash="b" * 64, slot_evaluations=self._slots(),
                published_at=datetime.now(timezone.utc), metadata={"label": "脱敏"},
            )
            legacy = json.loads((root / "research_memory_legacy" / f"{reference['run_id']}.json").read_text())
            self.assertEqual(legacy["entry_count"], 120)
            self.assertFalse((root / "research_memory" / "entries.jsonl").exists())
            self.assertEqual(reference["entry_count"], 120)
            self.assertEqual(reference["version"], "research-memory-legacy-v1")

    def test_republishing_same_memory_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = dict(run_id="run_" + "4" * 24, family_id="llmfamily_" + "5" * 24,
                          generation_seal_id="llmseal_" + "6" * 24, source_manifest_sha256="c" * 64,
                          data_contract_identity_hash="d" * 64, slot_evaluations=self._slots(),
                          published_at=datetime.now(timezone.utc))
            first = publish_campaign_memory(root, **values)
            second = publish_campaign_memory(root, **values)
            self.assertEqual(first, second)

    def test_reversed_direction_is_memory_not_hypothesis_mutation(self) -> None:
        """若正式记忆丢失反转关系，下一轮会错误回填事前方向。"""

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
        slots = list(self._slots())
        slots[0] = {
            **slots[0],
            "direction_decision": decision.model_dump(mode="json"),
            "direction_record_sha256": "e" * 64,
        }
        entry = build_campaign_memory_entries(
            family_id="llmfamily_" + "1" * 24,
            generation_seal_id="llmseal_" + "2" * 24,
            run_id="run_" + "3" * 24,
            slot_evaluations=slots,
            published_at=NOW,
            memory_snapshot_id="memsnap_" + "4" * 24,
            data_contract_identity_hash="5" * 64,
            source_manifest_sha256="6" * 64,
            coverage_graph_id="covgraph_" + "7" * 24,
        )[0]

        self.assertEqual(entry["hypothesis_direction"], "negative")
        self.assertEqual(entry["selected_direction"], "positive")
        self.assertEqual(entry["direction_relation"], "reversed")
        self.assertEqual(
            entry["failure_reason"],
            "事前负向假设在方向发现区间被反转，确认结果另行记录",
        )

    def test_unresolved_direction_is_explicit_formal_memory(self) -> None:
        """没有冻结决定的已登记槽位必须保留不明确状态。"""

        slots = list(self._slots())
        slots[0] = {
            **slots[0],
            "hypothesis_direction": "negative",
            "direction_relation": "unresolved",
        }
        entry = build_campaign_memory_entries(
            family_id="llmfamily_" + "1" * 24,
            generation_seal_id="llmseal_" + "2" * 24,
            run_id="run_" + "3" * 24,
            slot_evaluations=slots,
            published_at=NOW,
            memory_snapshot_id="memsnap_" + "4" * 24,
            data_contract_identity_hash="5" * 64,
            source_manifest_sha256="6" * 64,
            coverage_graph_id="covgraph_" + "7" * 24,
        )[0]

        self.assertEqual(entry["hypothesis_direction"], "negative")
        self.assertIsNone(entry["selected_direction"])
        self.assertEqual(entry["direction_relation"], "unresolved")
        self.assertEqual(entry["direction_source"], "discovery_window_unresolved")
        self.assertEqual(entry["failure_reason"], "方向发现未决，确认结果另行记录")

    def test_cross_round_publish_uses_full_snapshot_counts(self) -> None:
        """跨轮正式发布的引用和 Dashboard 计数必须与累计 snapshot 一致。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = load_verified_coverage_graph(_publish_graph(root, dense=True))
            source_memory = _memory_snapshot(graph.root)
            report = build_coverage_gap_report(graph, source_memory, policy=GapSelectionPolicy(max_cards=10))
            family = registered_llm_discovery_family(valid_family_spec())
            context = _context(report.report_sha256, source_memory.snapshot_sha256, family_id=family.discovery_family_id)
            drafts = parse_evolution_hypothesis_response(response_payload(), context=context)
            batch = approve_evolution_hypotheses(
                drafts, _approvals(context, drafts), context=context, expected_approval_role="research_reviewer"
            )
            objects = _synthetic_slot_objects(root, family, context, batch)
            seal_input = {
                "approval_batch": batch, "evolution_context": context, "slot_objects": objects,
                "llm_campaign_spec_hashes": ("1" * 64, "2" * 64),
                "prompt_bundle_hashes": ("3" * 64, "4" * 64, "5" * 64),
                "model_identity": "synthetic-recorded-provider",
                "generator_identity_hashes": {
                    arm.arm_id: sha256_json(arm.model_dump(mode="json")) for arm in family.spec.arm_specs
                },
                "evaluation_dependency_intervals": (),
            }
            with patch("factor_miner.llm_orchestrator.platform.system", return_value="Linux"):
                seal = seal_completed_generation(
                    family.discovery_family_id, GenerationSealDependencies(root, family, NOW), seal_input
                )

            def publish_round(run_id: str) -> dict[str, object]:
                publish_run_artifacts(root, run_id, {
                    "run/input_manifest.json": json.dumps({
                        "family_id": family.discovery_family_id,
                        "generation_seal_id": seal.generation_seal_id,
                        "generation_manifest_sha256": seal.manifest_sha256,
                        "evaluation_policy_id": "synthetic-cross-round",
                        "data_contract_identity_hash": seal.manifest.data_contract_identity_hash,
                        "statistical_budget_hash": "7" * 64,
                        "published_at": NOW.isoformat(),
                    }, sort_keys=True).encode(),
                })
                slots = tuple({
                    "slot_id": slot_id,
                    "status": "completed" if index <= 60 else "not_executed",
                } for index, slot_id in enumerate(expected_candidate_slot_ids(family.spec), start=1))
                return publish_campaign_memory(
                    root, run_id=run_id, family_id=family.discovery_family_id,
                    generation_seal_id=seal.generation_seal_id, source_manifest_sha256="6" * 64,
                    data_contract_identity_hash=seal.manifest.data_contract_identity_hash,
                    slot_evaluations=slots, published_at=NOW,
                    evolution_metadata={"coverage_graph_id": graph.manifest.coverage_graph_id},
                )

            first = publish_round("run_" + "1" * 24)
            second = publish_round("run_" + "2" * 24)
            self.assertEqual(first["entry_count"], 120)
            self.assertEqual(second["entry_count"], 240)
            self.assertEqual(second["terminal_counts"], {"completed": 120, "not_executed": 120})
            self.assertEqual(second["memory_snapshot_sha256"], ResearchMemoryStore(root).load_snapshot(second["memory_snapshot_id"]).snapshot_sha256)

            store = ResearchMemoryStore(root)
            store.verify()
            snapshot = store.load_snapshot(str(second["memory_snapshot_id"]))
            self.assertEqual(snapshot.entry_count, 240)
            self.assertEqual(len(snapshot.entry_ids), 240)
            projected = project_run_artifacts(root, "run_" + "2" * 24, InMemoryDashboardStore())
            self.assertEqual(projected.memory_snapshot["entry_count"], 240)
            self.assertEqual(projected.memory_snapshot["terminal_counts"], {"completed": 120, "not_executed": 120})
            self.assertEqual(len(projected.memory_entries), 240)
            self.assertEqual({entry["run_id"] for entry in projected.memory_entries}, {
                "run_" + "1" * 24, "run_" + "2" * 24,
            })

            class Cursor:
                def fetchone(self):
                    return None

            class Connection:
                def __init__(self):
                    self.sql = []
                    self.params = []

                def execute(self, statement, params=()):
                    self.sql.append(statement)
                    self.params.append(params)
                    return Cursor()

            connection = Connection()
            projection_payload = projected.model_dump(mode="json")
            projection_payload["campaign_metadata"] = {"family_id": family.discovery_family_id}
            projection_payload["evolution_metadata"] = {
                "context_id": "evolutionctx_" + "3" * 24,
                "context_sha256": "4" * 64,
                "approval_batch_hash": "5" * 64,
                "coverage_graph_id": graph.manifest.coverage_graph_id,
                "coverage_graph_manifest_hash": "6" * 64,
                "memory_snapshot_hash": second["memory_snapshot_sha256"],
                "gap_report_hash": "7" * 64,
                "design_policy_hash": "8" * 64,
                "hypothesis_count": 0,
                "slot_count": 120,
                "approval_count": 0,
                "gap_summary": {
                    "category_counts": {"structural": 0, "market_regime": 0, "data_availability": 0},
                    "sanitized_labels": [],
                    "truncated_count": 0,
                },
            }
            PostgresDashboardStore._project_evolution_memory(
                connection, "run_" + "2" * 24, projection_payload
            )
            entry_inserts = [statement for statement in connection.sql if "INSERT INTO research_memory_entries" in statement]
            self.assertEqual(len(entry_inserts), 240)
            for table in (
                "research_evolution_batches", "research_gap_summaries",
                "research_memory_snapshots", "research_memory_entries",
            ):
                self.assertTrue(any(f"INSERT INTO {table}" in statement for statement in connection.sql))
            parent_index = next(index for index, statement in enumerate(connection.sql) if "INSERT INTO research_memory_snapshots" in statement)
            first_child_index = next(index for index, statement in enumerate(connection.sql) if "INSERT INTO research_memory_entries" in statement)
            self.assertLess(parent_index, first_child_index)

    def test_append_failure_does_not_leave_snapshot_or_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("factor_miner.research_memory.atomic_write_immutable", side_effect=OSError("append failed")):
                with self.assertRaises(OSError):
                    publish_campaign_memory(
                        root, run_id="run_" + "9" * 24, family_id="llmfamily_" + "a" * 24,
                        generation_seal_id="llmseal_" + "b" * 24, source_manifest_sha256="c" * 64,
                        data_contract_identity_hash="d" * 64, slot_evaluations=self._slots(),
                        published_at=datetime.now(timezone.utc),
                    )
            memory_root = root / "research_memory"
            self.assertFalse((memory_root / "snapshots").exists())
            self.assertFalse((memory_root / "references").exists())

    def test_batch_failure_rolls_back_objects_events_index_and_next_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = tuple(ResearchMemoryEntry(
                memory_entry_id=f"mementry_{index:024x}", entry_kind="campaign_slot",
                discovery_family_id="llmfamily_" + "1" * 24, generation_seal_id="llmseal_" + "2" * 24,
                run_id="run_" + "3" * 24, hypothesis_slot_id=f"H{(index - 1) % 10 + 1:02d}",
                candidate_slot_id=f"coverage_outcome_llm:{index:03d}", hypothesis_spec_hash="4" * 64,
                candidate_spec_hash="5" * 64, ast_hash=f"{index:064x}", coverage_graph_id="covgraph_" + "6" * 24,
                memory_snapshot_id="memsnap_" + "7" * 24, field_signature=("close",),
                operator_signature=("rolling",), temporal_signature=("short",), structure_signature=("field_set",),
                gap_labels=("coverage",), terminal_state="not_executed", evaluation_summary=EvaluationSummary(
                    outcome_band="inconclusive", rank_ic_band="unknown", hac_significance_band="unknown",
                    portfolio_band="unknown", redundancy_band="unknown"), data_identity_summary=DataIdentitySummary(
                    release_hash="8" * 64, manifest_hash="9" * 64, field_registry_hash="a" * 64, cutoff_band="stable"),
                created_at=datetime.now(timezone.utc),
            ) for index in range(1, 121))
            store = ResearchMemoryStore(research_memory_root=root / "research_memory")
            with patch.object(store, "_assert_entry_contract"), self.assertRaisesRegex(RuntimeError, "injected"):
                store.publish_batch(entries, lambda _events: (_ for _ in ()).throw(RuntimeError("injected")))
            self.assertFalse((root / "research_memory" / "entries.jsonl").exists())
            self.assertEqual(list((root / "research_memory").glob("entry_objects/*.json")), [])
            self.assertFalse((root / "research_memory" / "index.json").exists())
            self.assertEqual(list((root / "research_memory").glob("snapshots/*.json")), [])
            self.assertEqual(list((root / "research_memory").glob("references/*.json")), [])
            with patch.object(store, "_assert_entry_contract"):
                store.publish_batch(entries, lambda events: {"count": len(events)})
                store.verify()
            self.assertEqual(store.load_entry(entries[-1].memory_entry_id), entries[-1])

    def test_snapshot_parent_is_projected_before_entry_child(self) -> None:
        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def __init__(self):
                self.sql = []
                self.params = []

            def execute(self, statement, params=()):
                self.sql.append(statement)
                self.params.append(params)
                return Cursor()

        connection = Connection()
        PostgresDashboardStore._project_evolution_memory(connection, "run_" + "1" * 24, {
            "memory_snapshot": {
                "memory_snapshot_id": "memsnap_" + "2" * 24,
                "memory_snapshot_sha256": "3" * 64,
                "entry_count": 120,
                "source_manifest_sha256": "4" * 64,
                "published_at": "2026-08-11T00:00:00+00:00",
            },
            "memory_entries": [{
                "memory_entry_id": "mementry_" + "5" * 24,
                "discovery_family_id": "llmfamily_" + "6" * 24,
                "generation_seal_id": "llmseal_" + "7" * 24,
                "run_id": "run_" + "1" * 24,
                "hypothesis_slot_id": "H01",
                "candidate_slot_id": "coverage_outcome_llm:001",
                "candidate_spec_hash": "8" * 64,
                "ast_hash": "9" * 64,
                "terminal_state": "not_executed",
                "entry_sha256": "a" * 64,
                "source_manifest_sha256": "4" * 64,
                "published_at": "2026-08-11T00:00:00+00:00",
            }],
        })
        parent_index = next(index for index, statement in enumerate(connection.sql) if "INSERT INTO research_memory_snapshots" in statement)
        child_index = next(index for index, statement in enumerate(connection.sql) if "INSERT INTO research_memory_entries" in statement)
        self.assertLess(parent_index, child_index)
        migration = Path("dashboard/migrations/006_research_evolution_memory.sql").read_text()
        self.assertIn("REFERENCES research_memory_snapshots(memory_snapshot_id) NOT DEFERRABLE", migration)

    def test_typed_entry_projection_uses_schema_fields_and_non_null_values(self) -> None:
        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params=()):
                self.calls.append((statement, params))
                return Cursor()

        connection = Connection()
        PostgresDashboardStore._project_evolution_memory(connection, "run_" + "1" * 24, {
            "memory_snapshot": {
                "memory_snapshot_id": "memsnap_" + "2" * 24, "memory_snapshot_sha256": "3" * 64,
                "entry_count": 120, "coverage_graph_id": "covgraph_" + "4" * 24,
                "source_family_ids": ["llmfamily_" + "5" * 24], "terminal_counts": {"completed": 120},
                "source_manifest_sha256": "6" * 64, "published_at": "2026-08-11T00:00:00+00:00",
            },
            "memory_entries": [{
                "memory_entry_id": "mementry_" + "7" * 24, "discovery_family_id": "llmfamily_" + "5" * 24,
                "generation_seal_id": "llmseal_" + "8" * 24, "run_id": "run_" + "1" * 24,
                "hypothesis_slot_id": "H01", "candidate_slot_id": "coverage_outcome_llm:001",
                "candidate_spec_hash": "9" * 64, "ast_hash": "a" * 64, "coverage_graph_id": "covgraph_" + "4" * 24,
                "terminal_state": "completed", "entry_sha256": "b" * 64,
                "field_signature": ["close"], "operator_signature": ["rolling"], "temporal_signature": ["short"],
                "structure_signature": ["field_set"], "gap_labels": ["coverage"],
                "evaluation_summary": {"outcome_band": "passed"},
                "data_identity_summary": {"manifest_hash": "c" * 64, "release_hash": "d" * 64},
                "created_at": "2026-08-10T00:00:00+00:00",
            }],
        })
        child = next(params for statement, params in connection.calls if "INSERT INTO research_memory_entries" in statement)
        self.assertEqual(child[14], '{"field_signature":["close"],"operator_signature":["rolling"],"temporal_signature":["short"],"structure_signature":["field_set"],"gap_labels":["coverage"]}')
        self.assertIn('"data_identity_summary"', child[15])
        self.assertEqual(child[17], "c" * 64)
        self.assertEqual(child[18], "2026-08-10T00:00:00+00:00")
        self.assertTrue(all(child[index] is not None for index in (*range(12), 14, 15, 16, 17, 18)))

    def test_evolution_batch_and_gap_are_projected_with_memory_identity_chain(self) -> None:
        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def __init__(self):
                self.sql = []
                self.params = []

            def execute(self, statement, params=()):
                self.sql.append(statement)
                self.params.append(params)
                return Cursor()

        connection = Connection()
        PostgresDashboardStore._project_evolution_memory(connection, "run_" + "1" * 24, {
            "campaign_metadata": {"family_id": "llmfamily_" + "6" * 24},
            "evolution_metadata": {
                "context_id": "evolutionctx_" + "2" * 24, "context_sha256": "3" * 64,
                "approval_batch_hash": "4" * 64, "coverage_graph_id": "covgraph_" + "5" * 24,
                "coverage_graph_manifest_hash": "6" * 64, "memory_snapshot_hash": "7" * 64,
                "gap_report_hash": "8" * 64, "design_policy_hash": "9" * 64,
                "hypothesis_count": 10, "slot_count": 120,
                "approval_count": 10,
                "gap_summary": {"category_counts": {"structural": 1, "market_regime": 2, "data_availability": 3}, "sanitized_labels": ["coverage"], "truncated_count": 4},
            },
            "memory_snapshot": {
                "memory_snapshot_id": "memsnap_" + "a" * 24, "memory_snapshot_sha256": "b" * 64,
                "entry_count": 120, "source_manifest_sha256": "c" * 64,
                "terminal_counts": {"not_executed": 120},
                "published_at": "2026-08-11T00:00:00+00:00",
            },
            "memory_entries": [],
        })
        joined = "\n".join(connection.sql)
        for table in ("research_evolution_batches", "research_gap_summaries", "research_memory_snapshots"):
            self.assertIn(f"INSERT INTO {table}", joined)
        batch_params = next(params for statement, params in zip(connection.sql, connection.params) if "INSERT INTO research_evolution_batches" in statement)
        gap_params = next(params for statement, params in zip(connection.sql, connection.params) if "INSERT INTO research_gap_summaries" in statement)
        snapshot_params = next(params for statement, params in zip(connection.sql, connection.params) if "INSERT INTO research_memory_snapshots" in statement)
        self.assertEqual(gap_params, (
            "8" * 64, "evolutionctx_" + "2" * 24, 1, 2, 3,
            '{"labels":["coverage"]}', 4, "6" * 64, "7" * 64,
            "2026-08-11T00:00:00+00:00",
        ))
        self.assertEqual(batch_params[11], 10)
        self.assertEqual(snapshot_params[6], '{"not_executed":120}')

    def test_legacy_nullable_projection_fields_are_backfilled_without_overwrite(self) -> None:
        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self):
                self.sql = []

            def execute(self, statement, params=()):
                self.sql.append(statement)
                if "FROM research_evolution_batches" in statement:
                    return Cursor((
                        "3" * 64, "llmfamily_" + "6" * 24, "4" * 64,
                        "covgraph_" + "5" * 24, "6" * 64, "7" * 64,
                        "8" * 64, "9" * 64, 10, 120, None, "d" * 64,
                    ))
                if "FROM research_gap_summaries" in statement:
                    return Cursor(("evolutionctx_" + "2" * 24, 1, 2, 3, {"labels": ["coverage"]}, None, "6" * 64, "7" * 64))
                if "FROM research_memory_snapshots" in statement:
                    return Cursor(("b" * 64, None))
                return Cursor(None)

        connection = Connection()
        PostgresDashboardStore._project_evolution_memory(connection, "run_" + "1" * 24, {
            "campaign_metadata": {"family_id": "llmfamily_" + "6" * 24},
            "evolution_metadata": {
                "context_id": "evolutionctx_" + "2" * 24, "context_sha256": "3" * 64,
                "approval_batch_hash": "4" * 64, "coverage_graph_id": "covgraph_" + "5" * 24,
                "coverage_graph_manifest_hash": "6" * 64, "memory_snapshot_hash": "7" * 64,
                "gap_report_hash": "8" * 64, "design_policy_hash": "9" * 64,
                "hypothesis_count": 10, "slot_count": 120, "approval_count": 10,
                "gap_summary": {"category_counts": {"structural": 1, "market_regime": 2, "data_availability": 3}, "sanitized_labels": ["coverage"], "truncated_count": 4},
            },
            "memory_snapshot": {
                "memory_snapshot_id": "memsnap_" + "a" * 24, "memory_snapshot_sha256": "b" * 64,
                "entry_count": 120, "source_manifest_sha256": "c" * 64,
                "terminal_counts": {"not_executed": 120}, "published_at": "2026-08-11T00:00:00+00:00",
            },
            "memory_entries": [],
        })
        self.assertIn("COALESCE(research_evolution_batches.approval_count", "\n".join(connection.sql))
        self.assertIn("COALESCE(research_gap_summaries.truncated_count", "\n".join(connection.sql))
        self.assertIn("COALESCE(research_memory_snapshots.terminal_counts", "\n".join(connection.sql))

    def test_migration_legacy_paths_keep_nullable_markers_and_json_invariants(self) -> None:
        migration = Path("dashboard/migrations/006_research_evolution_memory.sql").read_text()
        self.assertIn("ADD COLUMN IF NOT EXISTS approval_count INTEGER;", migration)
        self.assertIn("CHECK (approval_count IS NULL OR approval_count >= 0)", migration)
        self.assertIn("ADD COLUMN IF NOT EXISTS truncated_count INTEGER;", migration)
        self.assertIn("CHECK (truncated_count IS NULL OR truncated_count >= 0)", migration)
        self.assertIn("ADD COLUMN IF NOT EXISTS terminal_counts JSONB;", migration)
        self.assertIn("CHECK (terminal_counts IS NULL OR jsonb_typeof(terminal_counts) = 'object')", migration)
        self.assertIn("CHECK (jsonb_typeof(terminal_counts) = 'object')", migration)
        self.assertIn("entry_count INTEGER NOT NULL", migration)
        self.assertIn("CHECK (entry_count >= 0)", migration)
        self.assertIn("research_memory_snapshots_entry_count_nonnegative", migration)
        self.assertIn("pg_get_expr(conbin, conrelid)", migration)
        self.assertIn("DROP CONSTRAINT %I", migration)
        self.assertNotIn("entry_count = 120", migration)

    def test_evolution_batch_replay_rejects_independent_identity_conflict(self) -> None:
        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def execute(self, statement, params=()):
                if "FROM research_evolution_batches" in statement:
                    return Cursor((
                        "3" * 64, "llmfamily_" + "6" * 24, "x" * 64,
                        "covgraph_" + "5" * 24, "6" * 64, "7" * 64,
                        "8" * 64, "9" * 64, 10, 120, 10, "c" * 64,
                    ))
                return Cursor(None)

        with self.assertRaisesRegex(Exception, "approval_batch_hash"):
            PostgresDashboardStore._project_evolution_memory(Connection(), "run_" + "1" * 24, {
                "campaign_metadata": {"family_id": "llmfamily_" + "6" * 24},
                "evolution_metadata": {
                    "context_id": "evolutionctx_" + "2" * 24, "context_sha256": "3" * 64,
                    "approval_batch_hash": "4" * 64, "coverage_graph_id": "covgraph_" + "5" * 24,
                    "coverage_graph_manifest_hash": "6" * 64, "memory_snapshot_hash": "7" * 64,
                    "gap_report_hash": "8" * 64, "design_policy_hash": "9" * 64,
                    "hypothesis_count": 10, "slot_count": 120, "approval_count": 10,
                },
                "memory_snapshot": {
                    "memory_snapshot_id": "memsnap_" + "a" * 24,
                    "memory_snapshot_sha256": "b" * 64,
                    "entry_count": 120, "source_manifest_sha256": "c" * 64,
                    "published_at": "2026-08-11T00:00:00+00:00",
                },
            })

    def test_polluted_family_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            publish_campaign_memory(
                Path(tempfile.mkdtemp()), run_id="run_" + "7" * 24,
                family_id="llmfamily_1bae19965638a6ac9620e0b0", generation_seal_id="llmseal_" + "8" * 24,
                source_manifest_sha256="e" * 64, data_contract_identity_hash="f" * 64,
                slot_evaluations=self._slots(), published_at=datetime.now(timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
