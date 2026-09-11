"""既有冻结因子联合 Ridge：事件 purge、走步预测与统一因果组合回测。"""

from datetime import date, datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import shutil
import subprocess

import numpy as np
import polars as pl

from factor_miner.research_incremental import fit_purged_ridge_models
from factor_miner.research_report import write_json, report_schedule, ic_diagnostics, performance
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.ic_diagnostics import _hac_t


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def join_targets(signals: pl.LazyFrame, labels: pl.LazyFrame, *, label_column: str = 'label_o2o_5d') -> pl.DataFrame:
    """保留没有未来标签的预测证券；固定退出日使目标截面标准化可因果使用。"""
    panel = signals.join(labels, on=['date', 'asset'], how='left', validate='1:1').collect()
    finite = pl.col(label_column).is_finite().fill_null(False)
    if panel.filter(finite & pl.col('label_exit_date').is_null()).height:
        raise ValueError('有限标签缺少退出事件')
    if panel.filter(finite).group_by('date').agg(pl.col('label_exit_date').n_unique().alias('n')).filter(pl.col('n') != 1).height:
        raise ValueError('同一信号日必须使用同一固定日历标签退出日')
    return panel.with_columns(pl.when(finite).then(pl.col(label_column)).alias(label_column)).with_columns(
        ((pl.col(label_column) - pl.col(label_column).mean().over('date'))
         / pl.col(label_column).std(ddof=0).over('date')).alias('target_z')
    ).sort('date', 'asset')


def walk_forward(panel: pl.DataFrame, dates: list[date], models: dict[str, list[str]], policy: dict,
                 evaluation_start: date, *, label_column: str = 'label_o2o_5d') -> tuple[pl.DataFrame, list[dict]]:
    """在固定历史窗拟合，保留全部可预测证券并记录每轮参数和 purge 边界。"""
    train_weeks, embargo, frequency = (int(policy[k]) for k in ('train_weeks', 'embargo_weeks', 'retrain_weeks'))
    alpha = float(policy['alpha'])
    if min(train_weeks, embargo, frequency) < 1 or not math.isfinite(alpha) or alpha <= 0:
        raise ValueError('训练窗口、隔离周数、重训频率和 alpha 必须为正')
    start = max(train_weeks + embargo, next(i for i, d in enumerate(dates) if d >= evaluation_start))
    fitted, audits, predictions = {}, [], []
    for index in range(start, len(dates)):
        day = dates[index]
        current = panel.filter(pl.col('date') == day)
        if current.height < 20:
            raise ValueError(f'预测截面不足20只证券：{day}')
        if not fitted or (index - start) % frequency == 0:
            history = panel.filter(pl.col('date').is_in(dates[index-embargo-train_weeks:index-embargo]))
            print(f'Ridge 重训 {day}', flush=True)
            fitted_group = fit_purged_ridge_models(history, models, day, alpha)
            for name, columns in models.items():
                beta, intercept, audit = fitted_group[name]
                fitted[name] = beta, intercept
                audits.append(dict(model=name, prediction_start=day, features=columns, beta=beta.tolist(),
                    intercept=intercept, alpha=alpha, train_first_signal_date=history['date'].min(),
                    purged_unfinished_label_rows=history.filter(pl.col('label_exit_date') >= day).height,
                    **audit))
        for name, columns in models.items():
            beta, intercept = fitted[name]
            values = current.select(columns).to_numpy() @ beta + intercept
            if not np.isfinite(values).all():
                raise ValueError('预测包含非有限值')
            predictions.append(current.select('date', 'asset', label_column).with_columns(
                pl.Series('prediction', values), pl.lit(name).alias('model')))
    if not predictions:
        raise ValueError('历史不足，未产生预测')
    return pl.concat(predictions).sort('model', 'date', 'asset'), audits


def run_ridge_strategy(config_path: Path) -> Path:
    """CLI 专用入口：不可覆盖输出；失败保留冻结协议和已产生结果。"""
    config = json.loads(config_path.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / 'protocol.json', config)
    def event(status: str) -> None:
        with (root / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(status=status, time=datetime.now(timezone.utc).isoformat()), ensure_ascii=False) + '\n')
    event('registered_before_outcomes')
    try:
        _run(config, root, config_path)
    except Exception:
        event('failed_preserve_all_artifacts')
        raise
    event('completed')
    return root


def _run(config: dict, root: Path, config_path: Path) -> None:
    required_paths = [Path(config['dataset_root']) / f for f in ('manifest.json', 'market.parquet', 'state.parquet', 'calendar.parquet', 'label.parquet')]
    required_paths += [Path(config['terminal_events_path']), Path(config['library_review_path'])]
    required_paths += [Path(f[key]) for f in config['factors'] for key in ('raw_path', 'report_path')]
    if config.get('research_pool_path'):
        required_paths.append(Path(config['research_pool_path']))
    for path in required_paths:
        if str(path) not in config['input_sha256'] or file_hash(path) != config['input_sha256'][str(path)]:
            raise ValueError(f'输入身份不匹配：{path.name}')
    code_root = root / 'code'
    code_root.mkdir()
    code_hashes = {}
    for name in ('ridge_strategy.py', 'research_incremental.py', 'research_pool.py', 'research_report.py', 'causal_backtest.py', 'ic_diagnostics.py', 'trading_schedule.py'):
        source = Path(__file__).parent / name
        shutil.copyfile(source, code_root / name)
        code_hashes[name] = file_hash(source)
    write_json(root / 'identity.json', dict(code_sha256=code_hashes, config_sha256=file_hash(config_path),
        git_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        numpy_version=np.__version__, polars_version=pl.__version__, deterministic=True))
    dataset = Path(config['dataset_root'])
    days = pl.read_parquet(dataset / 'calendar.parquet')['date'].to_list()
    dates = [w.signal_date for w in report_schedule(days, date.fromisoformat(config['history_start']),
        date.fromisoformat(config['evaluation_end']), 'weekly_last_session')]
    state = pl.scan_parquet(dataset / 'state.parquet')
    signals = state.filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_rank')).select('date', 'asset')
    features = []
    for factor in config['factors']:
        name = factor['factor_id']
        features.append(name)
        raw = pl.scan_parquet(factor['raw_path']).filter(pl.col('date').is_in(dates)
            & pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite()).select('date', 'asset', pl.col('raw_factor').alias(name))
        signals = signals.join(raw, on=['date', 'asset'], how='inner', validate='1:1')
    # 仅按当日特征定义共同股票池，截面排名属于模型预处理，原始因子保持不变。
    signals = signals.with_columns(*((pl.col(n).rank(method='average').over('date') / pl.len().over('date') - .5).alias(n) for n in features))
    labels = pl.scan_parquet(dataset / 'label.parquet').select('date', 'asset', 'label_o2o_5d', 'label_exit_date')
    panel = join_targets(signals, labels)
    panel.write_parquet(root / 'weekly_panel.parquet')
    universe = state.filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_rank')).group_by('date').agg(
        pl.len().alias('universe')).collect()
    coverage = panel.group_by('date').agg(pl.len().alias('complete_signals')).join(universe, on='date').with_columns(
        (pl.col('complete_signals')/pl.col('universe')).alias('coverage')).sort('date')
    coverage.write_parquet(root/'common_universe_coverage.parquet')
    models = config.get('models') or {'baseline_ridge': [config['baseline_factor_id']], 'joint_ridge': features}
    if (not {'baseline_ridge', 'joint_ridge'} <= set(models)
            or any(not cols or len(cols) != len(set(cols)) or set(cols)-set(features) for cols in models.values())
            or len(models) > config.get('model_capacity', 128)):
        raise ValueError('模型名单非法或超过冻结模型容量')
    if config.get('research_pool_path'):
        from factor_miner.research_incremental import explicit_model_candidates
        pool = json.loads(Path(config['research_pool_path']).read_text())
        if set(features) != set(pool['selected_factor_ids']):
            raise ValueError('模型输入与研究池代表不一致')
        if any(f['raw_path'] != pool['raw_paths'][f['factor_id']] for f in config['factors']):
            raise ValueError('模型矩阵与研究池身份不一致')
        if config['diagnostic_family_size'] < pool['inherited_diagnostic_family_size']+config['model_capacity']:
            raise ValueError('组合模型诊断预算不能缩减已继承的试验历史')
        explicit_model_candidates(list(pool['reports'].values()), pool['baseline_factor_ids'], pool['addition_factor_ids'], pool)
    predicted, fits = walk_forward(panel, dates, models, config['model_policy'], date.fromisoformat(config['evaluation_start']))
    predicted.write_parquet(root / 'predictions.parquet')
    write_json(root / 'fit_audit.json', fits)
    write_json(root / 'prediction_freeze.json', dict(sha256=file_hash(root / 'predictions.parquet'),
        first_prediction=predicted['date'].min(), last_prediction=predicted['date'].max(),
        prediction_rows=predicted.height, missing_label_predictions=predicted['label_o2o_5d'].null_count()))
    del panel, signals
    schedule = report_schedule(days, predicted['date'].min(), predicted['date'].max(), 'weekly_last_session')
    signal_days = [w.signal_date for w in schedule]
    market = pl.scan_parquet(dataset / 'market.parquet').filter(pl.col('date') >= signal_days[0]).select(
        pl.col('date').alias('trade_date'), pl.col('asset').alias('security_id'), 'open').collect()
    trade_state = state.filter(pl.col('date') >= signal_days[0]).select(pl.col('date').alias('trade_date'),
        pl.col('asset').alias('security_id'), 'valid_for_factor_rank', 'can_open_long', 'can_close_long').collect()
    terminals = pl.read_parquet(config['terminal_events_path'])
    common = dict(terminal_policy='report_unresolved', terminal_events=terminals,
                  retain_daily_holdings=False, allow_noncontiguous_schedule=True)
    benchmark_signal = state.filter(pl.col('date').is_in(signal_days) & pl.col('valid_for_factor_compute')).select(
        pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'), pl.lit(1.).alias('factor_value')).collect()
    print('因果回测：合格股票池等权基准', flush=True)
    benchmark = simulate_causal_extreme_portfolio(benchmark_signal, market, trade_state, schedule, group_count=1, **common)
    maps = [{r['exit_date']: r for r in rows} for rows in (benchmark.daily_returns, benchmark.zero_recovery_daily_returns)]
    write_json(root / 'benchmark.json', dict(summary=benchmark.execution_summary, unresolved_positions=benchmark.unresolved_positions))
    def attach(rows: tuple, index: int) -> list[dict]:
        output = []
        for row in rows:
            match = maps[index].get(row['exit_date'])
            if match is None and row['exit_date'] <= max(maps[index]):
                raise ValueError('基准缺少中间交易日')
            output.append({**row, 'entry_date': str(row['entry_date']), 'exit_date': str(row['exit_date']),
                'benchmark_return': float(match['target_long_net_return']) if match else 0.})
        return output
    summaries, ic_series = {}, {}
    for name in models:
        folder = root / name
        folder.mkdir()
        raw = predicted.filter(pl.col('model') == name).select('date', 'asset', pl.col('prediction').alias('raw_factor'), pl.lit(True).alias('valid_for_factor_compute'))
        daily, ic = ic_diagnostics(raw.lazy(), state, labels, signal_days[0], signal_days[-1], None, int(config['diagnostic_family_size']))
        daily.write_parquet(folder / 'weekly_ic.parquet')
        ic_series[name] = daily
        signal = raw.select(pl.col('date').alias('signal_date'), pl.col('asset').alias('security_id'), pl.col('raw_factor').alias('factor_value'))
        scenarios = {}
        costs = config['round_trip_cost_bps'] if name in config.get('portfolio_models', list(models)) else []
        for cost in costs:
            print(f'因果回测：{name}，双边成本 {cost} bps', flush=True)
            result = simulate_causal_extreme_portfolio(signal, market, trade_state, schedule,
                direction='positive', group_count=10, round_trip_cost_bps=cost, **common)
            normal, stress = attach(result.daily_returns, 0), attach(result.zero_recovery_daily_returns, 1)
            payload = dict(model=name, round_trip_cost_bps=cost, daily_rows=normal, zero_recovery_daily_rows=stress,
                reference_metrics=performance(normal), zero_recovery_metrics=performance(stress),
                actual_metrics=None if result.unresolved_positions else performance(normal),
                unresolved_positions=result.unresolved_positions, execution_summary=result.execution_summary)
            write_json(folder / f'backtest_{cost}bps.json', payload)
            if cost == config['round_trip_cost_bps'][0]:
                pl.DataFrame(result.orders).write_parquet(folder / 'orders.parquet')
                pl.DataFrame(result.selections).write_parquet(folder / 'selections.parquet')
            scenarios[str(cost)] = {k: payload[k] for k in ('reference_metrics', 'zero_recovery_metrics', 'actual_metrics')}
            scenarios[str(cost)]['unresolved_positions'] = len(result.unresolved_positions)
        summaries[name] = dict(features=models[name], ic=ic, scenarios=scenarios)
    comparisons = {}
    for name in models:
        if name == 'joint_ridge':
            continue
        paired = ic_series['joint_ridge'].select('date', pl.col('rank_ic').alias('joint')).join(
            ic_series[name].select('date', pl.col('rank_ic').alias('reference')), on='date').sort('date')
        values = (paired['joint']-paired['reference']).to_list()
        statistic = _hac_t(values, 5)
        comparisons[name] = dict(joint_minus_reference_rank_ic=float(np.mean(values)), hac_t=statistic,
            raw_p=math.erfc(abs(statistic)/math.sqrt(2)),
            bonferroni_p=min(1., config['diagnostic_family_size']*math.erfc(abs(statistic)/math.sqrt(2))),
            yearly=[dict(year=year, delta=float(g.select((pl.col('joint')-pl.col('reference')).mean()).item()))
                    for (year,), g in paired.with_columns(pl.col('date').dt.year().alias('year')).partition_by('year', as_dict=True).items()])
    pairs = ic_series['joint_ridge'].select('date', pl.col('rank_ic').alias('joint')).join(
        ic_series['baseline_ridge'].select('date', pl.col('rank_ic').alias('baseline')), on='date').sort('date')
    delta = (pairs['joint'] - pairs['baseline']).to_list()
    t = _hac_t(delta, 5)
    write_json(root / 'summary.json', dict(status='completed', mode='Smoke', sealed_oos=False, test_consumed=True,
        scope='既有全期筛选因子的探索性组合；训练因果，但特征选择并非历史时点可知，不能称为无偏样本外',
        first_prediction=signal_days[0], last_prediction=signal_days[-1], weeks=len(signal_days),
        common_universe_median_coverage=float(coverage.filter(pl.col('date').is_in(signal_days))['coverage'].median()),
        models=summaries, comparisons=comparisons, incremental_rank_ic=float(np.mean(delta)), incremental_hac_t=t,
        incremental_bonferroni_p=min(1., config['diagnostic_family_size'] * math.erfc(abs(t) / math.sqrt(2)))))
