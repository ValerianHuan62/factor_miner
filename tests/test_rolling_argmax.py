"""高点出现时间必须区别于高点幅度，保持完整窗口、分组和因果性。"""
import numpy as np
import polars as pl
import pytest
from factor_miner.compiler import build_polars_expr
from factor_miner.schema import FactorNode
from factor_miner.dsl import validate_ast
from factor_miner.dsl_semantics import analyse_semantic_type
from factor_miner.construct_validation import ObservableCondition, validate_construct
from factor_miner.errors import FactorMinerError
from tests.test_dsl_semantics import semantic_registry


def test_argmax_independent_groups_missing_ties_and_future():
    values = np.random.default_rng(2201).integers(1, 8, 200).astype(float)
    values[22] = np.nan
    values[145] = np.inf
    frame = pl.DataFrame({'asset': ['A']*100+['B']*100, 'x': values})
    node = FactorNode(op='rolling_argmax', window=20, args=(FactorNode(op='field', field='x'),))
    def compute(data):
        return data.lazy().select(build_polars_expr(node)).collect()[:, 0].to_numpy()
    actual = compute(frame)
    expected = np.full(200, np.nan)
    for offset in (0, 100):
        for i in range(offset+19, offset+100):
            window = values[i-19:i+1].tolist()
            if all(np.isfinite(x) for x in window):
                expected[i] = max(j for j, x in enumerate(window) if x == max(window))
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(actual, compute(frame.with_columns((pl.col('x')*10+5).alias('x'))))
    modified = frame.with_row_index().with_columns(pl.when(pl.col('index')>180).then(1e9).otherwise(pl.col('x')).alias('x'))
    np.testing.assert_array_equal(actual[:181], compute(modified)[:181])
    constant = compute(frame.with_columns(pl.lit(3.).alias('x')))
    assert np.all(constant[19:100] == 19) and np.isnan(constant[100:119]).all()
    assert np.isnan(compute(frame.head(5))).all()


def test_high_timing_separates_price_proximity_and_validates_construct():
    field = FactorNode(op='field', field='high')
    node = FactorNode(op='rolling_argmax', window=120, args=(field,))
    old = [10.]*120; recent = old.copy()
    old[10] = recent[100] = 20.
    frame = pl.DataFrame({'asset': ['old']*120+['recent']*120, 'high': old+recent})
    actual = frame.with_columns(build_polars_expr(node).alias('value')).group_by('asset').last()
    assert dict(zip(actual['asset'], actual['value'])) == {'old': 10., 'recent': 100.}
    # 最大值和最新报价完全一致，仅高点日期不同。
    assert max(old) == max(recent) and old[-1] == recent[-1]
    metadata = validate_ast(node, {'high'}, ())
    assert (metadata.node_count, metadata.depth, metadata.lookback) == (2, 2, 119)
    condition = ObservableCondition(observation='高点出现时间', measurement='最近高点窗口位置', response_test='near_high')
    assert validate_construct(node, condition)['status'] == '基础检验符合'


def test_argmax_semantics_and_invalid_parameters():
    field = FactorNode(op='field', field='price_close')
    node = FactorNode(op='rolling_argmax', window=20, args=(field,))
    sign = FactorNode(op='sign', args=(field,))
    assert analyse_semantic_type(node, semantic_registry()).unit_dimension == analyse_semantic_type(sign, semantic_registry()).unit_dimension
    for bad in (node.model_copy(update={'center': True}), node.model_copy(update={'window': 19}), node.model_copy(update={'args': (field, field)})):
        with pytest.raises(FactorMinerError):
            validate_ast(bad, {'price_close'}, ())
