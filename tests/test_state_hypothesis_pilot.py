"""单因子状态小实验的时点、HAC差异和事件边界回归。"""
from datetime import date, timedelta
import numpy as np
import polars as pl
import pytest
import statsmodels.api as sm

from factor_miner.state_hypothesis_pilot import (pressure_state, state_contrasts, weekly_diagnostics,
    score_ensembles, joint_state_uplift, mean_inference, market_state_levels, align_combination_weeks)


def test_pressure_uses_only_prior_quotes_and_missing_dates_do_not_shift():
    dates = [date(2021, 1, 1)+timedelta(days=i) for i in range(30)]
    calendar = pl.DataFrame({'date': dates})
    daily = pl.DataFrame({'date': dates, 'coverage': [1.]*30, 'median_spread': list(np.linspace(.001, .003, 30))})
    original = pressure_state(daily, calendar, smooth=2, history=3)
    changed = daily.with_columns(pl.when(pl.col('date') >= dates[18]).then(10.).otherwise(pl.col('median_spread')).alias('median_spread'))
    attacked = pressure_state(changed, calendar, smooth=2, history=3)
    causal_columns = ['date', 'pressure', 'threshold', 'high_pressure']
    assert original.head(19).select(causal_columns).equals(attacked.head(19).select(causal_columns))
    missing = pressure_state(daily.filter(pl.col('date') != dates[18]), calendar, smooth=2, history=3)
    assert missing.height == calendar.height
    assert missing['pressure'][19] is None
    assert missing['pressure'][20] is None
    assert missing['pressure'][21] is not None


def test_hac_matches_independent_statsmodels_reference():
    rng = np.random.default_rng(54)
    high = np.tile([0., 1., 1., 0.], 40)
    errors = rng.normal(0, .02, len(high))
    y = .01 + .03*high + errors + .4*np.roll(errors, 1)
    result = state_contrasts(y.tolist(), high.tolist(), lags=5, family=17)
    reference = sm.OLS(y, sm.add_constant(high)).fit(cov_type='HAC', cov_kwds={'maxlags':5})
    assert result['difference']['estimate'] == pytest.approx(reference.params[1])
    assert result['difference']['standard_error'] == pytest.approx(reference.bse[1])
    assert result['difference']['bonferroni_p'] == pytest.approx(min(1., reference.pvalues[1]*17))


def test_missing_weeks_preserve_calendar_hac_and_small_states_are_inconclusive():
    rng = np.random.default_rng(18)
    h = np.tile([0., 0., 1., 1.], 30)
    y = rng.normal(size=len(h))
    gaps = np.arange(0, len(y), 3)
    y[gaps] = np.nan
    full = state_contrasts(y.tolist(), h.tolist(), lags=5, family=1)
    valid = np.isfinite(y)
    compressed = state_contrasts(y[valid].tolist(), h[valid].tolist(), lags=5, family=1)
    assert full['difference']['estimate'] == pytest.approx(compressed['difference']['estimate'])
    assert abs(full['difference']['standard_error']-compressed['difference']['standard_error']) > 1e-5
    assert state_contrasts([1.]*10, [0.]*10, lags=5, family=1)['status'] == 'insufficient_state_dates'


def test_future_label_loss_does_not_change_top_selection_and_purge_is_event_based():
    dates = [date(2023, 12, 20)+timedelta(days=i) for i in range(30)]
    calendar = pl.DataFrame({'date':dates})
    rows = [{'date':day,'asset':str(i),'huan007':float(i),'label_o2o_5d':float(i)/1000,
             'label_exit_date':dates[index+6]} for index, day in [(0,dates[0]),(7,dates[7])] for i in range(40)]
    panel = pl.DataFrame(rows)
    state = pl.DataFrame({'date':dates,'pressure':[.01]*30,'threshold':[.005]*30,'high_pressure':[1]*30})
    kwargs = dict(factor='huan007',sign=1,start=dates[0],boundary=dates[10],end=dates[10])
    actual = weekly_diagnostics(panel,state,calendar,**kwargs)
    assert actual['labeled'].to_list() == [40,0]
    changed = panel.with_columns(pl.when(pl.col('asset') == '39').then(None).otherwise(pl.col('label_o2o_5d')).alias('label_o2o_5d'))
    other = weekly_diagnostics(changed,state,calendar,**kwargs)
    assert actual['top_selected'].to_list() == other['top_selected'].to_list()
    invalid = panel.with_columns(pl.lit(dates[20]).alias('label_exit_date'))
    with pytest.raises(ValueError,match=r'T\+6'):
        weekly_diagnostics(invalid,state,calendar,**kwargs)


def test_conditional_weights_respect_original_sign_unknown_and_no_activation():
    days=[date(2024,1,1)+timedelta(days=i) for i in range(3)]
    panel=pl.DataFrame({'date':days,'asset':['a']*3,'F1':[.4]*3,'F2':[.2]*3,'label':[1.,2.,3.]})
    states=pl.DataFrame({'date':days,'s1':[1,0,None],'s2':[0,0,1]})
    factors=[dict(factor_id='F1',state_id='s1',active_value=1,direction=1),dict(factor_id='F2',state_id='s2',active_value=1,direction=-1)]
    actual=score_ensembles(panel,states,factors)
    assert actual['_always'].to_list()==pytest.approx([.1]*3)
    assert actual['_conditional'].to_list()==[.4,None,None]
    assert actual['active_factors'].to_list()==[1,0,None]
    changed=score_ensembles(panel.with_columns(pl.lit(-999.).alias('label')),states,factors)
    assert actual.select('_always','_conditional').equals(changed.select('_always','_conditional'))


def test_duplicate_factors_do_not_manufacture_independent_evidence():
    rng=np.random.default_rng(85);h=np.tile([0,1],70);y=rng.normal(0,.02,len(h))+.01*h
    single=state_contrasts(y.tolist(),h.tolist(),lags=5,family=1)
    duplicated=[dict(factor_id=str(i),mechanism_family=str(i//2),values=y.tolist(),states=h.tolist()) for i in range(12)]
    joint=joint_state_uplift(duplicated,1)
    assert joint['estimate']==pytest.approx(single['difference']['estimate'])
    assert joint['standard_error']==pytest.approx(single['difference']['standard_error'])


def test_paired_mean_matches_reference_without_compressing_missing_weeks():
    rng=np.random.default_rng(92);y=rng.normal(size=100)
    actual=mean_inference(y.tolist(),1)
    reference=sm.OLS(y,np.ones((100,1))).fit(cov_type='HAC',cov_kwds={'maxlags':5})
    assert actual['standard_error']==pytest.approx(reference.bse[0])
    assert actual['raw_p']==pytest.approx(reference.pvalues[0])


def test_market_states_do_not_use_future_quotes_or_bridge_missing_sessions():
    dates=[date(2020,1,1)+timedelta(days=i) for i in range(90)]
    calendar=pl.DataFrame({'date':dates})
    market=pl.DataFrame({'date':dates,'asset':['a']*90,'close':[100.+i+np.sin(i) for i in range(90)],'volume':[1000.]*90})
    masks=market.select('date','asset').with_columns(pl.lit(True).alias('valid_for_factor_compute'),pl.lit(True).alias('valid_for_factor_rank'))
    original=market_state_levels(market.lazy(),masks.lazy(),calendar)
    changed=market.with_columns(pl.when(pl.col('date')>=dates[70]).then(10000.).otherwise(pl.col('close')).alias('close'))
    attacked=market_state_levels(changed.lazy(),masks.lazy(),calendar)
    assert original.head(70).equals(attacked.head(70))
    missing=market_state_levels(market.filter(pl.col('date')!=dates[70]).lazy(),masks.lazy(),calendar)
    assert missing.filter(pl.col('date')==dates[71])['market_return'].item() is None
    assert missing.filter(pl.col('date')==dates[71])['activity'].item() is None


def test_combination_join_restores_chronological_grid_including_inactive_weeks():
    dates=[date(2024,1,1)+timedelta(days=7*i) for i in range(40)]
    always=pl.DataFrame({'date':dates,'rank_ic':np.linspace(0,.1,40)})
    conditional=always.with_columns((pl.col('rank_ic')+.01).alias('rank_ic')).filter(pl.col('date')!=dates[10])
    context=pl.DataFrame({'date':dates,'active_factors':[0 if i==10 else 3 for i in range(40)],'states_known':[True]*40})
    actual=align_combination_weeks(always.reverse(),conditional.reverse(),context.sample(fraction=1,shuffle=True,seed=7))
    assert actual['date'].to_list()==dates
    assert actual['paired_difference'][10] is None
    assert actual['paired_difference'].drop_nulls().to_list()==pytest.approx([.01]*39)
