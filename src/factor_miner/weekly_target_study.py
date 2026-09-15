"""冻结分数下的每周目标差额执行比较，不搜索因子或模型。"""
from datetime import date,datetime,timezone
from pathlib import Path
import json
import polars as pl
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.eight_factor_study import reconcile_pnl
from factor_miner.execution_study import align_cash_prefix,period_summary,paired_statistics
from factor_miner.research_report import report_schedule,write_json
from factor_miner.ridge_strategy import file_hash


def actual_portfolio_metrics(rows: list[dict], unresolved: bool) -> dict | None:
    """已平仓组合可报告绝对收益；继承的参考基准不冒充实际相对绩效。"""
    if unresolved:
        return None
    metrics = period_summary(rows)
    for key in ['information_ratio', 'benchmark_period_return', 'benchmark_annualized_return',
                'annualized_return_minus_benchmark']:
        metrics[key] = None
    metrics['relative_metrics_status'] = '继承基准是参考估值，相对指标仅在reference_metrics展示'
    return metrics


def run_weekly_target_study(config_path: Path) -> Path:
    """先登记全部依赖与执行比较，再计算每笔真实差额订单。"""
    c=json.loads(config_path.read_text());root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c)
    write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=file_hash(config_path)))
    try:
        if c['models']!=['ridge','equal_rank'] or c['costs']!=[0,10,20,40] or c['modes']!=['fixed_horizon','weekly_target']:
            raise ValueError('比较范围不符合已冻结合同')
        if not c['test_consumed'] or c['sealed_oos'] or c['diagnostic_family_size']<15157:
            raise ValueError('研究资格或继承预算错误')
        dataset=Path(c['dataset_root'])
        required={c['scores_path'],c['terminal_events_path'],c['benchmark_path'],c['contract_path'],*c['reference_paths'].values()}
        required.update(str(dataset/n) for n in ['market.parquet','state.parquet','calendar.parquet','manifest.json'])
        if required-set(c['input_sha256']):raise ValueError('依赖未绑定哈希')
        def verify():
            for p,digest in c['input_sha256'].items():
                if file_hash(Path(p))!=digest:raise ValueError(f'输入变化：{p}')
        verify()
        from factor_miner.artifact_storage import snapshot_code
        snapshot_code(root)
        end=date.fromisoformat(c['observation_end']);last=date.fromisoformat(c['last_signal_date'])
        if last>=end:raise ValueError('尾部退出窗口缺失')
        panel=pl.read_parquet(c['scores_path']).filter(pl.col('date')<=last).select('date','asset',*c['models'])
        if panel.select(pl.struct('date','asset').is_duplicated().any()).item():raise ValueError('重复分数')
        if panel.filter(pl.any_horizontal(*(~pl.col(m).is_finite().fill_null(False) for m in c['models']))).height:raise ValueError('无效分数')
        days=pl.read_parquet(dataset/'calendar.parquet')['date'].to_list();days=[d for d in days if d<=end]
        if days[-1]!=end or panel['date'].max()!=last:raise ValueError('分数或观察终点不符')
        schedule=report_schedule(days,panel['date'].min(),last,'weekly_last_session')
        if {w.signal_date for w in schedule}!=set(panel['date'].unique().to_list()):raise ValueError('周频分数缺日')
        write_json(root/'schedule.json',[w.model_dump(mode='json') for w in schedule])
        bounds=pl.col('date').is_between(panel['date'].min(),end)
        market=pl.scan_parquet(dataset/'market.parquet').filter(bounds).select(pl.col('date').alias('trade_date'),pl.col('asset').alias('security_id'),'open').collect()
        state=pl.scan_parquet(dataset/'state.parquet').filter(bounds).select(pl.col('date').alias('trade_date'),pl.col('asset').alias('security_id'),'valid_for_factor_rank','can_open_long','can_close_long').collect()
        terminal=pl.read_parquet(c['terminal_events_path']).filter((pl.col('event_date')<=end)&(pl.col('available_at')<=end))
        benchmark=[r for r in json.loads(Path(c['benchmark_path']).read_text())['daily_rows'] if str(r['exit_date'])<=str(end)]
        summaries={};daily={};parity={}
        for model in c['models']:
            signal=panel.select(pl.col('date').alias('signal_date'),pl.col('asset').alias('security_id'),pl.col(model).alias('factor_value'))
            for mode in c['modes']:
                for cost in c['costs']:
                    name=f'{model}_{mode}_{cost}bps';print(name,flush=True)
                    folder=root/name;folder.mkdir()
                    result=simulate_causal_extreme_portfolio(signal,market,state,schedule,group_count=10,round_trip_cost_bps=cost,terminal_policy='report_unresolved',terminal_events=terminal,retain_daily_holdings=(cost==20),allow_noncontiguous_schedule=True,execution_mode=mode)
                    rows=align_cash_prefix(list(result.daily_returns),benchmark);stress=align_cash_prefix(list(result.zero_recovery_daily_returns),benchmark)
                    if name in c['reference_paths']:
                        old=json.loads(Path(c['reference_paths'][name]).read_text())['daily_rows']
                        old=align_cash_prefix([r for r in old if str(r['exit_date'])<=str(end)],benchmark)
                        error=max(abs(float(a['target_long_net_return'])-float(b['target_long_net_return'])) for a,b in zip(rows,old) if str(a['exit_date'])<=str(last))
                        if error>1e-12:raise ValueError(f'原口径未复现：{name}/{error}')
                        parity[name]=error
                    summary=dict(execution=result.execution_summary,reference_metrics=period_summary(rows),zero_recovery_metrics=period_summary(stress),actual_metrics=actual_portfolio_metrics(rows,bool(result.unresolved_positions)),yearly={y:period_summary([r for r in rows if str(r['exit_date']).startswith(y)]) for y in sorted({str(r['exit_date'])[:4] for r in rows})})
                    write_json(folder/'backtest.json',dict(**summary,daily_rows=rows,zero_recovery_daily_rows=stress,unresolved_positions=result.unresolved_positions))
                    if cost==20:
                        pnl,error=reconcile_pnl(result);pnl.write_parquet(folder/'pnl.parquet')
                        for key in ['orders','holdings_daily','selections']:pl.DataFrame(getattr(result,key)).write_parquet(folder/f'{key}.parquet')
                        write_json(folder/'reconciliation.json',dict(max_error=error))
                    summaries[name]=summary;daily[name]=rows
        write_json(root/'summary.json',summaries);write_json(root/'baseline_parity.json',parity)
        write_json(root/'paired_statistics.json',[dict(model=m,cost=cost,**paired_statistics(daily[f'{m}_weekly_target_{cost}bps'],daily[f'{m}_fixed_horizon_{cost}bps'],20,c['diagnostic_family_size'])) for m in c['models'] for cost in c['costs']])
        verify();write_json(root/'completion.json',dict(status='completed',summary_sha256=file_hash(root/'summary.json')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error),status='preserved_failure'));raise
    return root
