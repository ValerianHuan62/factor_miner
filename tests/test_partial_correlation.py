"""偏相关必须等价于独立残差回归，且不能跨证券或读取未来。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.errors import FactorMinerError
from factor_miner.schema import FactorNode
from factor_miner.construct_validation import ObservableCondition, validate_construct


def node():
    return FactorNode(op='rolling_partial_corr', args=tuple(FactorNode(op='field',field=f) for f in ['x','y','z']), window=20)


class PartialCorrelationTest(unittest.TestCase):
    def test_independent_regressions_common_window_grouping_and_future(self):
        rng=np.random.default_rng(703)
        z=rng.normal(size=180); y=.6*z+rng.normal(size=180); x=2*z+.3*y+rng.normal(size=180)
        x[30]=np.nan; y[52]=np.inf; z[96]=np.nan
        frame=pl.DataFrame(dict(asset=['A']*90+['B']*90,x=x,y=y,z=z))
        actual=frame.with_columns(build_polars_expr(node()).alias('v'))['v'].to_numpy()
        expected=np.full(180,np.nan)
        for offset in [0,90]:
            for i in range(offset+19,offset+90):
                a=np.column_stack((x[i-19:i+1],y[i-19:i+1],z[i-19:i+1]))
                if not np.isfinite(a).all():continue
                controls=np.column_stack((np.ones(20),a[:,2]))
                residual=a[:,:2]-controls@np.linalg.lstsq(controls,a[:,:2],rcond=None)[0]
                expected[i]=np.corrcoef(residual.T)[0,1]
        np.testing.assert_allclose(actual,expected,rtol=1e-9,atol=1e-10,equal_nan=True)
        changed=frame.with_row_index().with_columns(pl.when(pl.col('index')>160).then(1000.).otherwise(pl.col('x')).alias('x'))
        other=changed.with_columns(build_polars_expr(node()).alias('v'))['v'].to_numpy()
        np.testing.assert_allclose(actual[:161],other[:161],equal_nan=True)

    def test_constant_and_collinear_inputs_are_unidentifiable(self):
        rng=np.random.default_rng(12);z=rng.normal(size=100);y=rng.normal(size=100)
        for x,control in [(2*z,z),(np.ones(100),z),(y,np.ones(100))]:
            frame=pl.DataFrame(dict(asset=['A']*100,x=x,y=y,z=control))
            self.assertTrue(frame.select(build_polars_expr(node()).alias('v'))['v'].is_null().all())

    def test_controls_remove_common_exposure_without_removing_lag_response(self):
        close=dict(op='field',field='close');market=dict(op='field',field='market_return')
        expression=dict(op='rolling_partial_corr',args=[dict(op='div',args=[close,dict(op='calendar_delay',args=[close],period=1)]),
            dict(op='calendar_delay',args=[market],period=1),market],window=120)
        condition=ObservableCondition(observation='延迟响应',measurement='控制同期后的次日偏相关',response_test='market_delay')
        self.assertEqual(validate_construct(expression,condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='volume'),condition)['status'],'偏离')
        metadata=validate_ast(FactorNode.model_validate(expression),{'close','market_return'},())
        self.assertEqual(metadata.lookback,120)
        self.assertEqual(metadata.node_count,8)
        # 控制变量的任意线性暴露不应改变偏相关。
        rng=np.random.default_rng(78);z=rng.normal(size=100);y=rng.normal(size=100);x=.2*y+rng.normal(size=100)
        frame=pl.DataFrame(dict(asset=['A']*100,x=x,y=y,z=z))
        actual=frame.select(build_polars_expr(node()))
        other=frame.with_columns((pl.col('x')+3*pl.col('z')+5).alias('x')).select(build_polars_expr(node()))
        np.testing.assert_allclose(actual.to_numpy(),other.to_numpy(),atol=1e-10,equal_nan=True)

    def test_arity_and_causal_parameters_rejected(self):
        for invalid in [node().model_copy(update={'args':node().args[:2]}),node().model_copy(update={'center':True}),
                        node().model_copy(update={'window':19})]:
            with self.assertRaises(FactorMinerError):validate_ast(invalid,{'x','y','z'},())
