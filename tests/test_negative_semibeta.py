"""共同下跌贡献的独立标量求和与完整日历测量。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition,validate_construct


def test_semibeta_independent_sum_and_zero_semantics():
    rng=np.random.default_rng(3801);x=rng.normal(0,.01,300);y=.7*x+rng.normal(0,.02,300);x[51]=np.inf;y[212]=np.nan
    data=pl.DataFrame({'asset':['A']*150+['B']*150,'x':x,'y':y})
    node=FactorNode(op='rolling_negative_semibeta',window=20,args=(FactorNode(op='field',field='y'),FactorNode(op='field',field='x')))
    def calc(frame):return frame.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    expected=np.full(300,np.nan)
    for start in [0,150]:
        for j in range(start+19,start+150):
            a=y[j-19:j+1];b=x[j-19:j+1]
            if np.isfinite(a).all() and np.isfinite(b).all():expected[j]=sum(min(u,0)*min(v,0) for u,v in zip(a,b,strict=True))/sum(v*v for v in b)
    np.testing.assert_allclose(calc(data),expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(calc(data.with_columns((pl.col('y')*3).alias('y'))),expected*3,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(calc(data.with_columns((pl.col('x')*2).alias('x'))),expected/2,atol=1e-12,equal_nan=True)
    future=data.with_row_index().with_columns(pl.when(pl.col('index')>260).then(-99.).otherwise(pl.col('y')).alias('y'))
    np.testing.assert_array_equal(calc(data)[:261],calc(future)[:261])
    assert np.isnan(calc(data.with_columns(pl.lit(0.).alias('x')))).all()
    clean=data.with_columns(pl.lit(-1.).alias('x'),pl.lit(2.).alias('y'))
    assert calc(clean)[-1]==0 # 完整窗口内确实没有共同负收益，而不是缺失回填。
    assert calc(clean.with_columns(pl.lit(-2.).alias('y')))[-1]==2 # 未中心化二阶矩，不是OLS。


def test_semibeta_calendar_and_downside_response():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return')
    r=FactorNode(op='div',args=(FactorNode(op='calendar_delta',period=1,args=(c,)),FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_negative_semibeta',window=20,args=(r,m))
    meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(7,4,20)
    cond=ObservableCondition(observation='公司与市场共同下跌的幅度',measurement='负收益乘积占市场全窗口平方和',response_test='market_downside_asymmetry')
    result=validate_construct(node,cond);assert result['status']=='基础检验符合',result
    days=np.delete(np.arange(100),55);prices=50*np.exp(.03*np.sin(days));market=.01*np.cos(days)
    frame=pl.DataFrame({'asset':['A']*99,'__market_session':days,'close':prices,'market_return':market})
    actual=frame.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy();lookup=dict(zip(days,prices,strict=True));returns=np.array([(p-lookup[d-1])/lookup[d-1] if d-1 in lookup else np.nan for d,p in zip(days,prices,strict=True)])
    expected=np.full(99,np.nan)
    for j in range(19,99):
        a=returns[j-19:j+1];b=market[j-19:j+1]
        if np.isfinite(a).all():expected[j]=sum(min(u,0)*min(v,0) for u,v in zip(a,b,strict=True))/sum(v*v for v in b)
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)


def test_hedging_semibeta_reuses_signed_input_without_new_operator():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return')
    r=FactorNode(op='div',args=(FactorNode(op='calendar_delta',period=1,args=(c,)),FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_negative_semibeta',window=20,args=(FactorNode(op='neg',args=(r,)),m))
    meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(8,5,20)
    condition=ObservableCondition(observation='市场下跌时个股上涨的保护贡献',measurement='混合符号半贝塔的正值',response_test='market_downside_asymmetry',expected_response='decrease')
    result=validate_construct(node,condition);assert result['status']=='基础检验符合',result
    returns=np.tile([.02,-.03,.01,-.02,.04],20);market=np.tile([-.01,-.02,.02,.01,-.03],20)
    prices=50*np.cumprod(1+returns)
    frame=pl.DataFrame({'asset':['A']*100,'__market_session':np.arange(100),'close':prices,'market_return':market})
    actual=frame.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    for j in range(20,100):
        y=returns[j-19:j+1];x=market[j-19:j+1]
        expected=sum(max(u,0)*max(-v,0) for u,v in zip(y,x,strict=True))/sum(v*v for v in x)
        assert abs(actual[j]-expected)<1e-12
