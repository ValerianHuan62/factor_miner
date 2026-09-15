"""高价锚距离应放大近期涨跌压力，而不是改变收益方向。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def expression():
    c=FactorNode(op='field',field='close')
    peak=FactorNode(op='rolling_max',window=120,args=(c,))
    distance=FactorNode(op='div',args=(FactorNode(op='sub',args=(peak,c)),peak))
    change=FactorNode(op='div',args=(FactorNode(op='calendar_delta',period=20,args=(c,)),FactorNode(op='calendar_delay',period=20,args=(c,))))
    return FactorNode(op='mul',args=(distance,change))


def test_anchor_interaction_independent_formula_and_response():
    node=expression();meta=validate_ast(node,{'close'},())
    assert (meta.node_count,meta.depth,meta.lookback)==(13,5,119)
    values=np.exp(np.random.default_rng(3401).normal(3,.2,400));values[70]=np.nan
    sessions=np.arange(400)
    frame=pl.DataFrame({'asset':['A']*400,'date':[date(2020,1,1)+timedelta(days=int(i)) for i in sessions],'__market_session':sessions,'close':values})
    def calc(data):return data.with_columns(build_polars_expr(node).alias('factor'))['factor'].to_numpy()
    expected=np.full(400,np.nan)
    for j in range(119,400):
        w=values[j-119:j+1]
        if np.isfinite(w).all():expected[j]=(max(w)-values[j])/max(w)*(values[j]-values[j-20])/values[j-20]
    actual=calc(frame)
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(actual,calc(frame.with_columns((pl.col('close')*19).alias('close'))),atol=1e-12,equal_nan=True)
    future=frame.with_columns(pl.when(pl.col('__market_session')>340).then(1e6).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_array_equal(actual[:341],calc(future)[:341])
    gap=frame.filter(pl.col('__market_session')!=300)
    assert np.isnan(calc(gap)[gap['__market_session'].to_list().index(320)])
    condition=ObservableCondition(observation='高点距离放大近期压力',measurement='历史回撤乘近期收益',response_test='anchor_reversal')
    result=validate_construct(node,condition)
    assert result['status']=='基础检验符合',result
    # 相同近期收益，较远锚增大幅度；亏损仍保留负号；历史高点为末价时距离0。
    cases=[]
    for peak,prior,last in [(120,90,100),(150,90,100),(150,110,100),(100,90,100)]:
        p=np.full(120,95.);p[0]=peak;p[99]=prior;p[-1]=last
        data=pl.DataFrame({'asset':['A']*120,'__market_session':np.arange(120),'close':p})
        cases.append(calc(data)[-1])
    assert 0<cases[0]<cases[1] and cases[2]<0 and cases[3]==0
