"""按测量类型探索信号；探索资格、预测诊断和最终通过不混写。"""
from pathlib import Path

import polars as pl

from factor_miner.compiler import build_polars_expr
from factor_miner.favor_validation import require_panel
from factor_miner.favor_integration import percentile_scores


def observation_dates(days, rule):
    """只使用日历确定观察日；不把未结束周月当作完整周期。"""
    if rule.observation_frequency == 'daily':
        return days
    key = (lambda d: (d.year, d.month)) if rule.observation_frequency == 'monthly_last_session' else (lambda d: d.isocalendar()[:2])
    return [d for d, following in zip(days, days[1:]) if key(d) != key(following)]


def observation_frame(raw, state, days, rule, direction):
    """覆盖率分母保留全部当时可排名证券；未知不补成无事件。"""
    require_panel(raw, {'date', 'asset', 'raw_factor', 'valid_for_factor_compute'}, '探索信号')
    require_panel(state, {'date', 'asset', 'valid_for_factor_rank'}, '探索状态')
    for frame, key in ((raw, 'valid_for_factor_compute'), (state, 'valid_for_factor_rank')):
        if frame.schema[key] != pl.Boolean or frame[key].null_count():
            raise ValueError('探索 mask 必须是非空布尔值')
    frame = state.filter(pl.col('valid_for_factor_rank')).select('date', 'asset').join(
        raw, on=['date', 'asset'], how='left', validate='1:1').with_columns(
        (pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite()).fill_null(False).alias('_valid'))
    if rule.kind == 'continuous':
        scores = percentile_scores(raw, state, direction)
        frame = frame.join(scores, on=['date', 'asset'], how='left', validate='1:1').with_columns(
            (pl.col('score') >= rule.activation_quantile).alias('_active'))
    else:
        if frame.filter(pl.col('_valid') & ~pl.col('raw_factor').is_in(rule.state_values)).height:
            raise ValueError('离散信号含未登记的状态取值')
        frame = frame.with_columns(pl.when(pl.col('_valid')).then(pl.col('raw_factor').is_in(rule.active_values)).otherwise(None).alias('_active'))
    dates = observation_dates(days, rule)
    frame = frame.filter(pl.col('date').is_in(dates)).join(
        pl.DataFrame({'date': dates, '_session': range(len(dates))}, schema={'date': pl.Date, '_session': pl.Int64}), on='date', validate='m:1').sort('asset', 'date')
    if rule.kind == 'event':
        # 只数事件开始；连续激活不能复制成多次事件，缺日后的激活不能臆断为新事件。
        previous = pl.col('_active').shift(1).over('asset')
        adjacent = pl.col('_session').diff().over('asset') == 1
        frame = frame.with_columns((pl.col('_active') & (previous == False) & adjacent).fill_null(False).alias('_trigger'),
                                   (pl.col('_active') == False).alias('_control'))
    else:
        frame = frame.with_columns(pl.col('_active').fill_null(False).alias('_trigger'), (~pl.col('_active')).alias('_control'))
    return frame


def validate_exploration_construct(raw, market, state, condition, start, end, rule, days):
    """连续测量检验截面秩关联；状态及事件检验事前激活组与对照组。"""
    # 在筛选发现区间前计算历史观察，避免制造起始事件或压缩缺失期。
    frame = observation_frame(raw.filter(pl.col('date') <= end), state.filter(pl.col('date') <= end), days, rule, condition.activation_direction)
    frame = frame.filter(pl.col('date').is_between(start, end))
    counts = frame.group_by('date').agg(pl.len().alias('universe'), pl.col('_valid').sum().alias('usable'))
    coverage = counts.select((pl.col('usable') / pl.col('universe')).median()).item() if counts.height else None
    valid = frame.filter(pl.col('_valid'))
    support = (coverage is not None and coverage >= rule.min_signal_coverage
               and valid['date'].n_unique() >= rule.min_dates and valid['asset'].n_unique() >= rule.min_assets)
    rows = []
    for measurement in condition.state_measurements:
        observed = market.filter(pl.col('date') <= end).sort('asset', 'date').select(
            'date', 'asset', build_polars_expr(measurement.expression).alias('_measurement'))
        paired = valid.join(observed, on=['date', 'asset'], how='left', validate='1:1').filter(pl.col('_measurement').is_finite())
        measurement_coverage = paired.height / valid.height if valid.height else 0.
        direction = 1 if measurement.expected_direction == 'increase' else -1
        if rule.kind == 'continuous':
            daily = paired.group_by('date').agg(pl.len().alias('n'),
                pl.col('raw_factor').n_unique().alias('unique'),
                pl.corr('raw_factor', '_measurement', method='spearman').alias('rho')).filter(
                (pl.col('n') >= rule.min_assets) & (pl.col('unique') >= 2) & pl.col('rho').is_finite())
            association = daily['rho'].median() if daily.height else None
            enough = daily.height >= rule.min_dates and paired.height >= 2*rule.min_group_samples
            aligned = association is not None and direction*association >= rule.min_rank_correlation
            detail = dict(valid_dates=daily.height, median_daily_rank_correlation=association)
        else:
            selected = paired.filter(pl.col('_trigger') | pl.col('_control').fill_null(False))
            active = selected.filter(pl.col('_trigger'))
            control = selected.filter(pl.col('_control').fill_null(False))
            comparison = active.group_by('date').agg(pl.col('_measurement').mean().alias('active')).join(
                control.group_by('date').agg(pl.col('_measurement').mean().alias('control')), on='date', validate='1:1')
            difference = comparison.select((pl.col('active')-pl.col('control')).median()).item() if comparison.height else None
            enough = (active.height >= rule.min_group_samples and control.height >= rule.min_group_samples
                      and comparison.height >= rule.min_dates and active['asset'].n_unique() >= rule.min_assets)
            aligned = difference is not None and direction*difference > 0
            detail = dict(active_samples=active.height, control_samples=control.height,
                active_assets=active['asset'].n_unique(), comparison_dates=comparison.height,
                median_daily_state_difference=difference)
        enough = enough and measurement_coverage >= rule.min_signal_coverage
        rows.append(dict(name=measurement.name, sufficient_support=enough,
            measurement_coverage=measurement_coverage, semantic_alignment=aligned, **detail))
    enough = support and all(r['sufficient_support'] for r in rows)
    passed = enough and all(r['semantic_alignment'] for r in rows)
    return dict(version='exploration-construct-v1', kind=rule.kind, passed=passed,
        status='eligible_for_exploration' if passed else 'insufficient_sample' if not enough else 'measurement_mismatch',
        signal_coverage=coverage, measurements=rows, return_labels_used=False,
        mechanism_status='mechanism_unverified', scope='测量探索资格；不证明预测能力或独立经济机制')


def finish_exploration(plan, root, ledger, reports, raw, market, state, days, guard=None):
    """固定方向和唯一持仓方案；仅发现期 IC 与验证期成本诊断，不打开测试标签。"""
    from factor_miner.canonical import sha256_json
    from factor_miner.favor_workflow import file_sha, _trade_inputs
    from factor_miner.favor_integration import execute_joint, portfolio_metrics
    from factor_miner.research_report import write_json, ic_diagnostics
    from factor_miner.research_pool import validate_search_budget
    from factor_miner.ledger import TrialEvent, EventType
    split = plan.splits
    family = validate_search_budget(plan.search_budget, [s.model_dump(exclude={'condition_id'}) for s in plan.slots])['diagnostic_family_size']
    # 先保存资格，不读取收益挑选谁可以进入探索。
    write_json(root/'exploration_freeze.json', dict(eligible_trials=sorted(raw),
        plan_sha256=sha256_json(plan.model_dump(mode='json')), outcome_used_for_eligibility=False))
    if raw:
        ledger.append_event(TrialEvent(event_type=EventType.OUTCOME_EXPOSED, outcome_exposed=True, status='exploration_discovery_validation'))
        h = plan.execution.holding_sessions
        column = f'label_o2o_{h}d'
        labels = pl.scan_parquet(Path(plan.dataset_root)/'label.parquet').filter(
            pl.col('date').is_between(split.discovery_start, split.discovery_end)).collect()
        require_panel(labels, {'date', 'asset', column, 'label_entry_date', 'label_exit_date'}, '探索标签')
        expected = pl.DataFrame([dict(date=d, entry=days[i+1], exit=days[i+h+1]) for i,d in enumerate(days[:-(h+1)])])
        checked = labels.join(expected, on='date', how='left', validate='m:1')
        if checked.filter(pl.col(column).is_finite() & ~(
            (pl.col('label_entry_date') == pl.col('entry')) & (pl.col('label_exit_date') == pl.col('exit'))).fill_null(False)).height:
            raise ValueError('探索标签不是固定市场日历入场退出')
        trade = _trade_inputs(plan, market, state, days, split.validation_start, split.validation_end, split.test_start)
        terminals = pl.read_parquet(plan.terminal_events_path) if plan.terminal_events_path else None
        benchmark_signals = state.filter(pl.col('valid_for_factor_compute') & pl.col('valid_for_factor_rank')).select('date','asset',pl.lit(True).alias('trigger'))
        benchmark = execute_joint(benchmark_signals, trade[1], trade[2], trade[0], cost_bps=0.,
            terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
        conditions = {c.condition_id:c for c in plan.conditions}
        for trial, values in raw.items():
            if guard is not None:
                guard.check()
            condition = conditions[reports[trial]['condition_id']]
            rule = plan.exploration[condition.condition_id]
            # 观察频率冻结；月度信号不会复制成每天一个 IC 样本。
            observed = values.filter(pl.col('date').is_in(observation_dates(days, rule)))
            # IC 的既有 60 日/20 证券门槛不下调。它不适用或样本不足时单列，不能否决事件探索。
            sample = observed.join(state.select('date','asset','valid_for_factor_rank'),on=['date','asset'],validate='1:1').join(
                labels,on=['date','asset'],validate='1:1').filter(pl.col('date').is_between(split.discovery_start,split.discovery_end)
                & (pl.col('label_exit_date') < split.validation_start) & pl.col('valid_for_factor_compute')
                & pl.col('valid_for_factor_rank') & pl.col('raw_factor').is_finite() & pl.col(column).is_finite())
            ic_dates = sample.group_by('date').agg(pl.len().alias('n'), pl.corr('raw_factor',column).alias('ic'),
                pl.corr('raw_factor',column,method='spearman').alias('rank')).filter(
                (pl.col('n') >= 20) & pl.col('ic').is_finite() & pl.col('rank').is_finite()).height
            reports[trial]['standalone_statistical_pass'] = None
            reports[trial]['ic_diagnostic_status'] = 'not_applicable' if rule.kind == 'event' else 'insufficient_sample'
            if rule.kind != 'event' and ic_dates >= 60:
                daily, metrics = ic_diagnostics(observed.lazy(), state.lazy(), labels.lazy(), split.discovery_start,
                    split.discovery_end, split.validation_start, family, label_column=column, hac_max_lags=max(5,h))
                daily.write_parquet(root/'factors'/trial/'discovery_ic.parquet')
                reports[trial]['discovery'] = metrics
                reports[trial]['ic_diagnostic_status'] = 'evaluated'
                if family is None:
                    reports[trial]['statistical_inference_status'] = 'unavailable_historical_budget'
                else:
                    reports[trial]['standalone_statistical_pass'] = bool(metrics['bonferroni_p_value'] < .05 and
                        metrics['rank_ic_mean']*(1 if condition.expected_return_sign == 'positive' else -1) > 0)
            signals = observation_frame(values, state, days, rule, condition.activation_direction).select(
                'date','asset',pl.col('_trigger').alias('trigger'))
            diagnostics = []
            for cost in (plan.execution.round_trip_cost_bps, *plan.execution.stress_cost_bps):
                result = execute_joint(signals, trade[1], trade[2], trade[0], cost_bps=cost,
                    terminal_policy=plan.execution.terminal_policy, terminal_events=terminals)
                diagnostics.append(dict(cost_bps=cost, metrics=portfolio_metrics(result, benchmark),
                    summary=result.execution_summary, daily_returns=result.daily_returns,
                    unresolved_positions=result.unresolved_positions, zero_recovery_daily_returns=result.zero_recovery_daily_returns,
                    orders=result.orders, selections=result.selections))
            write_json(root/'factors'/trial/'validation_diagnostics.json', diagnostics)
            reports[trial].update(status='exploration_evaluated', reason='固定方案探索已评价；收益高低均不自动晋级',
                qualification='仅探索资格；待独立确认及组合增量检验', validation=[dict(cost_bps=x['cost_bps'],metrics=x['metrics']) for x in diagnostics])
    for trial, report in reports.items():
        write_json(root/'trials'/f'{trial}.json', report)
        ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_COMPLETED,
            candidate_id=report.get('source_candidate_id',f'reserved_{trial}'), status=report['status'], outcome_exposed=trial in raw))
    write_json(root/'strategy_freeze.json', dict(combinations={}, retained_trials=[], test_used_for_selection=False,
        plan_sha256=sha256_json(plan.model_dump(mode='json'))))
    write_json(root/'summary.json', dict(version=plan.version, run_kind=plan.run_kind, status='completed', market_id=plan.market_id,
        plan_sha256=sha256_json(plan.model_dump(mode='json')), test_consumed=split.test_consumed, sealed_oos=False,
        test_evaluated=False, return_labels_used=bool(raw), registered=len(plan.slots),
        submitted=sum((root/'submissions'/s.trial_id/'receipt.json').exists() for s in plan.slots),
        construct_passed=len(raw), exploration_eligible=len(raw), retained_components=0, retained_combinations=0,
        diagnostic_family_size=family, factors=reports, combinations={},
        scope='探索结果，未做测试期评价、联合策略认证或组合增量确认；不得自动升级旧失败'))
    if guard is not None:
        guard.check()
    write_json(root/'completion.json', dict(summary_sha256=file_sha(root/'summary.json'),
        strategy_freeze_sha256=file_sha(root/'strategy_freeze.json'), exploration_freeze_sha256=file_sha(root/'exploration_freeze.json')))
