"""V0.4 因子状态画像必须使用当时可得的中文命名状态。"""

from datetime import date, datetime, timezone
import unittest

import polars as pl

from factor_miner.coverage_regime import build_regime_profiles
from factor_miner.coverage_schema import (
    CoverageFactorNode,
    CoverageRegimePolicy,
)
from factor_miner.errors import FactorMinerError
from factor_miner.regime_schema import RegimeAnnotation


SNAPSHOT_ID = "regsnap_" + "a" * 24


def _node() -> CoverageFactorNode:
    return CoverageFactorNode(
        factor_id="factor_a",
        factor_name_cn="合成因子",
        node_status="active",
        structure_source="legacy_formula_metadata",
        formula_expr="close",
        formula_hash="1" * 64,
        input_fields=("close",),
        operator_tags=(),
        windows=(),
        lookback_window=1,
        lag_days=1,
        category="momentum",
        subcategory="price",
        description="用于验证状态日期连接",
        preprocess_method="none",
        neutralization="none",
        orientation_sign=1,
        orientation_source="legacy_visible_mean_ic",
        legacy_mean_ic=0.1,
        source="synthetic.yaml",
    )


def _annotations() -> tuple[RegimeAnnotation, ...]:
    return (
        RegimeAnnotation(
            regime_snapshot_id=SNAPSHOT_ID,
            canonical_state_id=0,
            economic_label="高波动下跌·弱宽度",
            description="下跌、高波动且上涨股票占比较低",
            author="researcher",
            created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        ),
        RegimeAnnotation(
            regime_snapshot_id=SNAPSHOT_ID,
            canonical_state_id=1,
            economic_label="低波动平稳·宽度中性",
            description="收益平稳、波动较低且市场宽度中性",
            author="researcher",
            created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        ),
    )


def _daily_ic() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "factor_id": ["factor_a", "factor_a"],
            "date": [date(2024, 1, 3), date(2024, 1, 4)],
            "rank_ic": [1.0, -1.0],
            "coverage": [0.9, 0.9],
        },
        schema_overrides={"date": pl.Date},
    )


def _filtered() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "observation_date": [date(2024, 1, 2), date(2024, 1, 3)],
            "earliest_use_date": [date(2024, 1, 3), date(2024, 1, 4)],
            "status": ["ok", "ok"],
            "canonical_state_probabilities": [[1.0, 0.0], [0.0, 1.0]],
            "max_probability": [1.0, 1.0],
            "normalized_entropy": [0.0, 0.0],
        },
        schema_overrides={
            "observation_date": pl.Date,
            "earliest_use_date": pl.Date,
        },
    )


class CoverageRegimeTest(unittest.TestCase):
    """状态概率按 earliest_use_date 进入因子 IC 日期。"""

    def test_profiles_use_earliest_use_date_and_chinese_labels(self) -> None:
        profiles = build_regime_profiles(
            (_node(),),
            _daily_ic(),
            _filtered(),
            SNAPSHOT_ID,
            _annotations(),
            CoverageRegimePolicy(
                min_valid_dates_per_state=1,
                min_probability_mass=0.5,
            ),
        )

        self.assertEqual([item.economic_label for item in profiles], [
            "高波动下跌·弱宽度",
            "低波动平稳·宽度中性",
        ])
        self.assertEqual(profiles[0].canonical_state_id, 0)
        self.assertEqual(profiles[0].weighted_mean_rank_ic, 1.0)
        self.assertEqual(profiles[1].weighted_mean_rank_ic, -1.0)
        self.assertEqual(profiles[0].valid_dates, 1)
        self.assertEqual(profiles[1].valid_dates, 1)

    def test_missing_state_annotation_fails_closed(self) -> None:
        with self.assertRaisesRegex(FactorMinerError, "人工中文注释"):
            build_regime_profiles(
                (_node(),),
                _daily_ic(),
                _filtered(),
                SNAPSHOT_ID,
                _annotations()[:1],
                CoverageRegimePolicy(
                    min_valid_dates_per_state=1,
                    min_probability_mass=0.5,
                ),
            )

    def test_annotation_for_another_snapshot_is_rejected(self) -> None:
        wrong = _annotations()[0].model_copy(
            update={"regime_snapshot_id": "regsnap_" + "b" * 24}
        )
        with self.assertRaisesRegex(FactorMinerError, "快照"):
            build_regime_profiles(
                (_node(),),
                _daily_ic(),
                _filtered(),
                SNAPSHOT_ID,
                (wrong, _annotations()[1]),
                CoverageRegimePolicy(
                    min_valid_dates_per_state=1,
                    min_probability_mass=0.5,
                ),
            )

    def test_unavailable_aligned_date_is_rejected(self) -> None:
        unavailable = _filtered().with_columns(
            pl.when(pl.col("earliest_use_date") == date(2024, 1, 4))
            .then(pl.lit("unavailable"))
            .otherwise(pl.col("status"))
            .alias("status")
        )

        with self.assertRaisesRegex(FactorMinerError, "unavailable"):
            build_regime_profiles(
                (_node(),),
                _daily_ic(),
                unavailable,
                SNAPSHOT_ID,
                _annotations(),
                CoverageRegimePolicy(
                    min_valid_dates_per_state=1,
                    min_probability_mass=0.5,
                ),
            )

    def test_probability_dimension_must_match_annotations(self) -> None:
        bad = _filtered().with_columns(
            pl.lit([0.5, 0.3, 0.2]).alias("canonical_state_probabilities")
        )

        with self.assertRaisesRegex(FactorMinerError, "概率维度"):
            build_regime_profiles(
                (_node(),),
                _daily_ic(),
                bad,
                SNAPSHOT_ID,
                _annotations(),
                CoverageRegimePolicy(
                    min_valid_dates_per_state=1,
                    min_probability_mass=0.5,
                ),
            )

    def test_factor_without_valid_ic_keeps_named_unavailable_profiles(self) -> None:
        daily = _daily_ic().with_columns(
            pl.lit(None, dtype=pl.Float64).alias("rank_ic")
        )

        profiles = build_regime_profiles(
            (_node(),),
            daily,
            _filtered(),
            SNAPSHOT_ID,
            _annotations(),
            CoverageRegimePolicy(
                min_valid_dates_per_state=1,
                min_probability_mass=0.5,
            ),
        )

        self.assertEqual(len(profiles), 2)
        self.assertTrue(all(item.status == "unavailable" for item in profiles))
        self.assertEqual(
            {item.economic_label for item in profiles},
            {"高波动下跌·弱宽度", "低波动平稳·宽度中性"},
        )
        self.assertTrue(all(item.reason == "没有有效每日 IC" for item in profiles))


if __name__ == "__main__":
    unittest.main()
