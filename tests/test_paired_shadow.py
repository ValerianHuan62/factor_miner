"""配对局部变异影子层的纯合成测试。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factor_miner.lightweight_expressions import parse_lightweight_expression_response
from factor_miner.paired_shadow import (
    PairedShadowPolicy,
    build_paired_shadow_plan,
    build_paired_shadow_summary,
    evaluate_paired_probe,
    publish_paired_shadow_plan,
    publish_paired_shadow_summary,
)
from factor_miner.paired_shadow_server import evaluate_paired_shadow_plan
from tests.test_design_diversity import _registry
from tests.test_lightweight_expressions import NOW, response, reviewed


class PairedShadowTest(unittest.TestCase):
    """影子计划必须事前冻结，可靠性不足时必须放弃学习。"""

    def _candidates(self):
        hypotheses, review = reviewed(approved_count=2)
        from factor_miner.lightweight_expressions import build_lightweight_expression_request

        prepared = build_lightweight_expression_request(
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
        )
        batch = parse_lightweight_expression_response(
            prepared=prepared,
            response=response(),
            hypotheses=hypotheses,
            review=review,
            registry=_registry(),
            created_at=NOW,
        )
        return tuple(
            (item.slot_id, item.candidate)
            for item in batch.slot_results
            if item.candidate is not None
        )

    def test_plan_selects_three_parents_without_results_and_freezes_two_siblings(self) -> None:
        plan = build_paired_shadow_plan(
            autonomous_run_id="autrun_test",
            source_published_run_id="run_test",
            candidates=self._candidates(),
            registry=_registry(),
            policy=PairedShadowPolicy(enabled=True),
            created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(len(plan.probes), 3)
        for probe in plan.probes:
            self.assertEqual(
                tuple(item.role for item in probe.variants),
                ("parent", "sibling_1", "sibling_2"),
            )
            parent_fields = probe.variants[0].candidate.spec.required_fields
            self.assertTrue(
                all(item.candidate.spec.required_fields == parent_fields for item in probe.variants)
            )
            self.assertTrue(all(item.mutation is not None for item in probe.variants[1:]))

    def test_stable_sibling_is_accepted_by_four_block_gate(self) -> None:
        plan = build_paired_shadow_plan(
            autonomous_run_id="autrun_test",
            source_published_run_id="run_test",
            candidates=self._candidates(),
            registry=_registry(),
            policy=PairedShadowPolicy(enabled=True, minimum_common_dates=20),
            created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        probe = plan.probes[0]
        days = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(40))
        values = (0.01, 0.03, 0.015)
        series = {
            variant.candidate.candidate_id: {day: values[index] for day in days}
            for index, variant in enumerate(probe.variants)
        }

        result = evaluate_paired_probe(probe, series, plan.policy)

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.winner_agreement, 1.0)
        self.assertGreater(result.paired_lcb, 0.0)
        self.assertIsNotNone(result.preference_code)

    def test_unstable_sibling_abstains_instead_of_writing_preference(self) -> None:
        policy = PairedShadowPolicy(enabled=True, minimum_common_dates=20)
        plan = build_paired_shadow_plan(
            autonomous_run_id="autrun_test",
            source_published_run_id="run_test",
            candidates=self._candidates(),
            registry=_registry(),
            policy=policy,
            created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        probe = plan.probes[0]
        days = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(40))
        candidate_ids = tuple(item.candidate.candidate_id for item in probe.variants)
        series = {candidate_id: {} for candidate_id in candidate_ids}
        for index, day in enumerate(days):
            block = index // 10
            values = (0.01, 0.03, 0.015) if block % 2 == 0 else (0.01, 0.015, 0.03)
            for candidate_id, value in zip(candidate_ids, values, strict=True):
                series[candidate_id][day] = value

        result = evaluate_paired_probe(probe, series, policy)

        self.assertEqual(result.status, "abstained")
        self.assertIsNone(result.preference_code)

    def test_plan_and_summary_publish_as_separate_immutable_objects(self) -> None:
        plan = build_paired_shadow_plan(
            autonomous_run_id="autrun_test",
            source_published_run_id="run_test",
            candidates=self._candidates(),
            registry=_registry(),
            policy=PairedShadowPolicy(enabled=True),
            created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        summary = build_paired_shadow_summary(
            plan,
            (),
            status="not_executed",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = publish_paired_shadow_plan(root, plan)
            summary_path = publish_paired_shadow_summary(root, summary)
            self.assertTrue(plan_path.is_file())
            self.assertTrue(summary_path.is_file())
            self.assertEqual(publish_paired_shadow_plan(root, plan), plan_path)
            self.assertEqual(publish_paired_shadow_summary(root, summary), summary_path)

    def test_server_port_reads_only_discovery_window_and_returns_complete_results(self) -> None:
        policy = PairedShadowPolicy(enabled=True, minimum_common_dates=20)
        plan = build_paired_shadow_plan(
            autonomous_run_id="autrun_test",
            source_published_run_id="run_test",
            candidates=self._candidates(),
            registry=_registry(),
            policy=policy,
            created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        candidate_ids = tuple(
            item.candidate.candidate_id
            for probe in plan.probes
            for item in probe.variants
        )
        panels = tuple(SimpleNamespace(candidate_id=item) for item in candidate_ids)
        days = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(40))
        source = SimpleNamespace(
            inspect_inputs=lambda: SimpleNamespace(resolved_release_id="release_test")
        )
        request = SimpleNamespace(
            data_release_id="release_test",
            code_commit="a" * 40,
            config_hash="b" * 64,
            artifact_root=Path("/tmp/paired-shadow-test"),
        )

        def daily(_labeled, _policy):
            candidate_id = _labeled
            position = candidate_ids.index(candidate_id) % 3
            value = (0.01, 0.03, 0.015)[position]
            return tuple((day, value) for day in days)

        with patch(
            "factor_miner.paired_shadow_server.PilotQuantLakeFactorInputSource",
            return_value=source,
        ), patch(
            "factor_miner.paired_shadow_server.compute_fixed_signal_panels",
            return_value=panels,
        ), patch(
            "factor_miner.paired_shadow_server._load_pilot_calendar",
            return_value=SimpleNamespace(),
        ) as calendar, patch(
            "factor_miner.paired_shadow_server._load_pilot_market_open",
            return_value=SimpleNamespace(),
        ) as market, patch(
            "factor_miner.paired_shadow_server.build_open_to_open_ic_panel",
            side_effect=lambda panel, *_args, **_kwargs: panel.candidate_id,
        ), patch(
            "factor_miner.paired_shadow_server.evaluate_discovery_daily_rank_ic",
            side_effect=daily,
        ):
            results = evaluate_paired_shadow_plan(
                plan,
                paths=SimpleNamespace(),
                request=request,
                evaluation_policy=SimpleNamespace(),
            )

        self.assertEqual(len(results), len(plan.probes))
        self.assertTrue(all(item.status == "accepted" for item in results))
        calendar.assert_called_once_with(
            unittest.mock.ANY,
            date(2021, 1, 1),
            date(2023, 12, 31),
        )
        market.assert_called_once_with(
            unittest.mock.ANY,
            date(2021, 1, 1),
            date(2023, 12, 31),
        )


if __name__ == "__main__":
    unittest.main()
