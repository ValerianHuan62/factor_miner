"""冻结美股基底上的因果滚动 Ridge 增量诊断，不代替可成交回测。"""
from pathlib import Path
from datetime import date
import json
import math
import hashlib
import shutil
from statistics import mean

import numpy as np
import polars as pl

from factor_miner.ic_diagnostics import _hac_t
from factor_miner.research_report import write_json, report_schedule


def fit_purged_ridge(frame: pl.DataFrame, features: list[str], prediction_date: date,
                     alpha: float) -> tuple[np.ndarray, float, dict]:
    """目标退出未发生的行不能进入训练；特征缺失不填补。"""
    train = frame.filter((pl.col('label_exit_date') < prediction_date) & pl.col('target_z').is_finite())
    if train.height < 60 or not features:
        raise ValueError('Ridge 可用训练样本或特征不足')
    x = train.select(features).to_numpy()
    y = train['target_z'].to_numpy()
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Ridge 训练输入不完整')
    xc, yc = x.mean(axis=0), float(y.mean())
    centered = x - xc
    beta = np.linalg.solve(centered.T @ centered + alpha * np.eye(len(features)), centered.T @ (y - yc))
    return beta, yc - float(xc @ beta), dict(train_rows=train.height,
        train_last_signal_date=train['date'].max(), train_last_label_exit=train['label_exit_date'].max())


def explicit_model_candidates(reports: list[dict], baseline_ids: list[str], addition_ids: list[str],
                              research_pool: dict | None = None) -> tuple[list[dict], list[dict]]:
    """验证复核阶段冻结的基底与增量名单，不使用确认期指标重新选择。"""
    ids = baseline_ids + addition_ids
    if not baseline_ids or len(ids) != len(set(ids)):
        raise ValueError('增量基底必须非空，且不能与新增候选重复')
    by_id = {r['factor_id']: r for r in reports}
    if set(ids) - set(by_id):
        raise ValueError('增量名单包含未评价的因子')
    for fid in ids:
        r = by_id[fid]
        if research_pool is not None:
            from factor_miner.research_pool import research_admission
            entry = research_pool['by_factor'][fid]
            if (research_pool.get('schema_version') != 'research-pool-v1'
                    or entry['status'] != 'research_input'
                    or entry['source_candidate_id'] != r['candidate_id']
                    or fid not in research_pool['selected_factor_ids']
                    or research_admission(r)['status'] != 'eligible'):
                raise ValueError(f'{fid} 未通过独立版本的组合研究资格')
            continue
        d = r['discovery']
        sign = 1 if r['hypothesis_direction'] == 'positive' else -1
        if (r['construct_validation']['status'] != '基础检验符合' or d['rank_ic_mean'] * sign < .01
                or d['bonferroni_p_value'] >= .05 or d['median_coverage'] < .8):
            raise ValueError(f'{fid} 未通过原方向下的发现期门槛')
    return [by_id[fid] for fid in baseline_ids], [by_id[fid] for fid in addition_ids]


def fit_purged_ridge_models(frame: pl.DataFrame, models: dict[str, list[str]], prediction_date: date,
                            alpha: float) -> dict[str, tuple[np.ndarray, float, dict]]:
    """同一训练截面的多个特征子集复用充分统计量，保持原 Ridge 目标完全相同。"""
    columns = list(dict.fromkeys(c for features in models.values() for c in features))
    if not columns or any(not cols or len(cols) != len(set(cols)) for cols in models.values()):
        raise ValueError('模型特征不能为空或重复')
    train = frame.filter((pl.col('label_exit_date') < prediction_date) & pl.col('target_z').is_finite())
    if train.height < 60:
        raise ValueError('Ridge 可用训练样本不足')
    x, y = train.select(columns).to_numpy(), train['target_z'].to_numpy()
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Ridge 训练输入不完整')
    xc, yc = x.mean(axis=0), float(y.mean())
    centered = x - xc
    gram, response = centered.T @ centered, centered.T @ (y-yc)
    audit = dict(train_rows=train.height, train_last_signal_date=train['date'].max(),
                 train_last_label_exit=train['label_exit_date'].max())
    result = {}
    for name, features in models.items():
        indices = [columns.index(c) for c in features]
        beta = np.linalg.solve(gram[np.ix_(indices, indices)] + alpha*np.eye(len(indices)), response[indices])
        result[name] = beta, yc-float(xc[indices]@beta), audit.copy()
    return result


def run_incremental(config_path: Path) -> Path:
    """在同一完整信号截面比较基准与单因子增量，固定 alpha、窗口及重训频率。"""
    config = json.loads(config_path.read_text())
    run = Path(config['output_root'])
    manifest = json.loads((run / 'run_manifest.json').read_text())
    reports = manifest['reports']
    output = run / 'incremental'
    output.mkdir(exist_ok=False)
    code = Path(__file__)
    shutil.copyfile(code, output/'research_incremental.py')
    write_json(output/'code_identity.json',dict(module_sha256=hashlib.sha256(code.read_bytes()).hexdigest(),
        config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),numpy_version=np.__version__,polars_version=pl.__version__))
    for name, expected_hash in config.get('input_sha256', {}).items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected_hash:
            raise ValueError(f'冻结输入变化：{Path(name).name}')
    policy = config['incremental_contract']
    write_json(output / 'protocol.json', policy)
    family_size = int(config.get('family_size', 12))
    if family_size < len(reports):
        raise ValueError('增量校正不能缩减本次全部登记名额')
    if config.get('incremental_selection_path'):
        selection = json.loads(Path(config['incremental_selection_path']).read_text())
        if selection['source_run_manifest_sha256'] != hashlib.sha256((run/'run_manifest.json').read_bytes()).hexdigest():
            raise ValueError('增量名单与研究报告身份不一致')
        pool = None
        if config.get('research_pool_path'):
            path = config['research_pool_path']
            if path not in config.get('input_sha256', {}):
                raise ValueError('组合研究资格清单未冻结')
            pool = json.loads(Path(path).read_text())
        legacy, eligible = explicit_model_candidates(reports, selection['baseline_factor_ids'], selection['addition_factor_ids'], pool)
    else:
        eligible = [r for r in reports if not r['inference']['redundant']
            and r['construct_validation']['status']=='基础检验符合'
            and r['direction']==r['hypothesis_direction']
            and r['discovery']['bonferroni_p_value'] < .05
            and abs(r['discovery']['rank_ic_mean']) >= .01 and r['discovery']['median_coverage'] >= .8]
        legacy = json.loads((Path(config['legacy_report_root'])/'run_manifest.json').read_text())['reports']
    if not eligible:
        write_json(output/'summary.json',dict(status='没有合格发现期候选，不强行建立增量模型',models=0))
        return output
    write_json(output/'model_freeze.json',dict(baseline=[r['candidate_id'] for r in legacy],
        additions=[r['candidate_id'] for r in eligible],confirmation_selection=False,
        model_count=1+len(eligible),family_size=family_size))
    dataset = Path(config['dataset_root'])
    days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
    start, end = date.fromisoformat(config['discovery_start']), date.fromisoformat(config['confirmation_end'])
    dates = [w.signal_date for w in report_schedule(days,start,end,'weekly_last_session')]
    state = pl.scan_parquet(dataset/'state.parquet')
    panel = state.filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_rank')).select('date','asset')
    names=[]
    for index, report in enumerate([*legacy,*eligible]):
        name=f'f{index}'
        names.append(name)
        raw=pl.scan_parquet(Path(report['folder'])/'raw_factor.parquet').filter(
            pl.col('date').is_in(dates) & pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite()
        ).select('date','asset',pl.col('raw_factor').alias(name))
        panel=panel.join(raw,on=['date','asset'],how='inner',validate='1:1')
    # 先形成信号截面，再左连事后标签；预测股票池不由未来标签资格决定。
    panel=panel.with_columns(*((pl.col(n).rank(method='average').over('date')/pl.len().over('date')-.5).alias(n) for n in names))
    labels=pl.scan_parquet(dataset/'label.parquet').select('date','asset','label_o2o_5d','label_exit_date')
    data=panel.join(labels,on=['date','asset'],how='left',validate='1:1').with_columns(
        pl.when(pl.col('label_o2o_5d').is_finite()).then(pl.col('label_o2o_5d')).alias('label_o2o_5d')
    ).with_columns(((pl.col('label_o2o_5d')-pl.col('label_o2o_5d').mean().over('date'))/pl.col('label_o2o_5d').std(ddof=0).over('date')).alias('target_z')).sort('date','asset').collect()
    data.write_parquet(output/'weekly_panel.parquet')
    baseline=names[:len(legacy)]
    models=[('baseline',baseline)]+[(r['factor_id'],baseline+[names[len(legacy)+i]]) for i,r in enumerate(eligible)]
    train_weeks=int(policy['train_weeks']); embargo=int(policy['embargo_weeks']); frequency=int(policy['retrain_weeks'])
    confirmation_start=date.fromisoformat(config['confirmation_start'])
    start_index=max(train_weeks+embargo,next(i for i,d in enumerate(dates) if d>=confirmation_start))
    fits=[]; predictions=[]; fitted={}
    for index in range(start_index,len(dates)):
        day=dates[index]
        current=data.filter(pl.col('date')==day)
        if current.height<20:
            raise ValueError(f'完整信号截面不足20证券：{day}')
        if not fitted or (index-start_index)%frequency==0:
            train_dates=dates[index-embargo-train_weeks:index-embargo]
            history=data.filter(pl.col('date').is_in(train_dates))
            print(f'滚动 Ridge 重训：{day}，{len(models)}个固定模型',flush=True)
            for model,columns in models:
                beta,intercept,audit=fit_purged_ridge(history,columns,day,float(policy['alpha']))
                fitted[model]=(beta,intercept)
                fits.append(dict(model=model,prediction_start=day,alpha=policy['alpha'],**audit))
        for model,columns in models:
            beta,intercept=fitted[model]
            predictions.append(current.select('date','asset','label_o2o_5d').with_columns(
                pl.Series('prediction',current.select(columns).to_numpy()@beta+intercept),pl.lit(model).alias('model')))
    if not predictions:
        raise ValueError('冻结训练期之后没有可预测区间')
    predicted=pl.concat(predictions)
    predicted.write_parquet(output/'predictions.parquet')
    write_json(output/'fit_audit.json',fits)
    daily=predicted.filter(pl.col('label_o2o_5d').is_finite()).group_by('date','model').agg(
        pl.len().alias('labeled_names'),pl.corr('prediction','label_o2o_5d',method='spearman').alias('rank_ic')
    ).filter((pl.col('labeled_names')>=20)&pl.col('rank_ic').is_finite()).sort('date','model')
    daily.write_parquet(output/'daily_ic.parquet')
    base=daily.filter(pl.col('model')=='baseline').select('date',pl.col('rank_ic').alias('baseline_ic'))
    summary=[]
    for report in eligible:
        pair=daily.filter(pl.col('model')==report['factor_id']).join(base,on='date').sort('date')
        if pair.height<60:
            summary.append(dict(factor_id=report['factor_id'],status='增量重叠日期不足',dates=pair.height))
            continue
        delta=(pair['rank_ic']-pair['baseline_ic']).to_list()
        t=_hac_t(delta,5)
        summary.append(dict(factor_id=report['factor_id'],status='条件预测诊断',dates=pair.height,
            mean_rank_ic=float(pair['rank_ic'].mean()),baseline_rank_ic=float(pair['baseline_ic'].mean()),
            incremental_rank_ic=mean(delta),hac_t=t,bonferroni_p=min(1,family_size*math.erfc(abs(t)/math.sqrt(2))),
            yearly=[dict(year=year,delta=float(group.select((pl.col('rank_ic')-pl.col('baseline_ic')).mean()).item()))
                for (year,),group in pair.with_columns(pl.col('date').dt.year().alias('year')).partition_by('year',as_dict=True).items()]))
    write_json(output/'summary.json',dict(status='completed',models=len(models),results=summary,
        sealed_oos=False,scope='已消费确认期；严格走步训练的条件RankIC增量，不是投资组合收益',
        first_prediction=predicted['date'].min(),last_prediction=predicted['date'].max(),
        prediction_rows=predicted.height,missing_label_predictions=predicted['label_o2o_5d'].null_count()))
    return output
