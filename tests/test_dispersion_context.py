"""分散程度的总体构造、时点和条件斜率独立回归。"""
from datetime import date, timedelta
import hashlib
import json

import numpy as np
import polars as pl
import pytest

from factor_miner.dispersion_context import CONTRACT, build_dispersion_context, attach_dispersion_context
from factor_miner.compiler import build_polars_expr, attach_market_sessions
from factor_miner.schema import FactorNode
from factor_miner.construct_validation import ObservableCondition, validate_construct


def inputs():
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(8)]
    rng = np.random.default_rng(2601)
    returns = rng.normal(0, .02, (510, 8))
    closes = 50 * np.cumprod(1 + returns, axis=1)
    rows = [dict(date=d, asset=str(a), close=closes[a, j]) for a in range(510) for j, d in enumerate(dates)]
    market = pl.DataFrame(rows)
    state = market.select('date', 'asset').with_columns(pl.lit(True).alias('valid_for_factor_compute'), pl.lit(True).alias('valid_for_factor_rank'))
    index = pl.DataFrame({'date': dates, 'market_return': rng.normal(0, .01, 8)})
    return market, state, index.select('date'), index


def test_population_independent_formula_masks_gap_units_and_future():
    market, state, calendar, index = inputs()
    day = calendar['date'][3]
    market = market.filter(~((pl.col('asset') == '0') & (pl.col('date') == day)))
    state = state.join(market.select('date', 'asset'), on=['date', 'asset']).with_columns(
        (~((pl.col('asset') == '1') & (pl.col('date') == day))).alias('valid_for_factor_compute'))
    def calc(m, s): return build_dispersion_context(m.lazy(), s.lazy(), calendar, index)
    actual = calc(market, state)
    assert actual['dispersion_change'][:2].null_count() == 2
    lookup = {(r['date'], r['asset']): r['close'] for r in market.to_dicts()}
    masks = {(r['date'], r['asset']): r for r in state.to_dicts()}
    expected = [np.nan]
    for j, d in enumerate(calendar['date'][1:], 1):
        prior = calendar['date'][j-1]
        values = []
        for a in map(str, range(510)):
            t, p = (d, a), (prior, a)
            if t in lookup and p in lookup and masks[t]['valid_for_factor_compute'] and masks[t]['valid_for_factor_rank'] and masks[p]['valid_for_factor_compute']:
                values.append(abs(lookup[t]/lookup[p] - 1 - index['market_return'][j]))
        assert actual['names'][j] == len(values)
        expected.append(sum(values)/(len(values)-1))
    np.testing.assert_allclose(actual['dispersion'].to_numpy(), expected, equal_nan=True, atol=1e-16)
    np.testing.assert_allclose(actual['dispersion_change'][1:].to_numpy(), np.diff(expected), equal_nan=True, atol=1e-16)
    np.testing.assert_allclose(calc(market.with_columns((pl.col('close')*10).alias('close')), state)['dispersion_change'].to_numpy(), actual['dispersion_change'].to_numpy(), equal_nan=True, atol=1e-16)
    future = market.with_columns(pl.when(pl.col('date') > calendar['date'][5]).then(pl.col('close')*100).otherwise(pl.col('close')).alias('close'))
    unchanged = calc(future, state).head(6)
    assert unchanged.select('date', 'names').equals(actual.head(6).select('date', 'names'))
    np.testing.assert_allclose(unchanged.select('absolute_sum', 'dispersion', 'dispersion_change').to_numpy(), actual.head(6).select('absolute_sum', 'dispersion', 'dispersion_change').to_numpy(), rtol=0, atol=1e-13, equal_nan=True)
    with pytest.raises(ValueError, match='500'):
        calc(market.filter(pl.col('asset').cast(pl.Int32) < 490), state.filter(pl.col('asset').cast(pl.Int32) < 490))
    with pytest.raises(ValueError, match='主键重复'):
        calc(pl.concat([market, market.head(1)]), state)
    with pytest.raises(ValueError, match='主键不一致'):
        calc(market, state.slice(1))


def test_snapshot_identity_coverage_initial_nulls_and_population(tmp_path):
    market, state, calendar, index = inputs()
    population = build_dispersion_context(market.lazy(), state.lazy(), calendar, index)
    paths = {}
    for key, frame in [('market', market), ('state', state), ('calendar', calendar), ('market_context', index), ('population', population), ('data', population.select('date', 'dispersion_change'))]:
        path = tmp_path/f'{key}.parquet'; frame.write_parquet(path); paths[key] = path
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    contract = {**CONTRACT, **{f'{k}_path': str(paths[k]) for k in ['market', 'state', 'market_context', 'population']},
                'data_sha256': sha(paths['data']), 'calendar_sha256': sha(paths['calendar']),
                'source_sha256': {str(paths[k]): sha(paths[k]) for k in ['market', 'state', 'market_context', 'population']}}
    cp = tmp_path/'contract.json'
    def attach():
        cp.write_text(json.dumps(contract))
        return attach_dispersion_context(market.lazy(), paths['calendar'], paths['data'], cp).collect()
    assert attach().height == market.height
    for invalid in [population.select('date', 'dispersion_change').slice(1), population.select('date', pl.col('dispersion_change').fill_null(0)), population.select('date', pl.when(pl.col('date') == calendar['date'][4]).then(None).otherwise(pl.col('dispersion_change')).alias('dispersion_change'))]:
        invalid.write_parquet(paths['data']); contract['data_sha256'] = sha(paths['data'])
        with pytest.raises(ValueError): attach()
    population.select('date', 'dispersion_change').write_parquet(paths['data']); contract['data_sha256'] = sha(paths['data'])
    market.with_columns(pl.lit(1.).alias('close')).write_parquet(paths['market'])
    with pytest.raises(ValueError, match='身份变化'): attach()


def expression():
    close = {'op': 'field', 'field': 'close'}
    return {'op': 'rolling_partial_beta', 'window': 20, 'args': [
        {'op': 'div', 'args': [close, {'op': 'calendar_delay', 'args': [close], 'period': 1}]},
        {'op': 'field', 'field': 'dispersion_change'}, {'op': 'field', 'field': 'market_return'}]}


def test_dispersion_slope_control_gap_future_and_construct():
    rng = np.random.default_rng(2602); m = rng.normal(0, .01, 300); d = .2*m + rng.normal(0, .003, 300)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(300)]
    def frame(beta=3., market_beta=.5):
        r = .0002 + beta*d + market_beta*m; r[0] = 0
        return pl.DataFrame(dict(date=dates, asset=['A']*300, close=50*np.cumprod(1+r), market_return=m, dispersion_change=d))
    def calc(f):
        return attach_market_sessions(f.lazy(), pl.DataFrame({'date': dates})).select(build_polars_expr(FactorNode.model_validate(expression()))).collect()[:,0].to_numpy()
    actual = calc(frame()); np.testing.assert_allclose(actual[20:], 3., atol=1e-9)
    np.testing.assert_allclose(calc(frame(market_beta=2.))[20:], 3., atol=1e-9)
    np.testing.assert_allclose(calc(frame(beta=5.))[20:], 5., atol=1e-9)
    gap = calc(frame().filter(pl.col('date') != dates[150])); assert np.isnan(gap[150:170]).all()
    changed = frame().with_columns(pl.when(pl.col('date') > dates[250]).then(10000.).otherwise(pl.col('close')).alias('close'))
    np.testing.assert_allclose(actual[:251], calc(changed)[:251], equal_nan=True)
    condition = ObservableCondition(observation='分散冲击对冲', measurement='控制市场的分散差分beta', response_test='dispersion_hedge')
    result = validate_construct(expression(), condition)
    assert result['status'] == '基础检验符合'
    assert abs(result['checks'][-1]['median_response'] - 2) < 1e-9
