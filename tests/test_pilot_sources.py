"""阶段 A 输入合同和固定候选夹具测试。"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.canonical import sha256_json
from factor_miner.pilot_schema import PilotSourcePaths
from factor_miner.pilot_sources import (
    _scan_manifest_partitions,
    inspect_pilot_source_identity,
    inspect_pilot_sources,
    load_fixed_candidates,
)
from factor_miner.compiler import compile_candidate


FIXTURE = Path(__file__).parent / "fixtures" / "pilot" / "fixed_candidates.json"


def _partition_source_sha256(partitions: list[dict[str, str]]) -> str:
    """按正式 manifest 定义计算分区身份。"""

    return sha256_json(sorted(partitions, key=lambda item: item["path"]))


def _write_pilot_inputs(root: Path) -> PilotSourcePaths:
    """生成只含合同字段的合成 Pilot 输入。"""

    quantlake = root / "quantlake"
    quantlake.mkdir()
    (quantlake / "metadata").mkdir()
    (quantlake / "config").mkdir()
    (quantlake / "processed").mkdir()
    (quantlake / "state").mkdir()
    (quantlake / "calendar").mkdir()
    (quantlake / "benchmark").mkdir()

    release = {
        "version": "quantlake-release-v1",
        "source_sha256": "a" * 64,
        "release_id": "release-1",
        "market_cutoff": "2026-07-31",
        "l2_cutoff": "2026-07-31",
        "schema_version": "l2_state_v1",
        "market_release_id": "release-1",
        "state_release_id": "release-1",
        "market_partitions": [
            {
                "path": "market.parquet",
                "start": "2026-07-30",
                "end": "2026-07-31",
                "sha256": "b" * 64,
            }
        ],
        "state_partitions": [
            {
                "path": "state.parquet",
                "start": "2026-07-30",
                "end": "2026-07-31",
                "sha256": "c" * 64,
            }
        ],
    }
    (quantlake / "metadata" / "release.json").write_text(
        json.dumps(release), encoding="utf-8"
    )
    state_manifest = {
        "schema_version": "l2_state_v1",
        "state_table_version": "state-1",
        "state_table_cutoff": "2026-07-31",
        "adjustment_convention": "forward_adjusted_market; state_flags_unadjusted",
    }
    (quantlake / "metadata" / "state.json").write_text(
        json.dumps(state_manifest), encoding="utf-8"
    )
    pl.DataFrame(
        {
            "date": [date(2026, 7, 30), date(2026, 7, 31)],
            "code": ["600000.XSHG", "600000.XSHG"],
            "adj_open": [10.0, 10.5],
            "adj_close": [10.2, 10.7],
            "volume": [100.0, 110.0],
            "money": [1000.0, 1100.0],
            "adjust_factor": [1.0, 1.0],
        }
    ).write_parquet(quantlake / "processed" / "market.parquet")
    pl.DataFrame(
        {
            "date": [date(2026, 7, 30), date(2026, 7, 31)],
            "code": ["600000.XSHG", "600000.XSHG"],
            "is_st": [False, False],
            "is_newly_listed": [False, False],
            "is_suspended": [False, False],
            "can_buy": [True, True],
            "can_sell": [True, True],
            "valid_for_factor_compute": [True, True],
            "valid_for_factor_rank": [True, True],
            "valid_for_trading": [True, True],
        }
    ).write_parquet(quantlake / "state" / "state.parquet")
    release["market_partitions"][0]["sha256"] = hashlib.sha256(
        (quantlake / "processed" / "market.parquet").read_bytes()
    ).hexdigest()
    release["state_partitions"][0]["sha256"] = hashlib.sha256(
        (quantlake / "state" / "state.parquet").read_bytes()
    ).hexdigest()
    release["market_source_sha256"] = _partition_source_sha256(
        release["market_partitions"]
    )
    release["state_source_sha256"] = _partition_source_sha256(
        release["state_partitions"]
    )
    (quantlake / "metadata" / "release.json").write_text(
        json.dumps(release), encoding="utf-8"
    )
    (quantlake / "config" / "fields.csv").write_text(
        "canonical_field,source_field\nclose,adj_close\nvolume,volume\n",
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)],
            "is_open": [True, True, True],
        }
    ).write_parquet(quantlake / "calendar" / "sse_szse.parquet")
    pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 30), date(2026, 7, 31)],
            "open": [4000.0, 4010.0],
        }
    ).write_parquet(quantlake / "benchmark" / "csi300.parquet")
    calendar_manifest = quantlake / "calendar" / "manifest.json"
    calendar_partitions = [
        {
            "path": "sse_szse.parquet",
            "start": "2026-07-29",
            "end": "2026-07-31",
            "sha256": hashlib.sha256(
                (quantlake / "calendar" / "sse_szse.parquet").read_bytes()
            ).hexdigest(),
        }
    ]
    calendar_manifest.write_text(
        json.dumps(
            {
                "version": "pilot-source-manifest-v1",
                "source_sha256": _partition_source_sha256(calendar_partitions),
                "date_column": "trade_date",
                "partitions": calendar_partitions,
            }
        ),
        encoding="utf-8",
    )
    benchmark_manifest = quantlake / "benchmark" / "manifest.json"
    benchmark_partitions = [
        {
            "path": "csi300.parquet",
            "start": "2026-07-30",
            "end": "2026-07-31",
            "sha256": hashlib.sha256(
                (quantlake / "benchmark" / "csi300.parquet").read_bytes()
            ).hexdigest(),
        }
    ]
    benchmark_manifest.write_text(
        json.dumps(
            {
                "version": "pilot-source-manifest-v1",
                "source_sha256": _partition_source_sha256(benchmark_partitions),
                "date_column": "trade_date",
                "partitions": benchmark_partitions,
            }
        ),
        encoding="utf-8",
    )
    return PilotSourcePaths(
        quantlake_root=quantlake,
        release_manifest_uri=quantlake / "metadata" / "release.json",
        state_manifest_uri=quantlake / "metadata" / "state.json",
        market_uri=quantlake / "processed" / "market.parquet",
        state_uri=quantlake / "state" / "state.parquet",
        field_registry_uri=quantlake / "config" / "fields.csv",
        benchmark_root=quantlake / "benchmark",
        calendar_uri=quantlake / "calendar" / "sse_szse.parquet",
        calendar_manifest_uri=calendar_manifest,
        calendar_version="sse-szse-2026-v1",
        benchmark_uri=quantlake / "benchmark" / "csi300.parquet",
        benchmark_manifest_uri=benchmark_manifest,
        benchmark_schema_version="csi300-open-v1",
    )


class PilotSourceContractTest(unittest.TestCase):
    """阶段 A 输入缺陷必须 fail closed。"""

    def test_manifest_partition_scan_never_opens_pre_2021_parquet(self) -> None:
        """普通研究窗口只能打开 manifest 中与 2021 后重叠的分区。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "calendar"
            source.mkdir()
            old = source / "year=2012.parquet"
            current = source / "year=2021.parquet"
            pl.DataFrame(
                {"trade_date": [date(2012, 1, 3)], "is_open": [True]}
            ).write_parquet(old)
            pl.DataFrame(
                {"trade_date": [date(2021, 1, 4)], "is_open": [True]}
            ).write_parquet(current)
            manifest = root / "calendar-manifest.json"
            partitions = [
                {
                    "path": "year=2012.parquet",
                    "start": "2012-01-01",
                    "end": "2012-12-31",
                    "sha256": hashlib.sha256(old.read_bytes()).hexdigest(),
                },
                {
                    "path": "year=2021.parquet",
                    "start": "2021-01-01",
                    "end": "2021-12-31",
                    "sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
                },
            ]
            manifest.write_text(
                json.dumps(
                    {
                        "version": "pilot-source-manifest-v1",
                        "source_sha256": _partition_source_sha256(partitions),
                        "partitions": partitions,
                    }
                ),
                encoding="utf-8",
            )
            opened: list[Path] = []
            streamed: list[Path] = []
            original_scan = pl.scan_parquet
            original_open = Path.open
            original_read_bytes = Path.read_bytes

            def recording_scan(path, *args, **kwargs):
                opened.append(Path(path))
                return original_scan(path, *args, **kwargs)

            def recording_open(path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if path.suffix == ".parquet" and mode == "rb":
                    streamed.append(path.resolve())
                return original_open(path, *args, **kwargs)

            def forbid_parquet_read_bytes(path):
                if path.suffix == ".parquet":
                    raise AssertionError("Parquet SHA256 必须固定块流式读取")
                return original_read_bytes(path)

            with (
                patch("factor_miner.pilot_sources.pl.scan_parquet", side_effect=recording_scan),
                patch.object(Path, "open", recording_open),
                patch.object(Path, "read_bytes", forbid_parquet_read_bytes),
            ):
                result = _scan_manifest_partitions(
                    source,
                    manifest,
                    "calendar",
                    date(2021, 1, 1),
                    date(2026, 6, 30),
                ).collect()

            self.assertEqual(opened, [current.resolve()])
            self.assertEqual(streamed, [current.resolve()])
            self.assertEqual(result["trade_date"].to_list(), [date(2021, 1, 4)])

    def test_selected_partition_byte_tamper_is_rejected(self) -> None:
        """选中分区实际字节与 manifest SHA 不符时必须硬失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "year=2021.parquet"
            pl.DataFrame({"trade_date": [date(2021, 1, 4)]}).write_parquet(current)
            partitions = [
                {
                    "path": current.name,
                    "start": "2021-01-01",
                    "end": "2021-12-31",
                    "sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
                }
            ]
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": "pilot-source-manifest-v1",
                        "source_sha256": _partition_source_sha256(partitions),
                        "partitions": partitions,
                    }
                ),
                encoding="utf-8",
            )
            pl.DataFrame({"trade_date": [date(2021, 1, 5)]}).write_parquet(current)

            with self.assertRaisesRegex(FactorMinerError, "SHA256"):
                _scan_manifest_partitions(
                    current,
                    manifest,
                    "calendar",
                    date(2021, 1, 1),
                    date(2026, 6, 30),
                )

    def test_barra_metadata_identity_precedes_real_source_existence_check(self) -> None:
        """方向前只消费 Barra manifest，真实源存在性留到完整检查阶段。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            barra_root = root / "barra"
            barra_root.mkdir()
            source_specs = [
                {
                    "role": role,
                    "path": filename,
                    "sha256": digit * 64,
                }
                for role, filename, digit in (
                    ("exposures", "exposures.parquet", "1"),
                    ("factor_returns", "factor_returns.parquet", "2"),
                    ("benchmark_weights", "benchmark_weights.parquet", "3"),
                )
            ]
            manifest_path = barra_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": "barra-source-manifest-v1",
                        "sources": source_specs,
                        "input_sha256": sha256_json(
                            sorted(source_specs, key=lambda item: item["role"])
                        ),
                    }
                ),
                encoding="utf-8",
            )
            updated = paths.model_copy(
                update={
                    "barra_root": barra_root,
                    "barra_manifest_uri": manifest_path,
                    "barra_max_exposure_staleness_days": 7,
                    "barra_exposure_uri": barra_root / "exposures.parquet",
                    "barra_factor_returns_uri": barra_root / "factor_returns.parquet",
                    "barra_benchmark_weights_uri": barra_root
                    / "benchmark_weights.parquet",
                }
            )

            planned = inspect_pilot_source_identity(updated)

            self.assertEqual(planned.barra.status, "available")
            with self.assertRaisesRegex(FactorMinerError, "exposures"):
                inspect_pilot_sources(updated)

    def test_derived_source_without_manifest_identity_fails(self) -> None:
        """派生日历或基准缺少发布 manifest 时不得回退到全文件哈希。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "calendar.parquet"
            pl.DataFrame(
                {"trade_date": [date(2021, 1, 4)], "is_open": [True]}
            ).write_parquet(source)
            with self.assertRaisesRegex(FactorMinerError, "manifest"):
                _scan_manifest_partitions(
                    source,
                    None,
                    "calendar",
                    date(2021, 1, 1),
                    date(2026, 6, 30),
                )

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory)).model_copy(
                update={"calendar_manifest_uri": None}
            )
            with self.assertRaisesRegex(FactorMinerError, "manifest"):
                inspect_pilot_sources(paths)

    def test_missing_state_uri_fails(self) -> None:
        """缺状态表不得继续计算因子。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            missing = paths.model_copy(update={"state_uri": None})
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(missing)
            self.assertEqual(context.exception.code, FailureCode.PILOT_INPUT_CONTRACT_INVALID)

    def test_missing_calendar_fails(self) -> None:
        """缺交易日历不得生成周二窗口。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            missing = paths.model_copy(update={"calendar_uri": None})
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(missing)
            self.assertEqual(context.exception.code, FailureCode.CALENDAR_CONTRACT_INVALID)

    def test_missing_csi300_fails_with_stable_code(self) -> None:
        """缺沪深300不得伪造超额收益。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            missing = paths.model_copy(update={"benchmark_uri": None})
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(missing)
            self.assertEqual(
                context.exception.code, FailureCode.BENCHMARK_SOURCE_UNAVAILABLE
            )

    def test_duplicate_csi300_key_fails(self) -> None:
        """基准日主键重复时必须硬失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            pl.DataFrame(
                {
                    "trade_date": [date(2026, 7, 30), date(2026, 7, 30)],
                    "open": [4000.0, 4001.0],
                }
            ).write_parquet(paths.benchmark_uri)
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(paths)
            self.assertEqual(context.exception.code, FailureCode.PILOT_INPUT_CONTRACT_INVALID)

    def test_release_mismatch_fails(self) -> None:
        """行情与状态 release 不一致时必须硬失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            release_path = paths.release_manifest_uri
            payload = json.loads(release_path.read_text(encoding="utf-8"))
            payload["state_release_id"] = "release-2"
            release_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(paths)
            self.assertEqual(context.exception.code, FailureCode.DATA_RELEASE_MISMATCH)

    def test_field_mapping_must_prove_close_and_volume(self) -> None:
        """字段注册表不能证明候选字段映射时必须失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            paths.field_registry_uri.write_text(
                "canonical_field,source_field\nclose,adj_close\n",
                encoding="utf-8",
            )
            with self.assertRaises(FactorMinerError) as context:
                inspect_pilot_sources(paths)
            self.assertEqual(context.exception.code, FailureCode.FIELD_MISSING)

    def test_fixed_candidates_are_exactly_three_and_compile(self) -> None:
        """固定候选文件必须恰好包含三个可编译表达式。"""

        bundle = load_fixed_candidates(FIXTURE)
        self.assertEqual(
            tuple(item.candidate_id for item in bundle.candidates),
            ("pilot_fixed_001", "pilot_fixed_002", "pilot_fixed_003"),
        )
        for item in bundle.candidates:
            plan = compile_candidate(item.spec, allowed_fields=("close", "volume"))
            self.assertEqual(plan.candidate_id, f"cand_{item.spec_hash[:24]}")

    def test_complete_synthetic_inputs_produce_manifest(self) -> None:
        """完整合成输入应产生带身份和 Barra 缺失状态的清单。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            planned = inspect_pilot_source_identity(paths)
            manifest = inspect_pilot_sources(paths)
            self.assertEqual(planned, manifest)
            self.assertEqual(manifest.resolved_release_id, "release-1")
            self.assertEqual(manifest.canonical_field_map["close"], "adj_close")
            self.assertEqual(manifest.universe, "SSE_SZSE_WHOLE_MARKET")
            self.assertEqual(manifest.benchmark.benchmark, "CSI300")
            self.assertEqual(manifest.barra.status, "not_available")

    def test_derived_calendar_requires_explicit_root_and_accepts_datetime_benchmark(self) -> None:
        """派生日历必须显式声明根目录，CSI300 午夜 Datetime 可规范化为日期。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            derived = root / "derived_calendar"
            derived.mkdir()
            calendar_path = derived / "calendar.parquet"
            pl.DataFrame(
                {
                    "trade_date": [date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)],
                    "is_open": [True, True, True],
                }
            ).write_parquet(calendar_path)
            calendar_manifest = derived / "manifest.json"
            calendar_partitions = [
                {
                    "path": "calendar.parquet",
                    "start": "2026-07-29",
                    "end": "2026-07-31",
                    "sha256": hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
                }
            ]
            calendar_manifest.write_text(
                json.dumps(
                    {
                        "version": "pilot-source-manifest-v1",
                        "source_sha256": _partition_source_sha256(calendar_partitions),
                        "date_column": "trade_date",
                        "partitions": calendar_partitions,
                    }
                ),
                encoding="utf-8",
            )
            benchmark_path = paths.benchmark_uri
            assert benchmark_path is not None
            pl.DataFrame(
                {
                    "date": [datetime(2026, 7, 30), datetime(2026, 7, 31)],
                    "open": [4000.0, 4010.0],
                }
            ).write_parquet(benchmark_path)
            assert paths.benchmark_manifest_uri is not None
            benchmark_manifest_payload = json.loads(
                paths.benchmark_manifest_uri.read_text("utf-8")
            )
            benchmark_manifest_payload["date_column"] = "date"
            benchmark_manifest_payload["partitions"][0]["sha256"] = hashlib.sha256(
                benchmark_path.read_bytes()
            ).hexdigest()
            benchmark_manifest_payload["source_sha256"] = _partition_source_sha256(
                benchmark_manifest_payload["partitions"]
            )
            paths.benchmark_manifest_uri.write_text(
                json.dumps(benchmark_manifest_payload),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                PilotSourcePaths.model_validate(
                    {
                        **paths.model_dump(mode="python"),
                        "calendar_uri": calendar_path,
                    }
                )
            updated = paths.model_copy(
                update={
                    "calendar_root": derived,
                    "calendar_uri": calendar_path,
                    "calendar_manifest_uri": calendar_manifest,
                }
            )
            manifest = inspect_pilot_sources(updated)
            self.assertEqual(manifest.calendar.calendar_version, "sse-szse-2026-v1")
            self.assertEqual(manifest.benchmark.cutoff, date(2026, 7, 31))

    def test_legacy_csi300_inputs_are_not_read_or_recorded(self) -> None:
        """目标多头全市场输入不会读取旧 CSI300 成分或成分股开盘价。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            derived = root / "legacy_csi300"
            universe_path = derived / "components_daily.parquet"
            market_open_path = derived / "stock_daily"
            updated = paths.model_copy(
                update={
                    "universe_root": derived,
                    "universe_uri": universe_path,
                    "market_open_root": derived,
                    "market_open_uri": market_open_path,
                }
            )
            manifest = inspect_pilot_sources(updated)
            self.assertEqual(manifest.universe, "SSE_SZSE_WHOLE_MARKET")
            self.assertIsNone(manifest.universe_uri)
            self.assertIsNone(manifest.universe_sha256)
            self.assertIsNone(manifest.market_open_uri)
            self.assertIsNone(manifest.market_open_sha256)

    def test_barra_inputs_require_explicit_derived_root(self) -> None:
        """Barra 三类输入只能位于显式派生根，不能借 QuantLake 根越界。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = _write_pilot_inputs(root)
            derived = root / "factor_miner_derived"
            derived.mkdir()
            exposure = derived / "exposures.parquet"
            returns = derived / "factor_returns.parquet"
            weights = derived / "csi300_weights.parquet"
            for path in (exposure, returns, weights):
                pl.DataFrame({"placeholder": [1]}).write_parquet(path)

            with self.assertRaises(ValueError):
                PilotSourcePaths.model_validate(
                    {
                        **paths.model_dump(mode="python"),
                        "barra_exposure_uri": exposure,
                        "barra_factor_returns_uri": returns,
                        "barra_benchmark_weights_uri": weights,
                    }
                )

            updated = PilotSourcePaths.model_validate(
                {
                    **paths.model_dump(mode="python"),
                    "barra_root": derived,
                    "barra_manifest_uri": derived / "manifest.json",
                    "barra_max_exposure_staleness_days": 7,
                    "barra_exposure_uri": exposure,
                    "barra_factor_returns_uri": returns,
                    "barra_benchmark_weights_uri": weights,
                }
            )
            self.assertEqual(updated.barra_root, derived)


if __name__ == "__main__":
    unittest.main()
