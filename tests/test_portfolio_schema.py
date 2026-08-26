"""组合回测政策合同测试。"""

import unittest

from pydantic import ValidationError

from factor_miner.portfolio_schema import (
    PortfolioEvaluationPolicy,
    TradingCalendarIdentity,
    portfolio_policy_id,
)


class PortfolioSchemaTest(unittest.TestCase):
    """组合回测口径必须由 Schema 固定。"""

    def test_default_policy_freezes_requested_financial_convention(self) -> None:
        policy = PortfolioEvaluationPolicy()
        self.assertEqual(policy.rebalance_weekday, "Tuesday")
        self.assertEqual(policy.return_interval, "open_to_open")
        self.assertEqual(policy.group_count, 10)
        self.assertEqual(policy.group_weighting, "equal_weight")
        self.assertEqual(policy.metric_return_basis, "gross_and_net")
        self.assertEqual(policy.benchmark, "CSI300")
        self.assertEqual(policy.round_trip_cost_bps, 14)
        self.assertEqual(policy.sharpe_basis, "net_of_cost")
        self.assertEqual(policy.annualization_basis, "actual_trading_calendar")
        self.assertRegex(portfolio_policy_id(policy), r"^portpolicy_[0-9a-f]{24}$")

    def test_policy_rejects_any_drift_from_frozen_convention(self) -> None:
        base = PortfolioEvaluationPolicy().model_dump(mode="json")
        changes = (
            {"rebalance_weekday": "Wednesday"},
            {"return_interval": "close_to_close"},
            {"group_count": 5},
            {"group_weighting": "market_cap_weight"},
            {"long_short_pairs": ((10, 1), (8, 3))},
            {"benchmark": "CSI500"},
            {"round_trip_cost_bps": 10},
            {"annualization_basis": "252_fixed"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                PortfolioEvaluationPolicy.model_validate({**base, **change})

    def test_calendar_identity_requires_actual_calendar_hash(self) -> None:
        with self.assertRaises(ValidationError):
            TradingCalendarIdentity(
                calendar_version="sse_szse_2026",
                calendar_sha256="not-a-hash",
            )


if __name__ == "__main__":
    unittest.main()
