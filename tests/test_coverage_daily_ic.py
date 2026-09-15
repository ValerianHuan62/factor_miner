"""服务器侧可见标签与逐因子每日 IC 输入构建测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.coverage_daily_ic import (
    build_daily_ic_long,
    build_visible_o2o_5d_labels,
)
from factor_miner.coverage_signal import DailyICIdentity
from tests.test_coverage_snapshot import _nodes


class CoverageDailyICTest(unittest.TestCase):
    """标签属于 outcome 层，IC 逐因子逐日计算。"""

    def test_o2o_label_uses_global_future_open_one_to_six(self) -> None:
        dates = [date(2026, 1, day) for day in range(2, 10)]
        bars = pl.DataFrame(
            {
                "date": dates * 2,
                "asset": ["a"] * 8 + ["b"] * 8,
                "adj_open": [10.0 + value for value in range(8)]
                + [20.0 + 2 * value for value in range(8)],
            },
            schema_overrides={"date": pl.Date},
        )

        labels = build_visible_o2o_5d_labels(bars)

        first_a = labels.filter(
            (pl.col("asset") == "a") & (pl.col("date") == dates[0])
        ).item(0, "label_o2o_5d")
        first_b = labels.filter(
            (pl.col("asset") == "b") & (pl.col("date") == dates[0])
        ).item(0, "label_o2o_5d")
        self.assertAlmostEqual(first_a, 16.0 / 11.0 - 1.0)
        self.assertAlmostEqual(first_b, 32.0 / 22.0 - 1.0)
        self.assertEqual(labels.item(0, "label_entry_date"), dates[1])
        self.assertEqual(labels.item(0, "label_exit_date"), dates[6])
        self.assertEqual(
            labels.filter(pl.col("label_o2o_5d").is_not_null()).height,
            4,
        )

    def test_daily_ic_is_long_and_computed_one_factor_at_a_time(self) -> None:
        nodes = _nodes()
        raw = pl.DataFrame(
            {
                "date": [date(2026, 1, 2)] * 3,
                "asset": ["a", "b", "c"],
                "factor_a": [1.0, 2.0, 3.0],
                "factor_b": [3.0, 2.0, 1.0],
            },
            schema_overrides={"date": pl.Date},
        )
        state = pl.DataFrame(
            {
                "date": [date(2026, 1, 2)] * 3,
                "asset": ["a", "b", "c"],
                "valid_for_factor_rank": [True, True, True],
            },
            schema_overrides={"date": pl.Date},
        )
        labels = pl.DataFrame(
            {
                "date": [date(2026, 1, 2)] * 3,
                "asset": ["a", "b", "c"],
                "label_o2o_5d": [0.1, 0.2, 0.3],
            },
            schema_overrides={"date": pl.Date},
        )
        identity = DailyICIdentity(
            evaluation_policy_id="evalpol_" + "a" * 24,
            data_release_id="release-v1",
            label_id="label-v1",
            visible_start=date(2026, 1, 2),
            visible_end=date(2026, 1, 2),
        )

        result = build_daily_ic_long(
            raw,
            state,
            labels,
            nodes,
            identity,
            min_names_per_date=3,
        )

        self.assertEqual(result.columns, [
            "factor_id",
            "date",
            "rank_ic",
            "coverage",
            "evaluation_policy_id",
            "data_release_id",
            "label_id",
            "visible_start",
            "visible_end",
        ])
        self.assertEqual(result.height, 2)
        self.assertEqual(
            result.sort("factor_id").get_column("rank_ic").to_list(),
            [1.0, -1.0],
        )

    def test_more_than_one_hundred_initial_null_ic_rows_keep_float_schema(self) -> None:
        dates = [
            date(2026, 1, 1) + timedelta(days=index)
            for index in range(102)
        ]
        keys = [
            (current, asset)
            for current in dates
            for asset in ("a", "b")
        ]
        raw = pl.DataFrame(
            {
                "date": [item[0] for item in keys],
                "asset": [item[1] for item in keys],
                "factor_a": [1.0, 2.0] * len(dates),
                "factor_b": [2.0, 1.0] * len(dates),
            },
            schema_overrides={"date": pl.Date},
        )
        state = raw.select(["date", "asset"]).with_columns(
            pl.lit(True).alias("valid_for_factor_rank")
        )
        labels = raw.select(["date", "asset"]).with_columns(
            pl.when(pl.col("date") == dates[-1])
            .then(
                pl.when(pl.col("asset") == "a").then(0.1).otherwise(0.2)
            )
            .otherwise(None)
            .alias("label_o2o_5d")
        )
        identity = DailyICIdentity(
            evaluation_policy_id="evalpol_" + "a" * 24,
            data_release_id="release-v1",
            label_id="label-v1",
            visible_start=dates[0],
            visible_end=dates[-1],
        )

        result = build_daily_ic_long(
            raw,
            state,
            labels,
            _nodes(),
            identity,
            min_names_per_date=2,
        )

        self.assertEqual(result.schema["rank_ic"], pl.Float64)
        self.assertEqual(
            result.filter(pl.col("rank_ic").is_not_null()).height,
            2,
        )


if __name__ == "__main__":
    unittest.main()
