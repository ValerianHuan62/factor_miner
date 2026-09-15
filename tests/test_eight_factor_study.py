"""八因子归因的金融不变量回归。"""

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from factor_miner.eight_factor_study import (
    attach_industry, conditional_rank_ic, market_cap_group, reconcile_pnl, ten_day_schedule,
)
from factor_miner.trading_schedule import RebalanceWindow


def test_industry_does_not_backfill_or_extend():
    keys = pl.DataFrame({'signal_date':[date(2024,1,1),date(2025,1,1),date(2026,1,1)], 'security_id':['a']*3})
    master = pl.DataFrame({'security_id':['a'], 'valid_from':[date(2025,1,1)], 'valid_to':[date(2025,12,31)], 'sic_code':[3571]})
    assert attach_industry(keys, master)['sic_code'].to_list() == [None,3571,None]
    with pytest.raises(ValueError, match='冲突'):
        attach_industry(keys, pl.concat([master, master.with_columns(pl.lit(2834).cast(pl.Int64).alias('sic_code'))]))
    assert market_cap_group(None) == '未知'
    assert market_cap_group(300_000_000).startswith('2_')


def test_pnl_with_purchase_cost_and_terminal_recovery():
    signal, entry, end = date(2024,1,1), date(2024,1,2), date(2024,1,3)
    result = SimpleNamespace(
        holdings_daily=[dict(date=entry,signal_date=signal,security_id='a',market_value=.9)],
        orders=[dict(actual_date=entry,signal_date=signal,security_id='a',side='buy',status='filled',gross_notional=.9,transaction_cost=.01),
                dict(actual_date=end,signal_date=signal,security_id='a',side='terminal',status='settled',gross_notional=.2,transaction_cost=0.)],
        daily_returns=[dict(exit_date=entry,target_long_net_return=-.01),dict(exit_date=end,target_long_net_return=-.7/.99)])
    pnl,error = reconcile_pnl(result)
    assert error < 1e-12
    assert pnl['net_pnl'].sum() == pytest.approx(-.71)
    result.daily_returns[-1]['target_long_net_return'] = 0.
    with pytest.raises(ValueError, match='对账'):
        reconcile_pnl(result)


def test_control_removes_synthetic_size_effect():
    rng = np.random.default_rng(41)
    n = 1200
    size = rng.normal(size=n)
    frame = pl.DataFrame(dict(market_cap_usd=np.exp(size)*1e9, adv20=np.exp(rng.normal(size=n)),
        vol60=np.exp(rng.normal(size=n)), mom252_21=rng.normal(size=n), label_o2o_5d=size+rng.normal(size=n)*.1,
        score=size+rng.normal(size=n)*.1, industry=['制造']*n, sic2=['35']*n))
    result = conditional_rank_ic(frame,['score'])[0]
    assert result['before'] > .95
    assert abs(result['after']) < .12


def test_ten_day_has_new_fixed_exit_and_no_original_mutation():
    days = [date(2024,1,1)+timedelta(days=i) for i in range(30)]
    original = tuple(RebalanceWindow(signal_date=days[i],entry_date=days[i+1],exit_date=days[i+6]) for i in [0,5,10,15,20])
    result = ten_day_schedule(days, original)
    assert [w.signal_date for w in result] == [days[0],days[10]]
    assert [w.exit_date for w in result] == [days[11],days[21]]
    assert original[0].exit_date == days[6]
