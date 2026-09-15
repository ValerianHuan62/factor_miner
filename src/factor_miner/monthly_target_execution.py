"""冻结预测的单次月末差额执行，不产生候选、训练模型或搜索成本情景。"""
from datetime import date, datetime, timezone
from pathlib import Path
import json
import re

import polars as pl

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.eight_factor_study import reconcile_pnl
from factor_miner.favor_schema import FavorDataRelease
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash
from factor_miner.quote_research import _snapshot_code, _verify_code
from factor_miner.trading_schedule import build_month_end_target_schedule


def run_monthly_target_execution(config_path: Path) -> Path:
    """先绑定预测、数据、有限比较预约与日程，再运行一个成本情景。"""
    c = json.loads(config_path.read_text())
    root = Path(c['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    try:
        if c['version'] != 'monthly-target-execution-v1' or c['run_kind'] not in {'research', 'synthetic_demo'}:
            raise ValueError('月末执行版本或运行类型不合法')
        if not isinstance(c['code_commit'], str) or not c['code_commit'].strip():
            raise ValueError('必须记录代码提交身份')
        if c['run_kind'] == 'research' and not re.fullmatch(r'[0-9a-f]{40}', c['code_commit']):
            raise ValueError('真实研究须记录完整代码提交哈希，并另存实际代码快照')
        if c['prediction_horizon_sessions'] not in {1, 5, 20} or type(c['test_consumed']) is not bool:
            raise ValueError('预测期限或历史消费状态不明确')
        dataset = Path(c['dataset_root'])
        names = ['market.parquet', 'state.parquet', 'calendar.parquet']
        required = {str(dataset/n) for n in [*names, 'manifest.json']}
        required.update(c[k] for k in ['scores_path', 'release_path', 'reservation_path'])
        if c['terminal_events_path'] is not None:
            required.add(c['terminal_events_path'])
        if required-set(c['input_sha256']):
            raise ValueError('执行依赖未全部绑定哈希')
        def verify():
            for path, expected in c['input_sha256'].items():
                if file_hash(Path(path)) != expected:
                    raise ValueError(f'执行输入变化：{path}')
        verify()
        manifest = json.loads((dataset/'manifest.json').read_text())
        for name in names:
            entry = manifest['files'][name]
            expected = entry['sha256'] if isinstance(entry, dict) else entry
            if expected != c['input_sha256'][str(dataset/name)]:
                raise ValueError('输入哈希与面板发布清单不一致')
        reservation = json.loads(Path(c['reservation_path']).read_text())
        ids = reservation['comparison_ids']
        if (type(reservation['capacity']) is not int or not 1 <= len(ids) <= reservation['capacity']
                or len(set(ids)) != len(ids) or c['comparison_id'] not in ids):
            raise ValueError('月末执行未列入有限比较预约')
        release = FavorDataRelease.model_validate_json(Path(c['release_path']).read_text())
        start, end, liquidation, observation_end = [date.fromisoformat(c[k]) for k in
            ['signal_start', 'signal_end', 'liquidation_date', 'observation_end']]
        if release.market_id != c['market_id'] or not liquidation <= observation_end <= release.as_of_date:
            raise ValueError('市场、清仓终点或发布覆盖不符')
        days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
        if observation_end not in days:
            raise ValueError('观察终点不是市场交易日')
        schedule = build_month_end_target_schedule(days, start, end, liquidation)
        write_json(root/'schedule.json', [w.model_dump(mode='json') for w in schedule])
        code_identity = _snapshot_code(root)
        scores = pl.read_parquet(c['scores_path'])
        if scores.select(pl.struct('signal_date', 'security_id').is_duplicated().any()).item():
            raise ValueError('冻结分数存在重复主键')
        signal_dates = [w.signal_date for w in schedule]
        scores = scores.filter(pl.col('signal_date').is_in(signal_dates))
        if set(scores['signal_date'].to_list()) != set(signal_dates):
            raise ValueError('冻结分数缺少月末信号日')
        bounds = pl.col('date').is_between(schedule[0].signal_date, observation_end)
        market = pl.read_parquet(dataset/'market.parquet').filter(bounds).select(
            pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'), 'open')
        state = pl.read_parquet(dataset/'state.parquet').filter(bounds).select(
            pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'),
            'valid_for_factor_compute', 'valid_for_factor_rank', 'can_open_long', 'can_close_long')
        if state.schema['valid_for_factor_compute'] != pl.Boolean or state['valid_for_factor_compute'].null_count():
            raise ValueError('缺少明确的可计算状态')
        expected_days = [d for d in days if schedule[0].signal_date <= d <= observation_end]
        if market['trade_date'].unique().sort().to_list() != expected_days or state['trade_date'].unique().sort().to_list() != expected_days:
            raise ValueError('行情或状态缺少完整市场日期')
        scores = scores.join(state.select(pl.col('trade_date').alias('signal_date'), 'security_id', 'valid_for_factor_compute'),
                             on=['signal_date', 'security_id'], how='left', validate='1:1')
        if scores['valid_for_factor_compute'].null_count():
            raise ValueError('分数缺少信号日可计算状态')
        scores = scores.filter(pl.col('valid_for_factor_compute'))
        terminal = None if c['terminal_events_path'] is None else pl.read_parquet(c['terminal_events_path']).filter(
            (pl.col('event_date') <= observation_end) & (pl.col('available_at') <= observation_end))
        result = simulate_causal_extreme_portfolio(scores, market, state, schedule,
            direction=c['direction'], group_count=c['group_count'], round_trip_cost_bps=c['round_trip_cost_bps'],
            terminal_policy=c['terminal_policy'], terminal_events=terminal, retain_daily_holdings=True,
            execution_mode='target_difference')
        pnl, error = reconcile_pnl(result)
        pnl.write_parquet(root/'pnl.parquet')
        for key in ['orders', 'selections']:
            write_json(root/f'{key}.json', getattr(result, key))
        holdings_path = root/'holdings_daily.parquet'
        pl.DataFrame(result.holdings_daily).write_parquet(holdings_path)
        write_json(root/'holdings_manifest.json', dict(format='parquet', path=holdings_path.name,
            rows=len(result.holdings_daily), sha256=file_hash(holdings_path)))
        write_json(root/'backtest.json', dict(execution=result.execution_summary, daily_returns=result.daily_returns,
            unresolved_positions=result.unresolved_positions, zero_recovery_daily_returns=result.zero_recovery_daily_returns))
        write_json(root/'summary.json', dict(version=c['version'], run_kind=c['run_kind'],
            code_commit=c['code_commit'],
            prediction_horizon_sessions=c['prediction_horizon_sessions'], frequency='monthly_last_session',
            test_consumed=c['test_consumed'], comparison_id=c['comparison_id'],
            reconciliation_max_error=error, execution=result.execution_summary,
            actual_final_nav=None if result.unresolved_positions else result.execution_summary['final_cash'],
            return_series_kind='reference_valuation' if result.unresolved_positions else 'realized',
            scope='单次冻结预测执行结果；不训练、不筛选、不授予因子或样本外资格'))
        verify()
        _verify_code(code_identity)
        write_json(root/'completion.json', dict(status='completed', summary_sha256=file_hash(root/'summary.json')))
    except Exception as error:
        write_json(root/'failure.json', dict(status='preserved_failure', error=str(error)))
        raise
    return root
