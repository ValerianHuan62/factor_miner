"""市场分段斜率测量的金融含义、完整窗口和不可识别反例。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition,validate_construct
from factor_miner.dsl import validate_ast

def expression():
    c=dict(op='field',field='close');m=dict(op='field',field='market_return')
    return dict(op='rolling_partial_beta',args=[dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)]),dict(op='sub',args=[m,dict(op='abs',args=[m])]),m],window=120)

class DownsideAsymmetryTest(unittest.TestCase):
    def test_known_slope_difference_and_ordinary_beta_invariance(self):
        rng=np.random.default_rng(914);m=rng.normal(0,.005,500);r=.001+1.3*m+.4*(m-np.abs(m));close=50*np.cumprod(1+r)
        frame=pl.DataFrame(dict(asset=['A']*500,__market_session=np.arange(500),close=close,market_return=m))
        node=FactorNode.model_validate(expression());validate_ast(node,{'close','market_return'},())
        actual=frame.select(build_polars_expr(node)).to_series().to_numpy()
        np.testing.assert_allclose(actual[120:],.4,atol=1e-9)
        # 普通市场beta增加不会改变分段斜率差，单独增加下跌beta一单位应增加测量0.5。
        for extra,expected in [(2*m,.4),(np.minimum(m,0),.9)]:
            changed=frame.with_columns(pl.Series('close',50*np.cumprod(1+r+extra)))
            np.testing.assert_allclose(changed.select(build_polars_expr(node)).to_series().to_numpy()[120:],expected,atol=1e-9)
        for only_one_side in [np.abs(m),-np.abs(m)]:
            changed=frame.with_columns(pl.Series('market_return',only_one_side))
            self.assertTrue(changed.select(build_polars_expr(node)).to_series().is_null().all())

    def test_observable_condition_and_unrelated_proxy(self):
        condition=ObservableCondition(observation='下跌相对上涨的市场斜率增加',measurement='分段斜率差的一半',response_test='market_downside_asymmetry')
        self.assertEqual(validate_construct(expression(),condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='volume'),condition)['status'],'偏离')
