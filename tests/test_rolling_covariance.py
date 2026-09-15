"""共同窗口样本协方差及相对回跳测量必须可独立复算。"""
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


def relative_covariance():
    def f():return dict(op='field',field='close')
    def n(op,*args,**kw):return dict(op=op,args=list(args),**kw)
    avg=n('rolling_mean',f(),window=20)
    return n('div',n('rolling_cov',n('calendar_delta',f(),period=1),n('sub',n('calendar_delay',f(),period=1),n('calendar_delay',f(),period=2)),window=20),n('mul',avg,avg))


def test_covariance_independent_groups_and_complete_finite_window():
    rng=np.random.default_rng(2001);x=rng.normal(size=160);y=.2*x+rng.normal(size=160)
    x[25]=np.nan;y[60]=np.inf
    frame=pl.DataFrame(dict(asset=['A']*80+['B']*80,x=x,y=y))
    node=FactorNode(op='rolling_cov',window=20,args=(FactorNode(op='field',field='x'),FactorNode(op='field',field='y')))
    actual=frame.select(build_polars_expr(node))[:,0].to_numpy();expected=np.full(160,np.nan)
    for offset in (0,80):
        for i in range(offset+19,offset+80):
            values=np.column_stack((x[i-19:i+1],y[i-19:i+1]))
            if np.isfinite(values).all():expected[i]=np.cov(values.T,ddof=1)[0,1]
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    const=frame.with_columns(pl.lit(3.).alias('x'),pl.lit(5.).alias('y')).select(build_polars_expr(node))[:,0].drop_nulls()
    np.testing.assert_allclose(const.to_numpy(),0.,atol=1e-12)
    changed=frame.with_row_index().with_columns(pl.when(pl.col('index')>140).then(1e9).otherwise(pl.col('x')).alias('x'))
    np.testing.assert_allclose(actual[:141],changed.select(build_polars_expr(node))[:,0].to_numpy()[:141],equal_nan=True)


def test_relative_covariance_calendar_units_and_bounce():
    expr=relative_covariance();node=FactorNode.model_validate(expr)
    meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(14,5,21)
    days=[date(2020,1,1)+timedelta(days=i) for i in range(100)]
    close=100+np.cumsum(np.random.default_rng(2002).normal(size=100))
    frame=pl.DataFrame(dict(date=days,asset=['A']*100,close=close)).filter(pl.col('date')!=days[35])
    def compute(f):return attach_market_sessions(f.lazy(),pl.DataFrame(dict(date=days))).select(build_polars_expr(node)).collect()[:,0].to_numpy()
    actual=compute(frame);expected=np.full(frame.height,np.nan);lookup=dict(zip(frame['date'],frame['close']))
    xx=[];yy=[]
    for d in frame['date']:
        a,b=lookup.get(d-timedelta(days=1)),lookup.get(d-timedelta(days=2))
        xx.append(lookup[d]-a if a is not None else np.nan);yy.append(a-b if a is not None and b is not None else np.nan)
    for i in range(19,frame.height):
        pair=np.column_stack((xx[i-19:i+1],yy[i-19:i+1]))
        if np.isfinite(pair).all():expected[i]=np.cov(pair.T,ddof=1)[0,1]/np.mean(frame['close'][i-19:i+1].to_numpy())**2
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(actual,compute(frame.with_columns((pl.col('close')*10).alias('close'))),atol=1e-12,equal_nan=True)
    condition=ObservableCondition(observation='相邻价格回跳',measurement='相对价格自协方差',response_test='bid_ask_bounce',expected_response='decrease')
    assert validate_construct(expr,condition)['status']=='基础检验符合'


def test_covariance_type_arity_and_future_forbidden():
    field=FactorNode(op='field',field='price_close')
    node=FactorNode(op='rolling_cov',args=(field,field),window=20)
    product=FactorNode(op='mul',args=(field,field))
    assert analyse_semantic_type(node,semantic_registry()).unit_dimension==analyse_semantic_type(product,semantic_registry()).unit_dimension
    for bad in (node.model_copy(update={'args':(field,)}),node.model_copy(update={'center':True}),node.model_copy(update={'window':19})):
        with pytest.raises(FactorMinerError):validate_ast(bad,{'price_close'},())
