"""嵌套解释份额以独立OLS验证，禁止缺失配对与秩亏造值。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
import pytest
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_explained_fraction_independent_ols_groups_and_missing():
    rng=np.random.default_rng(3102);dates=[date(2020,1,1)+timedelta(days=j) for j in range(200)]
    rows=[]
    for asset in ['A','B']:
        x=rng.normal(size=(200,3));y=.7*x[:,0]+.5*x[:,1]-.3*x[:,2]+rng.normal(0,.1,200)
        for j,d in enumerate(dates):rows.append(dict(date=d,asset=asset,close=y[j],open=x[j,0],high=x[j,1],low=x[j,2]))
    data=pl.DataFrame(rows).with_columns(pl.when(pl.col('date')==dates[100]).then(None).otherwise(pl.col('high')).alias('high'))
    fields=['close','open','high','low'];node=FactorNode(op='rolling_explained_increment',window=20,args=tuple(FactorNode(op='field',field=f) for f in fields))
    def calc(f):return f.with_columns(build_polars_expr(node).alias('value'))
    actual=calc(data)
    for group in actual.partition_by('asset'):
        matrix=group.select(fields).to_numpy()
        for j,value in enumerate(group['value']):
            sample=matrix[max(0,j-19):j+1]
            if j<19 or not np.isfinite(sample).all():assert value is None;continue
            y=sample[:,0];sst=np.sum((y-y.mean())**2)
            full=np.column_stack([np.ones(20),sample[:,1:]]);base=full[:,:2]
            rf=1-np.sum((y-full@np.linalg.lstsq(full,y,rcond=None)[0])**2)/sst
            rb=1-np.sum((y-base@np.linalg.lstsq(base,y,rcond=None)[0])**2)/sst
            assert abs(value-(1-rb/rf))<1e-12 and 0<=value<=1
    reunit=calc(data.with_columns(pl.col('close')*3+4,pl.col('open')*100,pl.col('high')/7))
    np.testing.assert_allclose(reunit['value'].to_numpy(),actual['value'].to_numpy(),atol=1e-12,equal_nan=True)
    singular=calc(data.with_columns(pl.col('open').alias('low')));assert singular['value'].null_count()==data.height
    constant=calc(data.with_columns(pl.lit(1.).alias('close')));assert constant['value'].null_count()==data.height
    with pytest.raises(Exception):validate_ast(node.model_copy(update={'args':node.args[:2]}),set(fields),())


def expression():
    c=dict(op='field',field='close');m=dict(op='field',field='market_return')
    return dict(op='rolling_explained_increment',window=60,args=[dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)]),m,
        *(dict(op='calendar_delay',args=[m],period=p) for p in [1,2,3,4])])


def test_distributed_lags_response_gap_future_and_scale():
    node=FactorNode.model_validate(expression());meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(14,4,63)
    dates=[date(2020,1,1)+timedelta(days=j) for j in range(300)];rng=np.random.default_rng(3103);m=rng.normal(0,.01,300)
    def frame(delayed=0.):
        r=.8*m+delayed*np.r_[np.zeros(2),m[:-2]]+.0001*np.sin(np.arange(300));r[0]=0
        return pl.DataFrame(dict(date=dates,asset=['A']*300,close=50*np.cumprod(1+r),market_return=m))
    def calc(f):return attach_market_sessions(f.lazy(),pl.DataFrame({'date':dates})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    immediate=calc(frame());lagged=calc(frame(2.));assert np.nanmedian(immediate)<.001 and np.nanmedian(lagged)>.6
    np.testing.assert_allclose(calc(frame(2.).with_columns(pl.col('close')*10)),lagged,atol=1e-12,equal_nan=True)
    # 市场上下文按股票行连接，缺证券日会使四个精确市场滞后依次缺失，整窗失效。
    gap=calc(frame(2.).filter(pl.col('date')!=dates[150]));assert np.isnan(gap[150:213]).all()
    changed=frame(2.).with_columns(pl.when(pl.col('date')>dates[250]).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(calc(changed)[:251],lagged[:251],atol=1e-12,equal_nan=True)
    condition=ObservableCondition(observation='市场冲击跨多个日滞后体现',measurement='新增滞后解释占全模型解释比例',response_test='distributed_market_delay')
    assert validate_construct(expression(),condition)['status']=='基础检验符合'
