"""夜涨日跌事件须保持先后时点、完整窗口及未知状态。"""
from datetime import date,timedelta
import numpy as np
import polars as pl
import pytest
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr,attach_market_sessions
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.construct_validation import ObservableCondition,validate_construct
from factor_miner.errors import FactorMinerError
from tests.test_dsl_semantics import semantic_registry


def expression():
    def f(name):return dict(op='field',field=name)
    def n(op,*args,**kw):return dict(op=op,args=list(args),**kw)
    return n('rolling_mean',n('mul',n('gt',f('open'),n('calendar_delay',f('close'),period=1)),n('gt',f('open'),f('close'))),window=20)


def test_strict_comparison_invalids_and_units():
    node=FactorNode(op='gt',args=(FactorNode(op='field',field='x'),FactorNode(op='field',field='y')))
    frame=pl.DataFrame(dict(x=[2.,1.,0.,None,np.inf,np.nan,1.],y=[1.,1.,1.,0.,0.,0.,np.inf]))
    assert frame.select(build_polars_expr(node))[:,0].to_list()==[1.,0.,0.,None,None,None,None]
    field=FactorNode(op='field',field='price_close')
    same=FactorNode(op='gt',args=(field,field))
    assert analyse_semantic_type(same,semantic_registry()).unit_dimension==analyse_semantic_type(FactorNode(op='sign',args=(field,)),semantic_registry()).unit_dimension
    with pytest.raises(FactorMinerError):analyse_semantic_type(same.model_copy(update={'args':(field,FactorNode(op='const',value=1.))}),semantic_registry())
    for bad in (node.model_copy(update={'args':node.args[:1]}),node.model_copy(update={'window':20})):
        with pytest.raises(FactorMinerError):validate_ast(bad,{'x','y'},())


def test_event_frequency_independent_calendar_order_and_future():
    days=[date(2020,1,1)+timedelta(days=i) for i in range(100)]
    rng=np.random.default_rng(2101);close=100+rng.normal(size=100).cumsum();opening=close+rng.normal(size=100)
    frame=pl.DataFrame(dict(date=days,asset=['A']*100,close=close,open=opening)).filter(pl.col('date')!=days[35])
    node=FactorNode.model_validate(expression())
    assert validate_ast(node,{'open','close'},()).node_count==9
    def calc(f):return attach_market_sessions(f.lazy(),pl.DataFrame(dict(date=days))).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    lookup=dict(zip(frame['date'],frame['close']));events=[]
    for d,o,c in frame.select('date','open','close').iter_rows():
        prev=lookup.get(d-timedelta(days=1));events.append(float(o>prev and o>c) if prev is not None else np.nan)
    expected=np.full(frame.height,np.nan)
    for i in range(19,frame.height):
        v=events[i-19:i+1]
        if np.isfinite(v).all():expected[i]=sum(v)/20
    actual=calc(frame);np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    scaled=frame.with_columns((pl.col('open')*10).alias('open'),(pl.col('close')*10).alias('close'))
    np.testing.assert_allclose(actual,calc(scaled),atol=1e-12,equal_nan=True)
    changed=frame.with_columns(pl.when(pl.col('date')>days[80]).then(1e9).otherwise(pl.col('open')).alias('open'))
    np.testing.assert_allclose(actual[:80],calc(changed)[:80],equal_nan=True)
    # 完全相等不是事件；夜跌日涨也不能当作夜涨日跌。
    zero=pl.DataFrame(dict(date=days,asset=['A']*100,close=[100.]*100,open=[100.]*100))
    assert np.nanmax(calc(zero))==0
    assert np.nanmax(calc(zero.with_columns(pl.lit(99.).alias('open'))))==0
    assert np.nanmin(calc(zero.with_columns(pl.lit(101.).alias('open'))))==1


def test_preregistered_construct_response():
    condition=ObservableCondition(observation='夜涨日跌',measurement='事件频率',response_test='overnight_up_day_down')
    assert validate_construct(expression(),condition)['status']=='基础检验符合'
