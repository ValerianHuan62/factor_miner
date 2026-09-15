"""残差标准差与独立OLS、完整窗口、控制暴露不变性。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
import pytest
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_residual_std_matches_lstsq_and_groups_missing_constants():
    node = FactorNode(op='rolling_residual_std', window=5, args=(FactorNode(op='field', field='close'), FactorNode(op='field', field='open')))
    dates = [date(2020,1,1)+timedelta(days=i) for i in range(50)]
    rng = np.random.default_rng(2801); rows = []
    for a in ['A', 'B']:
        for j, d in enumerate(dates):
            x = 1. if j<8 else rng.normal()
            y = 4. if 30<=j<40 else 1+2*x+rng.normal(0,.3)
            rows.append(dict(date=d,asset=a,open=x,close=y if j!=12 else None))
    frame = pl.DataFrame(rows).with_columns(pl.when(pl.col('date') == dates[22]).then(float('inf')).otherwise(pl.col('open')).alias('open'))
    actual = frame.with_columns(build_polars_expr(node).alias('value'))
    for group in actual.partition_by('asset'):
        y,x = group['close'].to_numpy(),group['open'].to_numpy()
        for j, observed in enumerate(group['value']):
            if j<4 or not np.isfinite(y[j-4:j+1]).all() or not np.isfinite(x[j-4:j+1]).all() or np.ptp(x[j-4:j+1])==0:
                assert observed is None
            else:
                design = np.column_stack([np.ones(5),x[j-4:j+1]])
                residual = y[j-4:j+1]-design@np.linalg.lstsq(design,y[j-4:j+1],rcond=None)[0]
                assert abs(observed-np.std(residual,ddof=1))<1e-12
    with pytest.raises(Exception): validate_ast(node.model_copy(update={'args':node.args[:1]}), {'close','open'}, ())


def expression():
    c = dict(op='field',field='close')
    return dict(op='rolling_residual_std',window=20,args=[dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)]),dict(op='field',field='market_return')])


def test_stock_residual_exposure_scaling_gap_future_and_construct():
    node = FactorNode.model_validate(expression()); meta = validate_ast(node,{'close','market_return'},())
    assert (meta.node_count,meta.depth,meta.lookback)==(6,4,20)
    rng=np.random.default_rng(2802);m=rng.normal(0,.01,300);e=rng.normal(0,.015,300)
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(300)]
    def frame(beta=.5,scale=1.):
        r=.0003+beta*m+scale*e;r[0]=0
        return pl.DataFrame(dict(date=dates,asset=['A']*300,close=50*np.cumprod(1+r),market_return=m))
    def calc(f): return attach_market_sessions(f.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=calc(frame());np.testing.assert_allclose(calc(frame(beta=2.)),actual,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(calc(frame(scale=3.)),actual*3,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(calc(frame().with_columns(pl.col('close')*10)),actual,atol=1e-12,equal_nan=True)
    assert np.isnan(calc(frame().filter(pl.col('date')!=dates[150]))[150:170]).all()
    future=frame().with_columns(pl.when(pl.col('date')>dates[250]).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(calc(future)[:251],actual[:251],equal_nan=True)
    condition=ObservableCondition(observation='市场外收益波动放大',measurement='带截距市场回归的残差样本标准差',response_test='market_residual_variation')
    assert validate_construct(expression(),condition)['status']=='基础检验符合'
