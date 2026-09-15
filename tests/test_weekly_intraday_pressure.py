"""五行开至收盘收益不混入隔夜路径，时点与股份单位保持。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.dsl import validate_ast


def expression():
    return dict(op='rolling_mean', window=5, args=[dict(op='div', args=[dict(op='field', field='close'), dict(op='field', field='open')])])


def test_intraday_mean_independent_endpoints_units_future():
    node = FactorNode.model_validate(expression())
    meta = validate_ast(node, {'close', 'open'}, ())
    assert (meta.node_count, meta.depth, meta.lookback) == (4, 3, 4)
    dates = [date(2020,1,1)+timedelta(days=i) for i in range(40)]
    rows = []
    for a in ['A','B']:
        for j, d in enumerate(dates):
            o = 50*np.exp(.001*j + .05*np.sin(j*.3))
            rows.append(dict(date=d, asset=a, open=o, close=o*(1+.01*np.cos(j*.43))))
    frame = pl.DataFrame(rows).with_columns(pl.when(pl.col('date') == dates[12]).then(None).otherwise(pl.col('open')).alias('open'))
    def calc(f): return f.with_columns(build_polars_expr(node).alias('value'))['value'].to_numpy()
    actual = calc(frame)
    reference = []
    for group in frame.partition_by('asset'):
        g = group['close'].to_numpy()/group['open'].to_numpy()
        reference.extend([float(np.mean(g[max(0,j-4):j+1])) if j>=4 and np.isfinite(g[j-4:j+1]).all() else np.nan for j in range(40)])
    np.testing.assert_allclose(actual, reference, atol=1e-14, equal_nan=True)
    np.testing.assert_allclose(calc(frame.with_columns(pl.col('close')*10, pl.col('open')*10)), actual, atol=1e-14, equal_nan=True)
    changed = frame.with_columns(pl.when(pl.col('date') > dates[30]).then(pl.col('close')*100).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(calc(changed).reshape(2,40)[:,:31], actual.reshape(2,40)[:,:31], equal_nan=True)


def test_intraday_construct_rejects_unchanged_close_only_proxy():
    condition = ObservableCondition(observation='日内收益提高而收盘路径不变', measurement='五行日内毛收益均值', response_test='intraday_return')
    result = validate_construct(expression(), condition)
    assert result['status'] == '基础检验符合'
    close = dict(op='field', field='close')
    wrong = dict(op='div', args=[close, dict(op='delay', args=[close], period=5)])
    assert validate_construct(wrong, condition)['status'] == '偏离'
