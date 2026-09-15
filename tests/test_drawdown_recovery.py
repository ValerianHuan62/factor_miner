"""逐对枚举回撤谷底，验证恢复量与全窗口最低价格不同。"""
import numpy as np
import polars as pl
from factor_miner.schema import FactorNode
from factor_miner.compiler import build_polars_expr
from factor_miner.dsl import validate_ast
from factor_miner.construct_validation import ObservableCondition, validate_construct


def reference(prices):
    if not np.isfinite(prices).all() or np.any(prices <= 0):
        return np.nan
    worst = 1.
    trough = None
    for j in range(1, len(prices)):
        ratio = min(prices[j] / prices[i] for i in range(j))
        if ratio < 1 and ratio <= worst:
            worst, trough = ratio, j
    return np.nan if trough is None else prices[-1] / prices[trough]


def test_independent_pairs_missing_groups_units_and_future():
    prices = np.exp(np.random.default_rng(3201).normal(0, .15, 260))
    prices[35] = np.nan
    prices[161] = 0
    prices[228] = np.inf
    frame = pl.DataFrame({'asset': ['A']*130+['B']*130, 'close': prices})
    node = FactorNode(op='rolling_drawdown_recovery', window=20, args=(FactorNode(op='field', field='close'),))
    def compute(data):
        return data.with_columns(build_polars_expr(node).alias('factor'))['factor'].to_numpy()
    expected = np.full(260, np.nan)
    for start in [0, 130]:
        for j in range(start+19, start+130):
            expected[j] = reference(prices[j-19:j+1])
    actual = compute(frame)
    np.testing.assert_allclose(actual, expected, rtol=1e-13, equal_nan=True)
    np.testing.assert_allclose(actual, compute(frame.with_columns((pl.col('close')*17).alias('close'))), rtol=1e-13, equal_nan=True)
    future = frame.with_row_index().with_columns(pl.when(pl.col('index')>240).then(999.).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_array_equal(actual[:241], compute(future)[:241])
    assert np.isnan(compute(frame.head(4))).all()
    assert np.isnan(compute(frame.with_columns(pl.lit(10.).alias('close')))).all()


def test_deepest_episode_ties_and_construct():
    node = FactorNode(op='rolling_drawdown_recovery', window=5, args=(FactorNode(op='field', field='close'),))
    paths = [[50, 100, 60, 80, 90], [100, 50, 200, 100, 150], [100, 90, 80, 70, 60], [50, 60, 70, 80, 90]]
    actual = []
    for path in paths:
        result = pl.DataFrame({'asset': ['A']*5, 'close': path}).with_columns(build_polars_expr(node).alias('factor'))['factor'][-1]
        actual.append(result)
    assert actual[:3] == [1.5, 1.5, 1.]
    assert actual[3] is None
    # 第一个路径的最低价为50，但最大回撤的谷底是60，恢复量不得误算为90/50。
    long = node.model_copy(update={'window': 40})
    meta = validate_ast(long, {'close'}, ())
    assert (meta.node_count, meta.depth, meta.lookback) == (2, 2, 39)
    condition = ObservableCondition(observation='最深回撤后恢复', measurement='末价相对事件谷底', response_test='drawdown_recovery')
    result = validate_construct(long, condition)
    assert result['status'] == '基础检验符合', result
