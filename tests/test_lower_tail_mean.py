"""左尾均值的独立排序、完整窗口及固定日历收益检验。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_tail_mean_sorted_reference_and_boundaries():
    rng=np.random.default_rng(3601);values=rng.normal(size=400);values[45]=np.nan;values[275]=np.inf
    frame=pl.DataFrame({'asset':['A']*200+['B']*200,'x':values})
    for window in [5,20,40,60,120]:
        node=FactorNode(op='rolling_lower_tail_mean',window=window,args=(FactorNode(op='field',field='x'),))
        def calc(data):return data.with_columns(build_polars_expr(node).alias('y'))['y'].to_numpy()
        expected=np.full(400,np.nan)
        for start in [0,200]:
            for j in range(start+window-1,start+200):
                part=values[j-window+1:j+1]
                if np.isfinite(part).all():expected[j]=np.sort(part)[:int(np.ceil(window*.05))].mean()
        np.testing.assert_allclose(calc(frame),expected,atol=1e-14,equal_nan=True)
        np.testing.assert_allclose(calc(frame.with_columns((pl.col('x')*3+2).alias('x'))),expected*3+2,atol=1e-14,equal_nan=True)
        future=frame.with_row_index().with_columns(pl.when(pl.col('index')>350).then(-999.).otherwise(pl.col('x')).alias('x'))
        np.testing.assert_array_equal(calc(frame)[:351],calc(future)[:351])
    # 窗口40只取2个最小值；边界并列不扩为3个。
    tied=pl.DataFrame({'asset':['A']*40,'x':[1.,2.,2.]+[9.]*37})
    node=FactorNode(op='rolling_lower_tail_mean',window=40,args=(FactorNode(op='field',field='x'),))
    assert tied.select(build_polars_expr(node)).item(-1,0)==1.5
    assert tied.with_columns(pl.lit(2.).alias('x')).select(build_polars_expr(node)).item(-1,0)==2.


def test_tail_return_calendar_and_construct():
    c=FactorNode(op='field',field='close');r=FactorNode(op='div',args=(c,FactorNode(op='calendar_delay',period=1,args=(c,))))
    node=FactorNode(op='rolling_lower_tail_mean',window=120,args=(r,))
    meta=validate_ast(node,{'close'},());assert (meta.node_count,meta.depth,meta.lookback)==(5,4,120)
    sessions=np.delete(np.arange(400),180);prices=40*np.exp(.003*sessions+.04*np.sin(sessions))
    frame=pl.DataFrame({'asset':['A']*399,'__market_session':sessions,'close':prices})
    actual=frame.with_columns(build_polars_expr(node).alias('y'))['y'].to_numpy()
    lookup=dict(zip(sessions,prices,strict=True));r=np.array([p/lookup[s-1] if s-1 in lookup else np.nan for s,p in zip(sessions,prices,strict=True)])
    expected=np.full(len(prices),np.nan)
    for j in range(119,len(prices)):
        part=r[j-119:j+1]
        if np.isfinite(part).all():expected[j]=np.mean(sorted(part)[:6])
    np.testing.assert_allclose(actual,expected,atol=1e-14,equal_nan=True)
    condition=ObservableCondition(observation='持续较差的日收益',measurement='120观察最小6个毛收益的均值',response_test='negative_close_jump',expected_response='decrease')
    result=validate_construct(node,condition);assert result['status']=='基础检验符合',result
