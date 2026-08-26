"""V0.3 市场状态研究到快照的纯合成端到端验收。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import numpy as np
import polars as pl

from factor_miner.data_source import InputProvenance
from factor_miner.ledger import JsonlLedger
from factor_miner.regime_features import build_market_features
from factor_miner.regime_research import (
    run_regime_research,
    summarize_daily_diagnostics_by_regime,
)
from factor_miner.regime_schema import (
    CovarianceKind,
    RegimeDeploymentSpec,
    RegimeFeaturePolicy,
    RegimeQualityPolicy,
    RegimeResearchSpec,
    ReturnAggregation,
    TrainingWindow,
    registered_regime_deployment,
    registered_regime_research,
)
from factor_miner.regime_snapshot import publish_regime_snapshot, verify_regime_snapshot
from factor_miner.regime_workflow import validate_regime_deployment


def synthetic_three_regime_panel(days: int = 1420, assets: int = 12) -> pl.DataFrame:
    """生成低波上涨、高波下跌和宽度分化三种持续市场环境。"""

    rng = np.random.default_rng(20260730)
    start = date(2015, 1, 1)
    prices = np.full(assets, 100.0)
    rows: list[dict[str, object]] = []
    for day_index in range(days):
        regime = (day_index // 90) % 3
        if regime == 0:
            common, dispersion, amount_level = 0.0012, 0.0008, 1.0
        elif regime == 1:
            common, dispersion, amount_level = -0.0020, 0.0100, 2.8
        else:
            common, dispersion, amount_level = 0.0001, 0.0040, 1.5
        shocks = common + rng.normal(0.0, dispersion, size=assets)
        if regime == 2:
            shocks[: assets // 2] += 0.004
            shocks[assets // 2 :] -= 0.004
        prices *= 1.0 + shocks
        current_date = start + timedelta(days=day_index)
        for asset_index in range(assets):
            rows.append(
                {
                    "date": current_date,
                    "asset": f"S{asset_index:03d}",
                    "close": float(prices[asset_index]),
                    "amount": float(
                        1_000_000
                        * amount_level
                        * (1.0 + 0.03 * asset_index)
                        * (1.0 + rng.normal(0.0, 0.03))
                    ),
                    "is_st": False,
                    "is_newly_listed": False,
                    "is_suspended": False,
                    "can_buy": True,
                    "can_sell": True,
                    "valid_for_factor_rank": True,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


class RegimeSyntheticE2ETest(unittest.TestCase):
    """锁定三状态研究、人工部署、快照与诊断的整体边界。"""

    def test_three_state_research_deployment_snapshot_and_diagnostics(self) -> None:
        """完整合成流程必须无前视、固定 K 且只输出 filtered probability。"""

        panel = synthetic_three_regime_panel()
        feature_policy = RegimeFeaturePolicy()
        features = build_market_features(
            panel,
            feature_policy,
            ReturnAggregation.MEDIAN,
            min_daily_assets=10,
        ).frame
        spec = RegimeResearchSpec(
            candidate_state_counts=(3,),
            return_aggregations=(ReturnAggregation.MEDIAN,),
            training_windows=(TrainingWindow.EXPANDING,),
            covariance_kinds=(CovarianceKind.DIAG,),
            seeds=(7, 19),
            n_iter=150,
            tol=1e-2,
            min_covar=1e-6,
            min_daily_assets=10,
            min_oos_months=1,
            min_successful_month_ratio=0.5,
            feature_policy=feature_policy,
            quality_policy=RegimeQualityPolicy(),
            research_start=features.get_column("date")[0],
            research_end=features.get_column("date")[-1],
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        report = run_regime_research(
            {ReturnAggregation.MEDIAN: features},
            spec,
        )
        candidate = report.configurations[0]
        successful = tuple(
            item for item in report.monthly_results if item.status == "ok"
        )
        self.assertGreaterEqual(len(successful), 2)

        first = successful[0]
        changed_future = features.with_columns(
            pl.when(pl.col("date") > first.inference_end)
            .then(pl.col("market_return") + 10.0)
            .otherwise(pl.col("market_return"))
            .alias("market_return")
        )
        replay = run_regime_research(
            {ReturnAggregation.MEDIAN: changed_future},
            spec,
        )
        replay_first = next(
            item
            for item in replay.monthly_results
            if item.model_month == first.model_month
        )
        np.testing.assert_allclose(
            first.canonical_probabilities,
            replay_first.canonical_probabilities,
            rtol=0.0,
            atol=1e-12,
        )

        deployment = registered_regime_deployment(
            RegimeDeploymentSpec(
                regime_research_id=report.regime_research_id,
                research_candidate_id=candidate.candidate_id,
                state_count=3,
                return_aggregation=candidate.return_aggregation,
                training_window=candidate.training_window,
                covariance_kind=candidate.covariance_kind,
                seeds=spec.seeds,
                n_iter=spec.n_iter,
                tol=spec.tol,
                min_covar=spec.min_covar,
                min_daily_assets=spec.min_daily_assets,
                feature_policy=spec.feature_policy,
                quality_policy=spec.quality_policy,
                deployment_start=first.inference_start,
                visible_cutoff=first.training_end,
                created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            )
        )
        research = registered_regime_research(spec)
        validate_regime_deployment(research, report, deployment)
        provenance = InputProvenance(
            data_origin="synthetic-three-regime",
            resolved_release_id="synthetic-release-1",
            release_manifest_sha256="a" * 64,
            schema_version="synthetic-v1",
            market_cutoff=spec.research_end.isoformat(),
            adjustment_convention="unadjusted",
            calendar_version="synthetic-calendar-v1",
            state_table_version="synthetic-state-v1",
            state_table_cutoff=spec.research_end.isoformat(),
            code_commit="b" * 40,
            config_hash="c" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = JsonlLedger(root)
            ledger.register_regime_research(research)
            ledger.register_regime_deployment(deployment)
            snapshot = publish_regime_snapshot(
                artifact_root=root,
                deployment=deployment,
                provenance=provenance,
                monthly_results=report.monthly_results,
                market_features=features,
                annotations=(),
            )
            verified = verify_regime_snapshot(snapshot.snapshot_root)
            filtered = pl.read_parquet(
                verified.snapshot_root / "filtered_regimes.parquet"
            )
            saved_features = pl.read_parquet(
                verified.snapshot_root / "market_features.parquet"
            )
            for column in (
                "market_return_zscore",
                "realized_volatility_20d_zscore",
                "log_amount_relative_20d_zscore",
                "advancers_ratio_zscore",
            ):
                self.assertIn(column, saved_features.columns)
            ok = filtered.filter(pl.col("status") == "ok")
            probabilities = ok.get_column(
                "canonical_state_probabilities"
            ).to_list()
            self.assertTrue(
                all(abs(sum(row) - 1.0) < 1e-8 and len(row) == 3 for row in probabilities)
            )
            self.assertTrue(
                all(
                    use > observation
                    for observation, use in zip(
                        ok.get_column("observation_date"),
                        ok.get_column("earliest_use_date"),
                        strict=True,
                    )
                )
            )
            self.assertNotIn("smoothed_state_probability", filtered.columns)
            self.assertIn("state_mapping_cost", filtered.columns)
            self.assertIn("mapping_policy_version", filtered.columns)
            diagnostic = features.select(
                ["date", pl.col("market_return").alias("value")]
            )
            summary = summarize_daily_diagnostics_by_regime(
                ok,
                diagnostic,
                availability="close_signal_t",
            )
            self.assertEqual(len(summary.states), 3)


if __name__ == "__main__":
    unittest.main()
