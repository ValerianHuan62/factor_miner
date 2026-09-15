"""固定周度采样的多期限组合对照，预测期限与实际调仓分开检验。"""
from datetime import date
import json
import math
from pathlib import Path

import polars as pl

from factor_miner.favor_validation import require_panel
from factor_miner.ic_diagnostics import _hac_t
from factor_miner.quote_research import _snapshot_code, _verify_code, _verify_inputs
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash, join_targets, walk_forward


def model_rank_ic(predictions: pl.DataFrame, labels: pl.DataFrame, label_column: str) -> pl.DataFrame:
    """仅统计时排除缺标签和跨年度退出事件，预测截面始终完整保留。"""
    rows = predictions.join(labels.select('date', 'asset', 'label_exit_date'), on=['date', 'asset'], validate='m:1')
    return (rows.filter(pl.col(label_column).is_finite()
                        & (pl.col('label_exit_date').dt.year() == pl.col('date').dt.year()))
            .group_by('model', 'date').agg(pl.len().alias('names'),
                pl.corr('prediction', label_column, method='spearman').alias('rank_ic'))
            .filter((pl.col('names') >= 20) & pl.col('rank_ic').is_finite()).sort('model', 'date'))


def paired_model_effect(ic: pl.DataFrame, left: str, right: str, hac_lags: int, family_size: int) -> dict:
    """同日期对照，正值表示左侧模型更好。"""
    paired = ic.filter(pl.col('model') == left).select('date', pl.col('rank_ic').alias('left')).join(
        ic.filter(pl.col('model') == right).select('date', pl.col('rank_ic').alias('right')), on='date', validate='1:1').sort('date')
    if paired.height < 60:
        raise ValueError('配对周数不足60')
    paired = paired.with_columns((pl.col('left') - pl.col('right')).alias('delta'))
    t = _hac_t(paired['delta'].to_list(), hac_lags)
    p = math.erfc(abs(t) / math.sqrt(2))
    return dict(left=left, right=right, weeks=paired.height, mean_delta=paired['delta'].mean(),
                hac_max_lags=hac_lags, hac_t=t, raw_p=p, bonferroni_p=min(1., p * family_size),
                yearly=paired.with_columns(pl.col('date').dt.year().alias('year')).group_by('year').agg(
                    pl.col('delta').mean(), pl.len().alias('weeks')).sort('year').to_dicts())


def run_horizon_models(config_path: Path) -> Path:
    """冻结全部模型和期限后走步训练，不将IC对照升级为FaVOR或交易资格。"""
    config = json.loads(config_path.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / 'protocol.json', config)
    code = _snapshot_code(root)
    from datetime import datetime, timezone
    write_json(root / 'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    try:
        horizons = config['horizons']
        models = config['models']
        comparisons = config['comparisons']
        features = config['features']
        if horizons != [1, 5, 20] or not config['test_consumed'] or config['new_formula_capacity'] != 0:
            raise ValueError('本入口仅支持已冻结信号的1/5/20日探索性模型诊断')
        if config['signal_sampling'] != 'weekly_last_session':
            raise ValueError('本入口固定周度采样，不冒充日频或月频执行')
        if len(models) * len(horizons) > config['model_capacity'] or len(comparisons) * len(horizons) > config['comparison_capacity']:
            raise ValueError('超过冻结模型或比较预算')
        if config['diagnostic_family_size'] < config['inherited_diagnostic_family_size'] + config['model_capacity'] + config['comparison_capacity']:
            raise ValueError('诊断预算未继承完整历史与本次全部比较')
        if len(features) != len(set(features)) or any(not cols or len(cols) != len(set(cols)) or set(cols) - set(features) for cols in models.values()):
            raise ValueError('模型输入名单非法')
        if any(x['left'] not in models or x['right'] not in models for x in comparisons):
            raise ValueError('比较引用未登记模型')
        required = {config['source_panel_path'], config['calendar_path'], config['source_protocol_path'],
                    *config['label_paths'].values()}
        _verify_inputs(config, required)
        source = json.loads(Path(config['source_protocol_path']).read_text())
        if source['model_policy'] != config['model_policy']:
            raise ValueError('本对照不得同时改变训练参数')
        signals = pl.read_parquet(config['source_panel_path'], columns=['date', 'asset', *features])
        require_panel(signals, {'date', 'asset', *features}, '共同特征面板')
        if signals.select(pl.any_horizontal(*(~pl.col(f).is_finite().fill_null(False) for f in features)).any()).item():
            raise ValueError('共同预测特征包含未知值')
        dates = sorted(signals['date'].unique().to_list())
        days = pl.read_parquet(config['calendar_path'])['date'].to_list()
        last = {d.isocalendar()[:2]: d for d in days}
        expected_dates = [d for d in last.values() if date.fromisoformat(source['history_start']) <= d <= date.fromisoformat(source['evaluation_end'])]
        if dates != expected_dates:
            raise ValueError('输入面板缺少冻结的周末信号日')
        summaries = []
        for horizon in horizons:
            folder = root / f'{horizon}d'
            folder.mkdir()
            col = f'label_o2o_{horizon}d'
            labels = pl.read_parquet(config['label_paths'][str(horizon)], columns=['date', 'asset', col, 'label_exit_date'])
            require_panel(labels, {'date', 'asset', col, 'label_exit_date'}, '固定期限标签')
            panel = join_targets(signals.lazy(), labels.lazy(), label_column=col)
            predicted, fits = walk_forward(panel, dates, models, config['model_policy'],
                date.fromisoformat(config['evaluation_start']), label_column=col)
            predicted.write_parquet(folder / 'predictions.parquet')
            write_json(folder / 'fit_audit.json', fits)
            ic = model_rank_ic(predicted, labels, col)
            ic.write_parquet(folder / 'weekly_ic.parquet')
            lag = config['hac_week_lags'][str(horizon)]
            effects = [paired_model_effect(ic, x['left'], x['right'], lag, config['diagnostic_family_size']) for x in comparisons]
            conflicts = []
            for fit in fits:
                for feature, beta in zip(fit['features'], fit['beta']):
                    expected = 1 if config['expected_signs'][feature] == 'positive' else -1
                    if beta * expected < 0:
                        conflicts.append(dict(model=fit['model'], prediction_start=fit['prediction_start'], feature=feature, beta=beta))
            write_json(folder / 'coefficient_direction_conflicts.json', conflicts)
            summaries.append(dict(horizon=horizon, models=len(models), fits=len(fits), effects=effects,
                coefficient_direction_conflicts=len(conflicts), prediction_rows=predicted.height,
                missing_label_predictions=predicted[col].null_count(),
                model_ic=ic.group_by('model').agg(pl.col('rank_ic').mean(), pl.len().alias('weeks')).sort('model').to_dicts()))
            write_json(folder / 'completion.json', dict(predictions_sha256=file_hash(folder / 'predictions.parquet'),
                fits_sha256=file_hash(folder / 'fit_audit.json'), ic_sha256=file_hash(folder / 'weekly_ic.parquet')))
            print(f'{horizon}日组合对照完成', flush=True)
        _verify_inputs(config, required)
        _verify_code(code)
        write_json(root / 'summary.json', dict(rows=summaries, qualified_additions=0, selected_horizon=None,
            test_consumed=True, sealed_oos=False, execution_or_cost_tested=False,
            scope='固定周度采样的多期限预测对照；未替代FaVOR，未验证日频或月频策略'))
        write_json(root / 'completion.json', dict(status='completed', summary_sha256=file_hash(root / 'summary.json')))
    except Exception as error:
        write_json(root / 'failure.json', dict(error=str(error)))
        raise
    return root


def run_training_window_study(config_path: Path) -> Path:
    """固定因子和共同预测日期，只比较事前指定的训练长度，不选择最好窗口。"""
    from datetime import datetime, timezone
    import time
    from factor_miner.compiler import build_polars_expr, compile_candidate, attach_market_sessions
    from factor_miner.schema import TrustedCandidateFactorSpec
    from factor_miner.research_pool import ResearchResourceGuard, validate_search_budget
    c = json.loads(config_path.read_text())
    root = Path(c['output_root']); root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c)
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    try:
        if c['version'] != 'training-window-study-v1' or c['train_windows'] != [156, 520] or not c['test_consumed']:
            raise ValueError('本入口固定三年与十年训练对照，不自动搜索窗口')
        if c['budget']['validation_target'] != 'portfolio_increment' or c['budget']['model_capacity'] < 3:
            raise ValueError('训练对照须预约两个模型与一次比较')
        budget = validate_search_budget(c['budget'], [])
        guard = ResearchResourceGuard(c['budget'], root)
        required = {str(Path(c['dataset_root'])/n) for n in ['market.parquet','state.parquet','calendar.parquet','label.parquet','manifest.json']}
        required |= {f['spec_path'] for f in c['factors']} | {c['market_context_source']}
        _verify_inputs(c, required)
        code = _snapshot_code(root)
        dataset = Path(c['dataset_root'])
        calendar = pl.read_parquet(dataset/'calendar.parquet')
        days = calendar['date'].to_list()
        from factor_miner.research_report import report_schedule
        dates = [w.signal_date for w in report_schedule(days, date.fromisoformat(c['history_start']), date.fromisoformat(c['evaluation_end']), 'weekly_last_session')]
        evaluation_start = date.fromisoformat(c['evaluation_start'])
        first_index = next(i for i, day in enumerate(dates) if day >= evaluation_start)
        if first_index < max(c['train_windows']) + c['model_policy']['embargo_weeks']:
            raise ValueError('完整十年训练历史不足；不得延迟预测起点伪装同期间对照')
        market = pl.read_parquet(dataset/'market.parquet')
        context = pl.scan_parquet(c['market_context_source']).filter(pl.col(c['context_id_column']) == c['context_id']).select(
            pl.col(c['context_date_column']).alias('date'), pl.col(c['context_return_column']).alias('market_return')).collect()
        if context['date'].n_unique() != context.height:
            raise ValueError('市场上下文日期重复')
        market = market.join(context, on='date', how='left', validate='m:1')
        market = attach_market_sessions(market.lazy(), calendar).collect().sort('asset','date').with_columns(
            (pl.col('date').cum_count().over('asset') > 119).alias('_warmup'))
        state = pl.read_parquet(dataset/'state.parquet')
        signals = state.filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_rank')).select('date','asset')
        features = []
        for f in c['factors']:
            guard.check()
            spec = TrustedCandidateFactorSpec.model_validate_json(Path(f['spec_path']).read_text())
            compiled = compile_candidate(spec, set(spec.required_fields))
            if compiled.candidate_id != f['candidate_id']:
                raise ValueError('冻结因子身份不符')
            name = f['factor_id']; features.append(name)
            raw = market.with_columns(build_polars_expr(spec.expression).alias(name)).filter(pl.col('date').is_in(dates)).join(
                state.select('date','asset','valid_for_factor_compute'), on=['date','asset'], validate='1:1').filter(
                    pl.col('valid_for_factor_compute') & pl.col('_warmup') & pl.col(name).is_finite()).select('date','asset',name)
            signals = signals.join(raw, on=['date','asset'], how='inner', validate='1:1')
            print(f'训练对照原式计算完成：{name}', flush=True)
        if len(features) != len(set(features)):
            raise ValueError('模型因子身份重复')
        signals = signals.with_columns(*((pl.col(n).rank(method='average').over('date')/pl.len().over('date')-.5).alias(n) for n in features))
        if sorted(signals['date'].unique().to_list()) != dates:
            raise ValueError('共同特征面板缺少预定信号日')
        signals.write_parquet(root/'weekly_signals.parquet')
        labels = pl.read_parquet(dataset/'label.parquet')
        panel = join_targets(signals.lazy(), labels.lazy())
        effects, audits, counts = [], [], []
        for weeks in c['train_windows']:
            guard.check()
            name = f'train_{weeks}'
            started = time.perf_counter()
            cpu_started = time.process_time()
            predictions, fits = walk_forward(panel, dates, {name: features}, dict(c['model_policy'], train_weeks=weeks), evaluation_start)
            elapsed = time.perf_counter() - started
            cpu_elapsed = time.process_time() - cpu_started
            dates_used = predictions['date'].unique().sort().to_list()
            if dates_used != dates[first_index:]:
                raise ValueError('训练窗改变了预测日期，不可比较')
            effects.append(model_rank_ic(predictions, labels, 'label_o2o_5d'))
            audits.extend(fits)
            counts.append(dict(model=name, fits=len(fits), training_and_prediction_seconds=elapsed, cpu_seconds=cpu_elapsed,
                total_fitted_rows=sum(fit['train_rows'] for fit in fits), first_train_signal=str(fits[0]['train_first_signal_date']),
                last_train_signal=str(fits[-1]['train_last_signal_date']), prediction_dates=len(dates_used), prediction_rows=predictions.height))
            # 固定问题只需要逐周效果和拟合审计，不保存两份大预测矩阵。
        ic = pl.concat(effects)
        ic.write_parquet(root/'weekly_ic.parquet')
        write_json(root/'fit_audit.json', audits)
        effect = paired_model_effect(ic, 'train_520', 'train_156', 5, budget['diagnostic_family_size'])
        _verify_inputs(c, required); _verify_code(code); guard.check()
        write_json(root/'summary.json', dict(status='completed', models=counts, effect=effect,
            mean_rank_ic=ic.group_by('model').agg(pl.col('rank_ic').mean()).to_dicts(),
            selected_window=None, qualified_additions=0, test_consumed=True, sealed_oos=False,
            scope='同因子同预测日期的训练长度诊断；不授予因子资格或交易收益结论'))
        write_json(root/'completion.json', dict(summary_sha256=file_hash(root/'summary.json')))
    except Exception as error:
        write_json(root/'failure.json', dict(error=str(error)))
        raise
    return root
