"""可审计工作流测试。"""

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.data_source import (
    DataProvenance,
    DataRequest,
    FactorInputRequest,
    InputProvenance,
    OutcomeProvenance,
    ReferenceProvenance,
)
from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import EventType, JsonlLedger
from factor_miner.schema import (
    CandidateFactorSpec,
    FactorNode,
    RegisteredCandidate,
    registered_candidate,
)
from factor_miner.workflow import (
    CandidateTerminalStatus,
    run_smoke_campaign,
    run_visible_campaign,
    validate_trusted_plan_before_outcomes,
    prepare_trusted_campaign_plans,
    run_mandatory_output_redundancy,
    StagedRunPublisher,
    run_trusted_smoke_campaign,
    run_trusted_visible_campaign,
    run_incremental_visible_campaign,
)
from factor_miner.compiler import compile_candidate
from tests.helpers import (
    valid_registered_candidate,
    valid_registered_trusted_candidate,
    valid_research_family,
    valid_reference_factor_library,
    valid_trusted_candidate,
    valid_trusted_campaign,
)
from factor_miner.policy import (
    company_a_share_incremental_policy,
    company_a_share_visible_policy,
)
from factor_miner.schema import (
    HypothesisSlot,
    ResearchFamilySpec,
    evaluation_policy_id,
    registered_research_family,
    registered_reference_factor_library,
    registered_trusted_candidate,
)
from tests.helpers import valid_campaign, valid_candidate


def close_candidate() -> RegisteredCandidate:
    """构造只引用 close 的零 lookback 合成候选。"""

    spec = valid_candidate().model_copy(
        update={
            "expression": FactorNode(op="field", field="close"),
            "required_fields": ("close",),
            "max_lookback": 0,
        }
    )
    return registered_candidate(spec)


def workflow_campaign(candidate_id: str):
    """构造小日期范围和小股票数的 campaign。"""

    campaign = valid_campaign().model_copy(
        update={
            "visible_start": date(2020, 1, 1),
            "visible_end": date(2020, 1, 30),
            "candidate_ids": (candidate_id,),
            "max_hypotheses": 1,
            "min_names_per_date": 2,
            "min_valid_dates": 5,
            "min_median_coverage": 0.5,
            "hac_max_lags": 1,
        }
    )
    return campaign


def workflow_frame(*, null_label: bool = False) -> pl.DataFrame:
    """生成可用于 smoke 和 visible 的合成数据源面板。"""

    rows: list[dict[str, object]] = []
    for day_index in range(30):
        current = date(2020, 1, 1) + timedelta(days=day_index)
        reverse_label = day_index % 5 == 4
        for asset_index, asset in enumerate(("AAA", "BBB", "CCC")):
            rank = asset_index + 1
            label_rank = 4 - rank if reverse_label else rank
            rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "close": float(rank),
                    "valid_for_factor_compute": True,
                    "valid_for_factor_rank": True,
                    "valid_for_trading": True,
                    "label_o2o_5d": None if null_label else float(label_rank),
                }
            )
    return pl.DataFrame(rows).sort(["date", "asset"])


def workflow_reference_frame() -> pl.DataFrame:
    """生成与候选排序不高度相关的冻结参考输出。"""

    return (
        workflow_frame()
        .with_columns(
            pl.when(pl.col("asset") == "AAA")
            .then(pl.lit(1.0))
            .when(pl.col("asset") == "BBB")
            .then(pl.lit(3.0))
            .otherwise(pl.lit(2.0))
            .alias("reference")
        )
        .select(["date", "asset", "reference"])
    )


class WorkflowSource:
    """满足 DataSource 端口的纯内存合成实现。"""

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.inspect_count = 0

    def inspect(self) -> DataProvenance:
        """记录合同检查调用并返回固定 provenance。"""

        self.inspect_count += 1
        return DataProvenance(
            data_origin="synthetic-test",
            resolved_release_id="workflow-release-1",
            release_manifest_sha256="c" * 64,
            schema_version="workflow-v1",
            market_cutoff="2020-01-30",
            adjustment_convention="synthetic",
            calendar_version="workflow-calendar-v1",
            state_table_version="workflow-state-v1",
            state_table_cutoff="2020-01-30",
            code_commit="workflow-commit",
            config_hash="workflow-config",
        )

    def scan(self, request: DataRequest) -> pl.LazyFrame:
        """返回请求字段、mask 和标签。"""

        lower = request.start - timedelta(days=request.warmup_days)
        columns = [
            "date",
            "asset",
            *request.required_fields,
            "valid_for_factor_compute",
            "valid_for_factor_rank",
            "valid_for_trading",
            "label_o2o_5d",
        ]
        return (
            self.frame.lazy()
            .filter(pl.col("date").is_between(lower, request.end, closed="both"))
            .select(list(dict.fromkeys(columns)))
            .sort(["date", "asset"])
        )


class WorkflowTest(unittest.TestCase):
    """验证登记、运行、结果暴露和终态事件顺序。"""

    def test_smoke_is_deterministic_and_records_auditable_events(self) -> None:
        """Smoke 重复计算应得到相同 hash，并完成明确终态。"""
        candidate = close_candidate()
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            result = run_smoke_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                WorkflowSource(workflow_frame()),
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
            )
            self.assertEqual(
                result.statuses[candidate.candidate_id],
                CandidateTerminalStatus.SMOKE_PASSED,
            )
            events = JsonlLedger(Path(directory)).read_events()
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types,
                [
                    EventType.CANDIDATE_REGISTERED,
                    EventType.CAMPAIGN_REGISTERED,
                    EventType.RUN_STARTED,
                    EventType.OUTCOME_EXPOSED,
                    EventType.EVALUATION_COMPLETED,
                ],
            )
            self.assertEqual(len(result.artifact_refs[candidate.candidate_id]), 2)
            self.assertEqual(
                result.artifact_hashes[candidate.candidate_id][0],
                result.artifact_hashes[candidate.candidate_id][1],
            )

    def test_trusted_pre_outcome_checks_use_only_input_port(self) -> None:
        """动态探针完成前只能调用因子输入端口。"""

        frame = pl.DataFrame(
            {
                "date": [date(2020, 1, 1) + timedelta(days=index) for index in range(35)],
                "asset": ["AAA"] * 35,
                "close": [float(index) for index in range(35)],
                "valid_for_factor_compute": [True] * 35,
            }
        )

        class InputOnlySource:
            def inspect_inputs(self) -> InputProvenance:
                return InputProvenance(
                    data_origin="synthetic-test",
                    resolved_release_id="release-1",
                    release_manifest_sha256="a" * 64,
                    schema_version="schema-1",
                    market_cutoff="2020-02-04",
                    adjustment_convention="synthetic",
                    calendar_version="calendar-1",
                    state_table_version="state-1",
                    state_table_cutoff="2020-02-04",
                    code_commit="commit-1",
                    config_hash="config-1",
                )

            def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
                return frame.lazy()

        plan = compile_candidate(valid_registered_candidate(), {"close"})
        _, probe = validate_trusted_plan_before_outcomes(
            plan,
            InputOnlySource(),
            FactorInputRequest(
                date(2020, 1, 21),
                date(2020, 2, 4),
                ("close",),
                warmup_observations=20,
            ),
            checkpoints=(date(2020, 1, 26),),
            seed=17,
        )
        self.assertTrue(probe.passed)

    def test_structural_duplicate_fails_before_input_inspection(self) -> None:
        """同一公式的第二个候选在任何输入检查前失败。"""

        first = valid_registered_trusted_candidate()
        second_spec = valid_trusted_candidate().model_copy(
            update={"provenance": {"author": "第二位研究者", "source": "synthetic-test"}}
        )
        second = registered_trusted_candidate(second_spec)
        policy = company_a_share_visible_policy()
        family_spec = valid_research_family().model_copy(
            update={
                "slots": (
                    HypothesisSlot(slot_number=1, candidate_id=first.candidate_id),
                    HypothesisSlot(slot_number=2, candidate_id=second.candidate_id),
                )
            }
        )
        family = registered_research_family(family_spec)
        campaign = valid_trusted_campaign().model_copy(
            update={
                "candidate_ids": (first.candidate_id, second.candidate_id),
                "research_family_id": family.research_family_id,
            }
        )
        reference_plan = compile_candidate(valid_registered_candidate(), {"close"}).model_copy(
            update={
                "candidate_id": policy.reference_factor_ids[0],
                "ast_hash": "f" * 64,
                "required_lookback": 0,
                "expression_metadata": {
                    "node_count": 1,
                    "depth": 1,
                    "operator_signature": ("field",),
                    "field_signature": ("close",),
                },
            }
        )
        with self.assertRaisesRegex(FactorMinerError, "REDUNDANCY_THRESHOLD_EXCEEDED"):
            prepare_trusted_campaign_plans(
                campaign,
                {first.candidate_id: first, second.candidate_id: second},
                policy,
                family,
                {policy.reference_factor_ids[0]: reference_plan},
            )

    def test_missing_reference_metadata_fails_closed(self) -> None:
        """冻结政策中的参考因子编译元数据缺失时必须失败。"""

        candidate = valid_registered_trusted_candidate()
        with self.assertRaisesRegex(FactorMinerError, "FIELD_MISSING"):
            prepare_trusted_campaign_plans(
                valid_trusted_campaign(),
                {candidate.candidate_id: candidate},
                company_a_share_visible_policy(),
                registered_research_family(valid_research_family()),
                {},
            )

    def test_output_redundancy_loads_all_references_and_prior_candidates(self) -> None:
        """输出冗余自动加载冻结参考池和批次内更早候选。"""

        policy = company_a_share_visible_policy()
        campaign = valid_trusted_campaign().model_copy(
            update={"visible_start": date(2020, 1, 1), "visible_end": date(2020, 1, 3)}
        )
        family = registered_research_family(valid_research_family())
        rows: list[dict[str, object]] = []
        for day_index in range(3):
            current = date(2020, 1, 1) + timedelta(days=day_index)
            for asset_index in range(25):
                rows.append(
                    {
                        "date": current,
                        "asset": f"A{asset_index:03d}",
                        "raw_factor": float(asset_index),
                    }
                )
        candidate_frame = pl.DataFrame(rows)
        unrelated = candidate_frame.with_columns(
            ((pl.col("raw_factor") * 7) % 23).alias("raw_factor")
        )

        class ReferenceSource:
            def inspect_references(self, manifest_id: str) -> ReferenceProvenance:
                return ReferenceProvenance(
                    manifest_id=manifest_id,
                    manifest_sha256="d" * 64,
                    factor_ids=policy.reference_factor_ids,
                    data_cutoff="2020-01-03",
                )

            def scan_reference(self, factor_id: str, start: date, end: date) -> pl.LazyFrame:
                return unrelated.lazy()

        passed = run_mandatory_output_redundancy(
            candidate_frame,
            campaign,
            policy,
            family,
            ReferenceSource(),
            prior_candidate_frames={},
        )
        self.assertTrue(passed.passed)
        failed = run_mandatory_output_redundancy(
            candidate_frame,
            campaign,
            policy,
            family,
            ReferenceSource(),
            prior_candidate_frames={"prior": candidate_frame},
        )
        self.assertFalse(failed.passed)
        self.assertEqual(len(failed.comparisons), 2)

    def test_compile_failure_has_no_outcome_exposed_event(self) -> None:
        """编译失败只能形成 compile_failed，不能伪造结果暴露。"""
        candidate_spec = valid_candidate().model_copy(
            update={"expression": FactorNode(op="unknown", args=())}
        )
        candidate = registered_candidate(candidate_spec)
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            result = run_smoke_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                WorkflowSource(workflow_frame()),
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
            )
            self.assertEqual(
                result.statuses[candidate.candidate_id],
                CandidateTerminalStatus.COMPILE_FAILED,
            )
            events = JsonlLedger(Path(directory)).read_events()
            self.assertEqual(events[-1].event_type, EventType.COMPILE_FAILED)
            self.assertFalse(any(event.outcome_exposed for event in events))

    def test_visible_pass_records_evaluation_and_terminal_status(self) -> None:
        """可见评价通过时应记录 evaluation_completed 和 visible_passed。"""
        candidate = close_candidate()
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            result = run_visible_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                WorkflowSource(workflow_frame()),
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
                reference_frames={"reference_momentum": workflow_reference_frame()},
            )
            self.assertEqual(
                result.statuses[candidate.candidate_id],
                CandidateTerminalStatus.VISIBLE_PASSED,
            )
            event_types = [
                event.event_type for event in JsonlLedger(Path(directory)).read_events()
            ]
            self.assertEqual(
                event_types[-2:],
                [
                    EventType.EVALUATION_COMPLETED,
                    EventType.VISIBLE_PASSED,
                ],
            )

    def test_visible_cannot_skip_declared_reference_pool(self) -> None:
        """声明参考池后省略输出冗余输入必须在运行开始前失败。"""

        candidate = close_candidate()
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FactorMinerError, "FIELD_MISSING"):
                run_visible_campaign(
                    campaign,
                    {candidate.candidate_id: candidate},
                    WorkflowSource(workflow_frame()),
                    Path(directory),
                    code_hash="code-test",
                    config_hash="config-test",
                )
            self.assertFalse((Path(directory) / "state").exists())

    def test_staged_publisher_creates_complete_hashed_final_run(self) -> None:
        """完整候选文件必须先暂存、哈希，再一次性发布到最终目录。"""

        required = (
            "execution_plan.json",
            "lookahead_probe.json",
            "quality.json",
            "evaluation.json",
            "inference.json",
            "redundancy.json",
            "candidate_package.json",
        )
        with tempfile.TemporaryDirectory() as directory:
            publisher = StagedRunPublisher(Path(directory), "run_test")
            for name in required:
                publisher.write_json(f"candidates/candidate/{name}", {"name": name})
            raw_path = publisher.path("candidates/candidate/raw_factor.parquet")
            pl.DataFrame(
                {"date": [date(2020, 1, 1)], "asset": ["AAA"], "raw_factor": [1.0]}
            ).write_parquet(raw_path)
            published = publisher.publish({"run_id": "run_test"})
            self.assertFalse(publisher.staging_root.exists())
            self.assertTrue(published.final_root.is_dir())
            manifest = json.loads(published.manifest_path.read_text(encoding="utf-8"))
            expected_files = {
                f"candidates/candidate/{name}" for name in required
            } | {"candidates/candidate/raw_factor.parquet"}
            self.assertEqual(set(manifest["files"]), expected_files)
            for relative, expected_hash in manifest["files"].items():
                actual = hashlib.sha256(
                    (published.final_root / relative).read_bytes()
                ).hexdigest()
                self.assertEqual(actual, expected_hash)

    def test_publish_failure_keeps_staging_and_no_final_directory(self) -> None:
        """原子重命名前失败必须保留暂存字节且不得出现最终目录。"""

        with tempfile.TemporaryDirectory() as directory:
            publisher = StagedRunPublisher(Path(directory), "run_failed")
            publisher.write_json("diagnostic.json", {"status": "pending"})

            def fail_before_rename() -> None:
                raise RuntimeError("模拟发布中断")

            with self.assertRaisesRegex(RuntimeError, "模拟发布中断"):
                publisher.publish(
                    {"run_id": "run_failed"}, before_rename=fail_before_rename
                )
            self.assertTrue(publisher.staging_root.is_dir())
            self.assertFalse(publisher.final_root.exists())

    def test_trusted_run_publishes_all_files_before_terminal_events(self) -> None:
        """可信运行发布完整候选包后才追加候选和运行终态。"""

        candidate = valid_registered_trusted_candidate()
        policy = company_a_share_visible_policy().model_copy(
            update={
                "min_valid_dates": 5,
                "min_names_per_date": 3,
                "hac_max_lags": 1,
            }
        )
        family = registered_research_family(
            ResearchFamilySpec(
                evaluation_policy_id=evaluation_policy_id(policy),
                global_hypothesis_budget=1,
                slots=(HypothesisSlot(slot_number=1, candidate_id=candidate.candidate_id),),
                created_at=valid_research_family().created_at,
                provenance={"author": "合成测试", "source": "synthetic-test"},
            )
        )
        campaign = valid_trusted_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 21),
                "visible_end": date(2020, 1, 30),
                "evaluation_policy_id": evaluation_policy_id(policy),
                "research_family_id": family.research_family_id,
            }
        )
        input_rows: list[dict[str, object]] = []
        outcome_rows: list[dict[str, object]] = []
        reference_rows: list[dict[str, object]] = []
        for day_index in range(30):
            current = date(2020, 1, 1) + timedelta(days=day_index)
            for asset_index, asset in enumerate(("AAA", "BBB", "CCC"), start=1):
                input_rows.append(
                    {
                        "date": current,
                        "asset": asset,
                        "close": float(asset_index * (day_index + 1)),
                        "valid_for_factor_compute": True,
                        "valid_for_factor_rank": True,
                    }
                )
                label_rank = 4 - asset_index if day_index == 25 else asset_index
                outcome_rows.append(
                    {"date": current, "asset": asset, "label_o2o_5d": float(label_rank)}
                )
                reference_rank = {1: 1.0, 2: 3.0, 3: 2.0}[asset_index]
                reference_rows.append(
                    {"date": current, "asset": asset, "raw_factor": reference_rank}
                )
        input_frame = pl.DataFrame(input_rows)
        outcome_frame = pl.DataFrame(outcome_rows)
        reference_frame = pl.DataFrame(reference_rows)

        class InputSource:
            def inspect_inputs(self) -> InputProvenance:
                return InputProvenance(
                    data_origin="synthetic-test",
                    resolved_release_id="release-1",
                    release_manifest_sha256="a" * 64,
                    schema_version="schema-1",
                    market_cutoff="2020-01-30",
                    adjustment_convention="synthetic",
                    calendar_version="calendar-1",
                    state_table_version="state-1",
                    state_table_cutoff="2020-01-30",
                    code_commit="commit-1",
                    config_hash="config-1",
                )

            def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
                return input_frame.lazy()

        class OutcomeSource:
            def inspect_outcomes(self) -> OutcomeProvenance:
                return OutcomeProvenance(
                    label_id=policy.label_id,
                    label_manifest_sha256="b" * 64,
                    label_schema_version="schema-1",
                    label_cutoff="2020-01-30",
                    label_formula_version=policy.label_formula_version,
                    source_release_id="release-1",
                )

            def scan_outcomes(self, request) -> pl.LazyFrame:
                return outcome_frame.lazy().filter(
                    pl.col("date").is_between(request.start, request.end, closed="both")
                )

        class ReferenceSource:
            def inspect_references(self, manifest_id: str) -> ReferenceProvenance:
                return ReferenceProvenance(
                    manifest_id=manifest_id,
                    manifest_sha256="c" * 64,
                    factor_ids=policy.reference_factor_ids,
                    data_cutoff="2020-01-30",
                )

            def scan_reference(self, factor_id: str, start: date, end: date) -> pl.LazyFrame:
                return reference_frame.lazy().filter(
                    pl.col("date").is_between(start, end, closed="both")
                )

        reference_plan = compile_candidate(valid_registered_candidate(), {"close"}).model_copy(
            update={
                "candidate_id": policy.reference_factor_ids[0],
                "ast_hash": "e" * 64,
                "required_lookback": 0,
                "expression_metadata": {
                    "node_count": 1,
                    "depth": 1,
                    "operator_signature": ("field",),
                    "field_signature": ("close",),
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_trusted_visible_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                policy,
                family,
                InputSource(),
                OutcomeSource(),
                ReferenceSource(),
                {policy.reference_factor_ids[0]: reference_plan},
                root,
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            candidate_root = Path(result.run_manifest_path).parent / "candidates" / candidate.candidate_id
            self.assertEqual(
                {path.name for path in candidate_root.iterdir()},
                {
                    "execution_plan.json",
                    "lookahead_probe.json",
                    "raw_factor.parquet",
                    "quality.json",
                    "evaluation.json",
                    "inference.json",
                    "redundancy.json",
                    "candidate_package.json",
                },
            )
            events = JsonlLedger(root).read_events()
            self.assertEqual(events[-1].event_type, EventType.RUN_COMPLETED)
            terminal = next(
                event for event in events if event.event_type is EventType.VISIBLE_PASSED
            )
            self.assertTrue(Path(result.run_manifest_path).is_file())
            self.assertGreater(terminal.sequence, next(
                event.sequence for event in events if event.event_type is EventType.EVALUATION_COMPLETED
            ) if any(event.event_type is EventType.EVALUATION_COMPLETED for event in events) else 0)

    def test_trusted_smoke_is_input_only_and_reproducible(self) -> None:
        """可信冒烟不需要结果端口，两次运行的原始因子哈希必须一致。"""

        candidate = valid_registered_trusted_candidate()
        policy = company_a_share_visible_policy().model_copy(
            update={"min_valid_dates": 5, "min_names_per_date": 3, "hac_max_lags": 1}
        )
        family = registered_research_family(
            ResearchFamilySpec(
                evaluation_policy_id=evaluation_policy_id(policy),
                global_hypothesis_budget=1,
                slots=(HypothesisSlot(slot_number=1, candidate_id=candidate.candidate_id),),
                created_at=valid_research_family().created_at,
                provenance={"author": "合成测试", "source": "synthetic-test"},
            )
        )
        campaign = valid_trusted_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 21),
                "visible_end": date(2020, 1, 30),
                "evaluation_policy_id": evaluation_policy_id(policy),
                "research_family_id": family.research_family_id,
            }
        )
        frame = workflow_frame().drop("label_o2o_5d")

        class InputOnlySource:
            def inspect_inputs(self) -> InputProvenance:
                return InputProvenance(
                    data_origin="synthetic-test",
                    resolved_release_id="release-1",
                    release_manifest_sha256="a" * 64,
                    schema_version="schema-1",
                    market_cutoff="2020-01-30",
                    adjustment_convention="synthetic",
                    calendar_version="calendar-1",
                    state_table_version="state-1",
                    state_table_cutoff="2020-01-30",
                    code_commit="commit-1",
                    config_hash="config-1",
                )

            def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
                return frame.lazy()

        reference_plan = compile_candidate(valid_registered_candidate(), {"close"}).model_copy(
            update={
                "candidate_id": policy.reference_factor_ids[0],
                "ast_hash": "e" * 64,
                "required_lookback": 0,
                "expression_metadata": {
                    "node_count": 1,
                    "depth": 1,
                    "operator_signature": ("field",),
                    "field_signature": ("close",),
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = (
                campaign,
                {candidate.candidate_id: candidate},
                policy,
                family,
                InputOnlySource(),
                {policy.reference_factor_ids[0]: reference_plan},
                root,
            )
            first = run_trusted_smoke_campaign(
                *arguments,
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            second = run_trusted_smoke_campaign(
                *arguments,
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            first_manifest = json.loads(Path(first.run_manifest_path).read_text())
            second_manifest = json.loads(Path(second.run_manifest_path).read_text())
            relative = f"candidates/{candidate.candidate_id}/raw_factor.parquet"
            self.assertEqual(first_manifest["files"][relative], second_manifest["files"][relative])
            manifest = first_manifest
            self.assertFalse(manifest["outcomes_opened"])
            self.assertEqual(
                first.statuses[candidate.candidate_id], CandidateTerminalStatus.SMOKE_PASSED
            )

    def test_failure_after_label_read_exposes_outcome(self) -> None:
        """标签读取后的评价失败必须保留 outcome_exposed 事实。"""
        candidate = close_candidate()
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            result = run_visible_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                WorkflowSource(workflow_frame(null_label=True)),
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
                reference_frames={"reference_momentum": workflow_reference_frame()},
            )
            self.assertEqual(
                result.statuses[candidate.candidate_id],
                CandidateTerminalStatus.EVALUATION_FAILED,
            )
            events = JsonlLedger(Path(directory)).read_events()
            self.assertEqual(events[-1].event_type, EventType.EVALUATION_FAILED)
            self.assertTrue(events[-1].outcome_exposed)

    def test_run_manifest_records_lock_and_data_provenance(self) -> None:
        """运行 manifest 必须记录锁文件和完整数据 provenance。"""
        candidate = close_candidate()
        campaign = workflow_campaign(candidate.candidate_id)
        with tempfile.TemporaryDirectory() as directory:
            result = run_smoke_campaign(
                campaign,
                {candidate.candidate_id: candidate},
                WorkflowSource(workflow_frame()),
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            manifest = json.loads(Path(result.run_manifest_path).read_text())
            self.assertEqual(manifest["code_commit"], "code-test")
            self.assertEqual(manifest["config_hash"], "config-test")
            self.assertEqual(manifest["uv_lock_sha256"], "lock-test")
            self.assertEqual(
                manifest["campaign_spec_hash"],
                sha256_json(campaign.model_dump(mode="json")),
            )
            self.assertEqual(
                manifest["data_provenance"]["data_origin"],
                "synthetic-test",
            )
            self.assertEqual(result.run_manifest_sha256, sha256_json(manifest))

    def test_v02_joint_redundancy_cannot_visible_pass(self) -> None:
        """逐对低相关但联合解释充分的候选必须以 incremental_failed 终止。"""

        fixture = self._incremental_workflow_fixture(
            min_residual_variance_ratio=0.20
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_incremental_visible_campaign(
                *fixture,
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            candidate_id = fixture[0].candidate_ids[0]
            self.assertEqual(
                result.statuses[candidate_id],
                CandidateTerminalStatus.INCREMENTAL_FAILED,
            )
            candidate_root = (
                Path(result.run_manifest_path).parent / "candidates" / candidate_id
            )
            incremental_path = candidate_root / "incremental_information.json"
            package = json.loads(
                (candidate_root / "candidate_package.json").read_text(encoding="utf-8")
            )
            incremental = json.loads(incremental_path.read_text(encoding="utf-8"))
            self.assertTrue(package["incremental_information_evaluated"])
            self.assertFalse(package["incremental_information_passed"])
            self.assertTrue(package["visible_incremental_only"])
            self.assertFalse(incremental["passed"])
            self.assertIn(
                EventType.INCREMENTAL_FAILED,
                [event.event_type for event in JsonlLedger(Path(directory)).read_events()],
            )

    def test_v02_independent_residual_can_pass(self) -> None:
        """残差方差和预测力均满足冻结门槛时才能通过 V0.2。"""

        fixture = self._incremental_workflow_fixture(
            min_residual_variance_ratio=0.05
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_incremental_visible_campaign(
                *fixture,
                Path(directory),
                code_hash="code-test",
                config_hash="config-test",
                uv_lock_hash="lock-test",
            )
            candidate_id = fixture[0].candidate_ids[0]
            self.assertEqual(
                result.statuses[candidate_id],
                CandidateTerminalStatus.VISIBLE_PASSED,
            )
            self.assertTrue(
                result.metrics[candidate_id]["incremental_information"]["passed"]
            )

    def test_v02_reference_library_mismatch_precedes_outcome_open(self) -> None:
        """参考库身份不匹配必须在任何标签检查或读取前硬失败。"""

        fixture = list(
            self._incremental_workflow_fixture(
                min_residual_variance_ratio=0.05
            )
        )
        original_library = fixture[4]
        wrong_spec = original_library.spec.model_copy(
            update={"reference_manifest_id": "wrong-reference-manifest"}
        )
        fixture[4] = registered_reference_factor_library(wrong_spec)
        outcome_source = fixture[6]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FactorMinerError) as context:
                run_incremental_visible_campaign(
                    *fixture,
                    Path(directory),
                    code_hash="code-test",
                    config_hash="config-test",
                    uv_lock_hash="lock-test",
                )
            self.assertEqual(
                context.exception.code,
                FailureCode.REFERENCE_LIBRARY_MISMATCH,
            )
            self.assertEqual(outcome_source.inspect_count, 0)
            self.assertEqual(outcome_source.scan_count, 0)

    def test_v02_reference_manifest_hash_mismatch_precedes_outcome_open(self) -> None:
        """参考清单内容哈希不一致时不得打开标签。"""

        fixture = list(
            self._incremental_workflow_fixture(
                min_residual_variance_ratio=0.05
            )
        )
        reference_source = fixture[7]
        reference_source.manifest_sha256 = "f" * 64
        outcome_source = fixture[6]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FactorMinerError) as context:
                run_incremental_visible_campaign(
                    *fixture,
                    Path(directory),
                    code_hash="code-test",
                    config_hash="config-test",
                    uv_lock_hash="lock-test",
                )
            self.assertEqual(
                context.exception.code,
                FailureCode.REFERENCE_LIBRARY_MISMATCH,
            )
            self.assertEqual(outcome_source.inspect_count, 0)
            self.assertEqual(outcome_source.scan_count, 0)

    def _incremental_workflow_fixture(
        self,
        *,
        min_residual_variance_ratio: float,
    ):
        """构造含三个联合参考因子的 V0.2 可信运行依赖。"""

        candidate = valid_registered_trusted_candidate()
        reference_ids = (
            "reference_value",
            "reference_quality",
            "reference_momentum",
        )
        library_spec = valid_reference_factor_library().model_copy(
            update={"reference_factor_ids": reference_ids}
        )
        library = registered_reference_factor_library(library_spec)
        base_policy = company_a_share_incremental_policy(library)
        policy = base_policy.model_copy(
            update={
                "min_valid_dates": 5,
                "min_names_per_date": 2,
                "hac_max_lags": 1,
                "min_median_coverage": 0.8,
                "orthogonalization": base_policy.orthogonalization.model_copy(
                    update={
                        "min_reference_coverage": 1.0,
                        "min_cross_sectional_excess_names": 2,
                        "max_condition_number": 1_000.0,
                        "min_median_residual_variance_ratio": (
                            min_residual_variance_ratio
                        ),
                        "min_abs_mean_residual_rank_ic": 0.01,
                    }
                ),
            }
        )
        family = registered_research_family(
            ResearchFamilySpec(
                evaluation_policy_id=evaluation_policy_id(policy),
                global_hypothesis_budget=1,
                slots=(
                    HypothesisSlot(
                        slot_number=1,
                        candidate_id=candidate.candidate_id,
                    ),
                ),
                created_at=valid_research_family().created_at,
                provenance={"author": "合成测试", "source": "synthetic-test"},
            )
        )
        campaign = valid_trusted_campaign().model_copy(
            update={
                "visible_start": date(2020, 1, 21),
                "visible_end": date(2020, 1, 30),
                "evaluation_policy_id": evaluation_policy_id(policy),
                "research_family_id": family.research_family_id,
            }
        )
        permutations = (
            (1, 4, 2, 7, 3, 5, 6),
            (5, 2, 6, 1, 7, 4, 3),
            (5, 4, 7, 2, 1, 3, 6),
        )
        input_rows: list[dict[str, object]] = []
        outcome_rows: list[dict[str, object]] = []
        reference_rows: dict[str, list[dict[str, object]]] = {
            reference_id: [] for reference_id in reference_ids
        }
        for day_index in range(30):
            current = date(2020, 1, 1) + timedelta(days=day_index)
            for asset_index in range(7):
                asset = f"A{asset_index + 1:02d}"
                rank = asset_index + 1
                input_rows.append(
                    {
                        "date": current,
                        "asset": asset,
                        "close": float(rank * (day_index + 1)),
                        "valid_for_factor_compute": True,
                        "valid_for_factor_rank": True,
                    }
                )
                label_rank = 8 - rank if day_index == 25 else rank
                outcome_rows.append(
                    {
                        "date": current,
                        "asset": asset,
                        "label_o2o_5d": float(label_rank),
                    }
                )
                for reference_index, reference_id in enumerate(reference_ids):
                    reference_rows[reference_id].append(
                        {
                            "date": current,
                            "asset": asset,
                            "raw_factor": float(
                                permutations[reference_index][asset_index]
                            ),
                        }
                    )
        input_frame = pl.DataFrame(input_rows)
        outcome_frame = pl.DataFrame(outcome_rows)
        reference_frames = {
            key: pl.DataFrame(rows) for key, rows in reference_rows.items()
        }

        class InputSource:
            def inspect_inputs(self) -> InputProvenance:
                return InputProvenance(
                    data_origin="synthetic-test",
                    resolved_release_id="release-v02",
                    release_manifest_sha256="a" * 64,
                    schema_version="schema-v02",
                    market_cutoff="2020-01-30",
                    adjustment_convention="synthetic",
                    calendar_version="calendar-v02",
                    state_table_version="state-v02",
                    state_table_cutoff="2020-01-30",
                    code_commit="commit-v02",
                    config_hash="config-v02",
                )

            def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
                return input_frame.lazy().filter(pl.col("date") <= request.end)

        class IncrementalOutcomeSource:
            def __init__(self) -> None:
                self.inspect_count = 0
                self.scan_count = 0

            def inspect_outcomes(self) -> OutcomeProvenance:
                self.inspect_count += 1
                return OutcomeProvenance(
                    label_id=policy.label_id,
                    label_manifest_sha256="b" * 64,
                    label_schema_version="schema-v02",
                    label_cutoff="2020-01-30",
                    label_formula_version=policy.label_formula_version,
                    source_release_id="release-v02",
                )

            def scan_outcomes(self, request) -> pl.LazyFrame:
                self.scan_count += 1
                return outcome_frame.lazy().filter(
                    pl.col("date").is_between(
                        request.start,
                        request.end,
                        closed="both",
                    )
                )

        class ReferenceSource:
            def __init__(self) -> None:
                self.manifest_sha256 = "c" * 64

            def inspect_references(self, manifest_id: str) -> ReferenceProvenance:
                return ReferenceProvenance(
                    manifest_id=manifest_id,
                    manifest_sha256=self.manifest_sha256,
                    factor_ids=reference_ids,
                    data_cutoff="2020-01-30",
                )

            def scan_reference(
                self,
                factor_id: str,
                start: date,
                end: date,
            ) -> pl.LazyFrame:
                return reference_frames[factor_id].lazy().filter(
                    pl.col("date").is_between(start, end, closed="both")
                )

        base_plan = compile_candidate(valid_registered_candidate(), {"close"})
        reference_plans = {
            reference_id: base_plan.model_copy(
                update={
                    "candidate_id": reference_id,
                    "ast_hash": f"{index + 1:064x}",
                    "required_lookback": index + 1,
                    "expression_metadata": {
                        "node_count": 1,
                        "depth": 1,
                        "operator_signature": (f"reference_{index + 1}",),
                        "field_signature": ("close",),
                    },
                }
            )
            for index, reference_id in enumerate(reference_ids)
        }
        return (
            campaign,
            {candidate.candidate_id: candidate},
            policy,
            family,
            library,
            InputSource(),
            IncrementalOutcomeSource(),
            ReferenceSource(),
            reference_plans,
        )


if __name__ == "__main__":
    unittest.main()
