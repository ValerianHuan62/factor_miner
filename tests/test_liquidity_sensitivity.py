"""价格冲击代理的市场方向敏感度必须保持单位和独立测量一致。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition,validate_construct
from factor_miner.dsl import validate_ast

def expression():
    def f(name):return dict(op='field',field=name)
    def n(op,*args,**kw):return dict(op=op,args=list(args),**kw)
    c=f('close');m=f('market_return')
    impact=n('div',n('abs',n('calendar_delta',c,period=1)),n('mul',n('calendar_delay',c,period=1),n('mul',c,f('volume'))))
    return n('rolling_partial_beta',impact,m,n('abs',m),window=120)

class LiquiditySensitivityTest(unittest.TestCase):
    def test_known_cost_slope_shares_zero_and_missing(self):
        rng=np.random.default_rng(633);m=rng.normal(0,.01,400);r=.001+.0005*np.sin(np.arange(400)*.7)
        close=50*np.cumprod(1+r);impact=3e-8-1e-6*m+2e-6*np.abs(m);volume=np.abs(r)/(impact*close)
        frame=pl.DataFrame(dict(asset=['A']*400,__market_session=np.arange(400),close=close,volume=volume,market_return=m))
        node=FactorNode.model_validate(expression());meta=validate_ast(node,{'close','volume','market_return'},())
        self.assertLessEqual(meta.depth,5);self.assertLessEqual(meta.node_count,15);self.assertEqual(meta.lookback,120)
        actual=frame.select(build_polars_expr(node)).to_series().to_numpy();np.testing.assert_allclose(actual[120:],-1e-6,atol=1e-15)
        rebased=frame.with_columns((pl.col('close')*10).alias('close'),(pl.col('volume')/10).alias('volume'))
        np.testing.assert_allclose(rebased.select(build_polars_expr(node)).to_series().to_numpy(),actual,atol=1e-15,equal_nan=True)
        invalid=frame.with_row_index().with_columns(pl.when(pl.col('index')==160).then(0.).otherwise(pl.col('volume')).alias('volume'))
        values=invalid.select(build_polars_expr(node)).to_series().to_numpy();self.assertTrue(np.isnan(values[160:280]).all())
        missing=frame.filter(pl.col('__market_session')!=160)
        values=missing.select(build_polars_expr(node)).to_series().to_numpy();self.assertTrue(np.isnan(values[160:280]).all())

    def test_bad_market_activity_response_and_unrelated_formula(self):
        condition=ObservableCondition(observation='坏市场成交收缩',measurement='控制市场绝对幅度的价格冲击敏感度',response_test='liquidity_bad_market',expected_response='decrease')
        self.assertEqual(validate_construct(expression(),condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='close'),condition)['status'],'偏离')
