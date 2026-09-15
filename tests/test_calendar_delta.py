"""固定日历差分只使用指定历史端点，缺行不压缩时间。"""
from datetime import date,timedelta
import unittest
import numpy as np
import polars as pl
from factor_miner.compiler import attach_market_sessions,build_polars_expr
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.errors import FactorMinerError
from tests.test_dsl_semantics import semantic_registry

class CalendarDeltaTest(unittest.TestCase):
    def test_exact_dates_groups_missing_and_future(self):
        days=[date(2020,1,1)+timedelta(days=i) for i in range(8)]
        frame=pl.DataFrame(dict(date=[days[i] for i in [0,2,3,5,7,0,1,3,5,7]],asset=['A']*5+['B']*5,close=[10.,12.,None,15.,17.,100.,101.,103.,105.,107.]))
        node=FactorNode(op='calendar_delta',args=(FactorNode(op='field',field='close'),),period=2)
        meta=validate_ast(node,{'close'},());self.assertEqual(meta.lookback,2)
        context=attach_market_sessions(frame.lazy(),pl.DataFrame(dict(date=days)))
        result=context.with_columns(build_polars_expr(node).alias('v')).collect().sort('asset','date')
        self.assertEqual(result['v'].to_list(),[None,2.,None,None,2.,None,None,2.,2.,2.])
        lag=FactorNode(op='calendar_delay',args=node.args,period=2)
        explicit=context.with_columns((pl.col('close')-build_polars_expr(lag)).alias('v')).collect().sort('asset','date')
        self.assertTrue(result.equals(explicit))
        changed=frame.with_columns(pl.when(pl.col('date')==days[-1]).then(9999.).otherwise(pl.col('close')).alias('close'))
        other=attach_market_sessions(changed.lazy(),pl.DataFrame(dict(date=days))).with_columns(build_polars_expr(node).alias('v')).collect()
        self.assertTrue(result.filter(pl.col('date')<days[-1]).sort('asset','date').equals(other.filter(pl.col('date')<days[-1]).sort('asset','date')))
        scaled=context.with_columns((pl.col('close')*10).alias('close')).with_columns(build_polars_expr(node).alias('v')).collect().sort('asset','date')
        np.testing.assert_allclose(scaled['v'].to_numpy(),result['v'].to_numpy()*10,equal_nan=True)

    def test_field_only_units_and_missing_calendar(self):
        field=FactorNode(op='field',field='price_close');node=FactorNode(op='calendar_delta',args=(field,),period=1)
        self.assertEqual(analyse_semantic_type(node,semantic_registry()).unit_dimension,analyse_semantic_type(field,semantic_registry()).unit_dimension)
        for invalid in [node.model_copy(update={'period':-1}),node.model_copy(update={'args':(node,)})]:
            with self.assertRaises(FactorMinerError):validate_ast(invalid,{'price_close'},())
        with self.assertRaises(pl.exceptions.ColumnNotFoundError):pl.DataFrame(dict(asset=['A'],price_close=[1.])).select(build_polars_expr(node))
