"""可见 RankIC 评价测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.evaluation import evaluate_rank_ic
from factor_miner.schema import CampaignSpec
from tests.helpers import valid_campaign


def visible_campaign(
    *, min_names_per_date: int = 3, min_valid_dates: int = 2
) -> CampaignSpec:
    """构造适合黄金测试的短可见 campaign。"""

    return valid_campaign().model_copy(
        update={
            "visible_start": date(2020, 1, 1),
            "visible_end": date(2020, 1, 4),
            "max_hypotheses": 5,
            "min_names_per_date": min_names_per_date,
            "min_valid_dates": min_valid_dates,
            "min_median_coverage": 0.5,
        }
    )


def rank_panel(*, negative: bool = False, constant: bool = False) -> pl.DataFrame:
    """生成四日期、四资产的确定性排名面板。"""

    rows: list[dict[str, object]] = []
    for day_index in range(4):
        current = date(2020, 1, 1) + timedelta(days=day_index)
        for asset_index, asset in enumerate(("AAA", "BBB", "CCC", "DDD")):
            value = float(asset_index + 1)
            if negative:
                value = -value
            if constant:
                value = 1.0
            rows.append(
                {
                    "date": current,
                    "asset": asset,
                    "raw_factor": value,
                    "valid_for_factor_rank": True,
                    "label_o2o_5d": float(asset_index + 1),
                }
            )
    return pl.DataFrame(rows).sort(["date", "asset"])


class EvaluationTest(unittest.TestCase):
    """验证每日有效性和 Spearman 方向。"""

    def test_perfect_positive_rank_ic_is_one(self) -> None:
        """完美同向排名的每日 RankIC 必须为 1。"""
        result = evaluate_rank_ic(rank_panel(), visible_campaign())
        self.assertEqual(result.valid_dates, 4)
        self.assertEqual(result.invalid_dates, 0)
        self.assertEqual(result.rank_ic_values, (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(result.median_coverage, 1.0)

    def test_perfect_negative_rank_ic_is_minus_one(self) -> None:
        """完美反向排名的每日 RankIC 必须为 -1。"""
        result = evaluate_rank_ic(rank_panel(negative=True), visible_campaign())
        self.assertEqual(result.rank_ic_values, (-1.0, -1.0, -1.0, -1.0))

    def test_constant_factor_is_invalid_not_zero(self) -> None:
        """常数因子不能被伪造为零 IC。"""
        result = evaluate_rank_ic(rank_panel(constant=True), visible_campaign())
        self.assertEqual(result.valid_dates, 0)
        self.assertEqual(result.invalid_dates, 4)
        self.assertTrue(all(item.rank_ic is None for item in result.daily))
        self.assertTrue(all(item.reason == "常数输入" for item in result.daily))

    def test_minimum_names_and_null_labels_are_invalid_dates(self) -> None:
        """股票数不足和标签缺失日期必须明确标记为无效。"""
        frame = rank_panel().with_columns(
            pl.when(pl.col("date") == date(2020, 1, 2))
            .then(False)
            .otherwise(pl.col("valid_for_factor_rank"))
            .alias("valid_for_factor_rank"),
            pl.when(
                (pl.col("date") == date(2020, 1, 3))
                & (pl.col("asset") == "DDD")
            )
            .then(pl.lit(None))
            .otherwise(pl.col("label_o2o_5d"))
            .alias("label_o2o_5d"),
        )
        result = evaluate_rank_ic(
            frame,
            visible_campaign(min_names_per_date=4),
        )
        self.assertEqual(result.valid_dates, 2)
        self.assertEqual(result.invalid_dates, 2)
        reasons = {item.date: item.reason for item in result.daily if item.reason}
        self.assertEqual(reasons[date(2020, 1, 2)], "有效股票数不足")
        self.assertEqual(reasons[date(2020, 1, 3)], "有效股票数不足")

    def test_visible_interval_excludes_out_of_range_dates(self) -> None:
        """评价必须严格使用 campaign 可见日期区间。"""
        frame = rank_panel().vstack(
            pl.DataFrame(
                [
                    {
                        "date": date(2020, 1, 5),
                        "asset": "AAA",
                        "raw_factor": 1.0,
                        "valid_for_factor_rank": True,
                        "label_o2o_5d": 1.0,
                    }
                ]
            )
        )
        result = evaluate_rank_ic(frame, visible_campaign())
        self.assertEqual(result.total_dates, 4)
        self.assertNotIn(date(2020, 1, 5), {item.date for item in result.daily})

    def test_diagnostics_report_icir_quantiles_annual_and_sample_counts(self) -> None:
        """评价结果应包含 ICIR、方向占比、分位数、年度和样本数诊断。"""
        factor_ranks = (1.0, 2.0, 3.0, 4.0)
        label_ranks_by_day = (
            (1.0, 2.0, 3.0, 4.0),
            (1.0, 2.0, 3.0, 4.0),
            (4.0, 3.0, 2.0, 1.0),
            (1.0, 3.0, 4.0, 2.0),
        )
        rows: list[dict[str, object]] = []
        for day_index, labels in enumerate(label_ranks_by_day):
            current = date(2020, 1, 1) + timedelta(days=day_index)
            for asset_index, asset in enumerate(("AAA", "BBB", "CCC", "DDD")):
                rows.append(
                    {
                        "date": current,
                        "asset": asset,
                        "raw_factor": factor_ranks[asset_index],
                        "valid_for_factor_rank": True,
                        "label_o2o_5d": labels[asset_index],
                    }
                )
        result = evaluate_rank_ic(
            pl.DataFrame(rows),
            visible_campaign(),
        )
        self.assertAlmostEqual(result.mean_rank_ic, 0.35)
        self.assertGreater(result.std_rank_ic, 0.0)
        self.assertGreater(result.icir, 0.0)
        self.assertAlmostEqual(result.positive_rank_ic_ratio, 0.75)
        self.assertAlmostEqual(result.negative_rank_ic_ratio, 0.25)
        self.assertEqual(result.zero_rank_ic_ratio, 0.0)
        self.assertEqual(
            set(result.rank_ic_quantiles), {"q05", "q25", "q50", "q75", "q95"}
        )
        self.assertEqual(set(result.annual_summaries), {"2020"})
        self.assertEqual(
            (
                result.eligible_count_min,
                result.eligible_count_median,
                result.eligible_count_max,
            ),
            (4, 4.0, 4),
        )
        self.assertEqual(result.median_factor_coverage, 1.0)
        self.assertEqual(result.median_label_coverage, 1.0)

    def test_daily_factor_and_label_coverage_are_reported_separately(self) -> None:
        """每日结果应区分因子有限值、标签有限值和联合有效覆盖率。"""
        frame = rank_panel().with_columns(
            pl.when(
                (pl.col("date") == date(2020, 1, 2))
                & (pl.col("asset") == "DDD")
            )
            .then(pl.lit(None))
            .otherwise(pl.col("raw_factor"))
            .alias("raw_factor"),
            pl.when(
                (pl.col("date") == date(2020, 1, 3))
                & (pl.col("asset") == "DDD")
            )
            .then(pl.lit(None))
            .otherwise(pl.col("label_o2o_5d"))
            .alias("label_o2o_5d"),
        )
        result = evaluate_rank_ic(frame, visible_campaign())
        by_date = {item.date: item for item in result.daily}
        self.assertEqual(by_date[date(2020, 1, 2)].factor_coverage, 0.75)
        self.assertEqual(by_date[date(2020, 1, 2)].label_coverage, 1.0)
        self.assertEqual(by_date[date(2020, 1, 2)].coverage, 0.75)
        self.assertEqual(by_date[date(2020, 1, 3)].factor_coverage, 1.0)
        self.assertEqual(by_date[date(2020, 1, 3)].label_coverage, 0.75)

    def test_event_purge_removes_labels_crossing_next_split(self) -> None:
        """训练/发现样本的退出事件必须严格早于下一分区。"""

        frame = rank_panel().with_columns(
            pl.when(pl.col("date") <= date(2020, 1, 2))
            .then(pl.lit(date(2020, 1, 4)))
            .otherwise(pl.lit(date(2020, 1, 5)))
            .alias("label_exit_date")
        )
        campaign = visible_campaign(min_valid_dates=1).model_copy(
            update={
                "visible_end": date(2020, 1, 4),
                "next_split_start": date(2020, 1, 5),
            }
        )
        result = evaluate_rank_ic(frame, campaign)
        self.assertEqual(result.total_dates, 2)
        self.assertEqual(result.event_purge_removed_rows, 8)
        self.assertEqual(result.event_purge_removed_signal_dates, 2)


if __name__ == "__main__":
    unittest.main()
