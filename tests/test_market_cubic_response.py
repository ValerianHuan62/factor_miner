"""三次市场响应必须独立于线性及平方项，并可用完整OLS复算。"""
from datetime import date,timedelta
import numpy as np
import polars as pl
from factor_miner.compiler import build_polars_expr,attach_market_sessions
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition,validate_construct


def expression():
    def f(name):return dict(op='field',field=name)
    def n(op,*args,**kw):return dict(op=op,args=list(args),**kw)
    c=f('close');m=f('market_return');square=n('mul',m,m)
    return n('rolling_partial_beta',n('div',c,n('calendar_delay',c,period=1)),n('mul',m,square),m,square,window=120)


def test_market_cubic_known_slope_controls_units_gap_and_future():
    node=FactorNode.model_validate(expression());meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(14,4,120)
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(300)]
    m=np.random.default_rng(2501).normal(0,.015,300)
    def frame(b=300.,linear=.5,square=2.):
        r=.0002+linear*m+square*m*m+b*m**3;r[0]=0
        return pl.DataFrame(dict(date=dates,asset=['A']*300,market_return=m,close=100*np.cumprod(1+r)))
    def compute(f):return attach_market_sessions(f.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=compute(frame());np.testing.assert_allclose(actual[120:],300.,atol=1e-5)
    np.testing.assert_allclose(compute(frame(linear=1.5,square=-3.))[120:],300.,atol=1e-5)
    np.testing.assert_allclose(compute(frame(b=700.))[120:],700.,atol=1e-5)
    np.testing.assert_allclose(compute(frame().with_columns((pl.col('close')*10).alias('close'))),actual,atol=1e-5,equal_nan=True)
    missing=compute(frame().filter(pl.col('date')!=dates[150]));assert np.isnan(missing[150:270]).all()
    changed=frame().with_row_index().with_columns(pl.when(pl.col('index')>250).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(actual[:251],compute(changed)[:251],atol=1e-5,equal_nan=True)


def test_cubic_construct_has_known_increment():
    condition=ObservableCondition(observation='三次极端市场响应',measurement='控制线性平方的三次系数',response_test='market_cubic_response')
    result=validate_construct(expression(),condition)
    assert result['status']=='基础检验符合'
    assert abs(result['checks'][-1]['median_response']-1000)<1e-5
