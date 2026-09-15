"""市场平方斜率须分离线性暴露，并符合显式OLS及冻结构念。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.construct_validation import ObservableCondition, validate_construct


class MarketQuadraticExposureTest(unittest.TestCase):
    def test_asymmetric_market_linear_and_quadratic_components(self):
        rng = np.random.default_rng(1818)
        market = (rng.exponential(size=300) - 1) * .012
        expression = FactorNode.model_validate(dict(op='rolling_partial_beta', args=[
            dict(op='field', field='stock'), dict(op='mul', args=[
                dict(op='field', field='market'), dict(op='field', field='market')]),
            dict(op='field', field='market')], window=120))
        for curvature in [0., -12., 25.]:
            stock = .001 + 1.8 * market + curvature * market**2
            frame = pl.DataFrame(dict(asset=['A'] * 300, stock=stock, market=market))
            actual = frame.select(build_polars_expr(expression)).to_series().to_numpy()
            self.assertTrue(np.isnan(actual[:119]).all())
            np.testing.assert_allclose(actual[119:], curvature, atol=1e-9)
            # 纯线性市场关系在非对称市场分布下也会有平方相关；偏斜率应仍为零。
            if curvature == 0.:
                self.assertGreater(abs(np.corrcoef(stock, market**2)[0, 1]), .5)
            changed = frame.with_columns((pl.col('stock') + 4 * pl.col('market') + 1).alias('stock'))
            np.testing.assert_allclose(changed.select(build_polars_expr(expression)).to_series().to_numpy(),
                actual, atol=1e-8, equal_nan=True)
            reference = np.linalg.lstsq(np.column_stack((np.ones(120), market[-120:]**2, market[-120:])), stock[-120:], rcond=None)[0][1]
            self.assertAlmostEqual(actual[-1], reference, places=8)

    def test_actual_candidate_construct(self):
        c = dict(op='field', field='close'); m = dict(op='field', field='market_return')
        expression = dict(op='rolling_partial_beta', args=[dict(op='div', args=[c,
            dict(op='calendar_delay', args=[c], period=1)]), dict(op='mul', args=[m, m]), m], window=120)
        condition = ObservableCondition(observation='同期线性暴露以外的市场平方响应',
            measurement='完整窗口带截距平方系数', response_test='market_curvature')
        self.assertEqual(validate_construct(expression, condition)['status'], '基础检验符合')


if __name__ == '__main__':
    unittest.main()
