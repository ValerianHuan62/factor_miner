"""月末持仓延续、费用、锁仓和正式入口的合成回归。"""
from datetime import date, timedelta
from pathlib import Path
import json

import polars as pl
import pytest
from typer.testing import CliRunner

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio as simulate
from factor_miner.cli import app
from factor_miner.eight_factor_study import reconcile_pnl
from factor_miner.errors import FactorMinerError
from factor_miner.monthly_target_execution import run_monthly_target_execution
from factor_miner.ridge_strategy import file_hash
from factor_miner.trading_schedule import build_month_end_target_schedule


def fixture(second=('A', 'B')):
    days = [date(2025, 1, 1)+timedelta(days=i) for i in range(100)]
    days = [d for d in days if d.weekday() < 5 and d != date(2025, 2, 17)]
    schedule = build_month_end_target_schedule(days, date(2025, 1, 1), date(2025, 3, 15), date(2025, 4, 1))
    assets = ['A', 'B', 'C', 'D']
    scores = pl.DataFrame([dict(signal_date=w.signal_date, security_id=a, factor_value=float(4-order.index(a)))
        for w, order in zip(schedule, [assets, list(second)+[a for a in assets if a not in second]], strict=True) for a in assets])
    market = pl.DataFrame([dict(trade_date=d, security_id=a, open=100.) for d in days for a in assets])
    state = market.select('trade_date', 'security_id').with_columns(*(pl.lit(True).alias(k) for k in
        ['valid_for_factor_compute', 'valid_for_factor_rank', 'can_open_long', 'can_close_long']))
    return days, schedule, scores, market, state


def execute(f, cost=0., mode='target_difference'):
    _, schedule, scores, market, state = f
    return simulate(scores, market, state, schedule, group_count=2, round_trip_cost_bps=cost,
                    execution_mode=mode, terminal_policy='report_unresolved')


def test_month_end_is_complete_and_final_exit_is_explicit():
    days, schedule, *_ = fixture()
    assert [w.signal_date for w in schedule] == [date(2025, 1, 31), date(2025, 2, 28)]
    assert schedule[0].entry_date == date(2025, 2, 3)
    assert schedule[0].exit_date == schedule[1].entry_date == date(2025, 3, 3)
    assert schedule[-1].exit_date == date(2025, 4, 1)
    assert days.index(schedule[0].exit_date)-days.index(schedule[0].entry_date) == 19
    with pytest.raises(FactorMinerError, match='晚于最后一次入场'):
        build_month_end_target_schedule(days, days[0], date(2025, 3, 15), date(2025, 3, 3))


def test_month_end_unchanged_holdings_charge_only_initial_buy_and_final_sale():
    f = fixture(); result = execute(f, cost=20)
    assert not [o for o in result.orders if o['actual_date'] == date(2025, 3, 3) and o['status'] == 'filled']
    assert len([o for o in result.orders if o['status'] == 'filled']) == 4
    assert result.execution_summary['final_cash'] == pytest.approx((1-.001)/(1+.001))
    assert reconcile_pnl(result)[1] < 1e-12
    old = execute(f, cost=20, mode='weekly_target')
    assert old.orders == result.orders and old.daily_returns == result.daily_returns
    assert old.execution_summary['execution_contract'] == 'causal_weekly_target_v1'
    assert result.execution_summary['execution_contract'] == 'causal_target_difference_v1'


def test_locked_sale_cannot_fund_replacement_or_late_buy():
    days, schedule, scores, market, state = fixture(('B', 'C'))
    state = state.with_columns(pl.when((pl.col('security_id') == 'A') & pl.col('trade_date').is_between(date(2025, 3, 3), date(2025, 3, 4)))
        .then(False).otherwise(pl.col('can_close_long')).alias('can_close_long'))
    result = execute((days, schedule, scores, market, state))
    assert any(o['security_id'] == 'C' and o['reason'] == 'insufficient_cash_locked' for o in result.orders)
    assert not any(o['security_id'] == 'C' and o['side'] == 'buy' and o['status'] == 'filled' for o in result.orders)
    assert any(o['security_id'] == 'A' and o['side'] == 'sell' and o['actual_date'] == date(2025, 3, 5) and o['status'] == 'filled' for o in result.orders)
    assert reconcile_pnl(result)[1] < 1e-12


def test_future_nonfill_never_changes_signal_day_selection():
    f = fixture(('C', 'D')); old = execute(f)
    days, schedule, scores, market, state = f
    state = state.with_columns(pl.when((pl.col('security_id') == 'C') & (pl.col('trade_date') == date(2025, 3, 3)))
        .then(False).otherwise(pl.col('can_open_long')).alias('can_open_long'))
    changed = execute((days, schedule, scores, market, state))
    assert changed.selections == old.selections
    assert not any(o['security_id'] == 'C' and o['side'] == 'buy' and o['status'] == 'filled' for o in changed.orders)
    assert reconcile_pnl(changed)[1] < 1e-12


def test_month_end_reweights_only_price_drift_and_keeps_unresolved_values_explicit():
    days, schedule, scores, market, state = fixture()
    market = market.with_columns(pl.when((pl.col('security_id') == 'A') & (pl.col('trade_date') >= date(2025, 3, 3)))
        .then(200.).otherwise(pl.col('open')).alias('open'))
    result = execute((days, schedule, scores, market, state))
    orders = [o for o in result.orders if o['actual_date'] == date(2025, 3, 3) and o['status'] == 'filled']
    assert {(o['security_id'], o['side']) for o in orders} == {('A', 'sell'), ('B', 'buy')}
    assert all(o['gross_notional'] == pytest.approx(.25) for o in orders)
    assert result.execution_summary['final_cash'] == pytest.approx(1.5)
    assert reconcile_pnl(result)[1] < 1e-12
    state = state.with_columns(pl.when((pl.col('security_id') == 'A') & (pl.col('trade_date') >= date(2025, 4, 1)))
        .then(False).otherwise(pl.col('can_close_long')).alias('can_close_long'))
    unresolved = execute((days, schedule, scores, market, state))
    assert unresolved.execution_summary['valuation_status'] == 'unresolved'
    assert unresolved.execution_summary['unresolved_reference_value'] == pytest.approx(.75)
    assert unresolved.execution_summary['zero_recovery_final_nav'] == pytest.approx(.75)
    assert reconcile_pnl(unresolved)[1] < 1e-12


def config(tmp_path):
    days, schedule, scores, market, state = fixture()
    dataset = tmp_path/'dataset'; dataset.mkdir()
    scores.write_parquet(tmp_path/'scores.parquet')
    market.rename({'trade_date': 'date', 'security_id': 'asset'}).write_parquet(dataset/'market.parquet')
    state.rename({'trade_date': 'date', 'security_id': 'asset'}).write_parquet(dataset/'state.parquet')
    pl.DataFrame({'date': days}).write_parquet(dataset/'calendar.parquet')
    (dataset/'manifest.json').write_text(json.dumps(dict(files={p.name: dict(sha256=file_hash(p)) for p in dataset.iterdir()})))
    release = dict(market_id='us_equity', release_id='synthetic-v1', source='synthetic_fixture', adjustment='split_adjusted_price_only',
        calendar_version='synthetic', state_version='synthetic', as_of_date=str(days[-1]), state_as_of_date=str(days[-1]))
    (tmp_path/'release.json').write_text(json.dumps(release))
    (tmp_path/'reservation.json').write_text(json.dumps(dict(capacity=1, comparison_ids=['M1'])))
    c = dict(version='monthly-target-execution-v1', run_kind='synthetic_demo', code_commit='synthetic_fixture', output_root=str(tmp_path/'run'),
        prediction_horizon_sessions=20, test_consumed=True, dataset_root=str(dataset), scores_path=str(tmp_path/'scores.parquet'),
        release_path=str(tmp_path/'release.json'), reservation_path=str(tmp_path/'reservation.json'), comparison_id='M1',
        terminal_events_path=None, market_id='us_equity', signal_start='2025-01-01', signal_end='2025-03-15',
        liquidation_date='2025-04-01', observation_end=str(days[-1]), direction='positive', group_count=2,
        round_trip_cost_bps=20, terminal_policy='report_unresolved')
    c['input_sha256'] = {str(p): file_hash(p) for p in [*dataset.iterdir(), tmp_path/'scores.parquet', tmp_path/'release.json', tmp_path/'reservation.json']}
    path=tmp_path/'config.json'; path.write_text(json.dumps(c))
    return c, path


def test_cli_runs_frozen_monthly_schedule_and_reconciles(tmp_path):
    c, path = config(tmp_path)
    result = CliRunner().invoke(app, ['run-monthly-target-execution', str(path)])
    assert result.exit_code == 0, result.output
    root = Path(c['output_root'])
    summary = json.loads((root/'summary.json').read_text())
    assert summary['reconciliation_max_error'] < 1e-12
    assert summary['execution']['final_cash'] == pytest.approx((1-.001)/(1+.001))
    assert json.loads((root/'completion.json').read_text())['summary_sha256'] == file_hash(root/'summary.json')
    manifest = json.loads((root/'holdings_manifest.json').read_text())
    holdings = pl.read_parquet(root/manifest['path'])
    assert manifest['rows'] == holdings.height > 0
    assert manifest['sha256'] == file_hash(root/manifest['path'])
    assert not (root/'holdings_daily.json').exists()


def test_input_change_fails_and_preserves_failure(tmp_path):
    c, path = config(tmp_path)
    with Path(c['scores_path']).open('ab') as f: f.write(b'changed')
    with pytest.raises(ValueError, match='输入变化'):
        run_monthly_target_execution(path)
    assert (Path(c['output_root'])/'failure.json').exists()
    assert not (Path(c['output_root'])/'completion.json').exists()
