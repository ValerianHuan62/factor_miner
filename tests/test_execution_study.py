"""持有周期研究的日历、现金和事前判断不变量。"""

from copy import deepcopy
from datetime import date, timedelta
import json
import math

import polars as pl
import pytest

from factor_miner.execution_study import SCHEDULES, _run, align_cash_prefix, phased_schedule, research_decision
from factor_miner.research_report import report_schedule
from factor_miner.trading_schedule import RebalanceWindow


def test_phase_does_not_restart_at_year_boundary_or_extend_tail():
    days = [date(2024,12,23)+timedelta(days=i) for i in range(35)]
    original = tuple(RebalanceWindow(signal_date=days[i],entry_date=days[i+1],exit_date=days[i+6])
                     for i in [0,5,10,15,20,25])
    windows, excluded = phased_schedule(days,original,dict(horizon=10,stride=2,phase=1))
    assert [w.signal_date for w in windows] == [days[5],days[15]]
    assert [w.exit_date for w in windows] == [days[16],days[26]]
    assert [r['signal_date'] for r in excluded] == [days[25]]
    assert original[1].exit_date == days[11]


def test_only_initial_cash_can_be_filled():
    days = [date(2024,1,1)+timedelta(days=i) for i in range(5)]
    benchmark = [dict(entry_date=a,exit_date=b,benchmark_return=.02) for a,b in zip(days,days[1:])]
    rows = [dict(entry_date=r['entry_date'],exit_date=r['exit_date'],target_long_net_return=.01)
            for r in benchmark[1:]]
    result = align_cash_prefix(rows,benchmark)
    assert [r['target_long_net_return'] for r in result] == [0.,.01,.01,.01]
    assert result[0]['initial_cash_only'] is True
    assert result[0]['benchmark_return'] == .02
    with pytest.raises(ValueError,match='缺日'):
        align_cash_prefix([rows[0],rows[2]],benchmark)
    with pytest.raises(ValueError,match='缺日'):
        align_cash_prefix(rows[:-1],benchmark)


def test_development_requires_both_phases_and_pre_2026_gain():
    scenario = dict(reference_metrics=dict(annualized_return=.08,max_drawdown=.1),
                    zero_recovery_metrics=dict(annualized_return=.07),before_2026=dict(period_return=.04))
    summaries = {f'ridge_10d_p{p}_{cost}bps':deepcopy(scenario) for p in [0,1] for cost in [20,40]}
    summaries['ridge_5d_p0_20bps'] = dict(reference_metrics=dict(annualized_return=.02,max_drawdown=.2))
    assert research_decision(summaries)['continue_mining'] is True
    summaries['ridge_10d_p1_20bps']['before_2026']['period_return'] = -.001
    decision = research_decision(summaries)
    assert decision['continue_mining'] is False
    assert decision['criteria']['both_phases_positive_before_2026'] is False
    summaries['ridge_10d_p1_20bps']['before_2026']['period_return'] = .04
    summaries['ridge_10d_p0_20bps']['zero_recovery_metrics']['annualized_return'] = -.001
    assert research_decision(summaries)['continue_mining'] is False


@pytest.mark.parametrize('bounded',[False,True])
def test_synthetic_execution_saves_all_scenarios_and_reconciles(tmp_path,bounded):
    days = [date(2025,8,1)+timedelta(days=i) for i in range(120)]
    days = [d for d in days if d.weekday()<5]
    windows = report_schedule(days,days[0],days[-15],'weekly_last_session')
    assets = [f'a{i:02d}' for i in range(20)]
    scores = [dict(date=w.signal_date,asset=a,ridge=float(i),equal_rank=float(19-i))
              for w in windows for i,a in enumerate(assets)]
    pl.DataFrame(scores).write_parquet(tmp_path/'scores.parquet')
    pl.DataFrame({'date':days}).write_parquet(tmp_path/'calendar.parquet')
    market = [dict(date=d,asset=a,open=100*math.exp(.0003*t+.03*math.sin(t/8+i)))
              for t,d in enumerate(days) for i,a in enumerate(assets)]
    pl.DataFrame(market).write_parquet(tmp_path/'market.parquet')
    pl.DataFrame(market).select('date','asset').with_columns(pl.lit(True).alias('valid_for_factor_rank'),
        pl.lit(True).alias('can_open_long'),pl.lit(True).alias('can_close_long')).write_parquet(tmp_path/'state.parquet')
    pl.DataFrame(schema=dict(security_id=pl.String,event_date=pl.Date,available_at=pl.Date,
        terminal_value=pl.Float64,source=pl.String)).write_parquet(tmp_path/'terminal.parquet')
    selected = days[days.index(windows[0].signal_date):]
    benchmark = [dict(entry_date=str(a),exit_date=str(b),benchmark_return=.0001) for a,b in zip(selected,selected[1:])]
    (tmp_path/'benchmark.json').write_text(json.dumps(dict(daily_rows=benchmark)))
    config = dict(scores_path=str(tmp_path/'scores.parquet'),dataset_root=str(tmp_path),
        terminal_events_path=str(tmp_path/'terminal.parquet'),benchmark_path=str(tmp_path/'benchmark.json'),
        schedules=SCHEDULES,models=['ridge','equal_rank'],round_trip_cost_bps=[0,10,20,40],
        reference_paths={},hac_lags=20,diagnostic_family_size=12801)
    if bounded:
        config['research_window'] = dict(last_signal_date=str(windows[-3].signal_date),
            observation_end=str(days[-5]),parity_through=str(windows[-3].signal_date))
    root = tmp_path/'run'
    root.mkdir()
    _run(config,root)
    summaries = json.loads((root/'summary.json').read_text())
    assert len(summaries) == 24
    assert len({s['reference_metrics']['observations'] for s in summaries.values()}) == 1
    assert len(list(root.glob('*/reconciliation.json'))) == 6
    assert len(json.loads((root/'paired_statistics.json').read_text())) == 36
    assert (root/'decision.json').exists()
    schedule = json.loads((root/'schedule_10d_p1.json').read_text())
    assert schedule['windows'][0]['signal_date'] == str(windows[1].signal_date)
    if bounded:
        assert all(v['reference_metrics']['last_date']==str(days[-5]) for v in summaries.values())
        assert all(w['signal_date']<=str(windows[-3].signal_date) and w['exit_date']<=str(days[-5]) for w in schedule['windows'])
