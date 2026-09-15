"""显著性加权收益的独立概率权重参考、同分秩和背景反例。"""
import numpy as np
import polars as pl
import pytest
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.errors import FactorMinerError
from factor_miner.schema import FactorNode
from tests.test_dsl_semantics import semantic_registry


def reference(x, y):
    if not (np.isfinite(x).all() and np.isfinite(y).all()): return np.nan
    scores = [abs(a-b)/(abs(a)+abs(b)+.1) for a,b in zip(x,y,strict=True)]
    ranks = [1+sum(v>s for v in scores)+.5*(sum(v==s for v in scores)-1) for s in scores]
    probabilities = [.7**k for k in ranks]
    return sum(p*r for p,r in zip(probabilities,x,strict=True))/sum(probabilities)-sum(x)/len(x)


def test_reference_groups_gaps_reflection_and_future():
    rng=np.random.default_rng(4001);x=rng.normal(0,.03,600);y=rng.normal(0,.01,600);x[77]=np.nan;y[428]=np.inf
    node=FactorNode(op='rolling_salience_value',window=20,args=(FactorNode(op='field',field='x'),FactorNode(op='field',field='y')))
    def calc(a,b):return pl.DataFrame(dict(asset=['A']*300+['B']*300,x=a,y=b)).with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    expected=np.full(600,np.nan)
    for start in [0,300]:
        for j in range(start+19,start+300):expected[j]=reference(x[j-19:j+1],y[j-19:j+1])
    actual=calc(x,y);np.testing.assert_allclose(actual,expected,atol=1e-14,equal_nan=True)
    np.testing.assert_allclose(calc(-x,-y),-actual,atol=1e-14,equal_nan=True)
    changed=x.copy();changed[551:]=.9;np.testing.assert_array_equal(calc(changed,y)[:551],actual[:551])


def test_ties_context_zero_and_not_ordinary_skew():
    node=FactorNode(op='rolling_salience_value',window=20,args=(FactorNode(op='field',field='x'),FactorNode(op='field',field='y')))
    def calc(x,y):return pl.DataFrame(dict(asset=['A']*20,x=x,y=y)).with_columns(build_polars_expr(node).alias('z'))['z'][-1]
    x=np.array([.02,-.02]*10)
    assert abs(calc(x,x))<1e-15
    assert abs(calc(np.full(20,.02),np.zeros(20)))<1e-15
    assert calc(x,abs(x))<-.01 and calc(x,-abs(x))>.01
    for y in [abs(x),-abs(x)]:
        assert abs(calc(x,y)-reference(x,y))<1e-14
        order=np.random.default_rng(40).permutation(20)
        assert abs(calc(x,y)-calc(x[order],y[order]))<1e-14


def test_construct_calendar_and_units():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return')
    r=FactorNode(op='div',args=(FactorNode(op='calendar_delta',period=1,args=(c,)),FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_salience_value',window=20,args=(r,m))
    meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(7,4,20)
    result=validate_construct(node,ObservableCondition(observation='相同收益在不同市场背景下显著性不同',measurement='显著性加权均值减普通均值',response_test='salience_context'))
    assert result['status']=='基础检验符合',result
    registry=semantic_registry();registry=registry.model_copy(update={'fields':tuple(e.model_copy(update={'public_alias':e.field_id}) for e in registry.fields)})
    with pytest.raises(FactorMinerError,match='DSL_TYPE_ERROR'):analyse_semantic_type(FactorNode(op='rolling_salience_value',window=20,args=(c,c)),registry)
    assert analyse_semantic_type(FactorNode(op='rolling_salience_value',window=20,args=(r,r)),registry).unit_dimension=='dimensionless'
    # 第25市场日缺失；下一观察收益为空，20观察窗口均不得跨缺口制造有效收益。
    sessions=np.delete(np.arange(65),25);prices=50*np.exp(.001*sessions+.01*np.sin(sessions))
    data=pl.DataFrame(dict(asset=['A']*len(sessions),__market_session=sessions,close=prices,market_return=.01*np.cos(sessions)))
    actual=data.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    missing_position=int(np.flatnonzero(sessions==26)[0]);assert np.isnan(actual[missing_position:missing_position+20]).all()
    assert np.isfinite(actual[missing_position+20])
