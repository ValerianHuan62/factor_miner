from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.research_evolution_schema import (
    DataIdentitySummary,
    EvaluationSummary,
    POLLUTED_DISCOVERY_FAMILY_ID,
    ResearchMemoryEntry,
)
from factor_miner.research_memory import MemorySnapshotPolicy, ResearchMemoryStore


class _FakeCoverageManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_graph_id: str = Field(pattern=r"^covgraph_[0-9a-f]{24}$")
    manifest_version: str = "1"
    factor_count: int = 1


class ResearchMemoryStoreTest(unittest.TestCase):
    def _entry(
        self,
        *,
        memory_entry_id: str = "mementry_aaaaaaaaaaaaaaaaaaaaaaaa",
        discovery_family_id: str = "llmfamily_aaaaaaaaaaaaaaaaaaaaaaaa",
        generation_seal_id: str = "llmseal_aaaaaaaaaaaaaaaaaaaaaaaa",
        run_id: str = "run_aaaaaaaaaaaaaaaaaaaaaaaa",
        memory_snapshot_id: str = "memsnap_aaaaaaaaaaaaaaaaaaaaaaaa",
        candidate_slot_id: str = "coverage_outcome_llm:001",
        created_at: datetime | None = None,
        terminal_state: str = "completed",
        failure_reason: str | None = None,
    ) -> ResearchMemoryEntry:
        return ResearchMemoryEntry(
            memory_entry_id=memory_entry_id,
            entry_kind="evaluation_result",
            discovery_family_id=discovery_family_id,
            generation_seal_id=generation_seal_id,
            run_id=run_id,
            hypothesis_slot_id="H01",
            candidate_slot_id=candidate_slot_id,
            hypothesis_spec_hash="1" * 64,
            candidate_spec_hash="2" * 64,
            ast_hash="3" * 64,
            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
            memory_snapshot_id=memory_snapshot_id,
            field_signature=("close",),
            operator_signature=("rolling",),
            temporal_signature=("short",),
            structure_signature=("field_set",),
            gap_labels=("coverage",),
            terminal_state=terminal_state,
            failure_reason=failure_reason,
            duplicate_of=None,
            evaluation_summary=EvaluationSummary(
                outcome_band="passed",
                rank_ic_band="high",
                hac_significance_band="supported",
                portfolio_band="medium",
                redundancy_band="low",
            ),
            data_identity_summary=DataIdentitySummary(
                release_hash="4" * 64,
                manifest_hash="5" * 64,
                field_registry_hash="6" * 64,
                cutoff_band="stable",
            ),
            supersedes_entry_id=None,
            created_at=created_at or datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

    def _write_input_manifest(
        self,
        artifact_root: Path,
        *,
        run_id: str,
        family_id: str,
        seal_id: str,
        data_contract_identity_hash: str = "7" * 64,
        generation_manifest_sha256: str = "a" * 64,
        evaluation_policy_id: str = "policy_memory_v1",
        statistical_budget_hash: str = "b" * 64,
        published_at: datetime | None = None,
        extra_fields: dict[str, object] | None = None,
    ) -> None:
        path = artifact_root / "artifacts" / "runs" / run_id / "run" / "input_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "family_id": family_id,
            "generation_seal_id": seal_id,
            "generation_manifest_sha256": generation_manifest_sha256,
            "evaluation_policy_id": evaluation_policy_id,
            "data_contract_identity_hash": data_contract_identity_hash,
            "statistical_budget_hash": statistical_budget_hash,
            "published_at": (published_at or datetime(2026, 8, 1, tzinfo=timezone.utc)).isoformat(),
        }
        if extra_fields:
            payload.update(extra_fields)
        path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def _mock_ledger(self, entry: ResearchMemoryEntry) -> SimpleNamespace:
        return SimpleNamespace(
            load_family=lambda family_id: SimpleNamespace(discovery_family_id=family_id),
            load_generation_seal=lambda family_id: SimpleNamespace(
                generation_seal_id=entry.generation_seal_id,
                manifest=SimpleNamespace(
                    slot_object_hashes={entry.candidate_slot_id: "9" * 64},
                    data_contract_identity_hash="7" * 64,
                ),
            ),
        )

    def _mock_ledger_for_entries(
        self,
        *entries: ResearchMemoryEntry,
    ) -> SimpleNamespace:
        slot_hashes = {
            entry.candidate_slot_id: "9" * 64
            for entry in entries
        }
        return SimpleNamespace(
            load_family=lambda family_id: SimpleNamespace(discovery_family_id=family_id),
            load_generation_seal=lambda family_id: SimpleNamespace(
                generation_seal_id=entries[0].generation_seal_id,
                manifest=SimpleNamespace(
                    slot_object_hashes=slot_hashes,
                    data_contract_identity_hash="7" * 64,
                ),
            ),
        )

    def test_append_entry_is_immutable_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                first = store.append_entry(entry)
                second = store.append_entry(entry)
            self.assertEqual(first, second)
            self.assertEqual(len(store.entries_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_append_entry_accepts_complete_formal_input_manifest_shape(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
                published_at=entry.created_at,
                generation_manifest_sha256="c" * 64,
                evaluation_policy_id="evaluation_policy_memory_v1",
                statistical_budget_hash="d" * 64,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                path = store.append_entry(entry)
            self.assertTrue(path.exists())

    def test_append_entry_rejects_unknown_extra_input_manifest_field(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
                published_at=entry.created_at,
                extra_fields={"unexpected_field": "forbidden"},
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                with self.assertRaises(FactorMinerError) as error:
                    store.append_entry(entry)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_duplicate_entry_id_with_different_content_fails(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            changed = self._entry(failure_reason="后写入试图覆盖原条目")
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                store.append_entry(entry)
                with self.assertRaises(FactorMinerError) as error:
                    store.append_entry(changed)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_verify_rejects_truncated_jsonl(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                store.append_entry(entry)
                truncated = store.entries_path.read_bytes().rstrip(b"\n")
                store.entries_path.write_bytes(truncated)
                with self.assertRaises(FactorMinerError) as error:
                    store.verify()
            self.assertEqual(error.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_build_snapshot_respects_cutoff_and_revalidates_hash(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            first = self._entry(
                memory_entry_id="mementry_bbbbbbbbbbbbbbbbbbbbbbbb",
                run_id="run_bbbbbbbbbbbbbbbbbbbbbbbb",
                created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            second = self._entry(
                memory_entry_id="mementry_cccccccccccccccccccccccc",
                run_id="run_cccccccccccccccccccccccc",
                candidate_slot_id="coverage_outcome_llm:002",
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            for item in (first, second):
                self._write_input_manifest(
                    artifact_root,
                    run_id=item.run_id,
                    family_id=item.discovery_family_id,
                    seal_id=item.generation_seal_id,
                    published_at=item.created_at,
                )
            policy = MemorySnapshotPolicy(
                allowed_source_family_ids=(first.discovery_family_id,),
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger_for_entries(first, second)),
                patch("factor_miner.research_memory.verify_published_run", side_effect=lambda root, run_id: SimpleNamespace(run_id=run_id)),
                patch(
                    "factor_miner.research_memory.verify_coverage_graph",
                    return_value=SimpleNamespace(
                        manifest=_FakeCoverageManifest(
                            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa"
                        )
                    ),
                ),
            ):
                store.append_entry(first)
                store.append_entry(second)
                snapshot = store.build_snapshot(
                    cutoff=first.created_at,
                    source_family_ids=(first.discovery_family_id,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=policy,
                )
                self.assertEqual(snapshot.entry_count, 1)
                self.assertEqual(snapshot.entry_ids, (first.memory_entry_id,))
                self.assertEqual(
                    snapshot.entry_bindings,
                    (
                        snapshot.EntryBinding(
                            memory_entry_id=first.memory_entry_id,
                            entry_sha256=first.entry_sha256,
                        ),
                    ),
                )
                loaded = store.load_snapshot(snapshot.memory_snapshot_id)
                self.assertEqual(loaded.snapshot_sha256, snapshot.snapshot_sha256)
                path = store.snapshots_root / f"{snapshot.memory_snapshot_id}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["entry_hashes"] = ["f" * 64]
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(FactorMinerError) as error:
                    store.load_snapshot(snapshot.memory_snapshot_id)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_snapshot_binding_tamper_fails_for_multiple_entries(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            first = self._entry(
                memory_entry_id="mementry_111111111111111111111111",
                run_id="run_111111111111111111111111",
                created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            second = self._entry(
                memory_entry_id="mementry_222222222222222222222222",
                run_id="run_222222222222222222222222",
                candidate_slot_id="coverage_outcome_llm:002",
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            for item in (first, second):
                self._write_input_manifest(
                    artifact_root,
                    run_id=item.run_id,
                    family_id=item.discovery_family_id,
                    seal_id=item.generation_seal_id,
                    published_at=item.created_at,
                )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger_for_entries(first, second)),
                patch("factor_miner.research_memory.verify_published_run", side_effect=lambda root, run_id: SimpleNamespace(run_id=run_id)),
                patch(
                    "factor_miner.research_memory.verify_coverage_graph",
                    return_value=SimpleNamespace(
                        manifest=_FakeCoverageManifest(
                            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa"
                        )
                    ),
                ),
            ):
                store.append_entry(first)
                store.append_entry(second)
                snapshot = store.build_snapshot(
                    cutoff=second.created_at,
                    source_family_ids=(first.discovery_family_id,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=MemorySnapshotPolicy(
                        allowed_source_family_ids=(first.discovery_family_id,),
                    ),
                )
                self.assertEqual(snapshot.entry_count, 2)
                self.assertEqual(
                    tuple(binding.memory_entry_id for binding in snapshot.entry_bindings),
                    snapshot.entry_ids,
                )
                path = store.snapshots_root / f"{snapshot.memory_snapshot_id}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["entry_bindings"][1]["entry_sha256"] = "f" * 64
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(FactorMinerError) as error:
                    store.load_snapshot(snapshot.memory_snapshot_id)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_build_snapshot_rejects_polluted_family(self) -> None:
        with TemporaryDirectory() as directory:
            store = ResearchMemoryStore(Path(directory))
            policy = MemorySnapshotPolicy(
                allowed_source_family_ids=("llmfamily_dddddddddddddddddddddddd",),
            )
            with self.assertRaises(FactorMinerError) as error:
                store.build_snapshot(
                    cutoff=datetime.now(timezone.utc),
                    source_family_ids=(POLLUTED_DISCOVERY_FAMILY_ID,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=policy,
                )
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_can_load_from_formal_files_without_index(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
                published_at=entry.created_at,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
                patch(
                    "factor_miner.research_memory.verify_coverage_graph",
                    return_value=SimpleNamespace(
                        manifest=_FakeCoverageManifest(
                            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa"
                        )
                    ),
                ),
            ):
                store.append_entry(entry)
                snapshot = store.build_snapshot(
                    cutoff=entry.created_at + timedelta(days=1),
                    source_family_ids=(entry.discovery_family_id,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=MemorySnapshotPolicy(
                        allowed_source_family_ids=(entry.discovery_family_id,),
                    ),
                )
            store.index_path.unlink()
            self.assertEqual(store.load_entry(entry.memory_entry_id).entry_sha256, entry.entry_sha256)
            self.assertEqual(store.load_snapshot(snapshot.memory_snapshot_id).snapshot_sha256, snapshot.snapshot_sha256)

    def test_missing_input_manifest_or_data_identity_fails(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                with self.assertRaises(FactorMinerError) as error:
                    store.append_entry(entry)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
                data_contract_identity_hash="8" * 64,
                published_at=entry.created_at,
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                with self.assertRaises(FactorMinerError) as error:
                    store.append_entry(entry)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)

    def test_concurrent_writer_fails(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            self._write_input_manifest(
                artifact_root,
                run_id=entry.run_id,
                family_id=entry.discovery_family_id,
                seal_id=entry.generation_seal_id,
                published_at=entry.created_at,
            )
            store.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                    patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
                ):
                    with self.assertRaises(FactorMinerError) as error:
                        store.append_entry(entry)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(error.exception.code, FailureCode.LEDGER_CONCURRENT_WRITER)

    def test_index_keeps_two_entries_after_competition_and_retry(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            first_store = ResearchMemoryStore(artifact_root)
            second_store = ResearchMemoryStore(artifact_root)
            first = self._entry(
                memory_entry_id="mementry_abababababababababababab",
                run_id="run_abababababababababababab",
            )
            second = self._entry(
                memory_entry_id="mementry_cdcdcdcdcdcdcdcdcdcdcdcd",
                run_id="run_cdcdcdcdcdcdcdcdcdcdcdcd",
                candidate_slot_id="coverage_outcome_llm:002",
            )
            for item in (first, second):
                self._write_input_manifest(
                    artifact_root,
                    run_id=item.run_id,
                    family_id=item.discovery_family_id,
                    seal_id=item.generation_seal_id,
                    published_at=item.created_at,
                )
            ledger = self._mock_ledger_for_entries(first, second)
            locked = threading.Event()
            release = threading.Event()
            original_write_index = first_store._write_index
            first_call = {"seen": False}
            thread_error: list[BaseException] = []

            def blocking_write_index(events: tuple[object, ...], snapshots: dict[str, object]) -> None:
                if not first_call["seen"]:
                    first_call["seen"] = True
                    locked.set()
                    release.wait(timeout=5)
                original_write_index(events, snapshots)

            def append_first() -> None:
                try:
                    first_store.append_entry(first)
                except BaseException as error:  # pragma: no cover - 测试需保留异常
                    thread_error.append(error)

            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=ledger),
                patch("factor_miner.research_memory.verify_published_run", side_effect=lambda root, run_id: SimpleNamespace(run_id=run_id)),
                patch.object(first_store, "_write_index", side_effect=blocking_write_index),
            ):
                worker = threading.Thread(target=append_first)
                worker.start()
                self.assertTrue(locked.wait(timeout=5))
                with self.assertRaises(FactorMinerError) as error:
                    second_store.append_entry(second)
                self.assertEqual(error.exception.code, FailureCode.LEDGER_CONCURRENT_WRITER)
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(thread_error, [])
                second_store.append_entry(second)

            index_payload = json.loads(first_store.index_path.read_text(encoding="utf-8"))
            self.assertEqual(index_payload["entry_count"], 2)
            self.assertEqual(
                set(index_payload["entries"]),
                {first.memory_entry_id, second.memory_entry_id},
            )

    def test_build_snapshot_requires_published_at_before_cutoff(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            first = self._entry(
                memory_entry_id="mementry_313131313131313131313131",
                run_id="run_313131313131313131313131",
                created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            second = self._entry(
                memory_entry_id="mementry_414141414141414141414141",
                run_id="run_414141414141414141414141",
                candidate_slot_id="coverage_outcome_llm:002",
                created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            self._write_input_manifest(
                artifact_root,
                run_id=first.run_id,
                family_id=first.discovery_family_id,
                seal_id=first.generation_seal_id,
                published_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
            )
            self._write_input_manifest(
                artifact_root,
                run_id=second.run_id,
                family_id=second.discovery_family_id,
                seal_id=second.generation_seal_id,
                published_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger_for_entries(first, second)),
                patch("factor_miner.research_memory.verify_published_run", side_effect=lambda root, run_id: SimpleNamespace(run_id=run_id)),
                patch(
                    "factor_miner.research_memory.verify_coverage_graph",
                    return_value=SimpleNamespace(
                        manifest=_FakeCoverageManifest(
                            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa"
                        )
                    ),
                ),
            ):
                store.append_entry(first)
                store.append_entry(second)
                snapshot = store.build_snapshot(
                    cutoff=datetime(2026, 8, 3, tzinfo=timezone.utc),
                    source_family_ids=(first.discovery_family_id,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=MemorySnapshotPolicy(
                        allowed_source_family_ids=(first.discovery_family_id,),
                    ),
                )
            self.assertEqual(snapshot.entry_ids, (first.memory_entry_id,))

    def test_verify_rejects_snapshot_self_consistent_but_not_backed_by_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            first = self._entry(
                memory_entry_id="mementry_515151515151515151515151",
                run_id="run_515151515151515151515151",
                created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            second = self._entry(
                memory_entry_id="mementry_616161616161616161616161",
                run_id="run_616161616161616161616161",
                candidate_slot_id="coverage_outcome_llm:002",
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            for item in (first, second):
                self._write_input_manifest(
                    artifact_root,
                    run_id=item.run_id,
                    family_id=item.discovery_family_id,
                    seal_id=item.generation_seal_id,
                    published_at=item.created_at,
                )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger_for_entries(first, second)),
                patch("factor_miner.research_memory.verify_published_run", side_effect=lambda root, run_id: SimpleNamespace(run_id=run_id)),
                patch(
                    "factor_miner.research_memory.verify_coverage_graph",
                    return_value=SimpleNamespace(
                        manifest=_FakeCoverageManifest(
                            coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa"
                        )
                    ),
                ),
            ):
                store.append_entry(first)
                store.append_entry(second)
                snapshot = store.build_snapshot(
                    cutoff=second.created_at,
                    source_family_ids=(first.discovery_family_id,),
                    coverage_graph_id="covgraph_aaaaaaaaaaaaaaaaaaaaaaaa",
                    policy=MemorySnapshotPolicy(
                        allowed_source_family_ids=(first.discovery_family_id,),
                    ),
                )
                path = store.snapshots_root / f"{snapshot.memory_snapshot_id}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["entry_hashes"][1] = "f" * 64
                payload["entry_bindings"][1]["entry_sha256"] = "f" * 64
                payload["snapshot_sha256"] = None
                repaired = snapshot.__class__.model_validate(payload)
                path.write_text(json.dumps(repaired.model_dump(mode="json")), encoding="utf-8")
                index_payload = json.loads(store.index_path.read_text(encoding="utf-8"))
                index_payload["snapshots"][snapshot.memory_snapshot_id] = repaired.snapshot_sha256
                store.index_path.write_text(json.dumps(index_payload), encoding="utf-8")
                with self.assertRaises(FactorMinerError) as error:
                    store.verify()
            self.assertEqual(error.exception.code, FailureCode.LEDGER_CORRUPT)

    def test_append_entry_requires_formal_published_at(self) -> None:
        with TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            store = ResearchMemoryStore(artifact_root)
            entry = self._entry()
            path = artifact_root / "artifacts" / "runs" / entry.run_id / "run" / "input_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "family_id": entry.discovery_family_id,
                        "generation_seal_id": entry.generation_seal_id,
                        "generation_manifest_sha256": "a" * 64,
                        "evaluation_policy_id": "policy_memory_v1",
                        "data_contract_identity_hash": "7" * 64,
                        "statistical_budget_hash": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("factor_miner.research_memory.LLMDiscoveryLedger", return_value=self._mock_ledger(entry)),
                patch("factor_miner.research_memory.verify_published_run", return_value=SimpleNamespace(run_id=entry.run_id)),
            ):
                with self.assertRaises(FactorMinerError) as error:
                    store.append_entry(entry)
            self.assertEqual(error.exception.code, FailureCode.MEMORY_IMMUTABILITY_VIOLATION)


if __name__ == "__main__":
    unittest.main()
