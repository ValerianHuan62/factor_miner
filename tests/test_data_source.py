"""公司 A 股数据源合同测试。"""

from datetime import date, timedelta
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest

import polars as pl

from factor_miner.company_a_share import (
    CompanyAShareDataSource,
    CompanyAShareFactorInputSource,
    CompanyAShareOutcomeSource,
    ParquetReferenceFactorSource,
)
from factor_miner.data_source import FactorInputRequest, OutcomeRequest, DataRequest
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.runtime import ExecutionMode, RuntimeProfile


def _profile(
    root: Path,
    market_uri: Path,
    state_uri: Path,
    label_uri: Path,
    *,
    mode: ExecutionMode = ExecutionMode.SYNTHETIC,
    quantlake_root: Path | None = None,
    missing_provenance: bool = False,
) -> RuntimeProfile:
    """构造仅指向程序生成 fixture 的运行配置。"""

    return RuntimeProfile(
        mode=mode,
        platform_name="Linux" if mode in {ExecutionMode.SMOKE, ExecutionMode.VISIBLE} else "Darwin",
        artifact_root=root / "artifacts",
        quantlake_root=quantlake_root,
        market_uri=market_uri,
        state_uri=state_uri,
        label_uri=label_uri,
        data_origin=None if missing_provenance else "server_quantlake",
        resolved_release_id=None if missing_provenance else "release-test-1",
        release_manifest_sha256=None if missing_provenance else "a" * 64,
        schema_version=None if missing_provenance else "company-a-share-v1",
        market_cutoff=None if missing_provenance else "2020-01-03",
        adjustment_convention=None if missing_provenance else "unadjusted",
        calendar_version=None if missing_provenance else "calendar-test-1",
        state_table_version=None if missing_provenance else "state-test-1",
        state_table_cutoff=None if missing_provenance else "2020-01-03",
        environment={},
    )


def _write_fixture(
    root: Path,
    *,
    duplicate_market_key: bool = False,
    missing_rank_mask: bool = False,
    market_cutoff: date = date(2020, 1, 3),
    state_cutoff: date = date(2020, 1, 3),
    bad_mask: bool = False,
) -> tuple[Path, Path, Path]:
    """写入完全由代码生成的最小三表 Parquet fixture。"""

    market_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    label_rows: list[dict[str, object]] = []
    for day_offset in range((market_cutoff - date(2020, 1, 1)).days + 1):
        current = date(2020, 1, 1) + timedelta(days=day_offset)
        for asset_index, asset in enumerate(("AAA", "BBB")):
            market_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "open": 10.0 + asset_index + day_offset,
                    "high": 11.0 + asset_index + day_offset,
                    "low": 9.0 + asset_index + day_offset,
                    "close": 10.5 + asset_index + day_offset,
                    "volume": 100.0 + day_offset,
                    "amount": 1000.0 + day_offset,
                }
            )
    for day_offset in range((state_cutoff - date(2020, 1, 1)).days + 1):
        current = date(2020, 1, 1) + timedelta(days=day_offset)
        for asset in ("AAA", "BBB"):
            is_st = False
            is_newly_listed = False
            is_suspended = current == date(2020, 1, 2) and asset == "BBB"
            can_buy = True
            can_sell = asset == "AAA"
            compute = not (is_st or is_newly_listed)
            rank = compute and not is_suspended
            trading = compute and can_buy and can_sell
            if bad_mask and asset == "AAA" and current == date(2020, 1, 3):
                rank = not rank
            row = {
                "date": current,
                "asset": asset,
                "is_st": is_st,
                "is_newly_listed": is_newly_listed,
                "is_suspended": is_suspended,
                "can_buy": can_buy,
                "can_sell": can_sell,
                "can_open_long": can_buy,
                "can_close_long": can_sell,
                "valid_for_factor_compute": compute,
                "valid_for_factor_rank": rank,
                "valid_for_trading": trading,
            }
            if missing_rank_mask:
                row.pop("valid_for_factor_rank")
            state_rows.append(row)
    for row in market_rows:
        label_rows.append(
            {
                "date": row["date"],
                "asset": row["asset"],
                "label_entry_date": row["date"] + timedelta(days=1),
                "label_exit_date": row["date"] + timedelta(days=6),
                "label_o2o_5d": 0.01,
            }
        )
    if duplicate_market_key:
        market_rows.append(market_rows[-1].copy())

    market_uri = root / "market.parquet"
    state_uri = root / "state.parquet"
    label_uri = root / "label.parquet"
    pl.DataFrame(market_rows).write_parquet(market_uri)
    pl.DataFrame(state_rows).write_parquet(state_uri)
    pl.DataFrame(label_rows).write_parquet(label_uri)
    return market_uri, state_uri, label_uri


class CompanyAShareDataSourceTest(unittest.TestCase):
    """验证显式 URI 适配器的失败关闭合同。"""

    def test_valid_fixture_returns_projected_lazy_frame(self) -> None:
        """合法 fixture 应返回稳定排序且包含标准附加列的 LazyFrame。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uris = _write_fixture(root)
            source = CompanyAShareDataSource(
                _profile(root, *uris), code_commit="commit-1", config_hash="config-1"
            )
            provenance = source.inspect()
            frame = source.scan(
                DataRequest(date(2020, 1, 2), date(2020, 1, 3), ("close",), 1)
            ).collect()

            self.assertEqual(provenance.resolved_release_id, "release-test-1")
            self.assertEqual(
                frame.columns,
                [
                    "date",
                    "asset",
                    "close",
                    "valid_for_factor_compute",
                    "valid_for_factor_rank",
                    "valid_for_trading",
                    "can_open_long",
                    "can_close_long",
                    "label_o2o_5d",
                    "label_entry_date",
                    "label_exit_date",
                ],
            )
            self.assertEqual(frame.height, 6)
            self.assertEqual(frame.row(0)[:2], (date(2020, 1, 1), "AAA"))

    def test_factor_input_source_never_opens_or_returns_labels(self) -> None:
        """input 合同必须在标签文件不存在时仍通过，输出不得含 label。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market_uri, state_uri, label_uri = _write_fixture(root)
            label_uri.unlink()
            source = CompanyAShareFactorInputSource(
                _profile(root, market_uri, state_uri, label_uri),
                code_commit="commit-1",
                config_hash="config-1",
            )
            provenance = source.inspect_inputs()
            frame = source.scan_inputs(
                FactorInputRequest(
                    date(2020, 1, 2),
                    date(2020, 1, 3),
                    ("close",),
                    warmup_observations=1,
                )
            ).collect()
            self.assertEqual(provenance.resolved_release_id, "release-test-1")
            self.assertNotIn("label_o2o_5d", frame.columns)
            self.assertEqual(
                frame.columns,
                [
                    "date",
                    "asset",
                    "close",
                    "valid_for_factor_compute",
                    "valid_for_factor_rank",
                    "valid_for_trading",
                    "can_open_long",
                    "can_close_long",
                ],
            )

    def test_trusted_warmup_uses_trading_observations(self) -> None:
        """一百二十个观察值必须跨越周末读取足量交易日。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trading_dates: list[date] = []
            current = date(2020, 1, 1)
            while len(trading_dates) < 130:
                if current.weekday() < 5:
                    trading_dates.append(current)
                current += timedelta(days=1)
            market_uri, state_uri, label_uri = _write_trading_fixture(
                root, trading_dates, ("AAA",)
            )
            profile = replace(
                _profile(root, market_uri, state_uri, label_uri),
                market_cutoff=trading_dates[-1].isoformat(),
                state_table_cutoff=trading_dates[-1].isoformat(),
            )
            source = CompanyAShareFactorInputSource(
                profile, code_commit="commit-1", config_hash="config-1"
            )
            source.inspect_inputs()
            frame = source.scan_inputs(
                FactorInputRequest(
                    start=trading_dates[120],
                    end=trading_dates[125],
                    required_fields=("close",),
                    warmup_observations=120,
                )
            ).collect()
            self.assertEqual(frame.get_column("date").min(), trading_dates[0])
            self.assertGreater(
                (trading_dates[120] - trading_dates[0]).days,
                120,
            )

    def test_trusted_warmup_fails_when_one_asset_history_is_short(self) -> None:
        """任一资产的交易观察历史不足时必须硬失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trading_dates = [date(2020, 1, 1) + timedelta(days=index) for index in range(10)]
            market_uri, state_uri, label_uri = _write_trading_fixture(
                root, trading_dates, ("AAA", "BBB"), short_asset="BBB"
            )
            profile = replace(
                _profile(root, market_uri, state_uri, label_uri),
                market_cutoff=trading_dates[-1].isoformat(),
                state_table_cutoff=trading_dates[-1].isoformat(),
            )
            source = CompanyAShareFactorInputSource(
                profile, code_commit="commit-1", config_hash="config-1"
            )
            source.inspect_inputs()
            with self.assertRaisesRegex(FactorMinerError, "FACTOR_COVERAGE_TOO_LOW"):
                source.scan_inputs(
                    FactorInputRequest(
                        start=trading_dates[8],
                        end=trading_dates[9],
                        required_fields=("close",),
                        warmup_observations=5,
                    )
                )

    def test_new_listing_becomes_eligible_after_accumulating_warmup(self) -> None:
        """新股无效期不阻断批次，首次有效日必须已经积累足够历史观察。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trading_dates = [date(2020, 1, 1) + timedelta(days=index) for index in range(10)]
            market_uri, state_uri, label_uri = _write_trading_fixture(
                root, trading_dates, ("AAA", "BBB")
            )
            market = pl.read_parquet(market_uri).filter(
                ~((pl.col("asset") == "BBB") & (pl.col("date") < trading_dates[3]))
            )
            state = (
                pl.read_parquet(state_uri)
                .filter(~((pl.col("asset") == "BBB") & (pl.col("date") < trading_dates[3])))
                .with_columns(
                    pl.when(
                        (pl.col("asset") == "BBB") & (pl.col("date") < trading_dates[8])
                    )
                    .then(True)
                    .otherwise(pl.col("is_newly_listed"))
                    .alias("is_newly_listed")
                )
                .with_columns(
                    (~(pl.col("is_st") | pl.col("is_newly_listed"))).alias(
                        "valid_for_factor_compute"
                    )
                )
                .with_columns(
                    (
                        pl.col("valid_for_factor_compute") & ~pl.col("is_suspended")
                    ).alias("valid_for_factor_rank"),
                    (
                        pl.col("valid_for_factor_compute")
                        & pl.col("can_buy")
                        & pl.col("can_sell")
                    ).alias("valid_for_trading"),
                )
            )
            label = pl.read_parquet(label_uri).filter(
                ~((pl.col("asset") == "BBB") & (pl.col("date") < trading_dates[3]))
            )
            market.write_parquet(market_uri)
            state.write_parquet(state_uri)
            label.write_parquet(label_uri)
            profile = replace(
                _profile(root, market_uri, state_uri, label_uri),
                market_cutoff=trading_dates[-1].isoformat(),
                state_table_cutoff=trading_dates[-1].isoformat(),
            )
            source = CompanyAShareFactorInputSource(
                profile, code_commit="commit-1", config_hash="config-1"
            )
            source.inspect_inputs()
            frame = source.scan_inputs(
                FactorInputRequest(
                    start=trading_dates[5],
                    end=trading_dates[9],
                    required_fields=("close",),
                    warmup_observations=5,
                )
            ).collect()
            first_eligible = frame.filter(
                (pl.col("asset") == "BBB") & pl.col("valid_for_factor_compute")
            ).get_column("date").min()
            self.assertEqual(first_eligible, trading_dates[8])

    def test_outcome_source_is_physically_independent_from_market_inputs(self) -> None:
        """outcome 合同只读取 label，不得依赖行情或状态文件存在。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market_uri, state_uri, label_uri = _write_fixture(root)
            market_uri.unlink()
            state_uri.unlink()
            source = CompanyAShareOutcomeSource(
                _profile(root, market_uri, state_uri, label_uri),
                company_a_share_visible_policy(),
            )
            provenance = source.inspect_outcomes()
            frame = source.scan_outcomes(
                OutcomeRequest(date(2020, 1, 2), date(2020, 1, 3))
            ).collect()
            self.assertEqual(provenance.label_id, "company_a_share_o2o_5d_v1")
            self.assertEqual(
                frame.columns,
                ["date", "asset", "label_o2o_5d", "label_entry_date", "label_exit_date"],
            )

    def test_reference_source_requires_frozen_manifest_and_factor_set(self) -> None:
        """reference source 必须拒绝错误 manifest，并只投影冻结因子。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference_path = root / "reference.parquet"
            pl.DataFrame(
                {
                    "date": [date(2020, 1, 2), date(2020, 1, 3)],
                    "asset": ["AAA", "AAA"],
                    "raw_factor": [1.0, 2.0],
                }
            ).write_parquet(reference_path)
            source = ParquetReferenceFactorSource(
                manifest_id="reference-manifest-v1",
                manifest_sha256="d" * 64,
                factor_paths={"reference_momentum_20d": reference_path},
                data_cutoff="2020-01-03",
            )
            with self.assertRaises(FactorMinerError):
                source.inspect_references("wrong-manifest")
            provenance = source.inspect_references("reference-manifest-v1")
            frame = source.scan_reference(
                "reference_momentum_20d", date(2020, 1, 2), date(2020, 1, 3)
            ).collect()
            self.assertEqual(provenance.factor_ids, ("reference_momentum_20d",))
            self.assertEqual(frame.columns, ["date", "asset", "raw_factor"])

    def test_scan_requires_prior_inspection(self) -> None:
        """未完成 inspect 时 scan 必须硬失败。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uris = _write_fixture(root)
            source = CompanyAShareDataSource(
                _profile(root, *uris), code_commit="commit-1", config_hash="config-1"
            )
            with self.assertRaisesRegex(FactorMinerError, "STATE_COVERAGE_INCOMPLETE"):
                source.scan(DataRequest(date(2020, 1, 2), date(2020, 1, 3), ("close",), 0))

    def test_duplicate_date_asset_is_rejected(self) -> None:
        """行情主键重复时必须拒绝读取。"""
        self._assert_inspect_fails(duplicate_market_key=True, code=FailureCode.DATA_RELEASE_MISMATCH)

    def test_missing_mask_is_rejected(self) -> None:
        """缺少标准 mask 时必须拒绝读取。"""
        self._assert_inspect_fails(missing_rank_mask=True, code=FailureCode.FIELD_MISSING)

    def test_cutoff_mismatch_is_rejected(self) -> None:
        """行情和状态截止日期不一致时必须拒绝读取。"""
        self._assert_inspect_fails(state_cutoff=date(2020, 1, 2), code=FailureCode.DATA_RELEASE_MISMATCH)

    def test_state_table_stale_against_provenance_is_rejected(self) -> None:
        """状态表落后于 provenance 声明时必须拒绝读取。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uris = _write_fixture(root, state_cutoff=date(2020, 1, 2))
            profile = _profile(root, *uris)
            source = CompanyAShareDataSource(profile, code_commit="commit-1", config_hash="config-1")
            with self.assertRaisesRegex(FactorMinerError, "DATA_RELEASE_MISMATCH"):
                source.inspect()

    def test_derived_mask_semantics_are_recomputed(self) -> None:
        """提供的派生 mask 与基础状态不一致时必须拒绝读取。"""
        self._assert_inspect_fails(bad_mask=True, code=FailureCode.STATE_COVERAGE_INCOMPLETE)

    def test_missing_provenance_is_rejected(self) -> None:
        """任何 provenance 字段缺失时必须拒绝读取。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uris = _write_fixture(root)
            source = CompanyAShareDataSource(
                _profile(root, *uris, missing_provenance=True),
                code_commit="commit-1",
                config_hash="config-1",
            )
            with self.assertRaisesRegex(FactorMinerError, "DATA_RELEASE_MISMATCH"):
                source.inspect()

    def test_writable_local_input_root_is_accepted(self) -> None:
        """本地用户拥有输入目录权限不应阻止读取。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake_root = root / "quantlake"
            quantlake_root.mkdir()
            quantlake_root.chmod(0o755)
            uris = _write_fixture(root)
            source = CompanyAShareDataSource(
                _profile(
                    root,
                    *uris,
                    mode=ExecutionMode.SMOKE,
                    quantlake_root=quantlake_root,
                ),
                code_commit="commit-1",
                config_hash="config-1",
            )
            self.assertEqual(source.inspect().resolved_release_id, "release-test-1")

    def _assert_inspect_fails(self, **fixture_options: object) -> None:
        """对 fixture 变体执行统一 inspect 失败断言。"""
        expected_code = fixture_options.pop("code")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uris = _write_fixture(root, **fixture_options)
            source = CompanyAShareDataSource(
                _profile(root, *uris), code_commit="commit-1", config_hash="config-1"
            )
            with self.assertRaisesRegex(FactorMinerError, str(expected_code)):
                source.inspect()


if __name__ == "__main__":
    unittest.main()


def _write_trading_fixture(
    root: Path,
    trading_dates: list[date],
    assets: tuple[str, ...],
    *,
    short_asset: str | None = None,
) -> tuple[Path, Path, Path]:
    """写入可控制交易观察长度的纯合成三表。"""

    market_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    label_rows: list[dict[str, object]] = []
    for date_index, current in enumerate(trading_dates):
        for asset in assets:
            if asset == short_asset and date_index < len(trading_dates) - 3:
                continue
            market_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.0 + date_index,
                    "volume": 100.0,
                    "amount": 1000.0,
                }
            )
            state_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "is_st": False,
                    "is_newly_listed": False,
                    "is_suspended": False,
                    "can_buy": True,
                    "can_sell": True,
                    "can_open_long": True,
                    "can_close_long": True,
                    "valid_for_factor_compute": True,
                    "valid_for_factor_rank": True,
                    "valid_for_trading": True,
                }
            )
            label_rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "label_entry_date": current + timedelta(days=1),
                    "label_exit_date": current + timedelta(days=6),
                    "label_o2o_5d": 0.01,
                }
            )
    paths = (root / "market.parquet", root / "state.parquet", root / "label.parquet")
    pl.DataFrame(market_rows).write_parquet(paths[0])
    pl.DataFrame(state_rows).write_parquet(paths[1])
    pl.DataFrame(label_rows).write_parquet(paths[2])
    return paths
