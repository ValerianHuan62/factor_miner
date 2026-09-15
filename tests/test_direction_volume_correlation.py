"""涨跌方向成交相关须遵守精确日历端点和独立相关计算。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import attach_market_sessions, build_polars_expr
from factor_miner.construct_validation import ObservableCondition, validate_construct


def expression():
    return dict(op='rolling_corr', window=20, args=[dict(op='sign', args=[dict(op='calendar_delta',
        period=1, args=[dict(op='field', field='close')])]), dict(op='field', field='volume')])


def test_independent_missing_dates_units_and_future():
    rng = np.random.default_rng(1902)
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(120)]
    calendar = pl.DataFrame(dict(date=days))
    close = 100 + rng.normal(size=120).cumsum()
    volume = rng.uniform(1, 100, size=120)
    frame = pl.DataFrame(dict(date=days, asset=['A']*120, close=close, volume=volume)).filter(pl.col('date') != days[35])
    def compute(data):
        return attach_market_sessions(data.lazy(), calendar).with_columns(build_polars_expr(FactorNode.model_validate(expression())).alias('v')).collect()['v'].to_numpy()
    actual = compute(frame)
    lookup = dict(zip(days, close)); direction = [np.sign(lookup[d] - lookup[d-timedelta(days=1)]) if d != days[0] and d-timedelta(days=1) != days[35] else np.nan for d in frame['date']]
    expected = np.full(frame.height, np.nan)
    for i in range(19, frame.height):
        values = np.column_stack((direction[i-19:i+1], frame['volume'][i-19:i+1].to_numpy()))
        if np.isfinite(values).all(): expected[i] = np.corrcoef(values.T)[0,1]
    np.testing.assert_allclose(actual, expected, atol=1e-10, equal_nan=True)
    np.testing.assert_allclose(actual, compute(frame.with_columns((pl.col('volume')*10).alias('volume'))), atol=1e-10, equal_nan=True)
    changed = frame.with_columns(pl.when(pl.col('date') > days[100]).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(actual[:100], compute(changed)[:100], equal_nan=True)


def test_construct_response():
    condition = ObservableCondition(observation='上涨日成交偏向', measurement='方向成交相关', response_test='return_volume_coupling')
    assert validate_construct(expression(), condition)['status'] == '基础检验符合'
