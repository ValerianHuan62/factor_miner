"""Task 8 记忆辅助演化 CLI 的人工闸门与负例测试。"""

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from factor_miner.cli import _draft_batch_sha256, _require_raw_approval_batch_identity, app
from factor_miner.research_evolution import parse_evolution_hypothesis_response
from factor_miner.research_evolution_schema import (
    CoverageGapCard,
    CoverageGapReport,
    EvolutionHypothesisApproval,
    ResearchMemorySnapshot,
    build_evolution_context,
)
from factor_miner.research_gap import build_coverage_gap_report, load_verified_coverage_graph
from factor_miner.llm_brief import GapSelectionPolicy
from factor_miner.research_memory import MemorySnapshotPolicy, ResearchMemoryStore
from factor_miner.canonical import canonical_json_bytes
from tests.test_research_gap import _publish_graph
from tests.test_research_memory import ResearchMemoryStoreTest


def _context():
    """构造仅含脱敏身份的合成上下文。"""

    return build_evolution_context(
        discovery_family_id="llmfamily_" + "1" * 24,
        coverage_graph_manifest_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        gap_report_hash="c" * 64,
        field_registry_hash="d" * 64,
        evaluation_policy_hash="e" * 64,
        design_policy_hash="f" * 64,
        external_model_redaction_policy="evolution-gap-brief-v1",
    )


def _response():
    """构造正好十个、内容各异的合成逻辑草案。"""

    return {
        "hypotheses": [
            {
                "logical_slot_id": f"H{index:02d}",
                "mechanism_unverified": True,
                "prior_claim": f"主张 {index}",
                "mechanism": f"机制 {index}",
                "expected_direction": "positive",
                "observable_proxy": f"代理 {index}",
                "independent_verification": f"独立验证 {index}",
                "competing_explanations": [f"竞争解释 {index}"],
                "failure_modes": [f"失效方式 {index}"],
                "falsification_path": f"证伪路径 {index}",
                "gap_ids": [f"G{index:03d}"],
                "source_records": [{
                    "source_record_id": f"src-{index:02d}",
                    "claim_fragment": f"来源片段 {index}",
                    "rationale": f"检索理由 {index}",
                    "query_terms": ["price", f"signal{index}"],
                    "year_start": 2000,
                    "year_end": 2026,
                }],
            }
            for index in range(1, 11)
        ]
    }


def _report():
    """构造带完整审计 hash 的最小脱敏缺口报告。"""

    card = CoverageGapCard(
        gap_id="gap_" + "1" * 24,
        gap_category="structural",
        sanitized_labels=("coverage",),
        allowed_field_aliases=("close",),
        allowed_operator_families=("arithmetic",),
        temporal_window_bins=("short",),
        structure_cluster_count_band="low",
        signal_cluster_count_band="low",
        missing_or_failure_risk=("missing",),
    )
    return CoverageGapReport(
        coverage_graph_id="covgraph_" + "1" * 24,
        graph_manifest_hash="a" * 64,
        regime_snapshot_hash="b" * 64,
        memory_snapshot_hash="c" * 64,
        gap_cards=(card,),
        truncated_count=0,
        truncation_reason="未发生截断",
        internal_sort_policy_hash="d" * 64,
    )


class ResearchEvolutionCLITest(unittest.TestCase):
    """验证正式命令存在且批准入口不会被旧命令替代。"""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_evolution_commands_are_explicit(self) -> None:
        result = self.runner.invoke(app, ["evolution", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for command in ("prepare-context", "generate-hypotheses", "approve-hypotheses"):
            self.assertIn(command, result.output)

    def test_campaign_exposes_strict_evolution_bindings(self) -> None:
        result = self.runner.invoke(app, ["campaign", "run-approved", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("evolution-context", result.output)
        self.assertIn("approved-hypotheses", result.output)

    def test_approval_command_requires_all_explicit_inputs(self) -> None:
        result = self.runner.invoke(app, ["evolution", "approve-hypotheses", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for option in ("context", "draft-batch", "decisions", "output"):
            self.assertIn(option, result.output)

    def _approval_inputs(self, directory: Path):
        context = _context()
        report = _report()
        drafts = parse_evolution_hypothesis_response(_response(), context=context)
        context_path = directory / "context.json"
        draft_path = directory / "drafts.json"
        decisions_path = directory / "decisions.json"
        output_path = directory / "approved.json"
        context_path.write_text(json.dumps({
            "context": context.model_dump(mode="json"),
            "gap_report": report.model_dump(mode="json"),
        }))
        draft_payload = {
            "request_sha256": "7" * 64,
            "context_sha256": context.context_sha256,
            "discovery_family_id": context.discovery_family_id,
            "report_sha256": report.report_sha256,
            "gap_card_sha256": {card.gap_id: card.card_sha256 for card in report.gap_cards},
            "drafts": [draft.model_dump(mode="json") for draft in drafts],
        }
        draft_payload["draft_batch_sha256"] = _draft_batch_sha256(draft_payload)
        draft_path.write_text(json.dumps(draft_payload))
        decisions = [
            EvolutionHypothesisApproval(
                logical_slot_id=draft.logical_slot_id,
                context_sha256=context.context_sha256,
                draft_sha256=draft.draft_sha256,
                discovery_family_id=context.discovery_family_id,
                approval_role="research_reviewer",
                approved_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
                decision="approved",
            ).model_dump(mode="json")
            for draft in drafts
        ]
        decisions_path.write_text(json.dumps({
            "approval_role": "research_reviewer",
            "decisions": decisions,
        }))
        return context_path, draft_path, decisions_path, output_path

    def test_nine_of_ten_fails_without_output_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._approval_inputs(Path(temporary))
            decisions = json.loads(paths[2].read_text())
            decisions["decisions"] = decisions["decisions"][:-1]
            paths[2].write_text(json.dumps(decisions))
            result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses",
                "--context", str(paths[0]), "--draft-batch", str(paths[1]),
                "--decisions", str(paths[2]), "--output", str(paths[3]),
            ])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(paths[3].exists())
            self.assertNotIn("expression", result.output.lower())
            self.assertNotIn("backtest", result.output.lower())
            self.assertNotIn("publication", result.output.lower())

    def test_approved_wrapper_round_trips_approval_batch_identity(self) -> None:
        """approve 生成的 wrapper 原样按批准批次 schema 复核，不把 wrapper 当批次重算。"""

        with tempfile.TemporaryDirectory() as temporary:
            paths = self._approval_inputs(Path(temporary))
            result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses", "--context", str(paths[0]),
                "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                "--output", str(paths[3]),
            ])
            self.assertEqual(result.exit_code, 0, result.output)
            approved = json.loads(paths[3].read_text())
            self.assertEqual(
                _require_raw_approval_batch_identity(approved, label="approved-batch"),
                approved["approval_batch_sha256"],
            )
            self.assertIn("drafts", approved)

            run_result = self.runner.invoke(app, [
                "campaign", "run-approved", _context().discovery_family_id,
                str(Path(temporary) / "hypotheses.json"), str(Path(temporary) / "policy.json"),
                str(Path(temporary) / "payloads.json"), str(Path(temporary) / "registry.json"),
                str(Path(temporary) / "authorization.json"), "--artifact-root",
                str(Path(temporary) / "runtime"), "--evolution-context", str(paths[0]),
                "--approved-hypotheses", str(paths[3]),
            ])
            self.assertNotEqual(run_result.exit_code, 0)
            self.assertNotIn("approval_batch_sha256 与内容不一致", run_result.output)

    def test_draft_top_level_context_family_mismatch_fails_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._approval_inputs(Path(temporary))
            draft_payload = json.loads(paths[1].read_text())
            draft_payload["context_sha256"] = "9" * 64
            draft_payload["discovery_family_id"] = "llmfamily_" + "2" * 24
            paths[1].write_text(json.dumps(draft_payload))
            result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses",
                "--context", str(paths[0]), "--draft-batch", str(paths[1]),
                "--decisions", str(paths[2]), "--output", str(paths[3]),
            ])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(paths[3].exists())

    def test_missing_audit_hashes_fail_before_output(self) -> None:
        for field, path_index, item_index in (
            ("draft_sha256", 1, 0),
            ("decision_sha256", 2, 0),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = self._approval_inputs(Path(temporary))
                payload = json.loads(paths[path_index].read_text())
                target = payload["drafts"][item_index] if field == "draft_sha256" else payload["decisions"][item_index]
                target.pop(field)
                paths[path_index].write_text(json.dumps(payload))
                result = self.runner.invoke(app, [
                    "evolution", "approve-hypotheses", "--context", str(paths[0]),
                    "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                    "--output", str(paths[3]),
                ])
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(paths[3].exists())

    def test_missing_report_or_gap_card_hash_fails_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._approval_inputs(Path(temporary))
            payload = json.loads(paths[1].read_text())
            payload.pop("report_sha256")
            paths[1].write_text(json.dumps(payload))
            result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses", "--context", str(paths[0]),
                "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                "--output", str(paths[3]),
            ])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(paths[3].exists())

            paths = self._approval_inputs(Path(temporary))
            payload = json.loads(paths[1].read_text())
            payload["gap_card_sha256"].pop("gap_" + "1" * 24)
            paths[1].write_text(json.dumps(payload))
            result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses", "--context", str(paths[0]),
                "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                "--output", str(paths[3]),
            ])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(paths[3].exists())

    def test_draft_provenance_rewrite_fails_without_output(self) -> None:
        """相同十个 draft 换顶层 provenance 或 aggregate 时必须失败。"""

        for field, value in (
            ("request_sha256", "8" * 64),
            ("context_sha256", "9" * 64),
            ("discovery_family_id", "llmfamily_" + "2" * 24),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = self._approval_inputs(Path(temporary))
                payload = json.loads(paths[1].read_text())
                payload[field] = value
                paths[1].write_text(json.dumps(payload))
                result = self.runner.invoke(app, [
                    "evolution", "approve-hypotheses", "--context", str(paths[0]),
                    "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                    "--output", str(paths[3]),
                ])
                self.assertNotEqual(result.exit_code, 0, result.output)
                self.assertFalse(paths[3].exists())

    def test_missing_draft_batch_provenance_is_hard_failure(self) -> None:
        for field in ("request_sha256", "report_sha256", "gap_card_sha256", "context_sha256", "discovery_family_id", "draft_batch_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = self._approval_inputs(Path(temporary))
                payload = json.loads(paths[1].read_text())
                payload.pop(field)
                paths[1].write_text(json.dumps(payload))
                result = self.runner.invoke(app, [
                    "evolution", "approve-hypotheses", "--context", str(paths[0]),
                    "--draft-batch", str(paths[1]), "--decisions", str(paths[2]),
                    "--output", str(paths[3]),
                ])
                self.assertNotEqual(result.exit_code, 0, result.output)
                self.assertFalse(paths[3].exists())

    def _real_prepare_inputs(self, root: Path):
        graph_root = _publish_graph(root, dense=True)
        graph = load_verified_coverage_graph(graph_root)
        memory_root = root / "research_memory"
        store = ResearchMemoryStore(research_memory_root=memory_root)
        policy = MemorySnapshotPolicy(
            allowed_source_family_ids=("llmfamily_" + "2" * 24,),
            include_terminal_states=("completed", "published", "visible_failed", "visible_passed"),
        )
        memory_test = ResearchMemoryStoreTest()
        entry = memory_test._entry(
            discovery_family_id=policy.allowed_source_family_ids[0],
            created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        memory_test._write_input_manifest(root, run_id=entry.run_id, family_id=entry.discovery_family_id, seal_id=entry.generation_seal_id, published_at=entry.created_at)
        with patch.object(ResearchMemoryStore, "_assert_entry_contract", return_value=None):
            store.append_entry(entry)
        snapshot = store.build_snapshot(
            cutoff=datetime(2026, 8, 10, tzinfo=timezone.utc),
            source_family_ids=policy.allowed_source_family_ids,
            coverage_graph_id=graph.manifest.coverage_graph_id,
            policy=policy,
            created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        report = build_coverage_gap_report(graph, snapshot, policy=GapSelectionPolicy(max_cards=10))
        report_path = root / "gap_report.json"
        report_path.write_bytes(canonical_json_bytes(report.model_dump(mode="json")))
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps({
            "allowed_source_family_ids": list(policy.allowed_source_family_ids),
            "include_terminal_states": list(policy.include_terminal_states),
            "field_registry_hash": "d" * 64,
            "evaluation_policy_hash": "e" * 64,
            "design_policy_hash": "f" * 64,
        }), encoding="utf-8")
        return graph_root, memory_root, report_path, policy_path

    def test_prepare_context_uses_real_loader_for_post_cutoff_and_bad_chain(self) -> None:
        """真实脱敏 policy、graph、memory、gap fixture 的两类链断裂均无 output。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_root, memory_root, report_path, policy_path = self._real_prepare_inputs(root)
            store = ResearchMemoryStore(research_memory_root=memory_root)
            snapshot_path = next(store.snapshots_root.glob("*.json"))
            snapshot = json.loads(snapshot_path.read_text())
            snapshot["cutoff_at"] = "2026-08-01T00:00:00+00:00"
            snapshot.pop("snapshot_sha256", None)
            snapshot["snapshot_sha256"] = ResearchMemorySnapshot.model_validate(snapshot).snapshot_sha256
            snapshot_path.write_bytes(canonical_json_bytes(snapshot))
            index = json.loads(store.index_path.read_text())
            index["snapshots"][snapshot_path.stem] = snapshot["snapshot_sha256"]
            store.index_path.write_bytes(canonical_json_bytes(index))
            graph = load_verified_coverage_graph(graph_root)
            bad_snapshot = store.load_snapshot(snapshot_path.stem)
            report_path.write_bytes(canonical_json_bytes(build_coverage_gap_report(graph, bad_snapshot, policy=GapSelectionPolicy(max_cards=10)).model_dump(mode="json")))
            output = root / "post-cutoff.json"
            with patch.object(ResearchMemoryStore, "_assert_entry_contract", return_value=None):
                result = self.runner.invoke(app, [
                    "evolution", "prepare-context", "--coverage-graph-root", str(graph_root),
                    "--memory-root", str(memory_root), "--gap-report", str(report_path),
                    "--family-id", "llmfamily_" + "3" * 24, "--policy", str(policy_path),
                    "--output", str(output),
                ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertFalse(output.exists())

    def test_prepare_context_rejects_published_at_after_cutoff(self) -> None:
        """真实 loader 必须拒绝 cutoff 后发布的输入条目。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_root, memory_root, report_path, policy_path = self._real_prepare_inputs(root)
            store = ResearchMemoryStore(research_memory_root=memory_root)
            snapshot = store.load_snapshot(next(store.snapshots_root.glob("*.json")).stem)
            entry_id = snapshot.entry_ids[0]
            entry = store.load_entry(entry_id)
            manifest = root / "artifacts" / "runs" / entry.run_id / "run" / "input_manifest.json"
            payload = json.loads(manifest.read_text())
            payload["published_at"] = "2026-08-11T00:00:00+00:00"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "published-after-cutoff.json"
            with patch.object(ResearchMemoryStore, "_assert_entry_contract", return_value=None):
                result = self.runner.invoke(app, [
                    "evolution", "prepare-context", "--coverage-graph-root", str(graph_root),
                    "--memory-root", str(memory_root), "--gap-report", str(report_path),
                    "--family-id", "llmfamily_" + "3" * 24, "--policy", str(policy_path),
                    "--output", str(output),
                ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("published_at 晚于 snapshot cutoff", result.output)
            self.assertFalse(output.exists())

    def test_prepare_context_rejects_terminal_state_outside_policy(self) -> None:
        """真实 loader 必须按 policy 排除不在 include_terminal_states 的条目。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_root, memory_root, report_path, policy_path = self._real_prepare_inputs(root)
            policy = json.loads(policy_path.read_text())
            policy["include_terminal_states"] = ["published"]
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            output = root / "terminal-state-policy.json"
            with patch.object(ResearchMemoryStore, "_assert_entry_contract", return_value=None):
                result = self.runner.invoke(app, [
                    "evolution", "prepare-context", "--coverage-graph-root", str(graph_root),
                    "--memory-root", str(memory_root), "--gap-report", str(report_path),
                    "--family-id", "llmfamily_" + "3" * 24, "--policy", str(policy_path),
                    "--output", str(output),
                ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("terminal_state 不在 policy", result.output)
            self.assertFalse(output.exists())

    def test_prepare_context_rejects_family_outside_policy_whitelist(self) -> None:
        """真实 loader 必须拒绝 snapshot 来源 family 超出 policy 白名单。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_root, memory_root, report_path, policy_path = self._real_prepare_inputs(root)
            policy = json.loads(policy_path.read_text())
            policy["allowed_source_family_ids"] = ["llmfamily_" + "3" * 24]
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            output = root / "family-policy.json"
            with patch.object(ResearchMemoryStore, "_assert_entry_contract", return_value=None):
                result = self.runner.invoke(app, [
                    "evolution", "prepare-context", "--coverage-graph-root", str(graph_root),
                    "--memory-root", str(memory_root), "--gap-report", str(report_path),
                    "--family-id", "llmfamily_" + "3" * 24, "--policy", str(policy_path),
                    "--output", str(output),
                ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("snapshot family 超出当前 policy 白名单", result.output)
            self.assertFalse(output.exists())

    def test_campaign_run_approved_rejects_approved_context_mismatch_before_generation(self) -> None:
        """严格入口在表达式/回测/发布前拒绝批准批次与 context 的混合包装。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path, draft_path, decisions_path, approved_path = self._approval_inputs(root)
            approved_result = self.runner.invoke(app, [
                "evolution", "approve-hypotheses", "--context", str(context_path),
                "--draft-batch", str(draft_path), "--decisions", str(decisions_path),
                "--output", str(approved_path),
            ])
            self.assertEqual(approved_result.exit_code, 0, approved_result.output)
            approved = json.loads(approved_path.read_text())
            approved["context_sha256"] = "9" * 64
            approved_path.write_text(json.dumps(approved), encoding="utf-8")
            runtime_root = root / "runtime-artifacts"
            result = self.runner.invoke(app, [
                "campaign", "run-approved", "llmfamily_" + "1" * 24,
                str(root / "hypotheses.json"), str(root / "policy.json"),
                str(root / "payloads.json"), str(root / "registry.json"),
                str(root / "authorization.json"), "--artifact-root", str(runtime_root),
                "--evolution-context", str(context_path), "--approved-hypotheses", str(approved_path),
            ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertFalse(runtime_root.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_root, memory_root, report_path, policy_path = self._real_prepare_inputs(root)
            report = json.loads(report_path.read_text())
            report["graph_manifest_hash"] = "9" * 64
            report_path.write_bytes(canonical_json_bytes(report))
            output = root / "bad-chain.json"
            result = self.runner.invoke(app, [
                "evolution", "prepare-context", "--coverage-graph-root", str(graph_root),
                "--memory-root", str(memory_root), "--gap-report", str(report_path),
                "--family-id", "llmfamily_" + "3" * 24, "--policy", str(policy_path),
                "--output", str(output),
            ])
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
