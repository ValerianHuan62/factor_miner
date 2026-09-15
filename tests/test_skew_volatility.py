"""波动乘样本偏度等于有限样本修正后的第三中心矩除方差。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.construct_validation import ObservableCondition, validate_construct


def expression():
    c=dict(op='field',field='close');g=dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)])
    return dict(op='mul',args=[dict(op='rolling_std',args=[g],window=120),dict(op='rolling_skew',args=[g],window=120)])


def test_joint_moment_independent_identity_shape_scale_gap_and_future():
    node=FactorNode.model_validate(expression());meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(11,5,120)
    rng=np.random.default_rng(2901);r=.003*(rng.chisquare(2,320)-2);r[0]=0
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(320)]
    def frame(sign=1.,scale=1.): return pl.DataFrame(dict(date=dates,asset=['A']*320,close=50*np.cumprod(1+sign*scale*r)))
    def calc(f): return attach_market_sessions(f.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=calc(frame())
    for j in range(120,320):
        centered=r[j-119:j+1]-r[j-119:j+1].mean();expected=120/118*np.mean(centered**3)/np.mean(centered**2)
        assert abs(actual[j]-expected)<1e-9
    np.testing.assert_allclose(calc(frame(scale=2.)),2*actual,atol=1e-9,equal_nan=True)
    np.testing.assert_allclose(calc(frame(sign=-1.)),-actual,atol=1e-9,equal_nan=True)
    np.testing.assert_allclose(calc(frame().with_columns(pl.col('close')*10)),actual,atol=1e-9,equal_nan=True)
    assert np.isnan(calc(frame().filter(pl.col('date')!=dates[150]))[150:270]).all()
    future=frame().with_columns(pl.when(pl.col('date')>dates[280]).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(calc(future)[:281],actual[:281],atol=1e-9,equal_nan=True)
    constant=frame().with_columns(pl.lit(50.).alias('close'));assert np.isnan(calc(constant)).all()
    condition=ObservableCondition(observation='右尾强度与波动共同作用',measurement='120行标准差乘调整样本偏度',response_test='positive_close_jump')
    assert validate_construct(expression(),condition)['status']=='基础检验符合'
