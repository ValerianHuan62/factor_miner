"""冻结八因子的收益来源、条件相关性和有限执行诊断。"""

from datetime import date, datetime, timezone
from pathlib import Path
import json
import math
import shutil

import numpy as np
import polars as pl

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.ic_diagnostics import _hac_t
from factor_miner.research_report import performance, report_schedule, write_json
from factor_miner.ridge_strategy import file_hash
from factor_miner.trading_schedule import RebalanceWindow


UNKNOWN = '未知'


def market_cap_group(value: float | None) -> str:
    """以美元市值使用事前固定区间，未知不作为小市值。"""
    if value is None or not math.isfinite(value) or value <= 0:
        return UNKNOWN
    if value < 300_000_000:
        return '1_低于3亿美元'
    if value < 2_000_000_000:
        return '2_3亿至20亿美元'
    if value < 10_000_000_000:
        return '3_20亿至100亿美元'
    return '4_100亿美元以上'


def industry_division(code: int | None) -> str:
    """SIC 大类仅用于汇总，回归保留两位主组。"""
    if code is None:
        return UNKNOWN
    for lo, hi, label in [(100, 999, '农林渔'), (1000, 1499, '采矿'),
        (1500, 1799, '建筑'), (2000, 3999, '制造'), (4000, 4999, '运输通信公用事业'),
        (5000, 5199, '批发'), (5200, 5999, '零售'), (6000, 6799, '金融保险房地产'),
        (7000, 8999, '服务'), (9100, 9799, '公共管理')]:
        if lo <= code <= hi:
            return label
    return UNKNOWN


def attach_industry(keys: pl.DataFrame, master: pl.DataFrame) -> pl.DataFrame:
    """精确有效期连接；重叠且分类冲突时失败，不以最新记录覆盖历史。"""
    master = master.select('security_id', 'valid_from', 'valid_to', 'sic_code').unique()
    joined = keys.join(master, on='security_id', how='inner').filter(
        pl.col('signal_date').is_between(pl.col('valid_from'), pl.col('valid_to'))
        & (pl.col('sic_code') > 0)).select('signal_date', 'security_id', 'sic_code').unique()
    if joined.select(pl.struct('signal_date', 'security_id').is_duplicated().any()).item():
        raise ValueError('行业有效期重叠且分类冲突')
    return keys.join(joined, on=['signal_date', 'security_id'], how='left', validate='1:1')


def build_characteristics(config: dict, keys: pl.DataFrame, root: Path) -> pl.DataFrame:
    """只使用信号日及之前的固定市场日期窗口计算归因特征。"""
    dataset = Path(config['dataset_root'])
    calendar = pl.read_parquet(dataset/'calendar.parquet').with_row_index('_session')
    state = pl.scan_parquet(dataset/'state.parquet').select('date', 'asset', 'valid_for_factor_compute')
    market = pl.scan_parquet(dataset/'market.parquet').join(state, on=['date', 'asset'], validate='1:1').join(calendar.lazy(), on='date').sort('asset', 'date')
    market = market.with_columns(
        pl.when(pl.col('valid_for_factor_compute')).then(pl.col('close')).alias('_price'),
        pl.when(pl.col('valid_for_factor_compute')).then(pl.col('close')*pl.col('volume')).alias('_dollar'))
    market = market.with_columns(
        pl.when(pl.col('_session')-pl.col('_session').shift(1).over('asset') == 1)
        .then(pl.col('_price')/pl.col('_price').shift(1).over('asset')-1).alias('_ret'))
    market = market.with_columns(
        pl.when(pl.col('_session')-pl.col('_session').shift(19).over('asset') == 19)
        .then(pl.col('_dollar').rolling_mean(20, min_samples=20).over('asset')).alias('adv20'),
        pl.when(pl.col('_session')-pl.col('_session').shift(60).over('asset') == 60)
        .then(pl.col('_ret').rolling_std(60, min_samples=60).over('asset')).alias('vol60'),
        pl.when(pl.col('_session')-pl.col('_session').shift(252).over('asset') == 252)
        .then(pl.col('_price').shift(21).over('asset')/pl.col('_price').shift(252).over('asset')-1).alias('mom252_21'))
    signal_days = keys['signal_date'].unique().to_list()
    chars = market.filter(pl.col('date').is_in(signal_days)).select(
        pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'), 'adv20', 'vol60', 'mom252_21').collect()
    caps = pl.scan_parquet(config['crsp_prices_path']).filter(pl.col('session_date').is_in(signal_days)).select(
        pl.col('session_date').alias('signal_date'), 'security_id', (pl.col('market_cap')*1000).alias('market_cap_usd')).collect()
    chars = keys.join(chars, on=['signal_date', 'security_id'], how='left', validate='1:1').join(
        caps, on=['signal_date', 'security_id'], how='left', validate='1:1')
    chars = attach_industry(chars, pl.read_parquet(config['crsp_master_path']))
    # 分母只计非缺失值，未知不进入最小组；每个信号日使用同一研究截面。
    chars = chars.with_columns(
        pl.when(pl.col('adv20').is_finite() & (pl.col('adv20') > 0)).then(pl.col('adv20')).alias('adv20'),
        pl.col('market_cap_usd').map_elements(market_cap_group, return_dtype=pl.String, skip_nulls=False).alias('cap_group'),
        pl.col('sic_code').map_elements(industry_division, return_dtype=pl.String, skip_nulls=False).alias('industry'),
        (pl.col('sic_code')//100).cast(pl.String).fill_null(UNKNOWN).alias('sic2'))
    chars = chars.with_columns(
        ((pl.col('adv20').rank(method='average').over('signal_date') / pl.col('adv20').count().over('signal_date')*5)
            .ceil().clip(1, 5).cast(pl.Int64).cast(pl.String).fill_null(UNKNOWN)).alias('liquidity_group'))
    chars.write_parquet(root/'signal_characteristics.parquet')
    write_json(root/'data_coverage.json', chars.with_columns(pl.col('signal_date').dt.year().alias('year')).group_by('year').agg(
        pl.len().alias('rows'), pl.col('market_cap_usd').is_not_null().mean().alias('cap_coverage'),
        (pl.col('industry') != UNKNOWN).mean().alias('industry_coverage'),
        pl.col('adv20').is_not_null().mean().alias('liquidity_coverage')).sort('year').to_dicts())
    return chars


def reconcile_pnl(result) -> tuple[pl.DataFrame, float]:
    """从持仓和现金订单重建逐笔净损益，与每日净值严格对账。"""
    holdings, orders = {}, {}
    for row in result.holdings_daily:
        positions = holdings.setdefault(row['date'], {})
        key = row['signal_date'], row['security_id']
        # 差额减仓遇到锁仓时，同一原始批次可同时有保留和待卖部分。
        positions[key] = positions.get(key, 0.0) + float(row['market_value'])
    for row in result.orders:
        if row['status'] in {'filled', 'settled'}:
            orders.setdefault(row['actual_date'], []).append(row)
    previous, nav, output, error = {}, 1., [], 0.
    for day in result.daily_returns:
        current_date = day['exit_date']
        current = holdings.get(current_date, {})
        pnl = {key: current.get(key, 0.)-previous.get(key, 0.) for key in set(previous)|set(current)}
        costs = {}
        for order in orders.get(current_date, []):
            key = order['signal_date'], order['security_id']
            cashflow = (-1 if order['side'] == 'buy' else 1)*float(order['gross_notional'])
            cost = float(order['transaction_cost'])
            pnl[key] = pnl.get(key, 0.)+cashflow-cost
            costs[key] = costs.get(key, 0.)+cost
        expected = nav*float(day['target_long_net_return'])
        error = max(error, abs(sum(pnl.values())-expected))
        for (signal, security), value in pnl.items():
            output.append(dict(date=current_date, signal_date=signal, security_id=security,
                net_pnl=value, cost=costs.get((signal, security), 0.), previous_nav=nav,
                starting_market_value=previous.get((signal, security), 0.)))
        nav += expected
        previous = current
    if error > 1e-10:
        raise ValueError(f'逐笔损益对账失败：{error}')
    return pl.DataFrame(output), error


def statistics(values: list[float], family: int) -> dict:
    """固定 HAC 和完整试验族，短序列不作显著性认证。"""
    a = np.asarray(values, dtype=float)
    if not len(a) or not np.isfinite(a).all():
        raise ValueError('统计输入为空或非有限')
    t = _hac_t(a.tolist(), 5) if len(a) >= 20 else None
    p = math.erfc(abs(t)/math.sqrt(2)) if t is not None else None
    return dict(mean=float(a.mean()), observations=len(a), hac_t=t, raw_p=p,
        bonferroni_p=min(1., family*p) if p is not None else None)


def conditional_rank_ic(frame: pl.DataFrame, scores: list[str]) -> list[dict]:
    """Frisch-Waugh 残差相关性，控制前后严格使用相同证券。"""
    controls = ['market_cap_usd', 'adv20', 'vol60', 'mom252_21']
    valid = frame.filter(pl.all_horizontal(*(pl.col(c).is_finite().fill_null(False) for c in controls+['label_o2o_5d']))
        & (pl.col('industry') != UNKNOWN) & (pl.col('market_cap_usd') > 0))
    if valid.height < 100:
        return []
    ranked = valid.select(*(pl.col(c).rank(method='average').alias(c) for c in controls+scores+['label_o2o_5d'])).to_numpy()
    ranked = (ranked-ranked.mean(axis=0))/np.maximum(ranked.std(axis=0), 1e-12)
    industry = valid.select('sic2').to_dummies().to_numpy().astype(float)
    x = np.column_stack([industry, ranked[:, :len(controls)]])
    y = ranked[:, len(controls):]
    residual = y-x@np.linalg.lstsq(x, y, rcond=None)[0]
    return [dict(model=name, names=valid.height, before=float(np.corrcoef(y[:, i], y[:, -1])[0, 1]),
        after=float(np.corrcoef(residual[:, i], residual[:, -1])[0, 1])) for i, name in enumerate(scores)]


def score_diagnostics(panel: pl.DataFrame, chars: pl.DataFrame, scores: list[str], root: Path, family: int) -> dict:
    """逐年分层、十分组与有限风格控制；不输出未成交的假多空回报。"""
    joined = panel.rename({'date': 'signal_date', 'asset': 'security_id'}).join(chars, on=['signal_date', 'security_id'], validate='1:1')
    rows, conditioned, deciles, strata = [], [], [], []
    for (day,), group in joined.partition_by('signal_date', as_dict=True).items():
        for r in conditional_rank_ic(group, scores):
            conditioned.append(dict(date=day, **r))
        for score in scores:
            # 分组在标签可知前完成。缺失标签仅影响诊断覆盖率。
            ranked = group.sort([score, 'security_id']).with_row_index('_rank').with_columns(
                ((pl.col('_rank')*10/pl.len()).floor().cast(pl.Int64)+1).alias('decile'))
            finite = ranked.filter(pl.col('label_o2o_5d').is_finite().fill_null(False))
            ic = finite.select(pl.corr(score, 'label_o2o_5d', method='spearman')).item()
            rows.append(dict(date=day, model=score, rank_ic=ic, names=finite.height, signal_names=group.height))
            for r in ranked.group_by('decile').agg(pl.len().alias('selected'), pl.col('label_o2o_5d').count().alias('labeled'),
                pl.col('label_o2o_5d').mean().alias('mean_return')).to_dicts():
                deciles.append(dict(date=day, model=score, **r))
            for dimension in ['industry', 'cap_group', 'liquidity_group']:
                for (category,), subgroup in finite.partition_by(dimension, as_dict=True).items():
                    if subgroup.height >= 20:
                        value = subgroup.select(pl.corr(score, 'label_o2o_5d', method='spearman')).item()
                        if value is not None and math.isfinite(value):
                            strata.append(dict(date=day, model=score, dimension=dimension, category=category, names=subgroup.height, rank_ic=value))
    for name, data in [('weekly_ic', rows), ('conditional_ic', conditioned), ('deciles', deciles), ('stratum_ic', strata)]:
        if not data:
            raise ValueError(f'{name} 没有可用数据')
        pl.DataFrame(data).sort('model', 'date').write_parquet(root/(name+'.parquet'))
    summary = {}
    for score in scores:
        raw = [r for r in rows if r['model'] == score]
        cond = [r for r in conditioned if r['model'] == score]
        summary[score] = dict(rank_ic=statistics([r['rank_ic'] for r in raw], family),
            conditional_before=statistics([r['before'] for r in cond], family),
            conditional_after=statistics([r['after'] for r in cond], family),
            conditional_dates=[str(min(r['date'] for r in cond)), str(max(r['date'] for r in cond))],
            conditional_median_names=float(np.median([r['names'] for r in cond])))
    write_json(root/'score_summary.json', summary)
    return summary


def ten_day_schedule(days: list[date], original: tuple) -> tuple:
    """新合同隔周十日持有，不修改原五日窗口。"""
    positions = {d: i for i, d in enumerate(days)}
    return tuple(RebalanceWindow(signal_date=w.signal_date, entry_date=w.entry_date, exit_date=days[positions[w.signal_date]+11])
        for w in original[::2] if positions[w.signal_date]+11 < len(days))


def attribution_tables(pnl: pl.DataFrame, chars: pl.DataFrame, folder: Path, daily: list[dict]) -> None:
    """年度和组别贡献以年初净值归一，精确加总至年度复利收益。"""
    pnl = pnl.join(chars, on=['signal_date', 'security_id'], how='left', validate='m:1')
    if pnl['cap_group'].null_count():
        raise ValueError('已成交证券缺少信号日归因行')
    pnl = pnl.with_columns(pl.col('date').dt.year().alias('year'))
    pnl.write_parquet(folder/'pnl_attribution.parquet')
    annual, nav = [], 1.
    for year in sorted({date.fromisoformat(str(r['exit_date'])).year for r in daily}):
        period = [r for r in daily if date.fromisoformat(str(r['exit_date'])).year == year]
        start_nav = nav
        for r in period:
            nav *= 1+float(r['target_long_net_return'])
        annual.append(dict(year=year, start_nav=start_nav, end_nav=nav, period_return=nav/start_nav-1,
            first_date=period[0]['exit_date'], last_date=period[-1]['exit_date'], observations=len(period),
            two_sided_turnover=sum(float(r['target_long_turnover']) for r in period),
            cost_return_sum=sum(float(r['target_long_cost']) for r in period)))
    write_json(folder/'yearly.json', annual)
    output = []
    for dimension in ['industry', 'sic2', 'cap_group', 'liquidity_group']:
        agg = pnl.group_by('year', dimension).agg(pl.col('net_pnl').sum(), pl.col('cost').sum(),
            pl.col('starting_market_value').sum().alias('exposure_value_days'))
        for row in agg.to_dicts():
            y = next(a for a in annual if a['year'] == row['year'])
            output.append(dict(year=row['year'], dimension=dimension, category=row[dimension],
                net_contribution=row['net_pnl']/y['start_nav'], cost_contribution=row['cost']/y['start_nav'],
                capital_weighted_exposure=row['exposure_value_days']/float(pnl.filter(pl.col('year') == row['year'])['starting_market_value'].sum())))
        for y in annual:
            total = sum(r['net_contribution'] for r in output if r['dimension'] == dimension and r['year'] == y['year'])
            if abs(total-y['period_return']) > 1e-10:
                raise ValueError('年度组别贡献未对账')
    write_json(folder/'group_attribution.json', output)


def run_eight_factor_study(config_path: Path) -> Path:
    """唯一正式入口：先登记、检查身份，再生成新诊断。"""
    config = json.loads(config_path.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', config)
    def event(status):
        with (root/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(status=status, time=datetime.now(timezone.utc).isoformat()))+'\n')
    event('registered_before_new_outcomes')
    try:
        if len(config['factors']) != 8 or config['diagnostic_family_size'] < 12029+512:
            raise ValueError('八因子名单或诊断预算不符合合同')
        if len(set(f['factor_id'] for f in config['factors'])) != 8 or any(f['direction'] not in {'positive', 'negative'} for f in config['factors']):
            raise ValueError('因子重复或方向非法')
        if config['costs'] != {'single_factors': [20], '5d_combinations': [0, 10, 20, 40],
                '10d_combinations': [20, 40], 'bottom_long_baskets': [20]} or config['hac_lags'] != 5:
            raise ValueError('本命令只支持合同中已列明的成本和统计参数')
        required = [Path(config['dataset_root'])/f for f in ['manifest.json', 'market.parquet', 'state.parquet', 'calendar.parquet']]
        required += [Path(config[k]) for k in ['terminal_events_path', 'crsp_prices_path', 'crsp_master_path', 'contract']]
        required += [Path(config['source_strategy_root'])/f for f in ['protocol.json', 'weekly_panel.parquet', 'predictions.parquet', 'fit_audit.json', 'baseline_ridge/backtest_20bps.json']]
        required += [Path(f['report_path']) for f in config['factors']]
        if {str(p) for p in required}-set(config['input_sha256']):
            raise ValueError('必需输入没有登记哈希')
        for path, digest in config['input_sha256'].items():
            if file_hash(Path(path)) != digest:
                raise ValueError(f'冻结输入哈希不同：{path}')
        for factor in config['factors']:
            report = json.loads(Path(factor['report_path']).read_text())
            if report['factor_id'] != factor['factor_id'] or report['hypothesis_direction'] != factor['direction']:
                raise ValueError('因子身份或原假设方向不一致')
        original_config = json.loads((Path(config['source_strategy_root'])/'protocol.json').read_text())
        if original_config['models']['baseline_ridge'] != [f['factor_id'] for f in config['factors']]:
            raise ValueError('原 Ridge 基线与所声明的八因子名单不符')
        code = root/'code'
        code.mkdir()
        for name in ['eight_factor_study.py', 'causal_backtest.py', 'research_report.py', 'ic_diagnostics.py', 'trading_schedule.py', 'ridge_strategy.py']:
            shutil.copyfile(Path(__file__).with_name(name), code/name)
        write_json(root/'identity.json', dict(config_sha256=file_hash(config_path),
            code_sha256={p.name:file_hash(p) for p in code.iterdir()}, numpy_version=np.__version__, polars_version=pl.__version__))
        _run(config, root)
    except Exception:
        event('failed_preserve_artifacts')
        raise
    event('completed')
    return root


def _run(config: dict, root: Path) -> None:
    source = Path(config['source_strategy_root'])
    panel = pl.read_parquet(source/'weekly_panel.parquet')
    prediction = pl.read_parquet(source/'predictions.parquet').filter(pl.col('model') == 'baseline_ridge').select(
        'date', 'asset', pl.col('prediction').alias('ridge'))
    names = [f['factor_id'] for f in config['factors']]
    panel = panel.select('date', 'asset', 'label_o2o_5d', *names).join(prediction, on=['date', 'asset'], how='inner', validate='1:1')
    panel = panel.with_columns(*((pl.col(f['factor_id'])*(1 if f['direction'] == 'positive' else -1)).alias(f['factor_id']) for f in config['factors']))
    panel = panel.with_columns(pl.mean_horizontal(*names).alias('equal_rank')).sort('date', 'asset')
    panel.write_parquet(root/'scores.parquet')
    scores = names+['ridge', 'equal_rank']
    keys = panel.select(pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'))
    print('生成历史有效期行业、市值及量价控制变量', flush=True)
    chars = build_characteristics(config, keys, root)
    print('计算十分组、分层及控制后的排序相关性', flush=True)
    score_diagnostics(panel, chars, scores, root, config['diagnostic_family_size'])
    dataset = Path(config['dataset_root'])
    days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
    schedule = report_schedule(days, panel['date'].min(), panel['date'].max(), 'weekly_last_session')
    market = pl.scan_parquet(dataset/'market.parquet').filter(pl.col('date') >= panel['date'].min()).select(
        pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'), 'open').collect()
    state = pl.scan_parquet(dataset/'state.parquet').filter(pl.col('date') >= panel['date'].min()).select(
        pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'), 'valid_for_factor_rank', 'can_open_long', 'can_close_long').collect()
    terminals = pl.read_parquet(config['terminal_events_path'])
    # 原运行已经冻结且保存逐日相同基准，避免引入不同股票池。
    original = json.loads((source/'baseline_ridge/backtest_20bps.json').read_text())
    benchmark = {r['exit_date']:r['benchmark_return'] for r in original['daily_rows']}
    comparisons = {}
    plans = [(n, '5d', 'positive', 20) for n in scores]
    plans += [(n, '5d', 'positive', c) for n in ['ridge', 'equal_rank'] for c in [0, 10, 40]]
    plans += [(n, '5d', 'negative', 20) for n in ['ridge', 'equal_rank']]
    plans += [(n, '10d', 'positive', c) for n in ['ridge', 'equal_rank'] for c in [20, 40]]
    write_json(root/'execution_plan.json', plans)
    for name, horizon, direction, cost in plans:
        model = f'{name}_{horizon}_{direction}_{cost}bps'
        print(f'因果持仓与损益归因：{model}', flush=True)
        folder = root/model
        folder.mkdir()
        signal = panel.select(pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'), pl.col(name).alias('factor_value'))
        windows = schedule if horizon == '5d' else ten_day_schedule(days, schedule)
        result = simulate_causal_extreme_portfolio(signal, market, state, windows, direction=direction, group_count=10,
            round_trip_cost_bps=cost, terminal_policy='report_unresolved', terminal_events=terminals,
            retain_daily_holdings=(cost == 20 and horizon == '5d'), allow_noncontiguous_schedule=True)
        def attach(rows):
            return [dict(r, benchmark_return=benchmark[str(r['exit_date'])]) for r in rows]
        daily, stress = attach(result.daily_returns), attach(result.zero_recovery_daily_returns)
        if model == 'ridge_5d_positive_20bps':
            if len(daily) != len(original['daily_rows']):
                raise ValueError('原 Ridge 日期长度未复现')
            error = max(abs(r['target_long_net_return']-o['target_long_net_return']) for r,o in zip(daily, original['daily_rows']))
            if error > 1e-12:
                raise ValueError(f'原 Ridge 收益未复现：{error}')
            write_json(root/'baseline_parity.json', dict(max_abs_daily_return_error=error, rows=len(daily)))
        payload = dict(model=model, reference_metrics=performance(daily), zero_recovery_metrics=performance(stress),
            actual_metrics=None if result.unresolved_positions else performance(daily),
            execution_summary=result.execution_summary, unresolved_positions=result.unresolved_positions,
            daily_rows=daily, zero_recovery_daily_rows=stress)
        write_json(folder/'backtest.json', payload)
        comparisons[model] = {k: payload[k] for k in ['reference_metrics', 'zero_recovery_metrics', 'actual_metrics', 'execution_summary']}
        if cost == 20 and horizon == '5d':
            pl.DataFrame(result.orders).write_parquet(folder/'orders.parquet')
            pl.DataFrame(result.selections).write_parquet(folder/'selections.parquet')
            pnl, error = reconcile_pnl(result)
            write_json(folder/'reconciliation.json', dict(max_abs_error=error))
            attribution_tables(pnl, chars, folder, daily)
    write_json(root/'strategy_summary.json', comparisons)
    write_json(root/'summary.json', dict(status='completed', test_consumed=True, sealed_oos=False,
        strategy_count=len(plans), model_count=len(scores), diagnostic_family_size=config['diagnostic_family_size'],
        limitations=['2026 年行业和市值缺失，不外推历史分类', '只控制有限行业及量价风格，不能证明 pure alpha',
            '未确定终值情景不是正式投资业绩', '沿用原 28 因子共同截面，尚非八因子完整可用股票池',
            '十日持有复用五日目标预测，属于执行敏感性测试']))
