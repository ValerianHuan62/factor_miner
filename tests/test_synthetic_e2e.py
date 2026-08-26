"""全合成端到端验收测试。"""

from datetime import date, timedelta
from pathlib import Path
import random
import tempfile
import unittest

import polars as pl
from pydantic import ValidationError

from factor_miner.compiler import compile_candidate
from factor_miner.data_source import DataProvenance, DataRequest, MARKET_COLUMNS
from factor_miner.dsl import canonical_ast_hash
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import EventType, JsonlLedger, LedgerPaths
from factor_miner.redundancy import check_structural_redundancy
from factor_miner.schema import CampaignSpec, FactorNode, registered_candidate
from factor_miner.workflow import CandidateTerminalStatus, run_visible_campaign
from tests.helpers import valid_candidate, valid_registered_candidate


VISIBLE_START = date(2020, 1, 21)
VISIBLE_END = date(2020, 5, 19)
DATE_COUNT = 140
ASSET_COUNT = 150


class SyntheticSource:
    """固定随机种子生成的内存数据源，不读取任何外部数据文件。"""

    def __init__(self, *, label_mode: str = "signal", factor_mask: bool = True) -> None:
        self.frame = _build_panel(label_mode=label_mode, factor_mask=factor_mask)
        self.inspect_count = 0

    def inspect(self) -> DataProvenance:
        """返回合成数据合同并记录检查次数。"""

        self.inspect_count += 1
        return DataProvenance(
            data_origin="synthetic-e2e",
            resolved_release_id="synthetic-e2e-release-1",
            release_manifest_sha256="d" * 64,
            schema_version="synthetic-e2e-v1",
            market_cutoff=str(self.frame.get_column("date").max()),
            adjustment_convention="synthetic-unadjusted",
            calendar_version="synthetic-calendar-v1",
            state_table_version="synthetic-state-v1",
            state_table_cutoff=str(self.frame.get_column("date").max()),
            code_commit="synthetic-e2e-code",
            config_hash="synthetic-e2e-config",
        )

    def scan(self, request: DataRequest) -> pl.LazyFrame:
        """按请求日期和字段返回合成行情、mask 与未来标签。"""

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


def _build_panel(*, label_mode: str, factor_mask: bool) -> pl.DataFrame:
    """用固定种子生成 140 个日期、150 个资产的类价格面板。"""

    generator = random.Random(20260715)
    latent_signal = [generator.gauss(0.0, 1.0) for _ in range(ASSET_COUNT)]
    rows: list[dict[str, object]] = []
    start = date(2020, 1, 1)
    for day_index in range(DATE_COUNT):
        current = start + timedelta(days=day_index)
        for asset_index in range(ASSET_COUNT):
            signal = latent_signal[asset_index]
            close = (
                100.0
                + asset_index * 0.03
                + day_index * 0.10 * signal
                + generator.gauss(0.0, 0.001)
            )
            open_price = close - abs(generator.gauss(0.0, 0.01))
            high = max(open_price, close) + 0.02
            low = min(open_price, close) - 0.02
            volume = 1_000_000.0 + abs(signal) * 10_000.0
            rows.append(
                {
                    "date": current,
                    "asset": f"S{asset_index:03d}",
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": volume * close,
                    "valid_for_factor_compute": factor_mask,
                    "valid_for_factor_rank": True,
                    "valid_for_trading": True,
                    "label_o2o_5d": signal + generator.gauss(0.0, 0.01),
                }
            )
    if label_mode == "shuffled":
        shuffle_generator = random.Random(20260716)
        for start_index in range(0, len(rows), ASSET_COUNT):
            labels = [
                rows[row_index]["label_o2o_5d"]
                for row_index in range(start_index, start_index + ASSET_COUNT)
            ]
            shuffle_generator.shuffle(labels)
            for offset, label in enumerate(labels):
                rows[start_index + offset]["label_o2o_5d"] = label
    elif label_mode != "signal":
        raise ValueError(f"未知合成标签模式：{label_mode}")
    return pl.DataFrame(rows).sort(["date", "asset"])


def _campaign(candidate_id: str) -> CampaignSpec:
    """构造使用固定 visible 区间和统计预算的合成 campaign。"""

    return CampaignSpec(
        visible_start=VISIBLE_START,
        visible_end=VISIBLE_END,
        candidate_ids=(candidate_id,),
        max_hypotheses=1,
        alpha=0.05,
        label_column="label_o2o_5d",
        rank_mask_column="valid_for_factor_rank",
        hac_max_lags=5,
        min_valid_dates=100,
        min_names_per_date=100,
        min_median_coverage=0.8,
        max_abs_output_correlation=0.8,
    )


def _run(
    root: Path,
    candidate,
    source: SyntheticSource,
):
    """在临时 artifact 根目录运行单候选 visible campaign。"""

    return run_visible_campaign(
        _campaign(candidate.candidate_id),
        {candidate.candidate_id: candidate},
        source,
        root,
        code_hash="synthetic-e2e-code",
        config_hash="synthetic-e2e-config",
    )


class SyntheticE2ETest(unittest.TestCase):
    """验证合成成功、失败、重复、重启和账本审计路径。"""

    def test_panel_is_large_deterministic_and_has_price_like_fields(self) -> None:
        """合成面板规模、字段和固定种子结果必须稳定。"""

        first = _build_panel(label_mode="signal", factor_mask=True)
        second = _build_panel(label_mode="signal", factor_mask=True)
        self.assertEqual(first.shape, (DATE_COUNT * ASSET_COUNT, 12))
        self.assertEqual(first.get_column("date").n_unique(), DATE_COUNT)
        self.assertEqual(first.get_column("asset").n_unique(), ASSET_COUNT)
        self.assertEqual(set(MARKET_COLUMNS), set(first.columns[2:8]))
        self.assertEqual(first.write_json(), second.write_json())

    def test_only_strong_nonduplicate_candidate_passes_and_replay_is_idempotent(
        self,
    ) -> None:
        """强信号通过，打乱标签和全空候选失败，重复运行保持哈希稳定。"""

        strong = valid_registered_candidate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            passed = _run(root, strong, SyntheticSource(label_mode="signal"))
            self.assertEqual(
                passed.statuses[strong.candidate_id],
                CandidateTerminalStatus.VISIBLE_PASSED,
            )
            self.assertGreater(passed.metrics[strong.candidate_id]["evaluation"]["icir"], 0)

            shuffled = _run(root, strong, SyntheticSource(label_mode="shuffled"))
            self.assertEqual(
                shuffled.statuses[strong.candidate_id],
                CandidateTerminalStatus.VISIBLE_FAILED,
            )

            all_null = _run(
                root,
                strong,
                SyntheticSource(label_mode="signal", factor_mask=False),
            )
            self.assertEqual(
                all_null.statuses[strong.candidate_id],
                CandidateTerminalStatus.COMPUTE_FAILED,
            )

            replay = _run(root, strong, SyntheticSource(label_mode="signal"))
            self.assertEqual(
                replay.statuses[strong.candidate_id],
                CandidateTerminalStatus.VISIBLE_PASSED,
            )
            self.assertEqual(
                replay.artifact_hashes[strong.candidate_id],
                passed.artifact_hashes[strong.candidate_id],
            )
            self.assertTrue(JsonlLedger(root).verify())
            for path_text in replay.artifact_refs[strong.candidate_id]:
                path = Path(path_text)
                self.assertTrue(path.is_relative_to(root))
                self.assertFalse(path.is_relative_to(Path("/data/quantlake")))
                self.assertFalse(path.is_relative_to(Path.cwd()))

            paths = LedgerPaths(root)
            candidate_path = paths.candidates_root / f"{strong.candidate_id}.json"
            original = candidate_path.read_bytes()
            candidate_path.write_bytes(original.replace(b"synthetic-test", b"tampered-test"))
            with self.assertRaises(FactorMinerError) as context:
                JsonlLedger(root).register_candidate(strong)
            self.assertEqual(context.exception.code, FailureCode.LEDGER_CORRUPT)
            candidate_path.write_bytes(original)
            self.assertTrue(JsonlLedger(root).verify())

    def test_negative_shift_is_rejected_before_outcome_and_canonical_duplicates_are_blocked(
        self,
    ) -> None:
        """负 shift 不能暴露结果，交换律等价候选必须识别为结构重复。"""

        invalid_spec = valid_candidate().model_copy(
            update={
                "expression": FactorNode(
                    op="delay",
                    args=(FactorNode(op="field", field="close"),),
                    period=-1,
                ),
                "required_fields": ("close",),
                "max_lookback": 0,
            }
        )
        invalid = registered_candidate(invalid_spec)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _run(root, invalid, SyntheticSource(label_mode="signal"))
            self.assertEqual(
                result.statuses[invalid.candidate_id],
                CandidateTerminalStatus.COMPILE_FAILED,
            )
            events = JsonlLedger(root).read_events()
            self.assertEqual(events[-1].event_type, EventType.COMPILE_FAILED)
            self.assertFalse(any(event.outcome_exposed for event in events))

        left = FactorNode(op="field", field="close")
        right = FactorNode(op="const", value=0.0)
        first_spec = valid_candidate().model_copy(
            update={
                "expression": FactorNode(op="add", args=(left, right)),
                "required_fields": ("close",),
                "max_lookback": 0,
            }
        )
        second_spec = first_spec.model_copy(
            update={
                "expression": FactorNode(op="add", args=(right, left)),
            }
        )
        first = registered_candidate(first_spec)
        second = registered_candidate(second_spec)
        self.assertNotEqual(first.candidate_id, second.candidate_id)
        first_plan = compile_candidate(first, set(MARKET_COLUMNS))
        second_plan = compile_candidate(second, set(MARKET_COLUMNS))
        self.assertEqual(first_plan.ast_hash, second_plan.ast_hash)
        self.assertEqual(first_plan.ast_hash, canonical_ast_hash(second.spec.expression))
        structural = check_structural_redundancy(second_plan, (first_plan,))
        self.assertFalse(structural.passed)
        self.assertEqual(structural.matched_candidate_ids, (first.candidate_id,))

        duplicate_campaign = _campaign(first.candidate_id).model_dump(mode="json")
        duplicate_campaign.update(
            {
                "candidate_ids": [first.candidate_id, first.candidate_id],
                "max_hypotheses": 2,
            }
        )
        with self.assertRaises(ValidationError):
            CampaignSpec.model_validate(duplicate_campaign)


if __name__ == "__main__":
    unittest.main()
