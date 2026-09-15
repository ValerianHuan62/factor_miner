"""复用冻结分数，验证持有周期、起始相位与成本的研究稳健性。"""

from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import shutil

import polars as pl

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.eight_factor_study import reconcile_pnl
from factor_miner.ic_diagnostics import _hac_t
from factor_miner.research_report import performance, report_schedule, write_json
from factor_miner.ridge_strategy import file_hash
from factor_miner.trading_schedule import RebalanceWindow


SCHEDULES = [dict(horizon=5, stride=1, phase=0), dict(horizon=10, stride=2, phase=0),
             dict(horizon=10, stride=2, phase=1)]


def phased_schedule(days: list[date], original: tuple, plan: dict) -> tuple[tuple, list[dict]]:
    """相位沿全期信号序列固定；尾部仅依据市场日历截断。"""
    if plan not in SCHEDULES:
        raise ValueError('日程不属于冻结的三个方案')
    positions = {day: i for i, day in enumerate(days)}
    windows, excluded = [], []
    for window in original[plan['phase']::plan['stride']]:
        start = positions[window.signal_date]
        end = start + 1 + plan['horizon']
        if end >= len(days):
            excluded.append(dict(signal_date=window.signal_date, reason='固定退出超出市场日历'))
            continue
        if window.entry_date != days[start+1]:
            raise ValueError('原信号不符合下一市场交易日入场')
        windows.append(RebalanceWindow(signal_date=window.signal_date,
            entry_date=days[start+1], exit_date=days[end]))
    if not windows:
        raise ValueError('冻结相位没有完整窗口')
    return tuple(windows), excluded


def align_cash_prefix(rows: list[dict], benchmark: list[dict]) -> list[dict]:
    """仅将策略起始前明示为现金；中间和末端缺日绝不填零。"""
    dates = [str(r['exit_date']) for r in rows]
    target = [str(r['exit_date']) for r in benchmark]
    if not dates or len(set(dates)) != len(dates) or dates != sorted(dates):
        raise ValueError('策略收益日期为空、重复或无序')
    if dates[0] not in target or dates != target[target.index(dates[0]):]:
        raise ValueError('策略内部或尾部缺日，禁止以现金填补')
    output = []
    offset = target.index(dates[0])
    for index, bench in enumerate(benchmark):
        if index < offset:
            row = dict(entry_date=bench['entry_date'], exit_date=bench['exit_date'],
                target_long_gross_return=0., target_long_net_return=0., target_long_cost=0.,
                target_long_turnover=0., cost_turnover_sides=2, initial_cash_only=True)
        else:
            row = dict(rows[index-offset], initial_cash_only=False)
            if str(row['entry_date']) != str(bench['entry_date']):
                raise ValueError('收益起止时钟与基准不同')
        output.append(dict(row, benchmark_return=bench['benchmark_return']))
    return output


def period_summary(rows: list[dict]) -> dict:
    """包含完整费用口径及相同日期基准，不混用年度与区间收益。"""
    metrics = performance(rows)
    benchmark = performance([dict(row, target_long_net_return=row['benchmark_return']) for row in rows])
    metrics.update(benchmark_period_return=benchmark['period_return'],
        benchmark_annualized_return=benchmark['annualized_return'],
        annualized_return_minus_benchmark=metrics['annualized_return']-benchmark['annualized_return'],
        two_sided_turnover=sum(float(r['target_long_turnover']) for r in rows),
        cost_return_sum=sum(float(r['target_long_cost']) for r in rows),
        first_date=str(rows[0]['entry_date']), last_date=str(rows[-1]['exit_date']), observations=len(rows))
    return metrics


def research_decision(summaries: dict) -> dict:
    """固定 Ridge 主判断，任何单相位或单年份胜出都不能替代全部条件。"""
    base = summaries['ridge_5d_p0_20bps']
    phases = [summaries[f'ridge_10d_p{p}_20bps'] for p in [0, 1]]
    stress = [summaries[f'ridge_10d_p{p}_40bps'] for p in [0, 1]]
    checks = dict(
        both_phases_positive_and_above_5d=all(p['reference_metrics']['annualized_return'] >
            max(0., base['reference_metrics']['annualized_return']) for p in phases),
        both_phases_positive_before_2026=all(p['before_2026']['period_return'] > 0 for p in phases),
        average_40bps_annualized_positive=sum(p['reference_metrics']['annualized_return'] for p in stress)/2 > 0,
        both_drawdowns_no_worse_and_zero_recovery_positive=all(
            p['reference_metrics']['max_drawdown'] <= base['reference_metrics']['max_drawdown']
            and p['zero_recovery_metrics']['annualized_return'] > 0 for p in phases))
    return dict(continue_mining=all(checks.values()), criteria=checks,
        decision='继续有限机制补充研究' if all(checks.values()) else '停止本轮补挖，保留既有研究与纸面协议',
        primary_model='ridge', sealed_oos=False, test_consumed=True,
        interpretation='研究资源判断，不是统计显著性通过、纯 Alpha 或实盘资格')


def paired_statistics(left: list[dict], right: list[dict], lags: int, family: int) -> dict:
    """同日配对收益差；日频 HAC 参数与既有周频 IC 参数分开冻结。"""
    if [(str(r['entry_date']), str(r['exit_date'])) for r in left] != [
            (str(r['entry_date']), str(r['exit_date'])) for r in right]:
        raise ValueError('配对统计日期不同')
    values = [float(a['target_long_net_return'])-float(b['target_long_net_return']) for a,b in zip(left,right)]
    t_value = _hac_t(values, lags)
    p_value = math.erfc(abs(t_value)/math.sqrt(2))
    return dict(observations=len(values), mean_daily_difference=sum(values)/len(values),
        hac_t=t_value, hac_lags=lags, raw_p=p_value, bonferroni_p=min(1.,p_value*family))


def research_window(config: dict) -> dict[str, date] | None:
    """显式截断独立研究版本，不能只在显示层删掉缺数据年份。"""
    raw = config.get('research_window')
    if raw is None:
        return None
    if set(raw) != {'last_signal_date','observation_end','parity_through'}:
        raise ValueError('历史研究窗口字段不完整')
    result = {key:date.fromisoformat(value) for key,value in raw.items()}
    if not result['parity_through'] <= result['last_signal_date'] < result['observation_end']:
        raise ValueError('历史研究窗口日期顺序不合法')
    return result


def run_execution_study(config_path: Path) -> Path:
    """正式入口：先登记和验证依赖，再进行有限执行计算。"""
    config = json.loads(config_path.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    def event(status: str) -> None:
        with (root/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(status=status, time=datetime.now(timezone.utc).isoformat()))+'\n')
    event('registered_before_new_execution_outcomes')
    try:
        research_window(config)
        if config['schedules'] != SCHEDULES or config['models'] != ['ridge','equal_rank'] or config['round_trip_cost_bps'] != [0,10,20,40]:
            raise ValueError('执行比较不符合冻结合同')
        if config['hac_lags'] != 20 or config['diagnostic_capacity'] != 128 or config['inherited_family_size'] < 12673 or config['diagnostic_family_size'] != config['inherited_family_size']+128:
            raise ValueError('统计参数或继承预算不符合合同')
        if config['primary_model'] != 'ridge' or config['decision_rule'] != 'all_four_contract_conditions' or not config['test_consumed'] or config['sealed_oos']:
            raise ValueError('主模型或历史消费身份不符合合同')
        required = [config[k] for k in ['scores_path','terminal_events_path','contract','source_protocol_path','benchmark_path']]
        required += [str(Path(config['dataset_root'])/name) for name in ['manifest.json','calendar.parquet','market.parquet','state.parquet']]
        required += list(config['reference_paths'].values())
        if set(required)-set(config['input_sha256']):
            raise ValueError('运行依赖缺少登记哈希')
        for path, digest in config['input_sha256'].items():
            if file_hash(Path(path)) != digest:
                raise ValueError(f'输入身份改变：{path}')
        code = root/'code'
        code.mkdir()
        for path in Path(__file__).parent.glob('*.py'):
            shutil.copyfile(path, code/path.name)
        write_json(root/'identity.json', dict(config_sha256=file_hash(config_path),
            code_sha256={p.name:file_hash(p) for p in code.iterdir()}, polars_version=pl.__version__))
        _run(config, root)
        for path, digest in config['input_sha256'].items():
            if file_hash(Path(path)) != digest:
                raise ValueError(f'运行期间输入被修改：{path}')
        write_json(root/'completion.json', dict(status='completed',input_count=len(config['input_sha256']),
            summary_sha256=file_hash(root/'summary.json'), decision_sha256=file_hash(root/'decision.json')))
    except Exception:
        event('failed_preserve_artifacts')
        raise
    event('completed')
    return root


def _run(config: dict, root: Path) -> None:
    bounds = research_window(config)
    panel = pl.read_parquet(config['scores_path']).select('date','asset','ridge','equal_rank').sort('date','asset')
    if bounds:
        panel = panel.filter(pl.col('date') <= bounds['last_signal_date'])
    if panel.select(pl.struct('date','asset').is_duplicated().any()).item() or panel.filter(
            pl.any_horizontal(*(pl.col(c).is_null() | ~pl.col(c).is_finite() for c in ['ridge','equal_rank']))).height:
        raise ValueError('冻结分数有重复主键或缺失值')
    dataset = Path(config['dataset_root'])
    days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
    if bounds:
        days = [day for day in days if day <= bounds['observation_end']]
        if not days or days[-1] != bounds['observation_end']:
            raise ValueError('研究截止日不在输入市场日历')
    original = report_schedule(days,panel['date'].min(),panel['date'].max(),'weekly_last_session')
    if {w.signal_date for w in original} != set(panel['date'].unique().to_list()):
        raise ValueError('冻结分数未完整覆盖每个原周末信号日')
    end = bounds['observation_end'] if bounds else days[-1]
    market = pl.scan_parquet(dataset/'market.parquet').filter(pl.col('date').is_between(panel['date'].min(),end)).select(
        pl.col('date').alias('trade_date'),pl.col('asset').alias('security_id'),'open').collect()
    state = pl.scan_parquet(dataset/'state.parquet').filter(pl.col('date').is_between(panel['date'].min(),end)).select(
        pl.col('date').alias('trade_date'),pl.col('asset').alias('security_id'),
        'valid_for_factor_rank','can_open_long','can_close_long').collect()
    terminal = pl.read_parquet(config['terminal_events_path'])
    if bounds:
        terminal = terminal.filter((pl.col('event_date')<=end) & (pl.col('available_at')<=end))
    benchmark = json.loads(Path(config['benchmark_path']).read_text())['daily_rows']
    if bounds:
        benchmark = [r for r in benchmark if str(r['exit_date'])<=str(end)]
        write_json(root/'research_window.json',dict(**config['research_window'],
            market_last_date=market['trade_date'].max(),state_last_date=state['trade_date'].max(),
            last_signal_date_used=panel['date'].max(),
            interpretation='仅截断研究日期，不按未来行情覆盖过滤历史证券'))
    output, daily_by_name, parity = {}, {}, {}
    for plan in config['schedules']:
        windows, excluded = phased_schedule(days,original,plan)
        schedule_name = f"{plan['horizon']}d_p{plan['phase']}"
        write_json(root/f'schedule_{schedule_name}.json',dict(windows=[w.model_dump(mode='json') for w in windows],excluded=excluded))
        for model in config['models']:
            signal = panel.select(pl.col('date').alias('signal_date'),pl.col('asset').alias('security_id'),pl.col(model).alias('factor_value'))
            for cost in config['round_trip_cost_bps']:
                name = f'{model}_{schedule_name}_{cost}bps'
                print(f'执行冻结情景：{name}', flush=True)
                folder = root/name
                folder.mkdir()
                result = simulate_causal_extreme_portfolio(signal,market,state,windows,group_count=10,
                    round_trip_cost_bps=cost,terminal_policy='report_unresolved',terminal_events=terminal,
                    retain_daily_holdings=(cost==20),allow_noncontiguous_schedule=True)
                daily = align_cash_prefix(list(result.daily_returns),benchmark)
                stress = align_cash_prefix(list(result.zero_recovery_daily_returns),benchmark)
                if name in config['reference_paths']:
                    old = json.loads(Path(config['reference_paths'][name]).read_text())['daily_rows']
                    if bounds:
                        old = [r for r in old if str(r['exit_date'])<=str(end)]
                    old = align_cash_prefix(old,benchmark)
                    pairs = [(a,b) for a,b in zip(daily,old) if not bounds or str(a['exit_date'])<=str(bounds['parity_through'])]
                    if not pairs:
                        raise ValueError('原情景没有可复现前缀')
                    error = max(abs(float(a['target_long_net_return'])-float(b['target_long_net_return'])) for a,b in pairs)
                    if error > 1e-12:
                        raise ValueError(f'原情景收益未复现：{name}，误差 {error}')
                    parity[name] = dict(rows=len(pairs),max_abs_daily_error=error)
                years = sorted({str(r['exit_date'])[:4] for r in daily})
                summary = dict(reference_metrics=period_summary(daily),zero_recovery_metrics=period_summary(stress),
                    actual_metrics=None if result.unresolved_positions else period_summary(daily),
                    before_2026=period_summary([r for r in daily if str(r['exit_date'])<'2026-01-01']),
                    yearly={year:period_summary([r for r in daily if str(r['exit_date']).startswith(year)]) for year in years},
                    execution_summary=result.execution_summary,window_count=len(windows))
                write_json(folder/'backtest.json',dict(**summary,daily_rows=daily,zero_recovery_daily_rows=stress,
                    unresolved_positions=result.unresolved_positions))
                if cost == 20:
                    pnl, error = reconcile_pnl(result)
                    pnl.write_parquet(folder/'pnl.parquet')
                    for key in ['orders','selections','holdings_daily']:
                        pl.DataFrame(getattr(result,key)).write_parquet(folder/f'{key}.parquet')
                    write_json(folder/'reconciliation.json',dict(max_abs_daily_error=error))
                output[name] = summary
                daily_by_name[name] = daily
    statistics = []
    for model in config['models']:
        for cost in [0,20,40]:
            pairs = [(f'{model}_10d_p{p}_{cost}bps',f'{model}_5d_p0_{cost}bps') for p in [0,1]]
            pairs.append((f'{model}_10d_p1_{cost}bps',f'{model}_10d_p0_{cost}bps'))
            for left,right in pairs:
                for period in ['full','before_2026']:
                    a,b = daily_by_name[left],daily_by_name[right]
                    if period == 'before_2026':
                        a,b = [[r for r in rows if str(r['exit_date'])<'2026-01-01'] for rows in [a,b]]
                    statistics.append(dict(left=left,right=right,period=period,
                        **paired_statistics(a,b,config['hac_lags'],config['diagnostic_family_size'])))
    write_json(root/'paired_statistics.json',statistics)
    write_json(root/'baseline_parity.json',parity)
    write_json(root/'summary.json',output)
    write_json(root/'decision.json',research_decision(output))
