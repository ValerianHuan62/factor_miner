"""多期限 IC/RankIC 诊断测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ic_diagnostics import FROZEN_HORIZONS, evaluate_ic_horizons
from factor_miner.policy import company_a_share_visible_policy


def policy():
    return company_a_share_visible_policy().model_copy(
        update={"min_valid_dates": 3, "min_names_per_date": 4}
    )


def panel() -> pl.LazyFrame:
    rows = []
    patterns = (
        (1.0, 2.0, 3.0, 4.0),
        (1.0, 2.0, 4.0, 3.0),
        (4.0, 3.0, 2.0, 1.0),
        (1.0, 3.0, 4.0, 2.0),
    )
    for day_index, labels in enumerate(patterns):
        current = date(2026, 1, 1) + timedelta(days=day_index)
        for asset_index in range(4):
            row = {
                "date": current,
                "asset": f"A{asset_index}",
                "factor_value": float(asset_index + 1),
                "valid_for_factor_rank": True,
            }
            for horizon in FROZEN_HORIZONS:
                row[f"forward_return_{horizon}"] = labels[asset_index]
            rows.append(row)
    return pl.DataFrame(rows).lazy()


class ICDiagnosticsTest(unittest.TestCase):
    """IC 期限、阈值概率和 HAC 统计必须可复现。"""

    def test_frozen_horizons_produce_decay_and_series_diagnostics(self) -> None:
        result = evaluate_ic_horizons(panel(), FROZEN_HORIZONS, policy())
        self.assertEqual(result.horizons, FROZEN_HORIZONS)
        self.assertEqual(result.primary_horizon, 5)
        self.assertEqual(len(result.daily), 4)
        self.assertGreater(result.ic_std, 0)
        self.assertGreater(result.rank_ic_std, 0)
        self.assertEqual(len(result.decay), 5)
        self.assertEqual(set(result.ic_distribution), {"q05", "q25", "q50", "q75", "q95"})
        self.assertGreaterEqual(len(result.autocorrelation), 1)

    def test_horizon_change_and_missing_label_fail_closed(self) -> None:
        with self.assertRaises(FactorMinerError) as context:
            evaluate_ic_horizons(panel(), (1, 5), policy())
        self.assertEqual(context.exception.code, FailureCode.STAT_FAMILY_NOT_FROZEN)
        incomplete = panel().drop("forward_return_20")
        with self.assertRaises(FactorMinerError):
            evaluate_ic_horizons(incomplete, FROZEN_HORIZONS, policy())


if __name__ == "__main__":
    unittest.main()
