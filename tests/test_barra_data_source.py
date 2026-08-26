"""服务器派生 Barra Parquet 到归因端口的合成合同测试。"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.barra_data_source import (
    build_barra_portfolio_weights,
    load_derived_barra_inputs,
)
from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError
from factor_miner.pilot_schema import PilotSourcePaths
from factor_miner.pilot_runner import evaluate_fixed_backtest_barra
from factor_miner.pilot_sources import inspect_pilot_sources
from factor_miner.portfolio_evaluation import (
    ExtremeSpreadDiagnostic,
    PortfolioBacktestResult,
    PortfolioGovernanceSummary,
)
from factor_miner.trading_schedule import RebalanceWindow
from tests.test_barra_schema import policy
from tests.test_pilot_sources import _write_pilot_inputs


def _write_barra_inputs(root: Path) -> PilotSourcePaths:
    """写入两期不规则快照和日频收益，验证只向过去对齐。"""

    paths = _write_pilot_inputs(root)
    derived = root / "factor_miner_derived"
    derived.mkdir()
    exposure = derived / "exposures.parquet"
    returns = derived / "factor_returns.parquet"
    weights = derived / "csi300_weights.parquet"
    pl.DataFrame(
        {
            "date": [
                date(2026, 6, 30), date(2026, 6, 30),
                date(2026, 7, 8), date(2026, 7, 8),
            ],
            "security_id": ["A", "B", "A", "B"],
            "industry_bank": [1.0, 0.0, 1.0, 0.0],
            "Size": [2.0, 1.0, 2.1, 1.1],
        }
    ).write_parquet(exposure)
    pl.DataFrame(
        {
            "date": [
                date(2026, 7, 2), date(2026, 7, 3),
                date(2026, 7, 2), date(2026, 7, 3),
                date(2026, 7, 9), date(2026, 7, 10),
                date(2026, 7, 9), date(2026, 7, 10),
            ],
            "factor": [
                "industry_bank", "industry_bank", "Size", "Size",
                "industry_bank", "industry_bank", "Size", "Size",
            ],
            "factor_return": [0.01, 0.02, 0.03, -0.01, 0.01, 0.0, 0.02, 0.01],
        }
    ).write_parquet(returns)
    pl.DataFrame(
        {
            "date": [
                date(2026, 6, 29), date(2026, 6, 29),
                date(2026, 7, 7), date(2026, 7, 7),
            ],
            "security_id": ["A", "B", "A", "B"],
            "weight": [0.6, 0.4, 0.55, 0.45],
        }
    ).write_parquet(weights)
    sources = [
        {
            "role": role,
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for role, path in (
            ("exposures", exposure),
            ("factor_returns", returns),
            ("benchmark_weights", weights),
        )
    ]
    manifest = derived / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "barra-source-manifest-v1",
                "sources": sources,
                "input_sha256": sha256_json(
                    sorted(sources, key=lambda item: item["role"])
                ),
            }
        ),
        encoding="utf-8",
    )
    return paths.model_copy(
        update={
            "barra_root": derived,
            "barra_manifest_uri": manifest,
            "barra_max_exposure_staleness_days": 7,
            "barra_exposure_uri": exposure,
            "barra_factor_returns_uri": returns,
            "barra_benchmark_weights_uri": weights,
        }
    )


SCHEDULE = (
    RebalanceWindow(
        signal_date=date(2026, 7, 1),
        entry_date=date(2026, 7, 2),
        exit_date=date(2026, 7, 6),
    ),
    RebalanceWindow(
        signal_date=date(2026, 7, 8),
        entry_date=date(2026, 7, 9),
        exit_date=date(2026, 7, 13),
    ),
)


def governance() -> PortfolioGovernanceSummary:
    """构造不公开极端组差的最小治理摘要。"""

    return PortfolioGovernanceSummary.build(
        (
            ExtremeSpreadDiagnostic(
                entry_date=date(2026, 7, 2),
                exit_date=date(2026, 7, 6),
                extreme_spread_net_return=0.01,
            ),
        )
    )


class BarraDerivedDataSourceTest(unittest.TestCase):
    """派生数据必须遵守观察时点和归因字段合同。"""

    def test_whole_market_mode_keeps_non_benchmark_exposure_snapshots(self) -> None:
        """全市场模式没有单独成分表时，仍保留基准外目标多头暴露。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.universe_uri is None
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri)
            exposure = pl.concat([
                exposure,
                pl.DataFrame({
                    "date": [date(2026, 6, 30), date(2026, 7, 8)],
                    "security_id": ["C", "C"],
                    "industry_bank": [0.0, 0.0],
                    "Size": [0.8, 0.9],
                }),
            ])
            exposure.write_parquet(paths.barra_exposure_uri)

            inputs = load_derived_barra_inputs(paths, SCHEDULE, policy())

            self.assertIn(
                "C",
                inputs.exposures.collect().get_column("security_id").to_list(),
            )

    def test_exposures_cover_research_universe_outside_benchmark_weights(self) -> None:
        """研究池与米筐权重短期不一致时，基准外持仓仍要有真实暴露。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            universe = Path(directory) / "universe.parquet"
            pl.DataFrame({
                "date": [date(2026, 6, 29), date(2026, 6, 29), date(2026, 7, 7), date(2026, 7, 7)],
                "order_book_id": ["A", "C", "A", "C"],
            }).write_parquet(universe)
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri)
            exposure = pl.concat([
                exposure,
                pl.DataFrame({
                    "date": [date(2026, 6, 30), date(2026, 7, 8)],
                    "security_id": ["C", "C"],
                    "industry_bank": [0.0, 0.0],
                    "Size": [0.8, 0.9],
                }),
            ])
            exposure.write_parquet(paths.barra_exposure_uri)
            paths = paths.model_copy(update={
                "universe_root": Path(directory),
                "universe_uri": universe,
            })

            inputs = load_derived_barra_inputs(paths, SCHEDULE, policy())

            self.assertIn(
                "C",
                inputs.exposures.collect().get_column("security_id").to_list(),
            )

    def test_loads_asof_exposure_weights_and_compounds_daily_returns(self) -> None:
        """暴露与权重向后对齐，日收益在半开持有区间内复合。"""

        with tempfile.TemporaryDirectory() as directory:
            inputs = load_derived_barra_inputs(
                _write_barra_inputs(Path(directory)),
                SCHEDULE,
                policy(),
            )

            exposures = inputs.exposures.collect()
            first = exposures.filter(
                (pl.col("signal_date") == date(2026, 7, 1))
                & (pl.col("security_id") == "A")
            ).row(0, named=True)
            self.assertEqual(first["Size"], 2.0)
            first_asof = inputs.exposure_asof.collect().filter(
                (pl.col("signal_date") == date(2026, 7, 1))
                & (pl.col("security_id") == "A")
            ).item(0, "exposure_asof_date")
            self.assertEqual(first_asof, date(2026, 6, 30))
            weights = inputs.benchmark_weights.collect()
            self.assertEqual(weights.filter(pl.col("signal_date") == date(2026, 7, 1))["weight"].to_list(), [0.6, 0.4])
            factor_returns = inputs.factor_returns.collect()
            first_industry = factor_returns.filter(
                (pl.col("entry_date") == date(2026, 7, 2))
                & (pl.col("factor") == "industry_bank")
            ).item(0, "factor_return")
            self.assertAlmostEqual(first_industry, 1.01 * 1.02 - 1.0)
            self.assertEqual(inputs.identity.exposure_asof_date, date(2026, 7, 8))
            self.assertEqual(inputs.identity.signal_date, date(2026, 7, 8))

    def test_manifest_records_all_three_barra_sources(self) -> None:
        """输入清单必须同时绑定暴露、因子收益和 CSI300 权重。"""

        with tempfile.TemporaryDirectory() as directory:
            manifest = inspect_pilot_sources(_write_barra_inputs(Path(directory)))
            self.assertEqual(manifest.barra.status, "available")
            self.assertEqual(len(manifest.barra.source_uris), 3)
            self.assertIsNotNone(manifest.barra.input_sha256)

    def test_future_only_exposure_is_rejected(self) -> None:
        """信号日之后才发布的暴露不能回填到该信号。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri).filter(
                pl.col("date") > date(2026, 7, 1)
            )
            exposure.write_parquet(paths.barra_exposure_uri)
            with self.assertRaises(FactorMinerError):
                load_derived_barra_inputs(paths, SCHEDULE, policy())

    def test_exposure_older_than_frozen_staleness_limit_is_rejected(self) -> None:
        """任一证券暴露超过显式七日阈值时不能进入归因。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri).with_columns(
                pl.when(pl.col("date") == date(2026, 6, 30))
                .then(pl.lit(date(2026, 6, 20)))
                .otherwise(pl.col("date"))
                .alias("date")
            )
            exposure.write_parquet(paths.barra_exposure_uri)
            with self.assertRaises(FactorMinerError):
                load_derived_barra_inputs(paths, SCHEDULE, policy())

    def test_stale_exited_security_does_not_invalidate_current_components(self) -> None:
        """陈旧阈值只约束当期基准成分，不应被历史退出证券误触发。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri)
            exposure = pl.concat(
                [
                    exposure,
                    pl.DataFrame(
                        {
                            "date": [date(2025, 1, 2)],
                            "security_id": ["EXITED"],
                            "industry_bank": [0.0],
                            "Size": [0.5],
                        }
                    ),
                ]
            )
            exposure.write_parquet(paths.barra_exposure_uri)

            inputs = load_derived_barra_inputs(paths, SCHEDULE, policy())

            self.assertNotIn(
                "EXITED",
                inputs.exposures.collect().get_column("security_id").to_list(),
            )

    def test_benchmark_uses_one_global_snapshot_without_reviving_exited_names(self) -> None:
        """指数权重必须整期取同一快照，退出成分的旧权重不得带入新一期。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_benchmark_weights_uri is not None
            pl.DataFrame(
                {
                    "date": [
                        date(2026, 6, 29), date(2026, 6, 29),
                        date(2026, 7, 7), date(2026, 7, 7),
                    ],
                    "security_id": ["A", "B", "A", "C"],
                    "weight": [0.6, 0.4, 0.55, 0.45],
                }
            ).write_parquet(paths.barra_benchmark_weights_uri)
            assert paths.barra_exposure_uri is not None
            exposure = pl.read_parquet(paths.barra_exposure_uri)
            exposure = pl.concat(
                [
                    exposure,
                    pl.DataFrame(
                        {
                            "date": [date(2026, 7, 8)],
                            "security_id": ["C"],
                            "industry_bank": [0.0],
                            "Size": [1.2],
                        }
                    ),
                ]
            )
            exposure.write_parquet(paths.barra_exposure_uri)

            inputs = load_derived_barra_inputs(paths, SCHEDULE, policy())
            second = inputs.benchmark_weights.collect().filter(
                pl.col("signal_date") == date(2026, 7, 8)
            )
            self.assertEqual(second["security_id"].to_list(), ["A", "C"])

    def test_missing_exposure_for_current_component_is_rejected(self) -> None:
        """当前 CSI300 成分缺少信号日前暴露时不能静默降低覆盖。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_benchmark_weights_uri is not None
            weights = pl.read_parquet(paths.barra_benchmark_weights_uri).with_columns(
                pl.when(
                    (pl.col("date") == date(2026, 7, 7))
                    & (pl.col("security_id") == "B")
                )
                .then(pl.lit("C"))
                .otherwise(pl.col("security_id"))
                .alias("security_id")
            )
            weights.write_parquet(paths.barra_benchmark_weights_uri)
            with self.assertRaises(FactorMinerError):
                load_derived_barra_inputs(paths, SCHEDULE, policy())

    def test_full_risk_inputs_are_mapped_to_signal_dates(self) -> None:
        """完整风险模式必须加载协方差矩阵和个股特异风险的历史快照。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            assert paths.barra_root is not None
            covariance_path = paths.barra_root / "covariance.parquet"
            specific_path = paths.barra_root / "specific_risk.parquet"
            covariance_rows = []
            for asof in (date(2026, 6, 30), date(2026, 7, 8)):
                for factor_a, factor_b, value in (
                    ("industry_bank", "industry_bank", 0.04),
                    ("industry_bank", "Size", 0.01),
                    ("Size", "industry_bank", 0.01),
                    ("Size", "Size", 0.09),
                ):
                    covariance_rows.append(
                        {
                            "date": asof,
                            "factor_a": factor_a,
                            "factor_b": factor_b,
                            "covariance": value,
                        }
                    )
            pl.DataFrame(covariance_rows).write_parquet(covariance_path)
            pl.DataFrame(
                {
                    "date": [
                        date(2026, 6, 30), date(2026, 6, 30),
                        date(2026, 7, 8), date(2026, 7, 8),
                    ],
                    "security_id": ["A", "B", "A", "B"],
                    "specific_risk": [0.2, 0.3, 0.21, 0.31],
                }
            ).write_parquet(specific_path)
            full_paths = paths.model_copy(
                update={
                    "barra_covariance_uri": covariance_path,
                    "barra_specific_risk_uri": specific_path,
                }
            )

            inputs = load_derived_barra_inputs(
                full_paths,
                SCHEDULE,
                policy("full_risk_decomposition"),
            )
            self.assertEqual(inputs.covariance.collect().height, 8)
            self.assertEqual(inputs.specific_risk.collect().height, 4)
            self.assertEqual(
                inputs.covariance.collect()["signal_date"].unique().sort().to_list(),
                [date(2026, 7, 1), date(2026, 7, 8)],
            )
            backtest = PortfolioBacktestResult.build(
                policy_id="portfolio-policy",
                direction="positive",
                daily_returns=tuple(
                    {
                        "entry_date": window.entry_date,
                        "exit_date": window.exit_date,
                        "target_long_gross_return": 0.03,
                        "target_long_turnover": 1.0,
                        "target_long_cost": 0.0014,
                        "target_long_net_return": 0.0286,
                        "benchmark_return": 0.01,
                    }
                    for window in SCHEDULE
                ),
                weights=tuple(
                    {
                        "signal_date": window.signal_date,
                        "entry_date": window.entry_date,
                        "exit_date": window.exit_date,
                        "security_id": security_id,
                        "portfolio": "target_long",
                        "weight": weight,
                    }
                    for window in SCHEDULE
                    for security_id, weight in (("A", 0.6), ("B", 0.4))
                ),
                governance=governance(),
            )
            result = evaluate_fixed_backtest_barra(
                backtest,
                inputs,
                policy("full_risk_decomposition"),
            )
            self.assertEqual(result.availability.status, "available")
            assert result.attribution is not None
            self.assertEqual(
                result.attribution.risk_decomposition_status,
                "complete",
            )

    def test_portfolio_result_maps_gross_realized_and_benchmark_returns(self) -> None:
        """归因使用持仓期毛收益，不能把交易成本误当 Barra 因子贡献。"""

        backtest = PortfolioBacktestResult.build(
            policy_id="portfolio-policy",
            direction="positive",
            daily_returns=(
                {
                    "entry_date": date(2026, 7, 2),
                    "exit_date": date(2026, 7, 6),
                    "target_long_gross_return": 0.03,
                    "target_long_turnover": 1.0,
                    "target_long_cost": 0.0014,
                    "target_long_net_return": 0.0286,
                    "benchmark_return": 0.01,
                },
            ),
            weights=(
                {
                    "signal_date": date(2026, 7, 1),
                    "entry_date": date(2026, 7, 2),
                    "exit_date": date(2026, 7, 6),
                    "security_id": "A",
                    "portfolio": "target_long",
                    "weight": 1.0,
                },
            ),
            governance=governance(),
        )
        row = build_barra_portfolio_weights(backtest).collect().row(0, named=True)
        self.assertEqual(row["realized_return"], 0.03)
        self.assertEqual(row["benchmark_return"], 0.01)
        self.assertNotIn("asset_return", row)
        self.assertEqual(row["portfolio"], "target_long")

    def test_fixed_pilot_backtest_uses_derived_barra_adapter(self) -> None:
        """阶段 A 应能把真实组合结果和派生 Barra 三端口接入现有归因器。"""

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_barra_inputs(Path(directory))
            derived = load_derived_barra_inputs(paths, SCHEDULE, policy())
            backtest = PortfolioBacktestResult.build(
                policy_id="portfolio-policy",
                direction="positive",
                daily_returns=(
                    {
                        "entry_date": date(2026, 7, 2),
                        "exit_date": date(2026, 7, 6),
                        "target_long_gross_return": 0.03,
                        "target_long_turnover": 1.0,
                        "target_long_cost": 0.0014,
                        "target_long_net_return": 0.0286,
                        "benchmark_return": 0.01,
                    },
                    {
                        "entry_date": date(2026, 7, 9),
                        "exit_date": date(2026, 7, 13),
                        "target_long_gross_return": 0.02,
                        "target_long_turnover": 1.0,
                        "target_long_cost": 0.0014,
                        "target_long_net_return": 0.0186,
                        "benchmark_return": 0.005,
                    },
                ),
                weights=tuple(
                    {
                        "signal_date": window.signal_date,
                        "entry_date": window.entry_date,
                        "exit_date": window.exit_date,
                        "security_id": security_id,
                        "portfolio": "target_long",
                        "weight": weight,
                    }
                    for window in SCHEDULE
                    for security_id, weight in (("A", 0.6), ("B", 0.4))
                ),
                governance=governance(),
            )
            mapped = build_barra_portfolio_weights(backtest).collect()
            self.assertEqual(mapped.get_column("portfolio").unique().to_list(), ["target_long"])
            result = evaluate_fixed_backtest_barra(
                backtest,
                derived,
                policy(),
                source_uris=(
                    paths.barra_exposure_uri,
                    paths.barra_factor_returns_uri,
                    paths.barra_benchmark_weights_uri,
                ),
                input_sha256="a" * 64,
            )
            self.assertEqual(result.availability.status, "available")
            self.assertIsNotNone(result.attribution)


if __name__ == "__main__":
    unittest.main()
