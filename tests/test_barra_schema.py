"""Barra 数据和政策合同测试。"""

from datetime import date
import unittest

from pydantic import ValidationError

from factor_miner.barra_schema import (
    BarraEvaluationPolicy,
    BarraInputIdentity,
    barra_policy_id,
    validate_barra_input_identity,
)
from factor_miner.errors import FactorMinerError, FailureCode


def policy(mode: str = "realized_attribution_only") -> BarraEvaluationPolicy:
    return BarraEvaluationPolicy(
        model_version="barra_cn_v2",
        exposure_version="exposure_20260703",
        factor_return_version="returns_20260703",
        industry_schema_version="citics_v1",
        attribution_mode=mode,
        covariance_version="cov_20260703" if mode == "full_risk_decomposition" else None,
        specific_risk_version="specific_20260703" if mode == "full_risk_decomposition" else None,
    )


def identity() -> BarraInputIdentity:
    return BarraInputIdentity(
        exposure_version="exposure_20260703",
        factor_return_version="returns_20260703",
        exposure_asof_date=date(2026, 7, 1),
        signal_date=date(2026, 7, 2),
        industry_columns=("industry_bank",),
        style_columns=("Size", "Value"),
    )


class BarraSchemaTest(unittest.TestCase):
    """Barra 版本和可得日约束必须 fail closed。"""

    def test_policy_requires_size_and_full_risk_sources(self) -> None:
        with self.assertRaises(ValidationError):
            BarraEvaluationPolicy(
                model_version="barra_cn_v2",
                exposure_version="exposure_20260703",
                factor_return_version="returns_20260703",
                industry_schema_version="citics_v1",
                required_style_factors=("Value",),
            )
        with self.assertRaises(ValidationError):
            BarraEvaluationPolicy(
                model_version="barra_cn_v2",
                exposure_version="exposure_20260703",
                factor_return_version="returns_20260703",
                industry_schema_version="citics_v1",
                attribution_mode="full_risk_decomposition",
            )

    def test_input_cannot_use_future_exposure(self) -> None:
        payload = identity().model_dump(mode="json")
        payload["exposure_asof_date"] = "2026-07-03"
        with self.assertRaises(ValidationError):
            BarraInputIdentity.model_validate(payload)

    def test_identity_must_match_policy_versions(self) -> None:
        self.assertRegex(barra_policy_id(policy()), r"^barrapolicy_[0-9a-f]{24}$")
        changed = identity().model_copy(update={"factor_return_version": "other"})
        with self.assertRaises(FactorMinerError) as context:
            validate_barra_input_identity(policy(), changed)
        self.assertEqual(
            context.exception.code,
            FailureCode.BARRA_DATA_CONTRACT_INVALID,
        )


if __name__ == "__main__":
    unittest.main()
