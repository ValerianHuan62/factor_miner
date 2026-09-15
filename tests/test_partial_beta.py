"""偏回归斜率必须对应独立多元OLS、保留单位且不跨证券或读取未来。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.errors import FactorMinerError
from factor_miner.construct_validation import ObservableCondition,validate_construct
from tests.test_dsl_semantics import semantic_registry

def node():return FactorNode(op='rolling_partial_beta',args=tuple(FactorNode(op='field',field=f) for f in ['x','y','z']),window=20)

class PartialBetaTest(unittest.TestCase):
    def test_ols_common_samples_groups_and_future(self):
        rng=np.random.default_rng(371);z=rng.normal(0,.02,180);y=-40*z+rng.normal(size=180);x=.007*y+.8*z+rng.normal(0,.01,180)
        x[30]=np.nan;y[59]=np.inf;z[105]=np.nan
        frame=pl.DataFrame(dict(asset=['A']*90+['B']*90,x=x,y=y,z=z))
        actual=frame.select(build_polars_expr(node()).alias('v'))['v'].to_numpy();expected=np.full(180,np.nan)
        for offset in [0,90]:
            for i in range(offset+19,offset+90):
                a=np.column_stack((x[i-19:i+1],y[i-19:i+1],z[i-19:i+1]))
                if np.isfinite(a).all():expected[i]=np.linalg.lstsq(np.column_stack((np.ones(20),a[:,1:])),a[:,0],rcond=None)[0][1]
        np.testing.assert_allclose(actual,expected,atol=1e-10,equal_nan=True)
        altered=frame.with_row_index().with_columns(pl.when(pl.col('index')>165).then(999.).otherwise(pl.col('x')).alias('x'))
        other=altered.select(build_polars_expr(node())).to_numpy().ravel()
        np.testing.assert_allclose(actual[:166],other[:166],equal_nan=True)
        scaled=frame.with_columns((pl.col('x')+3*pl.col('z')+5).alias('x'),(pl.col('y')*10).alias('y'))
        other=scaled.select(build_polars_expr(node())).to_numpy().ravel()
        np.testing.assert_allclose(actual/10,other,atol=1e-10,equal_nan=True)

    def test_constant_response_is_zero_but_collinear_regressors_fail(self):
        rng=np.random.default_rng(18);z=rng.normal(size=100);y=rng.normal(size=100)
        frame=pl.DataFrame(dict(asset=['A']*100,x=np.ones(100),y=y,z=z))
        np.testing.assert_allclose(frame.select(build_polars_expr(node())).tail(81).to_numpy(),0,atol=1e-10)
        for altered in [frame.with_columns((pl.col('z')*2).alias('y')),frame.with_columns(pl.lit(1.).alias('z'))]:
            self.assertTrue(altered.select(build_polars_expr(node()).alias('v'))['v'].is_null().all())

    def test_units_arity_and_hedging_response(self):
        p=FactorNode(op='field',field='price_close');v=FactorNode(op='field',field='traded_value')
        beta=FactorNode(op='rolling_partial_beta',args=(p,v,p),window=20)
        self.assertEqual(analyse_semantic_type(beta,semantic_registry()).unit_dimension,analyse_semantic_type(FactorNode(op='div',args=(p,v)),semantic_registry()).unit_dimension)
        self.assertEqual(analyse_semantic_type(beta.model_copy(update={'args':(p,v,p,v)}),semantic_registry()).unit_dimension,analyse_semantic_type(beta,semantic_registry()).unit_dimension)
        with self.assertRaises(FactorMinerError):validate_ast(node().model_copy(update={'args':node().args[:2]}),{'x','y','z'},())
        c=dict(op='field',field='close')
        expression=dict(op='rolling_partial_beta',args=[dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)]),dict(op='field',field='vix_change'),dict(op='field',field='market_return')],window=20)
        condition=ObservableCondition(observation='波动冲击',measurement='控制市场的VIX斜率',response_test='volatility_hedge')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='volume'),condition)['status'],'偏离')


class PartialBetaTwoControlsTest(unittest.TestCase):
    def test_independent_ols_missing_groups_scaling_and_future(self):
        rng=np.random.default_rng(1471);z=rng.normal(0,.02,400);w=-25*z+rng.normal(size=400);y=2*w+10*z+rng.normal(0,2,400);x=.007*y+.9*z+.03*w+rng.normal(0,.01,400)
        x[75]=np.nan;w[158]=np.inf;y[260]=np.nan
        expression=FactorNode(op='rolling_partial_beta',args=tuple(FactorNode(op='field',field=f) for f in ['x','y','z','w']),window=60)
        validate_ast(expression,{'x','y','z','w'},())
        frame=pl.DataFrame(dict(asset=['A']*200+['B']*200,x=x,y=y,z=z,w=w))
        actual=frame.select(build_polars_expr(expression)).to_numpy().ravel();expected=np.full(400,np.nan)
        for offset in [0,200]:
            for i in range(offset+59,offset+200):
                a=np.column_stack((x[i-59:i+1],y[i-59:i+1],z[i-59:i+1],w[i-59:i+1]))
                if np.isfinite(a).all():expected[i]=np.linalg.lstsq(np.column_stack((np.ones(60),a[:,1:])),a[:,0],rcond=None)[0][1]
        np.testing.assert_allclose(actual,expected,atol=1e-10,equal_nan=True)
        changed=frame.with_columns((pl.col('x')+4*pl.col('z')-3*pl.col('w')+2).alias('x'),(pl.col('y')*10).alias('y'),(pl.col('z')*100).alias('z'))
        np.testing.assert_allclose(actual/10,changed.select(build_polars_expr(expression)).to_numpy().ravel(),atol=1e-10,equal_nan=True)
        changed=frame.with_row_index().with_columns(pl.when(pl.col('index')>380).then(999.).otherwise(pl.col('w')).alias('w'))
        np.testing.assert_allclose(actual[:381],changed.select(build_polars_expr(expression)).to_numpy().ravel()[:381],equal_nan=True)
        finite=frame.fill_nan(0).with_columns(pl.when(pl.col('w').is_finite()).then(pl.col('w')).otherwise(0).alias('w'))
        for changed in [finite.with_columns(pl.lit(1.).alias('w')),finite.with_columns((pl.col('z')*2).alias('w')),finite.with_columns((pl.col('z')*2+pl.col('w')*3).alias('y'))]:
            self.assertTrue(changed.select(build_polars_expr(expression)).to_series().is_null().all())
        constant=finite.with_columns(pl.lit(1.).alias('x')).select(build_polars_expr(expression)).to_series().drop_nulls()
        np.testing.assert_allclose(constant.to_numpy(),0,atol=1e-10)
        with self.assertRaises(FactorMinerError):validate_ast(expression.model_copy(update={'args':expression.args+(expression.args[0],)}),{'x','y','z','w'},())

    def test_uncertainty_response(self):
        c=dict(op='field',field='close')
        expression=dict(op='rolling_partial_beta',args=[dict(op='div',args=[c,dict(op='calendar_delay',args=[c],period=1)]),dict(op='field',field='vvix_change'),dict(op='field',field='market_return'),dict(op='field',field='vix_change')],window=60)
        condition=ObservableCondition(observation='波动不确定性冲击',measurement='控制市场与VIX后的VVIX斜率',response_test='volatility_uncertainty_hedge')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='volume'),condition)['status'],'偏离')
