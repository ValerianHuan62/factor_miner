"""低价锚与高价锚不同，验证完整窗口与单位不变性。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def test_low_anchor_independent_minima_paths_and_causality():
    close = FactorNode(op='field', field='close')
    node = FactorNode(op='div', args=(FactorNode(op='rolling_min', window=120, args=(close,)), close))
    values = np.exp(np.random.default_rng(3301).normal(3, .4, 520))
    values[150] = np.nan
    frame = pl.DataFrame({'asset': ['A']*260+['B']*260, 'close': values})
    def calc(data):
        return data.with_columns(build_polars_expr(node).alias('factor'))['factor'].to_numpy()
    expected = np.full(520, np.nan)
    for start in [0, 260]:
        for j in range(start+119,start+260):
            window = values[j-119:j+1]
            if np.isfinite(window).all():
                expected[j] = min(window) / values[j]
    actual = calc(frame)
    np.testing.assert_allclose(actual, expected, atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(actual, calc(frame.with_columns((pl.col('close')*30).alias('close'))), atol=1e-12, equal_nan=True)
    future = frame.with_row_index().with_columns(pl.when(pl.col('index')>480).then(.001).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_array_equal(actual[:481], calc(future)[:481])
    # 最高价和末价相同，低点不同；高点接近度完全相同，低点接近度不同。
    paths = [[80.]+[120.]*118+[100.], [40.]+[120.]*118+[100.]]
    result = [calc(pl.DataFrame({'asset':['A']*120,'close':p}))[-1] for p in paths]
    assert result == [.8,.4]
    assert (validate_ast(node, {'close'}, ()).node_count,validate_ast(node, {'close'}, ()).depth) == (4,3)
    condition=ObservableCondition(observation='接近历史低点',measurement='低点/现价',response_test='near_low')
    assert validate_construct(node,condition)['status']=='基础检验符合'
