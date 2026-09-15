"""两日高低收盘价差代理，时间可得性和零值修正分离。"""
import math
import numpy as np
import polars as pl
import pytest
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.errors import FactorMinerError
from factor_miner.construct_validation import ObservableCondition,validate_construct
from tests.test_dsl_semantics import semantic_registry


def candidate():
    def field(name):return FactorNode(op='field',field=name)
    def lag(name):return FactorNode(op='calendar_delay',period=1,args=(field(name),))
    point=FactorNode(op='hlc_spread',args=(lag('close'),lag('high'),lag('low'),field('high'),field('low')))
    return FactorNode(op='rolling_mean',window=20,args=(point,))


def test_independent_logprice_formula_calendar_and_future():
    rng=np.random.default_rng(4201);n=180;mid=100*np.exp(np.cumsum(rng.normal(0,.01,n)))
    high=mid*np.exp(.02);low=mid*np.exp(-.02);close=mid*np.exp(rng.uniform(-.019,.019,n))
    days=np.delete(np.arange(n),45);high=high[days];low=low[days];close=close[days]
    high[72]=np.nan;low[120]=high[120]
    frame=pl.DataFrame(dict(asset=['A']*len(days),__market_session=days,close=close,high=high,low=low))
    daily=np.full(len(days),np.nan)
    for j in range(1,len(days)):
        if days[j]-days[j-1]!=1:continue
        values=[close[j-1],high[j-1],low[j-1],high[j],low[j]]
        if not all(math.isfinite(v) and v>0 for v in values):continue
        cp,hp,lp,h,l=values
        if not(hp>lp and h>l and lp<=cp<=hp):continue
        previous_mid=(math.log(hp)+math.log(lp))/2;current_mid=(math.log(h)+math.log(l))/2
        daily[j]=2*math.sqrt(max((math.log(cp)-previous_mid)*(math.log(cp)-current_mid),0))
    expected=np.full(len(days),np.nan)
    for j in range(19,len(days)):
        if np.isfinite(daily[j-19:j+1]).all():expected[j]=sum(daily[j-19:j+1])/20
    node=candidate()
    def calc(data):return data.with_columns(build_polars_expr(node).alias('z'))['z'].to_numpy()
    actual=calc(frame);np.testing.assert_allclose(actual,expected,atol=1e-12,equal_nan=True)
    rebased=frame.with_columns(*(pl.col(k)*10 for k in ['close','high','low']))
    np.testing.assert_allclose(calc(rebased),actual,atol=1e-12,equal_nan=True)
    changed=frame.with_row_index().with_columns(*(pl.when(pl.col('index')>150).then(pl.col(k)*2).otherwise(pl.col(k)).alias(k) for k in ['close','high','low']))
    np.testing.assert_array_equal(calc(changed)[:151],actual[:151])
    doubled=pl.concat([frame,frame.with_columns(pl.lit('B').alias('asset'))]);np.testing.assert_array_equal(calc(doubled),np.tile(actual,2))


def test_truncation_not_missing_fill_and_midpoint_counterexample():
    def f(x):return FactorNode(op='field',field=x)
    node=FactorNode(op='hlc_spread',args=tuple(f(k) for k in ['pc','ph','pl','h','l']))
    data=pl.DataFrame(dict(pc=[101.,101.,None,101.,101.],ph=[103.]*5,pl=[97.]*5,h=[103.,107.,103.,0.,102.],l=[97.,103.,97.,0.,102.]))
    actual=data.with_columns(build_polars_expr(node).alias('z'))['z'].to_list()
    assert actual[0]>0 and actual[1]==0 and actual[2:]==[None,None,None]
    # 固定收盘偏离，扩大对称对数区间并不改变区间中点或价差代理。
    a=pl.DataFrame(dict(pc=[100*np.exp(.01)],ph=[110.],pl=[10000/110],h=[110.],l=[10000/110]))
    b=a.with_columns(pl.lit(120.).alias('ph'),pl.lit(10000/120).alias('pl'),pl.lit(120.).alias('h'),pl.lit(10000/120).alias('l'))
    for frame in [a,b]:assert abs(frame.with_columns(build_polars_expr(node).alias('z'))['z'][0]-.02)<1e-12


def test_ast_construct_and_price_units():
    node=candidate();meta=validate_ast(node,{'close','high','low'},());assert (meta.node_count,meta.depth,meta.lookback)==(10,4,20)
    result=validate_construct(node,ObservableCondition(observation='区间中点不变时收盘价偏离增大',measurement='两日高低收盘价差代理',response_test='closing_quote_displacement'))
    assert result['status']=='基础检验符合',result
    registry=semantic_registry();price=FactorNode(op='field',field='price_close');amount=FactorNode(op='field',field='traded_value')
    assert analyse_semantic_type(FactorNode(op='hlc_spread',args=(price,)*5),registry).unit_dimension=='dimensionless'
    with pytest.raises(FactorMinerError,match='DSL_TYPE_ERROR'):analyse_semantic_type(FactorNode(op='hlc_spread',args=(price,price,price,price,amount)),registry)
    with pytest.raises(FactorMinerError):validate_ast(FactorNode(op='hlc_spread',args=(price,)*4),{'price_close'},())
