"""组合、IC、Barra 与 generation seal 的合成工作流测试。"""

from datetime import date, timedelta
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

import polars as pl

from factor_miner.barra_attribution import calculate_barra_attribution
from factor_miner.barra_schema import BarraInputIdentity
from factor_miner.llm_seal import (
    VerifiedDiscoveryProjection,
    build_generation_seal,
)
from factor_miner.llm_state import FamilyGenerationState
from factor_miner.portfolio_schema import PortfolioEvaluationPolicy
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.trading_schedule import RebalanceWindow
from factor_miner.workflow import PortfolioSources, run_visible_portfolio_campaign
from tests.helpers import valid_registered_trusted_candidate, valid_trusted_campaign
from tests.test_barra_schema import identity as barra_identity
from tests.test_barra_schema import policy as barra_policy
from tests.test_llm_seal import hashes, seal_inputs, terminal_state
from tests.test_llm_schema import dependency
from tests.test_llm_candidate import price_registry
from factor_miner.policy import company_a_share_visible_policy


SCHEDULE = (
    RebalanceWindow(
        signal_date=date(2026, 7, 6),
        entry_date=date(2026, 7, 7),
        exit_date=date(2026, 7, 14),
    ),
    RebalanceWindow(
        signal_date=date(2026, 7, 13),
        entry_date=date(2026, 7, 14),
        exit_date=date(2026, 7, 21),
    ),
)


def portfolio_panel() -> pl.LazyFrame:
    rows = []
    for window_index, window in enumerate(SCHEDULE):
        for number in range(1, 11):
            rows.append(
                {
                    "signal_date": window.signal_date,
                    "entry_date": window.entry_date,
                    "exit_date": window.exit_date,
                    "security_id": f"S{number:02d}",
                    "factor_value": float(number if window_index == 0 else 11 - number),
                    "asset_return": number / 1000.0,
                }
            )
    return pl.DataFrame(rows).lazy()


def portfolio_market_and_state() -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """构造覆盖信号至退出的逐交易日开盘和执行状态。"""

    sessions = [date(2026, 7, 6) + timedelta(days=index) for index in range(16)]
    market = pl.DataFrame([
        {
            "trade_date": current,
            "security_id": f"S{number:02d}",
            "open": 100.0 + day_index * number,
        }
        for day_index, current in enumerate(sessions)
        for number in range(1, 11)
    ]).lazy()
    state = pl.DataFrame([
        {
            "trade_date": current,
            "security_id": f"S{number:02d}",
            "valid_for_factor_rank": True,
            "can_open_long": True,
            "can_close_long": True,
        }
        for current in sessions
        for number in range(1, 11)
    ]).lazy()
    return market, state


def benchmark_returns() -> pl.LazyFrame:
    sessions = [date(2026, 7, 6) + timedelta(days=index) for index in range(16)]
    return pl.DataFrame({
        "entry_date": sessions[:-1],
        "exit_date": sessions[1:],
        "benchmark_return": [0.0] * (len(sessions) - 1),
    }).lazy()


def ic_panel() -> pl.LazyFrame:
    rows = []
    for day_index in range(60):
        current = date(2026, 1, 1) + timedelta(days=day_index)
        for number in range(1, 21):
            rows.append(
                {
                    "date": current,
                    "asset": f"S{number:02d}",
                    "factor_value": float(number),
                    "valid_for_factor_rank": True,
                    "forward_return_1": number / 1000.0,
                    "forward_return_3": number / 1000.0,
                    "forward_return_5": number / 1000.0,
                    "forward_return_10": number / 1000.0,
                    "forward_return_20": number / 1000.0,
                }
            )
    return pl.DataFrame(rows).lazy()


def barra_frames() -> tuple[pl.LazyFrame, pl.LazyFrame, pl.LazyFrame, pl.LazyFrame]:
    weights = []
    benchmark = []
    exposures = []
    factor_returns = []
    for window in SCHEDULE:
        for number in range(1, 11):
            security = f"S{number:02d}"
            weights.append(
                {
                    "signal_date": window.signal_date,
                    "entry_date": window.entry_date,
                    "portfolio": f"Q{number}",
                    "security_id": security,
                    "weight": 1.0,
                    "realized_return": number / 1000.0,
                    "benchmark_return": 0.01,
                }
            )
            benchmark.append(
                {
                    "signal_date": window.signal_date,
                    "entry_date": window.entry_date,
                    "security_id": security,
                    "weight": 0.1,
                }
            )
            exposures.append(
                {
                    "signal_date": window.signal_date,
                    "security_id": security,
                    "industry_bank": 1.0,
                    "Size": float(number),
                }
            )
        for factor in ("industry_bank", "Size"):
            factor_returns.append(
                {
                    "entry_date": window.entry_date,
                    "factor": factor,
                    "factor_return": 0.0,
                }
            )
    return (
        pl.DataFrame(weights).lazy(),
        pl.DataFrame(benchmark).lazy(),
        pl.DataFrame(exposures).lazy(),
        pl.DataFrame(factor_returns).lazy(),
    )


def calendar() -> pl.DataFrame:
    dates = [date(2026, 7, 1) + timedelta(days=index) for index in range(25)]
    return pl.DataFrame({"trade_date": dates, "is_open": [True] * len(dates)})


class PortfolioWorkflowTest(unittest.TestCase):
    """未封存不能读结果，封存后发布清单和终态必须完整。"""

    def sources(self, *, sealed: bool) -> PortfolioSources:
        family, generating = terminal_state(sealed=False)
        seal = build_generation_seal(
            family,
            generating,
            slot_object_hashes=hashes(generating),
            evaluation_dependency_intervals=(
                dependency(factor_input_start=date(2026, 1, 9)),
            ),
            **seal_inputs(),
        )
        _, sealed_state = terminal_state(sealed=True)
        state = sealed_state if sealed else generating
        candidate = valid_registered_trusted_candidate()
        barra_weights, benchmark_weights, exposures, factor_returns = barra_frames()
        portfolio_market, portfolio_state = portfolio_market_and_state()
        return PortfolioSources(
            candidates={candidate.candidate_id: candidate},
            generation_family=family,
            generation_projection=VerifiedDiscoveryProjection(
                state=state,
                ledger_tip_hash="a" * 64,
            ),
            generation_seal=seal,
            portfolio_panels={candidate.candidate_id: portfolio_panel()},
            portfolio_market=portfolio_market,
            portfolio_state=portfolio_state,
            ic_panels={candidate.candidate_id: ic_panel()},
            benchmark_returns=benchmark_returns(),
            calendar=calendar(),
            calendar_version="calendar-synthetic-v1",
            calendar_sha256="b" * 64,
            schedule=SCHEDULE,
            ic_policy=company_a_share_visible_policy(),
            barra_benchmark_weights=benchmark_weights,
            barra_exposures=exposures,
            barra_factor_returns=factor_returns,
            barra_weights_by_candidate={candidate.candidate_id: barra_weights},
            barra_identity=barra_identity(),
        )

    def test_generation_seal_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sources = self.sources(sealed=False)
            with self.assertRaisesRegex(Exception, "LLM_FAMILY_NOT_SEALED"):
                run_visible_portfolio_campaign(
                    valid_trusted_campaign(),
                    PortfolioEvaluationPolicy(),
                    barra_policy(),
                    sources,
                    Path(directory),
                )

    def test_published_run_contains_metrics_and_final_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sources = self.sources(sealed=True)
            result = run_visible_portfolio_campaign(
                valid_trusted_campaign(),
                PortfolioEvaluationPolicy(),
                barra_policy(),
                sources,
                Path(directory),
            )
            self.assertEqual(len(result.statuses), 1)
            self.assertEqual(result.run_manifest_sha256.__len__(), 64)
            manifest = verify_published_run(Path(directory), result.run_id)
            self.assertEqual(manifest.manifest_sha256, result.run_manifest_sha256)
            run_root = Path(directory) / "artifacts" / "runs" / result.run_id
            self.assertTrue((run_root / "portfolio" / "metrics.json").is_file())
            self.assertTrue((run_root / "portfolio" / "daily.json").is_file())
            self.assertTrue((run_root / "ic" / "diagnostics.json").is_file())
            self.assertTrue((run_root / "barra" / "attribution.json").is_file())

    def test_candidate_diagnostic_failure_still_publishes_a_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sources = self.sources(sealed=True)
            bad_panel = pl.DataFrame(
                {
                    "signal_date": [SCHEDULE[0].signal_date],
                    "entry_date": [SCHEDULE[0].entry_date],
                    "exit_date": [SCHEDULE[0].exit_date],
                    "security_id": ["S01"],
                    "factor_value": [1.0],
                    "asset_return": [0.01],
                }
            ).lazy()
            failed_sources = replace(
                sources,
                portfolio_panels={
                    next(iter(sources.candidates)): bad_panel,
                },
            )
            result = run_visible_portfolio_campaign(
                valid_trusted_campaign(),
                PortfolioEvaluationPolicy(),
                barra_policy(),
                failed_sources,
                Path(directory),
            )
            candidate_id = next(iter(result.statuses))
            self.assertEqual(result.statuses[candidate_id].value, "evaluation_failed")
            run_root = Path(directory) / "artifacts" / "runs" / result.run_id
            self.assertTrue((run_root / "run_manifest.json").is_file())
            self.assertEqual(
                (run_root / "portfolio" / "metrics.json").read_text(),
                "{}",
            )


if __name__ == "__main__":
    unittest.main()
