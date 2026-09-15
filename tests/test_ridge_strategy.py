"""联合模型必须保留未知标签预测，并隔离尚未发生的训练收益。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
import pytest

from factor_miner.ridge_strategy import join_targets, walk_forward


def test_left_join_keeps_unknown_future_labels_and_rejects_variable_exit():
    signals = pl.DataFrame({'date': [date(2024, 1, 5)] * 3, 'asset': ['a', 'b', 'c'], 'f': [1., 2., 3.]}).lazy()
    labels = pl.DataFrame({'date': [date(2024, 1, 5)] * 2, 'asset': ['a', 'b'],
        'label_o2o_5d': [0.1, 0.2], 'label_exit_date': [date(2024, 1, 15)] * 2})
    result = join_targets(signals, labels.lazy())
    assert result.height == 3
    assert result.filter(pl.col('asset') == 'c')['label_o2o_5d'].null_count() == 1
    bad = labels.with_columns(pl.Series('label_exit_date', [date(2024, 1, 15), date(2024, 1, 16)]))
    with pytest.raises(ValueError, match='同一固定日历'):
        join_targets(signals, bad.lazy())


def test_walk_forward_future_targets_do_not_affect_earlier_predictions():
    days = [date(2024, 1, 5) + timedelta(weeks=i) for i in range(10)]
    rows = [dict(date=d, asset=str(i), f=float(i), target_z=float(i % 7),
        label_o2o_5d=None if i == 0 else float(i % 5), label_exit_date=d+timedelta(days=10))
        for d in days for i in range(40)]
    panel = pl.DataFrame(rows)
    policy = dict(train_weeks=4, embargo_weeks=1, retrain_weeks=2, alpha=10.)
    original, fits = walk_forward(panel, days, {'model': ['f']}, policy, days[5])
    attacked = panel.with_columns(pl.when(pl.col('label_exit_date') >= days[5]).then(1e8).otherwise(pl.col('target_z')).alias('target_z'))
    changed, _ = walk_forward(attacked, days, {'model': ['f']}, policy, days[5])
    for d in days[5:7]:
        np.testing.assert_allclose(original.filter(pl.col('date') == d)['prediction'], changed.filter(pl.col('date') == d)['prediction'])
    assert original['label_o2o_5d'].null_count() == 5
    assert all(f['train_last_label_exit'] < f['prediction_start'] for f in fits)
    assert len(fits) == 3
