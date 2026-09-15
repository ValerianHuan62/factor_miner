"""复用共同负二阶矩的下降变化占比，不新增候选专属算子。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition,validate_construct


def test_downside_fraction_reference_and_construct():
    c=FactorNode(op='field',field='close')
    r=FactorNode(op='div',args=(FactorNode(op='calendar_delta',period=1,args=(c,)),FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_negative_semibeta',window=20,args=(r,r))
    meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(11,4,20)
    response=validate_construct(node,ObservableCondition(observation='近期负收益平方在全部变化中的占比',measurement='下行二阶矩份额',response_test='negative_close_jump'))
    assert response['status']=='基础检验符合',response
    rng=np.random.default_rng(4101);returns=rng.normal(.001,.02,300);close=50*np.cumprod(1+returns)
    frame=pl.DataFrame(dict(asset=['A']*300,close=close,__market_session=np.arange(300)))
    actual=frame.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    expected=np.full(300,np.nan)
    for j in range(20,300):
        window=[(close[i]-close[i-1])/close[i-1] for i in range(j-19,j+1)]
        negative=sum(v*v for v in window if v<0);positive=sum(v*v for v in window if v>0)
        expected[j]=negative/(negative+positive)
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    assert np.nanmin(actual)>=0 and np.nanmax(actual)<=1
    constant=frame.with_columns(pl.lit(50.).alias('close')).with_columns(build_polars_expr(node).alias('z'))
    assert constant['z'].null_count()==300
    for path,target in [(50*np.power(1.01,np.arange(300)),0),(50*np.power(.99,np.arange(300)),1)]:
        value=frame.with_columns(pl.Series('close',path)).with_columns(build_polars_expr(node).alias('z'))['z'].tail(200).to_numpy()
        np.testing.assert_allclose(value,target,atol=1e-12)
