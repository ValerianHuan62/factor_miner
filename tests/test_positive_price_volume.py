"""正向量价测量的路径、尺度与未来边界反例。"""
from datetime import date,timedelta
import numpy as np
import polars as pl
import pytest
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.compiler import attach_market_sessions,build_polars_expr
from factor_miner.construct_validation import ObservableCondition,validate_construct


def expressions(w):
    def f(k):return dict(op='field',field=k)
    def n(op,*args,**kw):return dict(op=op,args=list(args),**kw)
    change=n('calendar_delta',f('close'),period=1)
    progress=n('div',n('calendar_delta',f('close'),period=w),n('rolling_std',change,window=w))
    volume=n('div',n('rolling_cov',change,f('volume'),window=w),n('mul',n('rolling_mean',f('close'),window=w),n('rolling_mean',f('volume'),window=w)))
    return progress,volume


@pytest.mark.parametrize('w',[20,60,120])
def test_scale_direction_and_future_invariance(w):
    index=np.arange(300);days=[date(2000,1,1)+timedelta(days=int(i)) for i in index]
    panel=pl.DataFrame(dict(date=days,asset=['A']*300,close=50*np.exp(.001*index+.01*np.sin(.73*index)),volume=1e6*(1+.2*np.sin(.41*index))))
    for expr,response in zip(expressions(w),['trend','return_volume_coupling']):
        node=FactorNode.model_validate(expr);meta=validate_ast(node,{'close','volume'},());assert meta.depth<=5 and meta.node_count<=15
        def values(frame):return attach_market_sessions(frame.lazy(),pl.DataFrame({'date':days})).select(build_polars_expr(node)).collect()[:,0].to_numpy()
        original=values(panel)
        np.testing.assert_allclose(values(panel.with_columns(pl.col('close')*10,pl.col('volume')/10)),original,equal_nan=True,atol=1e-10)
        changed=panel.with_columns(pl.when(pl.col('date')>days[250]).then(pl.col('close')*2).otherwise(pl.col('close')).alias('close'))
        np.testing.assert_allclose(values(changed)[:251],original[:251],equal_nan=True,atol=1e-10)
        condition=ObservableCondition(observation='冻结路径与成交响应',measurement='无量纲正向测量',response_test=response)
        assert validate_construct(expr,condition)['status']=='基础检验符合'


def test_gradual_progress_exceeds_one_jump_at_same_endpoint():
    """净涨幅相同，集中一次跳跃的变化离散度应比平滑进展更大。"""
    days=[date(2000,1,1)+timedelta(days=i) for i in range(61)]
    noise=.01*np.sin(np.arange(61));noise[-1]=noise[0]=0.
    smooth=np.linspace(50,60,61)+noise;jump=np.r_[np.full(60,50.),60.]+noise
    node=FactorNode.model_validate(expressions(60)[0])
    def last(prices):
        p=pl.DataFrame(dict(date=days,asset=['A']*61,close=prices))
        return attach_market_sessions(p.lazy(),pl.DataFrame({'date':days})).select(build_polars_expr(node)).collect()[-1,0]
    assert last(smooth)>last(jump)>0
