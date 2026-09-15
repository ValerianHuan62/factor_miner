"""连续信息交互须区分相同累计收益的不同路径及同方向不同强度。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.construct_validation import ObservableCondition, validate_construct


def expression():
    c=dict(op='field',field='close')
    cumulative=dict(op='div',args=[dict(op='calendar_delta',args=[c],period=120),dict(op='calendar_delay',args=[c],period=120)])
    breadth=dict(op='rolling_mean',window=120,args=[dict(op='sign',args=[dict(op='calendar_delta',args=[c],period=1)])])
    return dict(op='mul',args=[dict(op='abs',args=[cumulative]),breadth])


def test_continuous_path_at_fixed_return_and_return_at_fixed_breadth():
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(121)]
    node=FactorNode.model_validate(expression());meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(11,5,120)
    def calc(prices):
        frame=pl.DataFrame(dict(date=dates,asset=['A']*121,close=prices))
        return attach_market_sessions(frame.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    smooth=calc(50*np.exp(np.linspace(0,np.log(1.1),121)));jump=calc(np.r_[np.full(120,50.),55.])
    assert abs(smooth[-1]-.1)<1e-12 and abs(jump[-1]-.1/120)<1e-12
    stronger=calc(50*np.exp(np.linspace(0,np.log(1.2),121)));assert abs(stronger[-1]-.2)<1e-12
    down=calc(50*np.exp(np.linspace(0,np.log(.9),121)));assert abs(down[-1]+.1)<1e-12
    assert calc(np.full(121,50.))[-1]==0


def test_calendar_gap_unit_future_and_construct():
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(300)]
    prices=50*np.exp(.002*np.arange(300)+.01*np.sin(.7*np.arange(300)))
    frame=pl.DataFrame(dict(date=dates,asset=['A']*300,close=prices));node=FactorNode.model_validate(expression())
    def calc(f):return attach_market_sessions(f.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=calc(frame);np.testing.assert_allclose(calc(frame.with_columns(pl.col('close')*10)),actual,atol=1e-12,equal_nan=True)
    assert np.isnan(calc(frame.filter(pl.col('date')!=dates[150]))[150:270]).all()
    future=frame.with_columns(pl.when(pl.col('date')>dates[250]).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(calc(future)[:251],actual[:251],atol=1e-12,equal_nan=True)
    condition=ObservableCondition(observation='连续上涨路径的强度',measurement='累计涨跌绝对幅度乘上涨下跌日比例差',response_test='trend')
    assert validate_construct(expression(),condition)['status']=='基础检验符合'
