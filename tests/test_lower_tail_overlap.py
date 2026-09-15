"""共同严重损失频率，独立集合参考与边界识别。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition,validate_construct


def reference(x,y):
    if not (np.isfinite(x).all() and np.isfinite(y).all()):return np.nan
    k=int(np.ceil(len(x)/20));a=sorted(range(len(x)),key=lambda i:x[i]);b=sorted(range(len(y)),key=lambda i:y[i])
    if x[a[k-1]]==x[a[k]] or y[b[k-1]]==y[b[k]]:return np.nan
    return len(set(a[:k])&set(b[:k]))/k


def test_independent_intersection_missing_groups_and_monotone_invariance():
    rng=np.random.default_rng(3701);x=rng.normal(size=600);y=.5*x+rng.normal(size=600);x[55]=np.nan;y[433]=np.inf
    frame=pl.DataFrame({'asset':['A']*300+['B']*300,'x':x,'y':y})
    node=FactorNode(op='rolling_lower_tail_overlap',window=120,args=(FactorNode(op='field',field='x'),FactorNode(op='field',field='y')))
    def calc(data):return data.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    expected=np.full(600,np.nan)
    for start in [0,300]:
        for j in range(start+119,start+300):expected[j]=reference(x[j-119:j+1],y[j-119:j+1])
    actual=calc(frame);np.testing.assert_allclose(actual,expected,equal_nan=True)
    np.testing.assert_array_equal(actual,calc(frame.with_columns((pl.col('x')*3+2).alias('x'),pl.col('y').exp().alias('y'))))
    future=frame.with_row_index().with_columns(pl.when(pl.col('index')>550).then(-999.).otherwise(pl.col('x')).alias('x'))
    np.testing.assert_array_equal(actual[:551],calc(future)[:551])
    assert np.isnan(calc(frame.with_columns(pl.lit(1.).alias('x')))).all()
    # 边界并列拒绝，尾部集合内部的并列但边界唯一可以计算。
    clean=pl.DataFrame({'asset':['A']*120,'x':np.arange(120,dtype=float),'y':np.arange(120,dtype=float)})
    assert calc(clean)[-1]==1.
    assert calc(clean.with_columns((-pl.col('y')).alias('y')))[-1]==0.
    tied=clean.with_columns(pl.when(pl.col('x')==6).then(5.).otherwise(pl.col('x')).alias('x'))
    assert np.isnan(calc(tied)[-1])


def test_joint_tail_construct_and_ast():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return')
    r=FactorNode(op='div',args=(c,FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_lower_tail_overlap',window=120,args=(r,m))
    meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(6,4,120)
    condition=ObservableCondition(observation='公司和市场的严重损失同时发生',measurement='各自最差6观察的交集占比',response_test='joint_tail_events')
    result=validate_construct(node,condition);assert result['status']=='基础检验符合',result
