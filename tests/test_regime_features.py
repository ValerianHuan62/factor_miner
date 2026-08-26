from datetime import date, timedelta
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np
import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_data import (
    CompanyAShareRegimeDataSource,
    RegimeDataRequest,
)
from factor_miner.regime_features import (
    FEATURE_COLUMNS,
    build_market_features,
    fit_training_standardizer,
    transform_market_features,
)
from factor_miner.regime_schema import RegimeFeaturePolicy, ReturnAggregation
from tests.test_data_source import _profile, _write_fixture


def synthetic_market_panel() -> pl.DataFrame:
    """构造包含状态异常和交易受限股票的合成全市场面板。"""

    rows: list[dict[str, object]] = []
    start = date(2026, 1, 5)
    for day_index in range(25):
        current = start + timedelta(days=day_index)
        for asset_index in range(520):
            is_st = asset_index == 0
            is_new = asset_index == 1
            suspended = asset_index == 2
            eligible = not (is_st or is_new or suspended)
            if day_index == 23 and asset_index >= 33:
                eligible = False
                suspended = True
            base = 10.0 + asset_index * 0.001
            close = base * (1.01**day_index)
            if is_st:
                close = base * (2.0**day_index)
            if day_index == 24:
                close *= 50.0
            can_buy = asset_index != 3
            can_sell = asset_index != 4
            rows.append(
                {
                    "date": current,
                    "asset": f"A{asset_index:04d}",
                    "close": close,
                    "amount": 1_000_000.0 * (1.0 + 0.01 * day_index),
                    "is_st": is_st,
                    "is_newly_listed": is_new,
                    "is_suspended": suspended,
                    "can_buy": can_buy,
                    "can_sell": can_sell,
                    "valid_for_factor_rank": eligible,
                }
            )
    return pl.DataFrame(rows)


class MarketFeatureTest(unittest.TestCase):
    """市场状态四特征与训练期标准化测试。"""

    def test_point_in_time_universe_excludes_invalid_names(self) -> None:
        """ST、新股和停牌不得污染收益；交易受限股票仍保留。"""

        result = build_market_features(
            synthetic_market_panel(),
            RegimeFeaturePolicy(),
            ReturnAggregation.MEDIAN,
            min_daily_assets=500,
        ).frame
        day = result.row(20, named=True)
        self.assertEqual(day["eligible_assets"], 517)
        self.assertAlmostEqual(day["market_return"], 0.01, places=12)
        self.assertAlmostEqual(day["advancers_ratio"], 1.0, places=12)
        self.assertAlmostEqual(day["restricted_trading_ratio"], 2 / 517, places=12)
        self.assertTrue(day["feature_valid"])

    def test_incomplete_day_is_explicit_and_not_filled(self) -> None:
        """股票数低于硬门槛时必须产生不可用日而不是缩小股票池继续。"""

        result = build_market_features(
            synthetic_market_panel(),
            RegimeFeaturePolicy(),
            ReturnAggregation.EQUAL_WEIGHT,
            min_daily_assets=500,
        ).frame
        day = result.row(23, named=True)
        self.assertEqual(day["eligible_assets"], 30)
        self.assertFalse(day["feature_valid"])
        self.assertEqual(day["failure_code"], FailureCode.REGIME_FEATURE_INCOMPLETE.value)

    def test_future_panel_changes_do_not_change_historical_features(self) -> None:
        """修改未来价格和状态不能改变此前任何市场特征。"""

        panel = synthetic_market_panel()
        cutoff = date(2026, 1, 28)
        historical_panel = panel.filter(pl.col("date") <= cutoff)
        historical = build_market_features(
            historical_panel,
            RegimeFeaturePolicy(),
            ReturnAggregation.MEDIAN,
            min_daily_assets=500,
        ).frame
        full = build_market_features(
            panel,
            RegimeFeaturePolicy(),
            ReturnAggregation.MEDIAN,
            min_daily_assets=500,
        ).frame.filter(pl.col("date") <= cutoff)
        self.assertTrue(historical.equals(full))

    def test_training_standardizer_uses_sample_statistics(self) -> None:
        """训练 scaler 必须使用手算样本均值和样本标准差。"""

        frame = pl.DataFrame(
            {
                "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
                FEATURE_COLUMNS[0]: [1.0, 2.0, 3.0],
                FEATURE_COLUMNS[1]: [2.0, 3.0, 4.0],
                FEATURE_COLUMNS[2]: [3.0, 4.0, 5.0],
                FEATURE_COLUMNS[3]: [4.0, 5.0, 6.0],
            }
        )
        scaler = fit_training_standardizer(frame)
        self.assertEqual(scaler.means, (2.0, 3.0, 4.0, 5.0))
        self.assertEqual(scaler.scales, (1.0, 1.0, 1.0, 1.0))
        transformed = transform_market_features(frame, scaler)
        np.testing.assert_allclose(
            transformed,
            np.array(
                [
                    [-1.0, -1.0, -1.0, -1.0],
                    [0.0, 0.0, 0.0, 0.0],
                    [1.0, 1.0, 1.0, 1.0],
                ]
            ),
        )

    def test_standardizer_rejects_constant_or_non_finite_training_data(self) -> None:
        """常数和非有限训练列不能被静默缩放。"""

        for values in ([1.0, 1.0, 1.0], [1.0, math.nan, 3.0]):
            frame = pl.DataFrame(
                {
                    FEATURE_COLUMNS[0]: values,
                    FEATURE_COLUMNS[1]: [2.0, 3.0, 4.0],
                    FEATURE_COLUMNS[2]: [3.0, 4.0, 5.0],
                    FEATURE_COLUMNS[3]: [4.0, 5.0, 6.0],
                }
            )
            with self.subTest(values=values), self.assertRaises(FactorMinerError) as raised:
                fit_training_standardizer(frame)
            self.assertEqual(
                raised.exception.code,
                FailureCode.REGIME_STANDARDIZATION_INVALID,
            )

    def test_regime_source_is_input_only_and_keeps_base_state(self) -> None:
        """专用数据源不能打开标签，并必须返回一字板诊断所需基础状态。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market_uri, state_uri, label_uri = _write_fixture(root)
            label_uri.unlink()
            source = CompanyAShareRegimeDataSource(
                _profile(root, market_uri, state_uri, label_uri),
                code_commit="commit-1",
                config_hash="config-1",
            )
            provenance = source.inspect_inputs()
            frame = source.scan_inputs(
                RegimeDataRequest(
                    start=date(2020, 1, 1),
                    end=date(2020, 1, 3),
                )
            ).collect()

            self.assertEqual(provenance.resolved_release_id, "release-test-1")
            self.assertEqual(
                frame.columns,
                [
                    "date",
                    "asset",
                    "close",
                    "amount",
                    "is_st",
                    "is_newly_listed",
                    "is_suspended",
                    "can_buy",
                    "can_sell",
                    "valid_for_factor_rank",
                ],
            )
            self.assertNotIn("label_o2o_5d", frame.columns)


if __name__ == "__main__":
    unittest.main()
