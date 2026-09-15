"""阶段 A 固定候选因子面板合成测试。"""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import polars as pl

from factor_miner import pilot_sources
from factor_miner.canonical import sha256_json
from factor_miner.data_source import FactorInputRequest, InputProvenance
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.pilot_sources import (
    PilotQuantLakeFactorInputSource,
    load_fixed_candidates,
)
from factor_miner.pilot_runner import (
    FixedSignalPanel,
    build_benchmark_open_to_open_returns,
    build_open_to_open_ic_panel,
    compute_fixed_signal_panels,
    evaluate_fixed_candidate_barra,
    evaluate_fixed_candidate_ic,
    evaluate_fixed_candidate_portfolio,
    evaluate_long_only_windows,
    run_fixed_pilot,
    _load_pilot_benchmark,
    _load_pilot_calendar,
    _load_pilot_market_open,
)
from factor_miner.pilot_schema import PilotRunRequest
from factor_miner.ic_diagnostics import evaluate_ic_horizons
from factor_miner.policy import company_a_share_visible_policy
from factor_miner.schema import evaluation_policy_id
from factor_miner.portfolio_schema import PortfolioEvaluationPolicy, TradingCalendarIdentity
from factor_miner.trading_schedule import RebalanceWindow, build_tuesday_rebalance_schedule
from tests.test_barra_attribution import (
    benchmark_weights,
    exposures,
    factor_returns,
    weights,
)
from tests.test_barra_schema import identity, policy
from tests.test_pilot_sources import _partition_source_sha256, _write_pilot_inputs


FIXTURE = Path(__file__).parent / "fixtures" / "pilot" / "fixed_candidates.json"


class SyntheticFactorInputSource:
    """只提供程序生成的 input-only 合成面板。"""

    allowed_fields = ("close", "volume")

    def __init__(self, frame: pl.DataFrame, quantlake_root: Path) -> None:
        self._frame = frame
        self.quantlake_root = quantlake_root

    def inspect_inputs(self) -> InputProvenance:
        """返回合成输入身份。"""

        return InputProvenance(
            data_origin="synthetic",
            resolved_release_id="synthetic-pilot-v1",
            release_manifest_sha256="a" * 64,
            schema_version="synthetic-v1",
            market_cutoff="2026-03-31",
            adjustment_convention="synthetic_adjusted",
            calendar_version="synthetic-calendar-v1",
            state_table_version="synthetic-state-v1",
            state_table_cutoff="2026-03-31",
            code_commit="b" * 40,
            config_hash="c" * 64,
        )

    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
        """按请求返回 input-only 数据。"""

        self._frame = self._frame.filter(
            pl.col("date").is_between(request.start - timedelta(days=30), request.end)
        )
        return self._frame.lazy()


def _synthetic_frame() -> pl.DataFrame:
    """构造有序输入、足够 lookback 和一个无效状态点。"""

    start = date(2026, 1, 1)
    rows: list[dict[str, object]] = []
    for day_index in range(65):
        current = start + timedelta(days=day_index)
        for asset_index, asset in enumerate(("A", "B", "C")):
            rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "close": float(100 + day_index * (asset_index + 1) + asset_index),
                    "volume": float(1000 + 10 * day_index + asset_index),
                    "open": float(100 + day_index * (asset_index + 1) + asset_index),
                    "valid_for_factor_compute": not (
                        asset == "B" and day_index == 45
                    ),
                    "valid_for_factor_rank": not (
                        asset == "B" and day_index == 45
                    ),
                    "valid_for_trading": not (
                        asset == "B" and day_index == 45
                    ),
                    "can_open_long": not (
                        asset == "B" and day_index == 45
                    ),
                    "can_close_long": not (
                        asset == "B" and day_index == 45
                    ),
                }
            )
    return pl.DataFrame(rows)


class PilotRunnerTest(unittest.TestCase):
    """固定候选必须按统一 input-only 协议生成信号面板。"""

    def test_fixed_candidates_compute_to_signal_panels(self) -> None:
        """三个候选均应保留 lookback、应用 mask 且主键唯一。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake_root = root / "quantlake"
            quantlake_root.mkdir()
            output_root = root / "artifacts"
            source = SyntheticFactorInputSource(_synthetic_frame(), quantlake_root)
            bundle = load_fixed_candidates(FIXTURE)
            request = FactorInputRequest(
                start=date(2026, 2, 15),
                end=date(2026, 3, 1),
                required_fields=("close", "volume"),
                warmup_observations=20,
            )

            panels = compute_fixed_signal_panels(bundle, source, request, output_root)

            self.assertEqual(tuple(panel.candidate_id for panel in panels), (
                "pilot_fixed_001",
                "pilot_fixed_002",
                "pilot_fixed_003",
            ))
            for panel in panels:
                self.assertEqual(
                    panel.frame.select(["signal_date", "security_id"]).unique().height,
                    panel.frame.height,
                )
                self.assertEqual(panel.frame.columns, ["signal_date", "security_id", "factor_value"])
                self.assertGreater(panel.frame.filter(pl.col("factor_value").is_not_null()).height, 0)
                self.assertTrue(
                    panel.frame.filter(
                        (pl.col("signal_date") == date(2026, 2, 15))
                        & (pl.col("security_id") == "B")
                    ).select(pl.col("factor_value").is_null()).item()
                )

    def test_tuesday_schedule_uses_prior_open_signal_and_rolls_holiday(self) -> None:
        """周二休市时顺延，信号仍取入场日前一开放日。"""

        calendar = pl.DataFrame(
            {
                "trade_date": [
                    date(2026, 1, 5),
                    date(2026, 1, 6),
                    date(2026, 1, 12),
                    date(2026, 1, 14),
                    date(2026, 1, 19),
                ],
                "is_open": [True, False, True, True, True],
            }
        )
        schedule = build_tuesday_rebalance_schedule(
            calendar,
            visible_start=date(2026, 1, 1),
            visible_end=date(2026, 1, 19),
        )
        self.assertEqual(schedule[0].entry_date, date(2026, 1, 12))
        self.assertEqual(schedule[0].signal_date, date(2026, 1, 5))

    def test_quantlake_source_maps_adjusted_market_to_canonical_input(self) -> None:
        """QuantLake 的 code/adj_* 字段必须显式映射为 canonical input。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            source = PilotQuantLakeFactorInputSource(
                paths,
                code_commit="a" * 40,
                config_hash="b" * 64,
            )
            source.inspect_inputs()
            frame = source.scan_inputs(
                FactorInputRequest(
                    start=date(2026, 7, 31),
                    end=date(2026, 7, 31),
                    required_fields=("close", "volume"),
                    warmup_observations=1,
                )
            ).collect()
            self.assertEqual(frame.columns, [
                "date",
                "asset",
                "close",
                "volume",
                "valid_for_factor_compute",
                "valid_for_factor_rank",
                "valid_for_trading",
            ])
            visible = frame.filter(pl.col("date") == date(2026, 7, 31))
            self.assertEqual(
                visible.get_column("asset").unique().to_list(),
                ["600000.XSHG"],
            )
            self.assertEqual(visible.get_column("close").to_list(), [10.7])

    def test_ordinary_loaders_filter_out_2012_before_collecting(self) -> None:
        """普通日历、个股行情与基准读取必须只返回 2021 起研究区间。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar_path = root / "calendar.parquet"
            market_path = root / "market.parquet"
            benchmark_path = root / "benchmark.parquet"
            release_manifest = root / "release.json"
            calendar_manifest = root / "calendar-manifest.json"
            benchmark_manifest = root / "benchmark-manifest.json"
            dates = [date(2012, 1, 3), date(2021, 1, 4), date(2026, 6, 30)]
            pl.DataFrame(
                {"trade_date": dates, "is_open": [True, True, True]}
            ).write_parquet(calendar_path)
            pl.DataFrame(
                {
                    "date": dates,
                    "code": ["A.XSHG"] * 3,
                    "adj_open": [1.0, 2.0, 3.0],
                }
            ).write_parquet(market_path)
            pl.DataFrame(
                {"trade_date": dates, "open": [100.0, 200.0, 300.0]}
            ).write_parquet(benchmark_path)
            market_partitions = [
                {
                    "path": "market.parquet",
                    "start": "2012-01-03",
                    "end": "2026-06-30",
                    "sha256": hashlib.sha256(market_path.read_bytes()).hexdigest(),
                }
            ]
            release_manifest.write_text(
                json.dumps(
                    {
                        "version": "quantlake-release-v1",
                        "source_sha256": "a" * 64,
                        "market_source_sha256": _partition_source_sha256(market_partitions),
                        "market_partitions": market_partitions,
                    }
                ),
                encoding="utf-8",
            )
            for manifest_path, source_name, source_sha in (
                (calendar_manifest, "calendar.parquet", "c" * 64),
                (benchmark_manifest, "benchmark.parquet", "d" * 64),
            ):
                source_path = root / source_name
                partitions = [
                    {
                        "path": source_name,
                        "start": "2012-01-03",
                        "end": "2026-06-30",
                        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    }
                ]
                manifest_path.write_text(
                    json.dumps(
                        {
                            "version": "pilot-source-manifest-v1",
                            "source_sha256": _partition_source_sha256(partitions),
                            "date_column": "trade_date",
                            "partitions": partitions,
                        }
                    ),
                    encoding="utf-8",
                )
            paths = SimpleNamespace(
                calendar_uri=calendar_path,
                calendar_manifest_uri=calendar_manifest,
                market_open_uri=None,
                market_uri=market_path,
                release_manifest_uri=release_manifest,
                benchmark_uri=benchmark_path,
                benchmark_manifest_uri=benchmark_manifest,
            )

            calendar = _load_pilot_calendar(
                paths, date(2021, 1, 1), date(2026, 6, 30)
            )
            market = _load_pilot_market_open(
                paths, date(2021, 1, 1), date(2026, 6, 30)
            ).collect()
            benchmark = _load_pilot_benchmark(
                paths, date(2021, 1, 1), date(2026, 6, 30)
            ).collect()

            self.assertGreaterEqual(calendar["trade_date"].min(), date(2021, 1, 1))
            self.assertGreaterEqual(market["trade_date"].min(), date(2021, 1, 1))
            self.assertGreaterEqual(benchmark["trade_date"].min(), date(2021, 1, 1))

    def test_ic_labels_use_only_future_open_to_open_prices(self) -> None:
        """标签从入场后开盘构造，并包含冻结五个期限。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake_root = root / "quantlake"
            quantlake_root.mkdir()
            source = SyntheticFactorInputSource(_synthetic_frame(), quantlake_root)
            bundle = load_fixed_candidates(FIXTURE)
            request = FactorInputRequest(
                start=date(2026, 2, 1),
                end=date(2026, 2, 10),
                required_fields=("close", "volume"),
                warmup_observations=20,
            )
            panel = compute_fixed_signal_panels(
                bundle,
                source,
                request,
                root / "artifacts",
            )[0]
            market = (
                _synthetic_frame()
                .select(
                    [
                        pl.col("date").alias("trade_date"),
                        pl.col("asset").alias("security_id"),
                        "open",
                    ]
                )
                .lazy()
            )
            calendar = pl.DataFrame(
                {
                    "trade_date": [
                        date(2026, 1, 1) + timedelta(days=index)
                        for index in range(65)
                    ],
                    "is_open": [True] * 65,
                }
            )
            ic_input = build_open_to_open_ic_panel(panel, market, calendar).collect()
            first = ic_input.filter(
                (pl.col("date") == date(2026, 2, 1))
                & (pl.col("asset") == "A")
            ).row(0, named=True)
            self.assertAlmostEqual(first["forward_return_1"], 1.0 / 132.0)
            self.assertIn("forward_return_20", ic_input.columns)
            policy = company_a_share_visible_policy().model_copy(
                update={"min_valid_dates": 2, "min_names_per_date": 2}
            )
            diagnostics = evaluate_fixed_candidate_ic(panel, market, calendar, policy=policy)
            self.assertEqual(diagnostics.horizons, (1, 3, 5, 10, 20))
            self.assertGreaterEqual(len(diagnostics.daily), 2)

    def test_discovery_label_build_requires_only_five_day_horizon(self) -> None:
        """方向发现不能因 1/3/10/20 日标签缺失而失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake_root = root / "quantlake"
            quantlake_root.mkdir()
            source = SyntheticFactorInputSource(_synthetic_frame(), quantlake_root)
            panel = compute_fixed_signal_panels(
                load_fixed_candidates(FIXTURE),
                source,
                FactorInputRequest(
                    start=date(2026, 2, 1),
                    end=date(2026, 2, 10),
                    required_fields=("close", "volume"),
                    warmup_observations=20,
                ),
                root / "artifacts",
            )[0]
            market = _synthetic_frame().select(
                [
                    pl.col("date").alias("trade_date"),
                    pl.col("asset").alias("security_id"),
                    "open",
                ]
            ).lazy()
            calendar = pl.DataFrame(
                {
                    "trade_date": [
                        date(2026, 1, 1) + timedelta(days=index)
                        for index in range(65)
                    ],
                    "is_open": [True] * 65,
                }
            )

            discovery = build_open_to_open_ic_panel(
                panel,
                market,
                calendar,
                horizons=(5,),
            ).collect()

            self.assertIn("forward_return_5", discovery.columns)
            for horizon in (1, 3, 10, 20):
                self.assertNotIn(f"forward_return_{horizon}", discovery.columns)

    def test_duplicate_market_key_fails_before_label_generation(self) -> None:
        """未来开盘主键重复时不能静默选择一条记录。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quantlake_root = root / "quantlake"
            quantlake_root.mkdir()
            source = SyntheticFactorInputSource(_synthetic_frame(), quantlake_root)
            panel = compute_fixed_signal_panels(
                load_fixed_candidates(FIXTURE),
                source,
                FactorInputRequest(
                    start=date(2026, 2, 1),
                    end=date(2026, 2, 10),
                    required_fields=("close", "volume"),
                    warmup_observations=20,
                ),
                root / "artifacts",
            )[0]
            duplicate = _synthetic_frame().select(
                [
                    pl.col("date").alias("trade_date"),
                    pl.col("asset").alias("security_id"),
                    "open",
                ]
            ).vstack(
                _synthetic_frame().head(1).select(
                    [
                        pl.col("date").alias("trade_date"),
                        pl.col("asset").alias("security_id"),
                        "open",
                    ]
                )
            ).lazy()
            calendar = pl.DataFrame(
                {
                    "trade_date": [
                        date(2026, 1, 1) + timedelta(days=index)
                        for index in range(65)
                    ],
                    "is_open": [True] * 65,
                }
            )
            with self.assertRaises(FactorMinerError) as context:
                build_open_to_open_ic_panel(panel, duplicate, calendar).collect()
            self.assertEqual(context.exception.code, FailureCode.PILOT_INPUT_CONTRACT_INVALID)

    def test_portfolio_publishes_only_target_long_and_net_cost(self) -> None:
        """Pilot 的公开组合只能含目标多头、基准和首期 14bp 成本。"""

        schedule = (
            RebalanceWindow(
                signal_date=date(2026, 1, 5),
                entry_date=date(2026, 1, 6),
                exit_date=date(2026, 1, 13),
            ),
            RebalanceWindow(
                signal_date=date(2026, 1, 12),
                entry_date=date(2026, 1, 13),
                exit_date=date(2026, 1, 20),
            ),
            RebalanceWindow(
                signal_date=date(2026, 1, 19),
                entry_date=date(2026, 1, 20),
                exit_date=date(2026, 1, 27),
            ),
        )
        assets = [f"A{index:02d}" for index in range(20)]
        signal_rows = [
            {
                "signal_date": window.signal_date,
                "security_id": asset,
                "factor_value": float(index),
            }
            for window in schedule
            for index, asset in enumerate(assets)
        ]
        signal = pl.DataFrame(signal_rows)
        masks = pl.DataFrame(
            {
                "signal_date": [row["signal_date"] for row in signal_rows],
                "security_id": [row["security_id"] for row in signal_rows],
                "valid_for_factor_rank": [True] * len(signal_rows),
            }
        )
        execution_dates = sorted({value for window in schedule for value in (window.signal_date, window.entry_date, window.exit_date)})
        execution_state = pl.DataFrame([
            {
                "signal_date": current,
                "security_id": asset,
                "valid_for_factor_rank": True,
                "can_open_long": True,
                "can_close_long": True,
            }
            for current in execution_dates
            for asset in assets
        ])
        panel = FixedSignalPanel(
            candidate_id="pilot_fixed_001",
            compiler_candidate_id="cand_" + "a" * 24,
            ast_hash="a" * 64,
            plan_hash="b" * 64,
            frame=signal,
            rank_mask=masks.select(
                ["signal_date", "security_id", "valid_for_factor_rank"]
            ),
            trading_mask=execution_state.select(
                [
                    "signal_date", "security_id", "valid_for_factor_rank",
                    "can_open_long", "can_close_long",
                ]
            ),
        )
        market_rows = []
        for day_index, current in enumerate(execution_dates):
            for index, asset in enumerate(assets):
                market_rows.append(
                    {
                        "trade_date": current,
                        "security_id": asset,
                        "open": float(1000 + index + day_index),
                    }
                )
        market = pl.DataFrame(market_rows).lazy()
        index_panel = pl.DataFrame(
            {
                "trade_date": execution_dates,
                "open": [3000.0 + 10.0 * index for index in range(len(execution_dates))],
            }
        ).lazy()
        benchmark = build_benchmark_open_to_open_returns(index_panel, schedule)
        calendar = pl.DataFrame(
            {
                "trade_date": [
                    date(2026, 1, 5) + timedelta(days=index)
                    for index in range(30)
                ],
                "is_open": [True] * 30,
            }
        )
        result = evaluate_fixed_candidate_portfolio(
            panel,
            market,
            benchmark,
            schedule,
            calendar,
            calendar_identity=TradingCalendarIdentity(
                calendar_version="synthetic-calendar-v1",
                calendar_sha256="d" * 64,
            ),
            policy=PortfolioEvaluationPolicy(),
        )
        first_row = result.backtest.daily_returns[0]
        self.assertIn("target_long_net_return", first_row)
        self.assertIn("benchmark_return", first_row)
        self.assertFalse(any(key.startswith("Q") for key in first_row))
        self.assertLessEqual(
            first_row["target_long_net_return"], first_row["target_long_gross_return"]
        )
        self.assertEqual(
            set(result.metrics.series),
            {"target_long_gross_return", "target_long_net_return", "CSI300"},
        )
        self.assertEqual(result.metrics.benchmark, "CSI300")

    def test_direction_uses_discovery_only_before_confirmation_is_evaluated(self) -> None:
        """确认期反向结果不得参与方向选择，且读取确认期前必须已冻结方向记录。"""

        rows = []
        for current, sign in (
            (date(2021, 1, 4), 1.0),
            (date(2024, 1, 2), -1.0),
            (date(2025, 1, 2), -1.0),
        ):
            for index in range(20):
                row = {
                    "date": current,
                    "asset": f"A{index:02d}",
                    "factor_value": float(index),
                    "valid_for_factor_rank": True,
                    "label_exit_date": current + timedelta(days=5),
                }
                row.update(
                    {
                        f"forward_return_{horizon}": sign * float(index)
                        for horizon in (1, 3, 5, 10, 20)
                    }
                )
                rows.append(row)
        panel = pl.DataFrame(rows).lazy()
        policy = company_a_share_visible_policy().model_copy(
            update={"min_valid_dates": 1, "min_names_per_date": 20}
        )
        with tempfile.TemporaryDirectory() as directory:
            direction_path = Path(directory) / "direction.json"
            evaluated_windows: list[date] = []

            def evaluator(frame, horizons, frozen_policy):
                window_start = frame.select(pl.col("date").min()).collect().item()
                evaluated_windows.append(window_start)
                if window_start >= date(2024, 1, 1):
                    self.assertTrue(direction_path.is_file())
                return evaluate_ic_horizons(frame, horizons, frozen_policy)

            result = evaluate_long_only_windows(
                panel,
                hypothesis_direction="positive",
                candidate_id="synthetic_candidate",
                spec_sha256="a" * 64,
                record_provenance={
                    "data_release_id": "synthetic-release",
                    "input_manifest_sha256": "b" * 64,
                    "evaluation_policy_id": evaluation_policy_id(policy),
                    "family_size": 1,
                    "code_commit": "c" * 40,
                    "config_hash": "d" * 64,
                    "source_run_id": None,
                },
                evaluation_policy=policy,
                direction_record_path=direction_path,
                diagnostics_evaluator=evaluator,
            )

            self.assertTrue(direction_path.is_file())
            self.assertGreater(result.direction.discovery_rank_ic_mean, 0.0)
            self.assertEqual(result.direction.selected_direction, "positive")
            self.assertLess(result.confirmation.rank_ic_mean, 0.0)
            self.assertEqual(result.direction.hypothesis_relation, "supported")
            self.assertFalse(result.stress_test_eligible)
            self.assertEqual(
                evaluated_windows,
                [date(2024, 1, 2), date(2025, 1, 2)],
            )

    def test_production_confirmation_scan_starts_after_all_atomic_direction_records(self) -> None:
        """生产源的确认扫描入口不得早于全部候选方向文件落盘并核验。"""

        candidates = load_fixed_candidates(FIXTURE)
        sessions = [date(2021, 1, 4) + timedelta(days=index) for index in range(8)]
        calendar = pl.DataFrame(
            {"trade_date": sessions, "is_open": [True] * len(sessions)}
        )
        market = pl.DataFrame(
            [
                {
                    "trade_date": current,
                    "security_id": f"A{asset:02d}",
                    "open": 100.0 + day_index * asset,
                }
                for day_index, current in enumerate(sessions)
                for asset in range(20)
            ]
        ).lazy()
        signal = pl.DataFrame(
            {
                "signal_date": [sessions[0]] * 20,
                "security_id": [f"A{asset:02d}" for asset in range(20)],
                "factor_value": [float(asset) for asset in range(20)],
            }
        )
        mask = signal.select(["signal_date", "security_id"]).with_columns(
            pl.lit(True).alias("valid_for_factor_rank")
        )
        trading = signal.select(["signal_date", "security_id"]).with_columns(
            pl.lit(True).alias("valid_for_factor_rank"),
            pl.lit(True).alias("can_open_long"),
            pl.lit(True).alias("can_close_long"),
        )
        panels = tuple(
            FixedSignalPanel(
                candidate_id=item.candidate_id,
                compiler_candidate_id=item.candidate_id,
                ast_hash="a" * 64,
                plan_hash="b" * 64,
                frame=signal,
                rank_mask=mask,
                trading_mask=trading,
            )
            for item in candidates.candidates
        )
        provenance = InputProvenance(
            data_origin="synthetic",
            resolved_release_id="release-1",
            release_manifest_sha256="c" * 64,
            schema_version="synthetic-v1",
            market_cutoff="2026-07-31",
            adjustment_convention="synthetic_adjusted",
            calendar_version="calendar-v1",
            state_table_version="state-v1",
            state_table_cutoff="2026-07-31",
            code_commit="d" * 40,
            config_hash="e" * 64,
        )

        class ConfirmationReached(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = PilotRunRequest(
                visible_start=date(2021, 1, 1),
                visible_end=date(2026, 6, 30),
                candidate_file_sha256="f" * 64,
                evaluation_policy_id=evaluation_policy_id(
                    company_a_share_visible_policy().model_copy(
                        update={"min_valid_dates": 1, "min_names_per_date": 20}
                    )
                ),
                data_release_id="release-1",
                code_commit="d" * 40,
                config_hash="e" * 64,
                artifact_root=root,
            )
            policy = company_a_share_visible_policy().model_copy(
                update={"min_valid_dates": 1, "min_names_per_date": 20}
            )

            def confirmation_scan(_paths):
                records = tuple((root / "state" / "pilot_direction").glob("*.json"))
                self.assertEqual(len(records), len(candidates.candidates))
                self.assertTrue(all(path.read_bytes() for path in records))
                self.assertEqual(barra_opened, [])
                self.assertEqual(barra_hashed, [])
                raise ConfirmationReached

            fake_paths = _write_pilot_inputs(root)
            barra_root = root / "barra"
            barra_root.mkdir()
            barra_sources = tuple(
                barra_root / name
                for name in (
                    "exposures.parquet",
                    "factor_returns.parquet",
                    "benchmark_weights.parquet",
                )
            )
            for path in barra_sources:
                path.write_bytes(b"published-barra-source")
            barra_entries = [
                {
                    "role": role,
                    "path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for role, path in zip(
                    ("exposures", "factor_returns", "benchmark_weights"),
                    barra_sources,
                    strict=True,
                )
            ]
            barra_manifest = barra_root / "manifest.json"
            barra_manifest.write_text(
                json.dumps(
                    {
                        "version": "barra-source-manifest-v1",
                        "sources": barra_entries,
                        "input_sha256": sha256_json(
                            sorted(
                                barra_entries,
                                key=lambda item: item["role"],
                            )
                        ),
                    }
                ),
                encoding="utf-8",
            )
            fake_paths = fake_paths.model_copy(
                update={
                    "barra_root": barra_root,
                    "barra_manifest_uri": barra_manifest,
                    "barra_max_exposure_staleness_days": 7,
                    "barra_exposure_uri": barra_sources[0],
                    "barra_factor_returns_uri": barra_sources[1],
                    "barra_benchmark_weights_uri": barra_sources[2],
                }
            )
            barra_opened: list[Path] = []
            barra_hashed: list[Path] = []
            source_set = {path.resolve() for path in barra_sources}
            original_open = Path.open

            def recording_open(path, *args, **kwargs):
                if path.resolve() in source_set:
                    barra_opened.append(path.resolve())
                return original_open(path, *args, **kwargs)

            original_hash = pilot_sources._file_or_tree_sha256

            def recording_hash(path):
                if path.resolve() in source_set:
                    barra_hashed.append(path.resolve())
                return original_hash(path)

            with (
                patch.object(
                    PilotQuantLakeFactorInputSource,
                    "inspect_inputs",
                    return_value=provenance,
                ),
                patch(
                    "factor_miner.pilot_runner.compute_fixed_signal_panels",
                    return_value=panels,
                ),
                patch(
                    "factor_miner.pilot_runner._load_pilot_calendar",
                    return_value=calendar,
                ),
                patch(
                    "factor_miner.pilot_runner._load_pilot_market_open",
                    return_value=market,
                ),
                patch(
                    "factor_miner.pilot_runner.inspect_pilot_sources",
                    side_effect=confirmation_scan,
                ),
                patch.object(Path, "open", recording_open),
                patch(
                    "factor_miner.pilot_sources._file_or_tree_sha256",
                    side_effect=recording_hash,
                ),
            ):
                with self.assertRaises(ConfirmationReached):
                    run_fixed_pilot(
                        candidates=candidates,
                        paths=fake_paths,
                        request=request,
                        evaluation_policy=policy,
                    )

    def test_barra_complete_inputs_are_available_and_reconciled(self) -> None:
        """完整 Barra 输入必须调用现有归因器并返回对账结果。"""

        result = evaluate_fixed_candidate_barra(
            weights(),
            benchmark_weights(),
            exposures(),
            factor_returns(),
            policy(),
            identity=identity(),
        )
        self.assertEqual(result.availability.status, "available")
        self.assertIsNotNone(result.attribution)
        self.assertEqual(result.attribution.risk_decomposition_status, "realized_attribution_only")
        self.assertEqual(result.attribution.max_abs_reconciliation_error, 0.0)
        self.assertEqual(result.identity, identity())

    def test_barra_missing_inputs_are_not_pilot_failures(self) -> None:
        """缺行业、基准权重或因子收益时必须具体记录 not_available。"""

        missing_industry = evaluate_fixed_candidate_barra(
            weights(),
            benchmark_weights(),
            exposures().select(["signal_date", "security_id", "Size"]),
            factor_returns(),
            policy(),
            identity=identity(),
        )
        self.assertEqual(missing_industry.availability.status, "not_available")
        self.assertIn("industry_exposures", missing_industry.availability.missing_inputs)

        missing_benchmark = evaluate_fixed_candidate_barra(
            weights(),
            None,
            exposures(),
            factor_returns(),
            policy(),
            identity=identity(),
        )
        self.assertEqual(missing_benchmark.availability.status, "not_available")
        self.assertIn("benchmark_weights", missing_benchmark.availability.missing_inputs)

        missing_returns = evaluate_fixed_candidate_barra(
            weights(),
            benchmark_weights(),
            exposures(),
            factor_returns().filter(pl.col("factor") == "Size"),
            policy(),
            identity=identity(),
        )
        self.assertEqual(missing_returns.availability.status, "not_available")
        self.assertIn("factor_returns:industry_bank", missing_returns.availability.missing_inputs)

    def test_barra_identity_mismatch_is_not_a_pilot_failure(self) -> None:
        """Barra 身份漂移必须降级为不可用并保留具体原因。"""

        changed = identity().model_copy(update={"factor_return_version": "other"})
        result = evaluate_fixed_candidate_barra(
            weights(),
            benchmark_weights(),
            exposures(),
            factor_returns(),
            policy(),
            identity=changed,
        )
        self.assertEqual(result.availability.status, "not_available")
        self.assertIn("barra_input_identity", result.availability.missing_inputs)


if __name__ == "__main__":
    unittest.main()
