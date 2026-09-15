"""按事前规则诊断顶部选股和固定代表增量，最后由用户决定入库。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import json

import polars as pl

from factor_miner.artifact_storage import digest_file, snapshot_code
from factor_miner.research_report import write_json


def top_signals(scores: pl.DataFrame, column: str, count: int) -> pl.DataFrame:
    """仅根据信号日得分确定目标；未来缺报价或买不到不能换股。"""
    if count < 1 or scores.select(pl.struct('date', 'asset').is_duplicated().any()).item():
        raise ValueError('顶部选择数量或主键不合法')
    ranked = scores.filter(pl.col(column).is_finite()).sort(['date', column, 'asset'], descending=[False, True, False])
    return ranked.with_columns(pl.int_range(pl.len()).over('date').alias('_order')).select(
        'date', 'asset', (pl.col('_order') < count).alias('trigger'))


def oriented_rank_panel(panel: pl.DataFrame, directions: dict[str, int], baseline: list[str]) -> pl.DataFrame:
    """共同可得截面先冻结，再按原方向计算秩；不拟合或按收益挑权重。"""
    if not baseline or set(baseline) - directions.keys() or any(d not in {-1, 1} for d in directions.values()):
        raise ValueError('原方向或基线身份不合法')
    common = panel.filter(pl.all_horizontal(*[pl.col(c).is_finite() for c in directions]))
    common = common.with_columns(*[((pl.col(c) * sign).rank('average').over('date') - .5)
        .truediv(pl.len().over('date')).alias(c) for c, sign in directions.items()])
    return common.with_columns(pl.mean_horizontal(*baseline).alias('baseline36'))


def execute_selected_top(factor, market, state, schedule, *, cost, terminal_policy, terminals):
    """选股完成后仅加载可能持有证券的全时段价格；不按未来可成交性筛选。"""
    from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
    securities = factor['security_id'].unique().implode()
    return simulate_causal_extreme_portfolio(factor,
        market.filter(pl.col('security_id').is_in(securities)),
        state.filter(pl.col('security_id').is_in(securities)), schedule, group_count=1,
        round_trip_cost_bps=cost, terminal_policy=terminal_policy, terminal_events=terminals,
        allow_noncontiguous_schedule=True, retain_daily_holdings=False)


def top_label_column(config: dict, holding_sessions: int) -> str:
    """顶部参考标签与真实成交使用同一期限，旧五日协议不得被重解释。"""
    version = config['version']
    if version in {'top-selection-diagnostic-v1', 'top-selection-diagnostic-v2'}:
        if holding_sessions != 5:
            raise ValueError('旧顶部协议仅支持五日持有')
    elif version == 'top-selection-diagnostic-v3':
        if holding_sessions not in {1, 5, 20} or config.get('holding_sessions') != holding_sessions:
            raise ValueError('顶部标签与冻结持有期不一致')
    else:
        raise ValueError('未知顶部协议版本')
    return f'label_o2o_{holding_sessions}d'


def run_top_selection(config_path: Path, run_root: Path, output: Path) -> Path:
    """执行冻结的分层、成本与增量检查，保存所有结果而不写候选库。"""
    from factor_miner.favor_workflow import load_plan, verify_inputs, _trade_inputs
    from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
    from factor_miner.favor_integration import portfolio_metrics
    c = json.loads(config_path.read_text())
    plan = load_plan(run_root)
    if digest_file(Path(c['plan_path'])) != c['plan_sha256']:
        raise ValueError('顶部预登记身份不符')
    label_column = top_label_column(c, plan.execution.holding_sessions)
    positive_selection = c['version'] in {'top-selection-diagnostic-v2', 'top-selection-diagnostic-v3'}
    if positive_selection and any(x.expected_return_sign != 'positive' for x in plan.conditions):
        raise ValueError('量价正向选优要求所有候选事前正向')
    for key in ('library', 'feature'):
        if digest_file(Path(c[key + '_path'])) != c[key + '_sha256']:
            raise ValueError('顶部比较输入身份变化：' + key)
    if json.loads((run_root/'completion.json').read_text())['summary_sha256'] != digest_file(run_root/'summary.json'):
        raise ValueError('候选完成回执不符')
    # 顶部规则必须先于收益暴露；不能把事后分析冒充预登记。
    from datetime import datetime
    started = json.loads((run_root/'evaluation_started.json').read_text())['started_at']
    if datetime.fromisoformat(c['created_at']) >= datetime.fromisoformat(started):
        raise ValueError('顶部规则没有在收益暴露之前登记')
    verify_inputs(plan)
    summary = json.loads((run_root/'summary.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'protocol.json', c)
    snapshot_code(output)
    try:
        from factor_miner.research_pool import ResearchResourceGuard
        guard = ResearchResourceGuard(plan.search_budget, output)
        guard.check()
        members = [m for m in json.loads(Path(c['library_path']).read_text())['members'] if m['role'] == '研究代表']
        if len(members) != 36:
            raise ValueError('基线必须保持登记的36代表')
        baseline = ['factor_miner_' + m['factor_id'] for m in members]
        directions = {col: 1 if m['original_direction'] == 'positive' else -1 for col,m in zip(baseline,members)}
        conditions = {x.condition_id:x for x in plan.conditions}
        trials = [s.trial_id for s in plan.slots]
        if any(summary['factors'][t]['status'] != 'exploration_evaluated' for t in trials):
            write_json(output/'summary.json',dict(status='not_comparable',reason='共同候选存在未完成探索的名额',
                trials={t:summary['factors'][t]['status'] for t in trials},proposed_for_user_review=[]))
            return output
        raw_hashes={str(run_root/'factors'/t/'raw_factor.parquet'):digest_file(run_root/'factors'/t/'raw_factor.parquet') for t in trials}
        write_json(output/'candidate_inputs.json',raw_hashes)
        dataset = Path(plan.dataset_root)
        start,end=plan.splits.discovery_start,plan.splits.validation_end
        market=pl.read_parquet(dataset/'market.parquet',columns=['date','asset','open']).filter(pl.col('date').is_between(start,end))
        state=pl.read_parquet(dataset/'state.parquet').filter(pl.col('date').is_between(start,end))
        days=pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
        wide=pl.scan_parquet(c['feature_path']).filter(pl.col('date').is_between(start,end)).select(
            'date',pl.col('order_book_id').alias('asset'),*baseline).collect()
        wide=wide.join(state.filter(pl.col('valid_for_factor_compute') & pl.col('valid_for_factor_rank')).select('date','asset'),on=['date','asset'],validate='1:1')
        for slot in plan.slots:
            raw=pl.scan_parquet(run_root/'factors'/slot.trial_id/'raw_factor.parquet').filter(pl.col('valid_for_factor_compute')).select(
                'date','asset',pl.col('raw_factor').alias(slot.trial_id)).collect()
            wide=wide.join(raw,on=['date','asset'],validate='1:1')
            directions[slot.trial_id]=1 if conditions[slot.condition_id].expected_return_sign=='positive' else -1
        common=wide.filter(pl.all_horizontal(*[pl.col(x).is_finite() for x in directions]))
        discovery=common.filter(pl.col('date')<=plan.splits.discovery_end)
        redundancy={}
        for t in trials:
            daily=discovery.group_by('date').agg(*[pl.corr(t,b,method='spearman').alias(b) for b in baseline]).sort('date')
            daily.write_parquet(output/(t+'_redundancy.parquet'))
            values={b:daily[b].abs().quantile(.95,interpolation='linear') for b in baseline}
            finite={b:v for b,v in values.items() if v is not None}
            if len(finite)!=36:
                raise ValueError('冗余比较存在不可计算代表')
            closest=max(finite,key=finite.get)
            redundancy[t]=dict(max_absolute_daily_spearman_p95=finite[closest],closest=closest,passed=finite[closest]<.75)
        del discovery,wide
        reports={t:dict(redundancy=redundancy[t],windows={}) for t in trials}
        terminals=pl.read_parquet(plan.terminal_events_path)
        for window,(lo,hi) in c['windows'].items():
            lo,hi=date.fromisoformat(lo),date.fromisoformat(hi)
            schedule,trade_market,trade_state=_trade_inputs(plan,market,state,days,lo,hi,hi.fromordinal(hi.toordinal()+1))
            signal_dates=[w.signal_date for w in schedule]
            measured=common.filter(pl.col('date').is_in(signal_dates))
            scores=oriented_rank_panel(measured,directions,baseline).select('date','asset','baseline36',*trials)
            if set(signal_dates)-set(scores['date'].to_list()):
                raise ValueError('共同截面缺少登记信号日')
            scores=scores.with_columns(*[((pl.col('baseline36')*36+pl.col(t))/37).alias(t+'_augmented') for t in trials])
            scores.write_parquet(output/(window+'_scores.parquet'))
            coverage=state.filter(pl.col('valid_for_factor_rank')&pl.col('date').is_in(signal_dates)).group_by('date').len(name='universe').join(scores.group_by('date').len(name='common'),on='date',validate='1:1').with_columns((pl.col('common')/pl.col('universe')).alias('coverage'))
            coverage.write_parquet(output/(window+'_coverage.parquet'))
            labels=pl.scan_parquet(dataset/'label.parquet').filter(pl.col('date').is_in(signal_dates)&(pl.col('label_exit_date')<=hi)).select('date','asset',label_column).collect()
            # 信号排名已经冻结；标签缺失只影响参考统计，不删除实盘选择。
            reference=state.filter(pl.col('valid_for_factor_rank')&pl.col('date').is_in(signal_dates)).select('date','asset').join(labels,on=['date','asset'],how='left',validate='1:1').group_by('date').agg(pl.col(label_column).mean().alias('universe_ew_reference'))
            baseline_returns={}
            selections={}
            def evaluate(column,n,cost):
                guard.check()
                key=f'{column}_top{n}_{cost}bps'
                signals=top_signals(scores,column,n)
                factor=signals.filter(pl.col('trigger')).select(pl.col('date').alias('signal_date'),pl.col('asset').alias('security_id'),pl.lit(1.).alias('factor_value'))
                if positive_selection:
                    result=execute_selected_top(factor,trade_market,trade_state,schedule,cost=cost,
                        terminal_policy=plan.execution.terminal_policy,terminals=terminals)
                else:
                    result=simulate_causal_extreme_portfolio(factor,trade_market,trade_state,schedule,group_count=1,
                        round_trip_cost_bps=cost,terminal_policy=plan.execution.terminal_policy,terminal_events=terminals,
                        allow_noncontiguous_schedule=True,retain_daily_holdings=False)
                folder=output/window/key;folder.mkdir(parents=True)
                daily=pl.DataFrame(result.daily_returns);daily.write_parquet(folder/'daily_returns.parquet')
                pl.DataFrame(result.selections).write_parquet(folder/'selections.parquet')
                write_json(folder/'orders.json',result.orders)
                write_json(folder/'unresolved.json',result.unresolved_positions)
                write_json(folder/'zero_recovery.json',result.zero_recovery_daily_returns)
                metrics=portfolio_metrics(result)
                if positive_selection:
                    # 此处尚未传入基准，不能把相对零收益的比率称为超额收益信息比率。
                    metrics['reference']['information_ratio']=None
                    if metrics['actual'] is not None:
                        metrics['actual']['information_ratio']=None
                metrics['mean_daily_turnover']=daily['target_long_turnover'].mean()
                metrics['daily_return_q01']=daily['target_long_net_return'].quantile(.01)
                annual=[]
                for yr in sorted(set(str(d)[:4] for d in daily['exit_date'])):
                    part=daily.filter(pl.col('exit_date').cast(pl.String).str.starts_with(yr))
                    annual.append(dict(year=yr,net_return=float((part['target_long_net_return']+1).product()-1),days=part.height,reference_only=bool(result.unresolved_positions)))
                metrics['yearly']=annual;write_json(folder/'metrics.json',metrics)
                selections[key]=metrics
                return daily,metrics
            print(window+'：执行36代表顶部基线',flush=True)
            for n in [10,20,50]:
                for cost in c['cost_bps']:
                    daily,metrics=evaluate('baseline36',n,cost)
                    if n==20:baseline_returns[cost]=(daily,metrics)
            for t in trials:
                print(window+'：顶部与增量 '+t,flush=True)
                ranked=scores.select('date','asset',((pl.col(t)*10).floor().clip(0,9)+1).cast(pl.Int32).alias('decile'))
                grouped=ranked.join(labels,on=['date','asset'],how='left',validate='1:1').group_by('date','decile').agg(pl.len().alias('selected'),pl.col(label_column).count().alias('known_labels'),pl.col(label_column).mean().alias('mean_return')).join(reference,on='date',validate='m:1')
                grouped.write_parquet(output/(window+'_'+t+'_deciles.parquet'))
                q10=grouped.filter(pl.col('decile')==10)['mean_return'].mean()
                q6_9=grouped.filter(pl.col('decile').is_between(6,9)).group_by('date').agg(pl.col('mean_return').mean())['mean_return'].mean()
                report=dict(q10_reference_mean=q10,q6_q9_reference_mean=q6_9,upper_half_advantage=q10>q6_9,
                    q10_minus_universe_ew_reference=grouped.filter(pl.col('decile')==10).select((pl.col('mean_return')-pl.col('universe_ew_reference')).mean()).item(),
                    median_common_coverage=coverage['coverage'].median(),top={},augmentation={})
                def paired(daily,cost):
                    base,bm=baseline_returns[cost]
                    paired=daily.select('exit_date','target_long_net_return').join(base.select('exit_date',pl.col('target_long_net_return').alias('baseline')),on='exit_date',validate='1:1')
                    if paired.height!=daily.height or paired.height!=base.height:raise ValueError('顶部基线日期不一致')
                    return paired.select((pl.col('target_long_net_return')-pl.col('baseline')).mean()).item(),bm['actual'] is not None
                for n in [10,20,50]:
                    for cost in c['cost_bps']:
                        daily,metrics=evaluate(t,n,cost)
                        if n==20:
                            delta,base_actual=paired(daily,cost);metrics=dict(metrics,mean_daily_excess_vs_baseline36=delta,baseline_actual_available=base_actual)
                        report['top'][f'{n}_{cost}']=metrics
                for cost in c['cost_bps']:
                    daily,metrics=evaluate(t+'_augmented',20,cost);delta,base_actual=paired(daily,cost)
                    report['augmentation'][str(cost)]=dict(metrics,mean_daily_excess_vs_baseline36=delta,baseline_actual_available=base_actual)
                reports[t]['windows'][window]=report
            write_json(output/(window+'_execution_index.json'),selections)
        proposed=[]
        for t in trials:
            factor=summary['factors'][t];sign=directions[t]
            v=reports[t]['windows']['validation'];top=v['top']['20_14'];aug=v['augmentation']['14']
            q10=factor['validation'][0]['metrics']['reference']['annualized_return']
            gates=dict(construct_and_direction=factor['discovery']['ic_mean']*sign>0 and factor['discovery']['rank_ic_mean']*sign>0,
                q10_positive=q10>0,upper_half_advantage=all(x['upper_half_advantage'] for x in reports[t]['windows'].values()),
                top20_positive_and_increment=top['actual'] is not None and top['baseline_actual_available'] and top['actual']['annualized_return']>0 and top['mean_daily_excess_vs_baseline36']>0,
                augmentation_positive=aug['actual'] is not None and aug['baseline_actual_available'] and aug['mean_daily_excess_vs_baseline36']>0,
                redundancy=reports[t]['redundancy']['passed'])
            if positive_selection:
                ws=reports[t]['windows']
                gates['q10_exceeds_universe_both_windows']=all(x['q10_minus_universe_ew_reference']>0 for x in ws.values())
                gates['top20_positive_both_windows']=all(x['top']['20_14']['actual'] is not None and
                    x['top']['20_14']['actual']['annualized_return']>0 for x in ws.values())
                gates['top20_increment_both_windows']=all(x['top']['20_14']['baseline_actual_available'] and
                    x['top']['20_14']['mean_daily_excess_vs_baseline36']>0 for x in ws.values())
                years=[y for x in ws.values() for y in x['top']['20_14']['yearly'] if y['year']!='2026']
                gates['positive_in_three_of_five_years']=len(years)==5 and sum(
                    not y['reference_only'] and y['net_return']>0 for y in years)>=3
            reports[t]['gates']=gates;reports[t]['proposed_for_user_review']=all(gates.values())
            if all(gates.values()):proposed.append(t)
        verify_inputs(plan)
        if any(digest_file(Path(p))!=h for p,h in raw_hashes.items()):
            raise ValueError('候选原始矩阵在比较期间变化')
        for key in ('library','feature'):
            if digest_file(Path(c[key+'_path']))!=c[key+'_sha256']:raise ValueError('结束时输入身份变化')
        write_json(output/'summary.json',dict(status='completed',factors=reports,proposed_for_user_review=proposed,
            formal_pass=None,library_written=False,industry_size_reference='未执行：缺少经核验的历史可得匹配输入',
            source_run=str(run_root),source_summary_sha256=digest_file(run_root/'summary.json')))
        write_json(output/'completion.json',dict(summary_sha256=digest_file(output/'summary.json')))
    except Exception as error:
        write_json(output/'failure.json',dict(error=str(error)))
        raise
    return output
