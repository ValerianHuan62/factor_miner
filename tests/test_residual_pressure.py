"""独立逐窗OLS末端残差及不同滚动窗口的近期平均，不把同窗残差均值当信号。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_last_residual_independent_ols_and_invariances():
    rng=np.random.default_rng(3501);x=rng.normal(size=260);y=2+.7*x+rng.normal(0,.1,260)
    y[40]=np.nan;x[171]=np.inf
    frame=pl.DataFrame({'asset':['A']*130+['B']*130,'y':y,'x':x})
    node=FactorNode(op='rolling_residual_last',window=20,args=(FactorNode(op='field',field='y'),FactorNode(op='field',field='x')))
    def calc(data):return data.with_columns(build_polars_expr(node).alias('e'))['e'].to_numpy()
    expected=np.full(260,np.nan)
    for start in [0,130]:
        for j in range(start+19,start+130):
            yw=y[j-19:j+1];xw=x[j-19:j+1]
            if np.isfinite(yw).all() and np.isfinite(xw).all() and np.ptp(xw)>0:
                design=np.column_stack((np.ones(20),xw));beta=np.linalg.lstsq(design,yw,rcond=None)[0]
                expected[j]=yw[-1]-design[-1]@beta
    actual=calc(frame);np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    np.testing.assert_allclose(actual*3,calc(frame.with_columns((pl.col('y')*3+2).alias('y'))),atol=1e-12,equal_nan=True)
    # 仅有效x时移除已知市场成分，残差应不变。
    altered=frame.with_columns((pl.col('y')+2*pl.col('x')).alias('y'))
    np.testing.assert_allclose(actual,calc(altered),atol=1e-12,equal_nan=True)
    future=frame.with_row_index().with_columns(pl.when(pl.col('index')>235).then(99.).otherwise(pl.col('y')).alias('y'))
    np.testing.assert_array_equal(actual[:236],calc(future)[:236])
    assert np.isnan(calc(frame.with_columns(pl.lit(2.).alias('x')))).all()


def test_residual_pressure_window_identity_and_construct():
    c=FactorNode(op='field',field='close');m=FactorNode(op='field',field='market_return')
    gross=FactorNode(op='div',args=(c,FactorNode(op='calendar_delay',period=1,args=(c,))))
    residual=FactorNode(op='rolling_residual_last',window=60,args=(gross,m))
    node=FactorNode(op='rolling_mean',window=20,args=(residual,))
    meta=validate_ast(node,{'close','market_return'},());assert (meta.node_count,meta.depth,meta.lookback)==(7,5,79)
    rng=np.random.default_rng(3502);mvalues=rng.normal(0,.01,400);r=.7*mvalues+np.arange(400)*.00001+rng.normal(0,.001,400);r[0]=0
    prices=50*np.cumprod(1+r)
    frame=pl.DataFrame({'asset':['A']*400,'__market_session':np.arange(400),'close':prices,'market_return':mvalues})
    actual=frame.with_columns(build_polars_expr(node).alias('x'))['x'].to_numpy()
    last=np.full(400,np.nan)
    for j in range(60,400):
        y=prices[j-59:j+1]/prices[j-60:j];x=np.column_stack((np.ones(60),mvalues[j-59:j+1]))
        last[j]=y[-1]-x[-1]@np.linalg.lstsq(x,y,rcond=None)[0]
    expected=np.full(400,np.nan)
    for j in range(79,400):expected[j]=last[j-19:j+1].mean()
    np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    assert np.mean(actual[79:])>0 # 同窗OLS所有残差均值为0，但不同窗末残差可累计。
    condition=ObservableCondition(observation='市场以外的近期压力',measurement='滚动末残差的20观察均值',response_test='residual_pressure')
    result=validate_construct(node,condition);assert result['status']=='基础检验符合',result
