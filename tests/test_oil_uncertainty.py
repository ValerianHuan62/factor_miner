"""控制同期市场与VIX后的石油ETF波动变化载荷。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition,validate_construct


def test_oil_loading_independent_ols_and_construct():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return');v=FactorNode(op='field',field='vix_change');o=FactorNode(op='field',field='ovx_change')
    r=FactorNode(op='div',args=(c,FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_partial_beta',window=60,args=(r,o,m,v))
    meta=validate_ast(node,{'close','market_return','vix_change','ovx_change'},());assert (meta.node_count,meta.depth,meta.lookback)==(8,4,60)
    rng=np.random.default_rng(3901);ov=rng.normal(0,1,400);vv=rng.normal(0,1,400);mv=rng.normal(0,.01,400);y=.003*ov+.002*vv+.7*mv+rng.normal(0,.001,400);y[0]=0
    prices=50*np.cumprod(1+y);ov[120]=np.nan
    frame=pl.DataFrame({'asset':['A']*400,'__market_session':np.arange(400),'close':prices,'market_return':mv,'vix_change':vv,'ovx_change':ov})
    actual=frame.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy();expected=np.full(400,np.nan)
    for j in range(60,400):
        x=np.column_stack((np.ones(60),ov[j-59:j+1],mv[j-59:j+1],vv[j-59:j+1]))
        if np.isfinite(x).all():expected[j]=np.linalg.lstsq(x,prices[j-59:j+1]/prices[j-60:j],rcond=None)[0][1]
    np.testing.assert_allclose(actual,expected,atol=1e-10,equal_nan=True)
    condition=ObservableCondition(observation='市场与VIX以外的石油ETF波动敏感度',measurement='OVX点数变化载荷',response_test='oil_uncertainty_hedge')
    result=validate_construct(node,condition);assert result['status']=='基础检验符合',result
