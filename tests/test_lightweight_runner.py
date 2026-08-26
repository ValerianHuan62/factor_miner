"""轻量研究单入口和阶段恢复测试。"""

from pathlib import Path
import json
import tempfile
import unittest

from factor_miner.lightweight_runner import (
    LightweightRunDependencies,
    run_lightweight_research,
)
from factor_miner.lightweight_schema import (
    LightweightCandidateBinding,
    LightweightResearchConfig,
    build_lightweight_batch_manifest,
)
from factor_miner.llm_state import CandidateSlotState
from factor_miner.research_campaign_runner import (
    CampaignSlotEvaluation,
    LightweightCampaignEvaluationResult,
)


class FakeDependencies(LightweightRunDependencies):
    """只记录阶段调用次数的合成依赖。"""

    def __init__(self, *, fail_projection_once: bool = False) -> None:
        self.calls = {name: 0 for name in ("context", "freeze", "evaluate", "publish", "project", "refresh")}
        self.fail_projection_once = fail_projection_once

    def prepare_context(self, config):
        self.calls["context"] += 1
        return {"memory_snapshot_hash": "c" * 64, "coverage_graph_hash": "d" * 64}

    def freeze_batch(self, config, context):
        self.calls["freeze"] += 1
        research = LightweightResearchConfig(
            hypothesis_count=config.hypothesis_count,
            candidates_per_hypothesis=config.candidates_per_hypothesis,
            allow_external_llm=config.allow_external_llm,
        )
        bindings = tuple(
            LightweightCandidateBinding(
                slot_id=f"H01:C{index + 1:03d}",
                source_candidate_id=f"candidate_{index + 1:03d}",
                candidate_spec_hash=f"{index + 1:064x}",
            )
            for index in range(research.slot_count)
        )
        return build_lightweight_batch_manifest(
            config=research,
            candidate_bindings=bindings,
            evaluation_policy_hash="b" * 64,
            memory_snapshot_hash=str(context["memory_snapshot_hash"]),
            coverage_graph_hash=str(context["coverage_graph_hash"]),
        )

    def evaluate(self, config, manifest):
        self.calls["evaluate"] += 1
        return LightweightCampaignEvaluationResult.build(
            manifest=manifest,
            slot_evaluations=tuple(
                CampaignSlotEvaluation(
                    slot_id=binding.slot_id,
                    slot_state=CandidateSlotState.READY_FOR_REGISTRATION,
                    status="evaluated",
                )
                for binding in manifest.candidate_bindings
            ),
        )

    def publish(self, config, manifest, evaluation):
        self.calls["publish"] += 1
        return {"published_run_id": "run_" + "1" * 24}

    def project(self, config, publication):
        self.calls["project"] += 1
        if self.fail_projection_once:
            self.fail_projection_once = False
            raise RuntimeError("合成投影中断")
        return {"projection_status": "projected"}

    def refresh_evolution(self, config, publication):
        self.calls["refresh"] += 1
        return {"memory_snapshot_id": "memory_next", "coverage_graph_id": "graph_next"}


class LightweightRunnerTest(unittest.TestCase):
    """阶段标记必须让同一配置安全恢复且不重复调用已完成步骤。"""

    @staticmethod
    def _config(root: Path) -> Path:
        path = root / "research.json"
        path.write_text(
            json.dumps(
                {
                    "artifact_root": str(root / "artifacts"),
                    "hypothesis_count": 1,
                    "candidates_per_hypothesis": 3,
                    "allow_external_llm": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_replay_resumes_without_recalling_completed_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            dependencies = FakeDependencies()
            first = run_lightweight_research(config, dependencies=dependencies)
            calls_after_first = dict(dependencies.calls)
            second = run_lightweight_research(config, dependencies=dependencies)
            self.assertEqual(first, second)
            self.assertEqual(second.stage.value, "evolution_refreshed")
            self.assertEqual(dependencies.calls, calls_after_first)

    def test_projection_failure_preserves_publication_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            dependencies = FakeDependencies(fail_projection_once=True)
            with self.assertRaisesRegex(RuntimeError, "合成投影中断"):
                run_lightweight_research(config, dependencies=dependencies)
            self.assertEqual(dependencies.calls["publish"], 1)
            result = run_lightweight_research(config, dependencies=dependencies)
            self.assertEqual(result.stage.value, "evolution_refreshed")
            self.assertEqual(dependencies.calls["context"], 1)
            self.assertEqual(dependencies.calls["freeze"], 1)
            self.assertEqual(dependencies.calls["evaluate"], 1)
            self.assertEqual(dependencies.calls["publish"], 1)
            self.assertEqual(dependencies.calls["project"], 2)
            self.assertEqual(dependencies.calls["refresh"], 1)


if __name__ == "__main__":
    unittest.main()
