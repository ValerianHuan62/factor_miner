"""历史收益顺序与均价偏离的独立测量，不使用未来收益。"""
from datetime import date,timedelta
import numpy as np
import polars as pl
import pytest
from factor_miner.compiler import build_polars_expr,attach_market_sessions
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.construct_validation import ObservableCondition,validate_construct
from factor_miner.errors import FactorMinerError
from tests.test_dsl_semantics import semantic_registry


def chronology():
    f=FactorNode(op='field',field='close')
    return FactorNode(op='rolling_time_corr',window=20,args=(FactorNode(op='div',args=(f,FactorNode(op='calendar_delay',period=1,args=(f,)))),))


def test_time_correlation_independent_windows_groups_and_nulls():
    rng=np.random.default_rng(2401);x=rng.normal(size=160);x[35]=np.nan;x[115]=np.inf
    node=FactorNode(op='rolling_time_corr',window=20,args=(FactorNode(op='field',field='x'),))
    frame=pl.DataFrame({'asset':['A']*80+['B']*80,'x':x})
    def compute(data):return data.lazy().select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=compute(frame);expected=np.full(160,np.nan)
    for offset in (0,80):
        for i in range(offset+19,offset+80):
            values=x[i-19:i+1]
            if np.isfinite(values).all():expected[i]=np.corrcoef(values,np.arange(20))[0,1]
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(actual,compute(frame.with_columns((pl.col('x')*10+5).alias('x'))),atol=1e-12,equal_nan=True)
    assert np.isnan(compute(frame.with_columns(pl.lit(7.).alias('x')))).all()
    modified=frame.with_row_index().with_columns(pl.when(pl.col('index')>140).then(1e6).otherwise(pl.col('x')).alias('x'))
    np.testing.assert_allclose(actual[:141],compute(modified)[:141],atol=1e-12,equal_nan=True)


def test_same_return_distribution_and_endpoint_opposite_order():
    node=chronology();meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(5,4,20)
    returns=np.linspace(-.02,.03,20);days=[date(2020,1,1)+timedelta(days=i) for i in range(21)]
    rows=[]
    for asset,values in [('up',returns),('down',returns[::-1])]:
        close=100*np.cumprod(np.r_[1.,1+values]);rows.extend({'asset':asset,'date':d,'close':v} for d,v in zip(days,close))
    frame=pl.DataFrame(rows);calendar=pl.DataFrame({'date':days})
    result=attach_market_sessions(frame.lazy(),calendar).with_columns(build_polars_expr(node).alias('value')).collect().group_by('asset').last()
    by=dict(zip(result['asset'],result['value']));assert abs(by['up']-1)<1e-12 and abs(by['down']+1)<1e-12
    assert abs(result['close'][0]-result['close'][1])<1e-10
    condition=ObservableCondition(observation='日收益越来越高',measurement='收益与旧至新顺序相关',response_test='recent_return_acceleration')
    assert validate_construct(node,condition)['status']=='基础检验符合'
    missing=frame.filter(pl.col('date')!=days[10])
    assert attach_market_sessions(missing.lazy(),calendar).select(build_polars_expr(node)).collect()[:,0].null_count()==missing.height


def test_time_correlation_types_and_forbidden_parameters():
    field=FactorNode(op='field',field='price_close');node=FactorNode(op='rolling_time_corr',window=20,args=(field,))
    assert analyse_semantic_type(node,semantic_registry()).unit_dimension==analyse_semantic_type(FactorNode(op='sign',args=(field,)),semantic_registry()).unit_dimension
    for bad in (node.model_copy(update={'center':True}),node.model_copy(update={'window':19}),node.model_copy(update={'args':(field,field)})):
        with pytest.raises(FactorMinerError):validate_ast(bad,{'price_close'},())


def test_short_average_distance_matches_independent_window_and_units():
    f=FactorNode(op='field',field='close');node=FactorNode(op='sub',args=(FactorNode(op='div',args=(f,FactorNode(op='rolling_mean',window=10,args=(f,)))),FactorNode(op='const',value=1.)))
    prices=np.r_[np.linspace(80,100,30),[100]*20].astype(float)
    frame=pl.DataFrame({'asset':['A']*50,'close':prices})
    actual=frame.select(build_polars_expr(node))[:,0].to_numpy()
    expected=np.full(50,np.nan)
    for i in range(9,50):expected[i]=prices[i]/np.mean(prices[i-9:i+1])-1
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(actual,frame.with_columns((pl.col('close')*10).alias('close')).select(build_polars_expr(node))[:,0].to_numpy(),atol=1e-12,equal_nan=True)
    condition=ObservableCondition(observation='现价偏离近期均价',measurement='10行均价距离',response_test='trend')
    assert validate_construct(node,condition)['status']=='基础检验符合'
