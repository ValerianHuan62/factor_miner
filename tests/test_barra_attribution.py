"""Barra 暴露与已实现收益归因测试。"""

from datetime import date
import unittest

import polars as pl

from factor_miner.barra_attribution import calculate_barra_attribution
from factor_miner.barra_schema import BarraInputIdentity
from factor_miner.errors import FactorMinerError, FailureCode
from tests.test_barra_schema import identity, policy


def weights(specific_return: float | None = None) -> pl.LazyFrame:
    rows = []
    for portfolio, weight in (("Q10", 0.5), ("Q1", 0.5)):
        for security_id, benchmark_weight in (("A", 0.5), ("B", 0.5)):
            row = {
                "signal_date": date(2026, 7, 1),
                "entry_date": date(2026, 7, 2),
                "portfolio": portfolio,
                "security_id": security_id,
                "weight": weight if portfolio == "Q10" and security_id == "A" else 0.0,
                "realized_return": 0.03 if portfolio == "Q10" else 0.01,
                "benchmark_return": 0.01,
            }
            if specific_return is not None:
                row["specific_return"] = specific_return
            rows.append(row)
    return pl.DataFrame(rows).lazy()


def benchmark_weights() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "signal_date": [date(2026, 7, 1), date(2026, 7, 1)],
            "entry_date": [date(2026, 7, 2), date(2026, 7, 2)],
            "security_id": ["A", "B"],
            "weight": [0.5, 0.5],
        }
    ).lazy()


def exposures() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "signal_date": [date(2026, 7, 1), date(2026, 7, 1)],
            "security_id": ["A", "B"],
            "industry_bank": [1.0, 0.0],
            "Size": [2.0, 1.0],
        }
    ).lazy()


def factor_returns() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "entry_date": [date(2026, 7, 2), date(2026, 7, 2)],
            "factor": ["industry_bank", "Size"],
            "factor_return": [0.01, 0.02],
        }
    ).lazy()


def covariance(
    *,
    industry_size: float = 0.01,
    size_industry: float = 0.01,
    industry_variance: float = 0.04,
) -> pl.LazyFrame:
    """构造按信号日可得的因子协方差长表。"""

    return pl.DataFrame(
        {
            "signal_date": [date(2026, 7, 1)] * 4,
            "factor_a": ["industry_bank", "industry_bank", "Size", "Size"],
            "factor_b": ["industry_bank", "Size", "industry_bank", "Size"],
            "covariance": [
                industry_variance,
                industry_size,
                size_industry,
                0.09,
            ],
        }
    ).lazy()


def specific_risk(
    *,
    include_b: bool = True,
    risk_a: float = 0.2,
    risk_b: float = 0.3,
) -> pl.LazyFrame:
    """构造按信号日可得的个股特异波动率。"""

    securities = ["A", "B"] if include_b else ["A"]
    risks = [risk_a, risk_b] if include_b else [risk_a]
    return pl.DataFrame(
        {
            "signal_date": [date(2026, 7, 1)] * len(securities),
            "security_id": securities,
            "specific_risk": risks,
        }
    ).lazy()


class BarraAttributionTest(unittest.TestCase):
    """Barra 结果必须可对账并拒绝数据身份漂移。"""

    def test_realized_attribution_reports_active_exposure_and_residual(self) -> None:
        result = calculate_barra_attribution(
            weights(),
            benchmark_weights(),
            exposures(),
            factor_returns(),
            policy(),
            identity=identity(),
        )
        self.assertEqual(result.risk_decomposition_status, "realized_attribution_only")
        self.assertEqual(result.max_abs_reconciliation_error, 0.0)
        self.assertTrue(any(row["factor"] == "Size" for row in result.attribution))
        self.assertTrue(any(row["factor"] == "specific_residual" for row in result.attribution))

    def test_active_exposure_uses_union_of_portfolio_and_benchmark(self) -> None:
        """组合外基准成分和基准外持仓都必须进入主动暴露。"""

        sparse_weights = pl.DataFrame({
            "signal_date": [date(2026, 7, 1)],
            "entry_date": [date(2026, 7, 2)],
            "portfolio": ["Q10"],
            "security_id": ["A"],
            "weight": [1.0],
            "realized_return": [0.03],
            "benchmark_return": [0.01],
        }).lazy()
        benchmark_only_b = pl.DataFrame({
            "signal_date": [date(2026, 7, 1)],
            "entry_date": [date(2026, 7, 2)],
            "security_id": ["B"],
            "weight": [1.0],
        }).lazy()

        result = calculate_barra_attribution(
            sparse_weights,
            benchmark_only_b,
            exposures(),
            factor_returns(),
            policy(),
            identity=identity(),
        )

        summary = result.exposure_summary[0]
        self.assertAlmostEqual(summary["industry_bank"], 1.0)
        self.assertAlmostEqual(summary["Size"], 1.0)

    def test_mismatched_version_and_specific_return_fail(self) -> None:
        changed = identity().model_copy(update={"exposure_version": "other"})
        with self.assertRaises(FactorMinerError) as context:
            calculate_barra_attribution(
                weights(), benchmark_weights(), exposures(), factor_returns(), policy(), identity=changed
            )
        self.assertEqual(context.exception.code, FailureCode.BARRA_DATA_CONTRACT_INVALID)
        with self.assertRaises(FactorMinerError):
            calculate_barra_attribution(
                weights(specific_return=0.0),
                benchmark_weights(),
                exposures(),
                factor_returns(),
                policy(),
                identity=identity(),
            )

    def test_full_risk_mode_requires_covariance_and_specific_risk(self) -> None:
        with self.assertRaises(FactorMinerError):
            calculate_barra_attribution(
                weights(), benchmark_weights(), exposures(), factor_returns(),
                policy("full_risk_decomposition"), identity=identity()
            )

    def test_full_risk_mode_calculates_active_risk_for_each_portfolio(self) -> None:
        """删除协方差二次型或忘记平方特异风险时，本测试必须失败。"""

        full_identity = identity().model_copy(
            update={
                "covariance_version": "cov_20260703",
                "specific_risk_version": "specific_20260703",
            }
        )
        result = calculate_barra_attribution(
            weights(),
            benchmark_weights(),
            exposures(),
            factor_returns(),
            policy("full_risk_decomposition"),
            identity=full_identity,
            covariance=covariance(),
            specific_risk=specific_risk(),
        )

        self.assertEqual(result.risk_decomposition_status, "complete")
        by_portfolio = {
            row["portfolio"]: row for row in result.risk_decomposition
        }
        self.assertAlmostEqual(by_portfolio["Q10"]["factor_variance"], 0.0225)
        self.assertAlmostEqual(by_portfolio["Q10"]["specific_variance"], 0.0225)
        self.assertAlmostEqual(by_portfolio["Q10"]["total_variance"], 0.045)
        self.assertAlmostEqual(
            by_portfolio["Q10"]["volatility"], 0.21213203435596426
        )
        self.assertAlmostEqual(by_portfolio["Q1"]["factor_variance"], 0.2275)
        self.assertAlmostEqual(by_portfolio["Q1"]["specific_variance"], 0.0325)
        self.assertAlmostEqual(by_portfolio["Q1"]["total_variance"], 0.26)
        self.assertAlmostEqual(by_portfolio["Q1"]["volatility"], 0.5099019513592785)

    def test_full_risk_mode_rejects_covariance_factor_mismatch(self) -> None:
        """协方差遗漏任何暴露因子时必须硬失败。"""

        full_identity = identity().model_copy(
            update={
                "covariance_version": "cov_20260703",
                "specific_risk_version": "specific_20260703",
            }
        )
        mismatched = covariance().collect().filter(
            (pl.col("factor_a") != "Size") & (pl.col("factor_b") != "Size")
        ).lazy()
        with self.assertRaises(FactorMinerError):
            calculate_barra_attribution(
                weights(), benchmark_weights(), exposures(), factor_returns(),
                policy("full_risk_decomposition"), identity=full_identity,
                covariance=mismatched, specific_risk=specific_risk(),
            )

    def test_full_risk_mode_rejects_invalid_covariance(self) -> None:
        """非对称矩阵和负方差都不能产生看似有效的组合波动率。"""

        full_identity = identity().model_copy(
            update={
                "covariance_version": "cov_20260703",
                "specific_risk_version": "specific_20260703",
            }
        )
        for invalid in (
            covariance(industry_size=0.01, size_industry=0.02),
            covariance(industry_variance=-0.04),
        ):
            with self.subTest(covariance=invalid.collect().to_dicts()):
                with self.assertRaises(FactorMinerError):
                    calculate_barra_attribution(
                        weights(), benchmark_weights(), exposures(), factor_returns(),
                        policy("full_risk_decomposition"), identity=full_identity,
                        covariance=invalid, specific_risk=specific_risk(),
                    )

    def test_full_risk_mode_rejects_missing_or_nonpositive_specific_risk(self) -> None:
        """缺证券、零值或负值特异风险都必须硬失败。"""

        full_identity = identity().model_copy(
            update={
                "covariance_version": "cov_20260703",
                "specific_risk_version": "specific_20260703",
            }
        )
        for invalid in (
            specific_risk(include_b=False),
            specific_risk(risk_a=0.0),
            specific_risk(risk_b=-0.3),
        ):
            with self.subTest(specific_risk=invalid.collect().to_dicts()):
                with self.assertRaises(FactorMinerError):
                    calculate_barra_attribution(
                        weights(), benchmark_weights(), exposures(), factor_returns(),
                        policy("full_risk_decomposition"), identity=full_identity,
                        covariance=covariance(), specific_risk=invalid,
                    )


if __name__ == "__main__":
    unittest.main()
