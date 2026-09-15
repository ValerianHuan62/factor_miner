"""成交量变化的压力响应须剔除市场方向，且不等于真实交易成本。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def expression():
    def f(name): return dict(op='field', field=name)
    def n(op,*args,**kw): return dict(op=op,args=list(args),**kw)
    return n('rolling_partial_beta',n('div',f('volume'),n('calendar_delay',f('volume'),period=1)),n('abs',f('market_return')),f('market_return'),window=120)


def test_known_stress_slope_market_control_and_calendar_gap():
    node=FactorNode.model_validate(expression()); meta=validate_ast(node,{'volume','market_return'},())
    assert (meta.node_count,meta.depth,meta.lookback)==(8,4,120)
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(300)]
    market=np.random.default_rng(2301).normal(0,.01,300)
    def frame(beta=4.,direction=2.):
        gross=1+beta*np.abs(market)+direction*market;gross[0]=1
        return pl.DataFrame(dict(date=dates,asset=['A']*300,volume=1e6*np.cumprod(gross),market_return=market))
    def compute(data):
        return attach_market_sessions(data.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=compute(frame());assert np.isnan(actual[:120]).all()
    np.testing.assert_allclose(actual[120:],4.,atol=1e-9)
    np.testing.assert_allclose(compute(frame(direction=-3.))[120:],4.,atol=1e-9)
    np.testing.assert_allclose(compute(frame(beta=9.))[120:],9.,atol=1e-9)
    np.testing.assert_allclose(actual,compute(frame().with_columns((pl.col('volume')/10).alias('volume'))),atol=1e-9,equal_nan=True)
    missing=frame().filter(pl.col('date')!=dates[150]);gap=compute(missing)
    assert np.isnan(gap[150:270]).all() and np.isfinite(gap[270:]).all()
    changed=frame().with_row_index().with_columns(pl.when(pl.col('index')>250).then(1e20).otherwise(pl.col('volume')).alias('volume'))
    np.testing.assert_allclose(actual[:251],compute(changed)[:251],atol=1e-9,equal_nan=True)


def test_stress_volume_construct_response():
    condition=ObservableCondition(observation='成交增长的压力响应',measurement='绝对市场收益斜率',response_test='stress_volume_support')
    result=validate_construct(expression(),condition)
    assert result['status']=='基础检验符合' and result['mechanism_status']=='mechanism_unverified'
    assert abs(result['checks'][-1]['median_response']-10)<1e-8
