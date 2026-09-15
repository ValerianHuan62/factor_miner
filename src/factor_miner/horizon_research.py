"""冻结多期限标签与既有信号的期限诊断，不自动选择最好期限或升级候选资格。"""
from pathlib import Path
from datetime import date,datetime,timezone
import json
import polars as pl
from factor_miner.label_dataset import build_fixed_session_o2o_labels
from factor_miner.favor_validation import require_panel
from factor_miner.quote_research import _snapshot_code,_verify_code,_verify_inputs
from factor_miner.research_report import write_json,ic_diagnostics
from factor_miner.ridge_strategy import file_hash


def tradable_fixed_labels(market: pl.DataFrame,state: pl.DataFrame,calendar: pl.DataFrame,horizon: int) -> pl.DataFrame:
    """固定日期缺少价格或不可交易则标签未知，不顺延，不改变信号股票池。"""
    require_panel(state,{'date','asset','can_open_long','can_close_long','valid_for_factor_rank','valid_for_o2o_label'},'成交状态')
    if any(state.schema[c]!=pl.Boolean or state[c].null_count() for c in ['can_open_long','can_close_long','valid_for_factor_rank','valid_for_o2o_label']):
        raise ValueError('成交状态必须为非空布尔')
    labels=build_fixed_session_o2o_labels(market,holding_sessions=horizon,calendar=calendar)
    entry=state.select(pl.col('date').alias('label_entry_date'),'asset','can_open_long',pl.col('valid_for_o2o_label').alias('entry_label_eligible'))
    exit=state.select(pl.col('date').alias('label_exit_date'),'asset','can_close_long',pl.col('valid_for_o2o_label').alias('exit_label_eligible'))
    col=f'label_o2o_{horizon}d'
    joined=labels.join(entry,on=['label_entry_date','asset'],how='left',validate='m:1').join(exit,on=['label_exit_date','asset'],how='left',validate='m:1')
    joined=joined.join(state.select('date','asset','valid_for_factor_rank'),on=['date','asset'],how='left',validate='1:1')
    valid=pl.col('valid_for_factor_rank')&pl.col('can_open_long')&pl.col('can_close_long')&pl.col('entry_label_eligible')&pl.col('exit_label_eligible')
    return joined.with_columns(pl.when(valid).then(pl.col(col)).otherwise(None).alias(col)).select(labels.columns)


def run_horizon_diagnostics(config_path: Path) -> Path:
    """先冻结输入和期限网格，再读取新标签；不改变旧FaVOR筛选或交易频率。"""
    c=json.loads(config_path.read_text());root=Path(c['output_root']);root.mkdir(parents=True,exist_ok=False)
    write_json(root/'protocol.json',c);write_json(root/'registration.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=file_hash(config_path)))
    code=_snapshot_code(root)
    try:
        if c['horizons']!=[1,5,20] or c['new_formula_capacity']!=0 or not c['test_consumed']:
            raise ValueError('本次仅允许已授权的1/5/20日固定信号期限诊断')
        if len(c['factors'])*len(c['horizons'])*len(c['splits'])>c['diagnostic_cell_capacity']:raise ValueError('超出期限诊断预留容量')
        required={str(Path(c['dataset_root'])/n) for n in ['market.parquet','state.parquet','calendar.parquet','label.parquet','manifest.json']}
        required|={f[k] for f in c['factors'] for k in ['raw_path','report_path']}
        _verify_inputs(c,required)
        dataset=Path(c['dataset_root']);market=pl.read_parquet(dataset/'market.parquet',columns=['date','asset','open'])
        state=pl.read_parquet(dataset/'state.parquet');calendar=pl.read_parquet(dataset/'calendar.parquet',columns=['date'])
        labels={};quality=[]
        old=pl.read_parquet(dataset/'label.parquet',columns=['date','asset','label_o2o_5d'])
        for h in c['horizons']:
            x=tradable_fixed_labels(market,state,calendar,h);col=f'label_o2o_{h}d';path=root/f'label_{h}d.parquet';x.write_parquet(path);labels[h]=path
            q=dict(horizon=h,rows=x.height,finite_labels=x[col].is_finite().sum(),label_sha256=file_hash(path),label_column=col)
            if h==5:
                compare=x.join(old.rename({'label_o2o_5d':'old_label'}),on=['date','asset'],validate='1:1')
                both=compare.filter(pl.col(col).is_finite()&pl.col('old_label').is_finite())
                error=both.select((pl.col(col)-pl.col('old_label')).abs().max()).item()
                if error is None or error>1e-12:raise ValueError('五日标签在共同有效样本上未复现旧价格收益')
                q.update(common_finite_labels=both.height,max_old_label_error=error,old_finite_new_unknown=compare.filter(pl.col('old_label').is_finite()&~pl.col(col).is_finite().fill_null(False)).height,new_finite_old_unknown=compare.filter(pl.col(col).is_finite()&~pl.col('old_label').is_finite().fill_null(False)).height)
            quality.append(q)
        write_json(root/'label_audit.json',quality)
        rows=[]
        for factor in c['factors']:
            for h in c['horizons']:
                for split in c['splits']:
                    daily,metrics=ic_diagnostics(pl.scan_parquet(factor['raw_path']),state.lazy(),pl.scan_parquet(labels[h]),
                        date.fromisoformat(split['start']),date.fromisoformat(split['end']),date.fromisoformat(split['boundary']),c['diagnostic_family_size'],label_column=f'label_o2o_{h}d',hac_max_lags=max(5,h))
                    folder=root/factor['factor_id']/f'{h}d';folder.mkdir(parents=True,exist_ok=True);daily.write_parquet(folder/f"{split['name']}_ic.parquet")
                    rows.append(dict(factor_id=factor['factor_id'],horizon=h,split=split['name'],expected_sign=factor['expected_sign'],oriented_rank_ic=metrics['rank_ic_mean']*(1 if factor['expected_sign']=='positive' else -1),**metrics))
            print(factor['factor_id'],'三期限诊断完成',flush=True)
        write_json(root/'summary.json',dict(rows=rows,input_factors=len(c['factors']),horizons=c['horizons'],formula_search=False,selected_horizon=None,qualification_upgraded=False,rebalance_or_cost_tested=False,test_consumed=True,sealed_oos=False))
        _verify_inputs(c,required);_verify_code(code)
        write_json(root/'completion.json',dict(status='completed',summary_sha256=file_hash(root/'summary.json'),label_audit_sha256=file_hash(root/'label_audit.json')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root
