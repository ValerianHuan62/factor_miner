"""原始因子计算和质量报告测试。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.compiler import compile_candidate
from factor_miner.compute import compute_raw_factor, compute_trusted_raw_factor
from factor_miner.data_source import (
    DataProvenance,
    DataRequest,
    FactorInputRequest,
    InputProvenance,
)
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import FactorNode, RegisteredCandidate, registered_candidate
from tests.helpers import valid_candidate


def rolling_candidate() -> RegisteredCandidate:
    """构造五日 rolling mean 候选。"""

    spec = valid_candidate().model_copy(
        update={
            "expression": FactorNode(
                op="rolling_mean",
                args=(FactorNode(op="field", field="close"),),
                window=5,
            ),
            "required_fields": ("close",),
            "max_lookback": 5,
        }
    )
    return registered_candidate(spec)


def division_candidate() -> RegisteredCandidate:
    """构造用于检查零除数和空值保留的候选。"""

    spec = valid_candidate().model_copy(
        update={
            "expression": FactorNode(
                op="div",
                args=(
                    FactorNode(op="field", field="close"),
                    FactorNode(op="field", field="denominator"),
                ),
            ),
            "required_fields": ("close", "denominator"),
            "max_lookback": 0,
        }
    )
    return registered_candidate(spec)


def synthetic_panel(*, denominator_mode: str = "normal") -> pl.DataFrame:
    """生成完全由代码构造的三资产、三十日期原始面板。"""

    rows: list[dict[str, object]] = []
    start = date(2020, 1, 1)
    for day_index in range(30):
        for asset_index, asset in enumerate(("AAA", "BBB", "CCC")):
            denominator = 2.0
            close: float | None = float(100 + asset_index + day_index)
            if denominator_mode == "zero" or (
                denominator_mode == "partial" and day_index == 2
            ):
                denominator = 0.0
            if denominator_mode == "partial" and day_index == 1:
                close = None
            rows.append(
                {
                    "date": start + timedelta(days=day_index),
                    "asset": asset,
                    "close": close,
                    "denominator": denominator,
                    "valid_for_factor_compute": not (
                        day_index == 3 and asset == "CCC"
                    ),
                    "valid_for_factor_rank": True,
                    "valid_for_trading": True,
                    "label_o2o_5d": 0.01,
                }
            )
    return pl.DataFrame(rows).sort(["date", "asset"])


class SyntheticDataSource:
    """满足数据源端口的内存合成实现，不读取外部文件。"""

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.inspected = False

    def inspect(self) -> DataProvenance:
        """记录合同检查已完成并返回固定 provenance。"""

        self.inspected = True
        return DataProvenance(
            data_origin="synthetic-test",
            resolved_release_id="synthetic-release-1",
            release_manifest_sha256="b" * 64,
            schema_version="synthetic-v1",
            market_cutoff="2020-01-30",
            adjustment_convention="synthetic",
            calendar_version="synthetic-calendar-v1",
            state_table_version="synthetic-state-v1",
            state_table_cutoff="2020-01-30",
            code_commit="synthetic-commit",
            config_hash="synthetic-config",
        )

    def scan(self, request: DataRequest) -> pl.LazyFrame:
        """返回带 warmup 的请求字段和计算 mask。"""

        if not self.inspected:
            raise FactorMinerError(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                "合成数据源尚未 inspect",
            )
        lower = request.start - timedelta(days=request.warmup_days)
        columns = [
            "date",
            "asset",
            *request.required_fields,
            "valid_for_factor_compute",
        ]
        return (
            self.frame.lazy()
            .filter(pl.col("date").is_between(lower, request.end, closed="both"))
            .select(list(dict.fromkeys(columns)))
            .sort(["date", "asset"])
        )


class TrustedSyntheticInputSource:
    """只实现 input 端口；没有任何 outcome 方法。"""

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame.drop("label_o2o_5d")
        self.inspect_count = 0
        self.scan_count = 0

    def inspect_inputs(self) -> InputProvenance:
        """记录独立 input 合同检查。"""

        self.inspect_count += 1
        return InputProvenance(
            data_origin="synthetic-test",
            resolved_release_id="synthetic-release-1",
            release_manifest_sha256="b" * 64,
            schema_version="synthetic-v1",
            market_cutoff="2020-01-30",
            adjustment_convention="synthetic",
            calendar_version="synthetic-calendar-v1",
            state_table_version="synthetic-state-v1",
            state_table_cutoff="2020-01-30",
            code_commit="synthetic-commit",
            config_hash="synthetic-config",
        )

    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
        """只返回行情字段和 mask。"""

        self.scan_count += 1
        lower = request.start - timedelta(days=request.warmup_observations)
        return (
            self.frame.lazy()
            .filter(pl.col("date").is_between(lower, request.end, closed="both"))
            .select(
                [
                    "date",
                    "asset",
                    *request.required_fields,
                    "valid_for_factor_compute",
                    "valid_for_factor_rank",
                    "valid_for_trading",
                ]
            )
            .sort(["date", "asset"])
        )


class ComputeTest(unittest.TestCase):
    """验证原始计算层不做任何预处理。"""

    def test_rolling_factor_removes_warmup_rows(self) -> None:
        """rolling 因子应使用 warmup，但保存结果不包含 warmup 日期。"""
        candidate = rolling_candidate()
        plan = compile_candidate(candidate, {"close"})
        request = DataRequest(date(2020, 1, 10), date(2020, 1, 12), ("close",), 5)
        with tempfile.TemporaryDirectory() as directory:
            artifact = compute_raw_factor(
                plan,
                SyntheticDataSource(synthetic_panel()),
                request,
                Path(directory) / "raw.parquet",
            )
            frame = pl.read_parquet(artifact.artifact_path)
            self.assertEqual(frame.height, 9)
            self.assertEqual(frame.get_column("date").min(), request.start)
            self.assertEqual(frame.get_column("date").max(), request.end)
            self.assertTrue(frame.get_column("raw_factor").is_not_null().all())

    def test_trusted_compute_uses_only_factor_input_port(self) -> None:
        """trusted raw compute 不得要求 inspect/scan outcome。"""

        candidate = rolling_candidate()
        plan = compile_candidate(candidate, {"close"})
        request = FactorInputRequest(
            date(2020, 1, 10),
            date(2020, 1, 12),
            ("close",),
            warmup_observations=5,
        )
        source = TrustedSyntheticInputSource(synthetic_panel())
        with tempfile.TemporaryDirectory() as directory:
            artifact = compute_trusted_raw_factor(
                plan,
                source,
                request,
                Path(directory) / "trusted.raw.parquet",
            )
            self.assertEqual(source.inspect_count, 1)
            self.assertEqual(source.scan_count, 1)
            self.assertEqual(pl.read_parquet(artifact.artifact_path).height, 9)

    def test_nulls_are_preserved_and_infinity_becomes_null(self) -> None:
        """原始空值和零除产生的非有限值都必须保持为 null。"""
        candidate = division_candidate()
        plan = compile_candidate(candidate, {"close", "denominator"})
        request = DataRequest(date(2020, 1, 1), date(2020, 1, 3), ("close", "denominator"))
        with tempfile.TemporaryDirectory() as directory:
            artifact = compute_raw_factor(
                plan,
                SyntheticDataSource(synthetic_panel(denominator_mode="partial")),
                request,
                Path(directory) / "raw.parquet",
            )
            frame = pl.read_parquet(artifact.artifact_path)
            self.assertGreater(frame.get_column("raw_factor").null_count(), 0)
            self.assertTrue(frame.get_column("raw_factor").is_finite().fill_null(True).all())

    def test_all_null_factor_is_rejected(self) -> None:
        """最终 raw factor 全为空时必须硬失败。"""
        plan = compile_candidate(
            division_candidate(), {"close", "denominator"}
        )
        request = DataRequest(date(2020, 1, 1), date(2020, 1, 3), ("close", "denominator"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FactorMinerError, "FACTOR_ALL_NULL"):
                compute_raw_factor(
                    plan,
                    SyntheticDataSource(synthetic_panel(denominator_mode="zero")),
                    request,
                    Path(directory) / "raw.parquet",
                )

    def test_quality_reports_low_coverage_and_all_null_dates(self) -> None:
        """质量报告应记录每日覆盖率中位数和全空日期。"""
        plan = compile_candidate(division_candidate(), {"close", "denominator"})
        request = DataRequest(date(2020, 1, 1), date(2020, 1, 3), ("close", "denominator"))
        with tempfile.TemporaryDirectory() as directory:
            artifact = compute_raw_factor(
                plan,
                SyntheticDataSource(synthetic_panel(denominator_mode="partial")),
                request,
                Path(directory) / "raw.parquet",
            )
            self.assertLess(artifact.quality.median_daily_coverage, 1.0)
            self.assertEqual(
                artifact.quality.all_null_dates,
                (date(2020, 1, 2), date(2020, 1, 3)),
            )
            self.assertGreater(artifact.quality.null_ratio, 0.0)

    def test_repeated_run_has_identical_content_hash_and_quality_metrics(self) -> None:
        """相同输入重复运行应得到相同 Parquet 和确定性质量字段。"""
        plan = compile_candidate(rolling_candidate(), {"close"})
        request = DataRequest(date(2020, 1, 10), date(2020, 1, 12), ("close",), 5)
        with tempfile.TemporaryDirectory() as directory:
            first = compute_raw_factor(
                plan, SyntheticDataSource(synthetic_panel()), request, Path(directory) / "first.parquet"
            )
            second = compute_raw_factor(
                plan, SyntheticDataSource(synthetic_panel()), request, Path(directory) / "second.parquet"
            )
            self.assertEqual(first.plan_hash, second.plan_hash)
            self.assertEqual(first.artifact_sha256, second.artifact_sha256)
            self.assertEqual(
                first.quality.model_dump(exclude={"elapsed_seconds"}),
                second.quality.model_dump(exclude={"elapsed_seconds"}),
            )

    def test_compute_source_has_no_raw_layer_preprocessing(self) -> None:
        """计算源码不得出现标准化、去极值、中性化或横截面排序。"""
        source = Path(__file__).parents[1].joinpath("src", "factor_miner", "compute.py")
        text = source.read_text(encoding="utf-8")
        for forbidden in ("zscore", "winsorize", "neutralize", "fill_null(0)", ".rank("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
