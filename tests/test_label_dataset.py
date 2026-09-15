"""固定交易日日历标签和事件区间 purge 测试。"""

from datetime import date, timedelta
import unittest

import polars as pl

from factor_miner.label_dataset import build_fixed_session_o2o_labels, purge_label_event_overlap


class LabelDatasetTest(unittest.TestCase):
    """缺行证券不得把自己的下一行误当统一 T+1。"""

    def test_missing_asset_session_produces_structural_null_without_shift(self) -> None:
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(8)]
        rows = []
        for asset in ("A", "B"):
            for index, session in enumerate(sessions):
                if asset == "B" and index == 1:
                    continue
                rows.append({"date": session, "asset": asset, "open": 100.0 + index})
        labels = build_fixed_session_o2o_labels(pl.DataFrame(rows))
        b_first = labels.filter((pl.col("date") == sessions[0]) & (pl.col("asset") == "B")).row(0, named=True)
        self.assertEqual(b_first["label_entry_date"], sessions[1])
        self.assertEqual(b_first["label_exit_date"], sessions[6])
        self.assertIsNone(b_first["label_o2o_5d"])

    def test_purge_removes_labels_touching_next_partition(self) -> None:
        panel = pl.DataFrame({
            "date": [date(2023, 12, 27), date(2023, 12, 28), date(2023, 12, 29)],
            "label_exit_date": [date(2023, 12, 29), date(2024, 1, 1), None],
            "asset": ["A", "A", "A"],
        })
        kept, audit = purge_label_event_overlap(panel, next_split_start=date(2024, 1, 1))
        self.assertEqual(kept["date"].to_list(), [date(2023, 12, 27)])
        self.assertEqual(audit.removed_rows, 2)
        self.assertEqual(audit.removed_signal_dates, 2)


if __name__ == "__main__":
    unittest.main()


def test_explicit_calendar_and_horizon_name_do_not_skip_missing_market_day():
    from datetime import date,timedelta
    days=[date(2025,1,1)+timedelta(days=i) for i in range(25)]
    market=pl.DataFrame({'date':[d for d in days if d!=days[2]],'asset':['A']*24,'open':[100.+i for i,d in enumerate(days) if d!=days[2]]})
    cal=pl.DataFrame({'date':days})
    one=build_fixed_session_o2o_labels(market,holding_sessions=1,calendar=cal)
    assert 'label_o2o_1d' in one.columns and 'label_o2o_5d' not in one.columns
    first=one.filter(pl.col('date')==days[0]).row(0,named=True)
    assert first['label_entry_date']==days[1] and first['label_exit_date']==days[2]
    assert first['label_o2o_1d'] is None
    month=build_fixed_session_o2o_labels(market,holding_sessions=20,calendar=cal)
    first=month.filter(pl.col('date')==days[0]).row(0,named=True)
    assert first['label_exit_date']==days[21]
    assert abs(first['label_o2o_20d']-(121/101-1))<1e-12
