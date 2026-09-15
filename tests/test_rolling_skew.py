"""偏度按完整历史窗口和固定样本修正计算，不以最大涨幅代替。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.construct_validation import ObservableCondition,validate_construct
from tests.test_dsl_semantics import semantic_registry

def reference(x):
    n=len(x);centered=x-x.mean();m2=np.mean(centered**2)
    return np.sqrt(n*(n-1))/(n-2)*np.mean(centered**3)/m2**1.5 if np.isfinite(x).all() and m2>0 else np.nan

class RollingSkewTest(unittest.TestCase):
    def test_independent_centered_moments_groups_missing_and_affine(self):
        rng=np.random.default_rng(3761);x=rng.lognormal(0,.5,360);x[29]=np.nan;x[170]=np.inf
        frame=pl.DataFrame(dict(asset=['A']*180+['B']*180,x=x))
        node=FactorNode(op='rolling_skew',args=(FactorNode(op='field',field='x'),),window=20);validate_ast(node,{'x'},())
        actual=frame.select(build_polars_expr(node)).to_series().to_numpy();expected=np.full(360,np.nan)
        for offset in [0,180]:
            for j in range(offset+19,offset+180):
                window=x[j-19:j+1]
                if np.isfinite(window).all():expected[j]=reference(window)
        np.testing.assert_allclose(actual,expected,atol=1e-9,equal_nan=True)
        for multiplier in [10.,-10.]:
            changed=frame.with_columns((pl.col('x')*multiplier+3).alias('x'))
            np.testing.assert_allclose(changed.select(build_polars_expr(node)).to_series().to_numpy(),actual*np.sign(multiplier),atol=1e-9,equal_nan=True)
        changed=frame.with_row_index().with_columns(pl.when(pl.col('index')>345).then(999.).otherwise(pl.col('x')).alias('x'))
        np.testing.assert_allclose(changed.select(build_polars_expr(node)).to_series().to_numpy()[:346],actual[:346],equal_nan=True)
        constant=frame.with_columns(pl.lit(1.).alias('x'));self.assertTrue(constant.select(build_polars_expr(node)).to_series().is_null().all())

    def test_same_maximum_different_shape_and_units(self):
        node=FactorNode(op='rolling_skew',args=(FactorNode(op='field',field='x'),),window=120)
        symmetric=np.tile([-1.,1.],60);asymmetric=np.r_[np.full(119,-.1),1.]
        self.assertEqual(symmetric.max(),asymmetric.max())
        result=pl.DataFrame(dict(asset=['A']*120+['B']*120,x=np.r_[symmetric,asymmetric])).select(build_polars_expr(node)).to_series().to_numpy()
        self.assertAlmostEqual(result[119],0.);self.assertGreater(result[-1],10.)
        price=FactorNode(op='field',field='price_close')
        self.assertEqual(analyse_semantic_type(FactorNode(op='rolling_skew',args=(price,),window=20),semantic_registry()).unit_dimension,'dimensionless')

    def test_positive_price_jump_response(self):
        c=dict(op='field',field='close')
        expression=dict(op='rolling_skew',args=[dict(op='div',args=[dict(op='calendar_delta',args=[c],period=1),dict(op='calendar_delay',args=[c],period=1)])],window=120)
        condition=ObservableCondition(observation='少量较大正收益改变收益分布右尾',measurement='调整后的样本偏度',response_test='positive_close_jump')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
