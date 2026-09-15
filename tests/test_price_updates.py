"""报价更新频率只测实际观察行的精确价格变化，不填补缺失日。"""
import unittest
import numpy as np
import polars as pl
from factor_miner.compiler import build_polars_expr
from factor_miner.schema import FactorNode
from factor_miner.construct_validation import ObservableCondition,validate_construct

def expression(window=60):
    return dict(op='rolling_mean',window=window,args=[dict(op='abs',args=[dict(op='sign',args=[dict(op='delta',period=1,args=[dict(op='field',field='close')])])])])

class PriceUpdatesTest(unittest.TestCase):
    def test_updates_ignore_magnitude_and_preserve_nulls(self):
        # 第二个证券独立预热；无报价的日期没有被插入价格或当成平盘。
        values=[10.,10.,11.,11.,11.,12.,np.nan,12.,12.,13.,13.,14.]
        frame=pl.DataFrame(dict(asset=['A']*12+['B']*12,close=values+values))
        actual=frame.select(build_polars_expr(FactorNode.model_validate(expression(5))).alias('v'))['v'].to_numpy()
        expected=np.full(24,np.nan)
        for offset in [0,12]:
            for j in range(offset+5,offset+12):
                x=np.array((values+values)[j-5:j+1])
                if np.isfinite(x).all():expected[j]=np.count_nonzero(np.diff(x))/5
        np.testing.assert_allclose(actual,expected,equal_nan=True)
        changed=frame.with_columns((pl.col('close')*10).alias('close')).select(build_polars_expr(FactorNode.model_validate(expression(5))))
        np.testing.assert_allclose(actual,changed.to_numpy().ravel(),equal_nan=True)

    def test_less_frequent_price_updates_reduce_proxy(self):
        condition=ObservableCondition(observation='报价更新',measurement='相邻收盘变化占比',response_test='price_updates',expected_response='decrease')
        self.assertEqual(validate_construct(expression(),condition)['status'],'基础检验符合')
        self.assertEqual(validate_construct(dict(op='field',field='volume'),condition)['status'],'偏离')
