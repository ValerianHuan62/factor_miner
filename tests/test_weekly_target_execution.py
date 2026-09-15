"""每周目标差额交易：成交因果、真实费用与锁仓资金。"""
from datetime import date,timedelta
import math
import polars as pl
import pytest
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio as simulate
from factor_miner.trading_schedule import RebalanceWindow
from factor_miner.eight_factor_study import reconcile_pnl

def fixture(second=('A','B')):
    days=[date(2025,1,1)+timedelta(days=i) for i in range(15)]
    schedule=(RebalanceWindow(signal_date=days[0],entry_date=days[1],exit_date=days[6]),RebalanceWindow(signal_date=days[5],entry_date=days[6],exit_date=days[11]))
    assets=['A','B','C','D']
    factor=pl.DataFrame([dict(signal_date=d,security_id=a,factor_value=float(4-order.index(a))) for d,order in [(days[0],assets),(days[5],list(second)+[a for a in assets if a not in second])] for a in assets])
    market=pl.DataFrame([dict(trade_date=d,security_id=a,open=100.) for d in days for a in assets])
    state=market.select('trade_date','security_id').with_columns(*(pl.lit(True).alias(c) for c in ['valid_for_factor_rank','can_open_long','can_close_long']))
    return days,schedule,factor,market,state

def run(fixture,**kw):
    _,schedule,factor,market,state=fixture
    return simulate(factor,market,state,schedule,group_count=2,terminal_policy='report_unresolved',execution_mode='weekly_target',**kw)

def test_unchanged_targets_have_no_roundtrip_or_extra_cost():
    f=fixture();r=run(f,round_trip_cost_bps=20)
    assert not [o for o in r.orders if o['actual_date']==f[0][6] and o['status']=='filled']
    assert r.execution_summary['final_cash']==pytest.approx((1-.001)/(1+.001))
    assert len([o for o in r.orders if o['status']=='filled'])==4
    _,error=reconcile_pnl(r);assert error<1e-12

def test_partial_rebalance_sells_only_excess_and_accounts_price_change():
    days,schedule,factor,market,state=fixture()
    market=market.with_columns(pl.when((pl.col('trade_date')>=days[6]) & (pl.col('security_id')=='A')).then(200.).otherwise(pl.col('open')).alias('open'))
    r=run((days,schedule,factor,market,state))
    orders=[o for o in r.orders if o['actual_date']==days[6] and o['status']=='filled']
    assert {(o['security_id'],o['side']) for o in orders}=={('A','sell'),('B','buy')}
    assert all(o['gross_notional']==pytest.approx(.25) for o in orders)
    assert r.execution_summary['final_cash']==pytest.approx(1.5)
    _,error=reconcile_pnl(r);assert error<1e-12

def test_locked_exit_never_finances_replacement_or_offday_buy():
    days,schedule,factor,market,state=fixture(('B','C'))
    state=state.with_columns(pl.when((pl.col('trade_date').is_between(days[6],days[7])) & (pl.col('security_id')=='A')).then(False).otherwise(pl.col('can_close_long')).alias('can_close_long'))
    r=run((days,schedule,factor,market,state))
    assert any(o['security_id']=='C' and o['status']=='rejected_final' and o['reason']=='insufficient_cash_locked' for o in r.orders)
    assert not any(o['side']=='buy' and o['security_id']=='C' and o['status']=='filled' for o in r.orders)
    assert any(o['security_id']=='A' and o['actual_date']==days[8] and o['status']=='filled' for o in r.orders)
    assert r.execution_summary['final_cash']==pytest.approx(1.)
    reconcile_pnl(r)

def test_failed_partial_exit_keeps_both_parts_in_pnl_and_terminal_stress():
    days,schedule,factor,market,state=fixture()
    market=market.with_columns(pl.when((pl.col('trade_date')>=days[5]) & (pl.col('security_id')=='A')).then(200.).otherwise(pl.col('open')).alias('open'))
    state=state.with_columns(pl.when((pl.col('trade_date')>=days[6]) & (pl.col('security_id')=='A')).then(False).otherwise(pl.col('can_close_long')).alias('can_close_long'))
    r=run((days,schedule,factor,market,state))
    assert r.execution_summary['valuation_status']=='unresolved'
    assert r.execution_summary['unresolved_reference_value']==pytest.approx(1.)
    assert r.execution_summary['zero_recovery_final_nav']==pytest.approx(.5)
    assert all(o['side']!='buy' for o in r.orders if o['status']=='filled' and o['actual_date']>days[1])
    reconcile_pnl(r)

def test_future_untradable_asset_keeps_same_selection_and_cash():
    f=fixture(('C','D'));r=run(f)
    days,schedule,factor,market,state=f
    state=state.with_columns(pl.when((pl.col('trade_date')==days[6]) & (pl.col('security_id')=='C')).then(False).otherwise(pl.col('can_open_long')).alias('can_open_long'))
    changed=run((days,schedule,factor,market,state))
    assert r.selections==changed.selections
    assert not any(o['security_id']=='C' and o['side']=='buy' and o['status']=='filled' for o in changed.orders)
    reconcile_pnl(changed)

def test_holiday_week_uses_next_weekly_decision_and_final_fixed_exit():
    days,schedule,factor,market,state=fixture()
    schedule=(schedule[0],RebalanceWindow(signal_date=days[6],entry_date=days[7],exit_date=days[12]))
    factor=factor.with_columns(pl.when(pl.col('signal_date')==days[5]).then(days[6]).otherwise(pl.col('signal_date')).alias('signal_date'))
    r=run((days,schedule,factor,market,state),allow_noncontiguous_schedule=True)
    assert not [o for o in r.orders if o['side']=='sell' and o['actual_date']<days[12]]
    assert r.execution_summary['final_cash']==pytest.approx(1.)
    reconcile_pnl(r)
