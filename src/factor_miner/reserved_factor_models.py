"""执行事前预留的单期限增量模型；构念失败与弱单因子显著性分别处理。"""
from datetime import date
from pathlib import Path
import json

import numpy as np
import polars as pl

from factor_miner.favor_workflow import load_plan
from factor_miner.horizon_models import model_rank_ic, paired_model_effect
from factor_miner.quote_research import _snapshot_code, _verify_code, _verify_inputs
from factor_miner.research_report import write_json
from factor_miner.ridge_strategy import file_hash, join_targets, walk_forward


def join_cached_features(cached: pl.LazyFrame, raw: dict[str, pl.LazyFrame],
                         state: pl.LazyFrame, dates: list[date], features: list[str]) -> pl.DataFrame:
    """缓存仅为当日单调排名；与新增信号取共同截面后重新排名，不接触标签。"""
    from factor_miner.favor_validation import require_panel
    cache_names = set(cached.collect_schema().names()) - {'date', 'asset'}
    panel = state.filter(pl.col('valid_for_factor_rank') & pl.col('date').is_in(dates)).select('date', 'asset')
    selected = [f for f in features if f in cache_names]
    if selected:
        panel = panel.join(cached.select('date', 'asset', *selected).filter(pl.col('date').is_in(dates)),
                           on=['date', 'asset'], how='inner', validate='1:1')
    for f in features:
        if f in cache_names:
            continue
        values = raw[f].filter(pl.col('date').is_in(dates) & pl.col('valid_for_factor_compute')).select(
            'date', 'asset', pl.col('raw_factor').alias(f))
        panel = panel.join(values, on=['date', 'asset'], how='inner', validate='1:1')
    signals = (panel.filter(pl.all_horizontal(*(pl.col(f).is_finite().fill_null(False) for f in features)))
        .with_columns(*((pl.col(f).rank(method='average').over('date') / pl.len().over('date') - .5).alias(f)
                        for f in features)).collect().sort('date', 'asset'))
    require_panel(signals, {'date', 'asset', *features}, '共同特征面板')
    if sorted(signals['date'].unique().to_list()) != dates:
        raise ValueError('共同特征截面缺少预留信号日')
    return signals


def predict_frozen_fits(signals: pl.DataFrame, signal_dates: list[date], fits: list[dict], models: dict) -> pl.DataFrame:
    """月末使用当时最新的既定重训系数，不能提前用未来拟合结果。"""
    rows = []
    for name, columns in models.items():
        relevant = sorted((f for f in fits if f['model'] == name), key=lambda f: str(f['prediction_start']))
        for day in signal_dates:
            available = [f for f in relevant if date.fromisoformat(str(f['prediction_start'])) <= day]
            if not available:
                raise ValueError('月末之前没有可用的冻结拟合')
            fit = available[-1]
            if fit['features'] != columns or date.fromisoformat(str(fit['train_last_label_exit'])) >= day:
                raise ValueError('月末模型字段或训练退出时点不正确')
            current = signals.filter(pl.col('date') == day)
            if current.height < 20:
                raise ValueError('月末预测截面不足20只证券')
            values = current.select(columns).to_numpy() @ np.asarray(fit['beta']) + fit['intercept']
            if not np.isfinite(values).all():
                raise ValueError('月末预测包含非有限值')
            rows.append(current.select('date', 'asset').with_columns(pl.Series('prediction', values), pl.lit(name).alias('model'),
                pl.lit(date.fromisoformat(str(fit['prediction_start']))).alias('fit_date')))
    return pl.concat(rows).sort('model', 'date', 'asset')


def run_reserved_factor_models(config_path: Path) -> Path:
    """只使用冻结合同中的模型和比较，缺失构念分支不补位、不释放预算。"""
    c = json.loads(config_path.read_text()); root = Path(c['output_root']); root.mkdir(parents=True, exist_ok=False)
    write_json(root/'protocol.json', c); code = _snapshot_code(root)
    from datetime import datetime, timezone
    write_json(root/'registration.json', dict(at=datetime.now(timezone.utc).isoformat(), config_sha256=file_hash(config_path)))
    try:
        required = {c['reservation_path'], c['favor_summary_path'], c['favor_plan_path'], *c['input_sha256']}
        _verify_inputs(c, required)
        plan = load_plan(Path(c['favor_plan_path']).parent)
        r = json.loads(Path(c['reservation_path']).read_text())
        cached_weekly = r.get('version') == 'reserved-factor-models-v2'
        if plan.input_sha256.get(c['reservation_path']) != file_hash(Path(c['reservation_path'])):
            raise ValueError('比较合同没有在FaVOR结果之前绑定')
        if r['capacity'] != len(r['comparisons']) or r['comparison_ids'] != [x['comparison_id'] for x in r['comparisons']]:
            raise ValueError('比较预留身份不一致')
        if r['diagnostic_family_size'] != r['inherited_diagnostic_family_size'] + r['candidate_capacity'] + r['capacity']:
            raise ValueError('诊断预算未完整继承')
        if any(c['input_sha256'].get(path) != digest for path, digest in r['factor_input_sha256'].items()):
            raise ValueError('原八输入身份偏离结果前合同')
        if r['training_sampling'] != 'weekly_last_session' or r['ic_sampling'] != 'weekly_last_session':
            raise ValueError('本入口要求冻结的周度训练及周度IC')
        if cached_weekly:
            if plan.execution.holding_sessions != 5 or r['model_policy']['train_weeks'] != 156:
                raise ValueError('缓存周度对照要求五日目标及156周训练')
            cached_protocol = json.loads(Path(r['baseline_protocol_path']).read_text())
            if (cached_protocol['model_policy'] != {k:v for k,v in r['model_policy'].items() if k != 'train_weeks'}
                    or [f['factor_id'] for f in cached_protocol['factors']] != r['baseline_features']):
                raise ValueError('缓存特征发布、参数或名单与预留合同不一致')
            lineage = None
            if cached_protocol['dataset_root'] != plan.dataset_root:
                path = str(Path(plan.dataset_root)/'manifest.json')
                if c['input_sha256'].get(path) != file_hash(Path(path)) or plan.input_sha256.get(path) != c['input_sha256'][path]:
                    raise ValueError('缓存的研究子集缺少冻结血缘清单')
                lineage = json.loads(Path(path).read_text())
                if (lineage.get('base_dataset_root') != cached_protocol['dataset_root']
                        or any(lineage.get(k) is not True for k in ['base_fields_unchanged','ranking_subset_only','labels_unchanged'])):
                    raise ValueError('缓存只允许原行情不变、排名收窄的发布')
            for name in ('market.parquet', 'state.parquet', 'calendar.parquet', 'label.parquet'):
                path = str(Path(cached_protocol['dataset_root'])/name)
                declared = plan.input_sha256.get(path) if lineage is None else lineage['base_input_sha256'].get(path)
                if not declared or cached_protocol['input_sha256'].get(path) != declared:
                    raise ValueError('缓存特征与当前数据发布身份不同')
                if lineage is not None and name in {'calendar.parquet','label.parquet'} and declared != plan.input_sha256.get(str(Path(plan.dataset_root)/name)):
                    raise ValueError('研究子集不能改变原日历或标签')
        elif plan.execution.holding_sessions != 20:
            raise ValueError('旧入口执行二十日预测与月末系数应用')
        summary = json.loads(Path(c['favor_summary_path']).read_text())
        if summary['status'] != 'completed' or summary['plan_sha256'] != json.loads((Path(c['favor_plan_path']).parent/'registration.json').read_text())['plan_sha256']:
            raise ValueError('FaVOR尚未完成或身份错误')
        factors = {f['factor_id']: f['raw_path'] for f in r['factor_inputs']}
        if cached_weekly:
            factors.update({f:r['baseline_panel_path'] for f in r['baseline_features']})
        for trial, report in summary['factors'].items():
            if report.get('empirical', {}).get('passed') and report.get('synthetic', {}).get('status') == '基础检验符合':
                factors[trial] = report['raw_path']
        models = {}; skipped = []
        for item in r['comparisons']:
            if item['kind'] != 'model': continue
            missing = set(item['features']) - set(factors)
            if missing:
                skipped.append(dict(comparison_id=item['comparison_id'], status='not_executed_construct_failed', missing_features=sorted(missing)))
            else:
                models[item['comparison_id'].removeprefix('ridge_')] = item['features']
        features = list(dict.fromkeys(f for columns in models.values() for f in columns))
        if not models or 'baseline' not in models:
            raise ValueError('没有合法的冻结基线模型')
        for feature in features:
            if factors[feature] not in c['input_sha256']:
                raise ValueError('模型原始输入没有绑定哈希')
        dataset = Path(plan.dataset_root)
        for name in ('state.parquet', 'calendar.parquet', 'label.parquet'):
            if c['input_sha256'].get(str(dataset/name)) != plan.input_sha256.get(str(dataset/name)):
                raise ValueError('模型数据输入没有绑定哈希')
        days = pl.read_parquet(dataset/'calendar.parquet')['date'].to_list()
        start, end = date.fromisoformat(r['history_start']), date.fromisoformat(r['evaluation_end'])
        weekly = [d for d in {d.isocalendar()[:2]: d for d in days}.values() if start <= d <= end]
        monthly = ([] if cached_weekly else [d for d in {(d.year,d.month): d for d in days}.values()
            if date.fromisoformat(r['evaluation_start']) <= d <= date.fromisoformat(r['execution_signal_end'])])
        wanted = sorted(set(weekly + monthly))
        cached = (pl.scan_parquet(r['baseline_panel_path']).select('date','asset',*r['baseline_features']) if cached_weekly
                  else pl.DataFrame(schema={'date':pl.Date,'asset':pl.String}).lazy())
        raw = {f:pl.scan_parquet(path) for f,path in factors.items()
               if not cached_weekly or f not in r['baseline_features']}
        signals = join_cached_features(cached,raw,pl.scan_parquet(dataset/'state.parquet'),wanted,features)
        if sorted(signals['date'].unique().to_list()) != wanted:
            raise ValueError('共同特征截面缺少预留信号日')
        if not cached_weekly:
            signals.write_parquet(root/'signal_panel.parquet')
        write_json(root/'model_freeze.json',dict(models=models,skipped=skipped,standalone_ic_filter=False,common_features=features,
            preprocessing='当日共同截面rank/人数减0.5，预期方向用于审计，不根据回测翻号',model_policy=r['model_policy']))
        col = f'label_o2o_{plan.execution.holding_sessions}d'
        labels = pl.read_parquet(dataset/'label.parquet',columns=['date','asset',col,'label_exit_date'])
        data = join_targets(signals.filter(pl.col('date').is_in(weekly)).lazy(), labels.lazy(), label_column=col)
        predicted, fits = walk_forward(data,weekly,models,r['model_policy'],date.fromisoformat(r['evaluation_start']),label_column=col)
        predicted.write_parquet(root/'weekly_predictions.parquet');write_json(root/'fit_audit.json',fits)
        ic = model_rank_ic(predicted,labels,col);ic.write_parquet(root/'weekly_ic.parquet')
        effects = []
        for item in r['comparisons']:
            if item['kind'] != 'paired_weekly_ic': continue
            if item['left'] not in models or item['right'] not in models:
                skipped.append(dict(comparison_id=item['comparison_id'],status='not_executed_construct_failed'))
                continue
            effect = paired_model_effect(ic,item['left'],item['right'],r['ic_hac_lags'],r['diagnostic_family_size'])
            if effect['weeks'] < r['ic_min_paired_observations']:
                raise ValueError('配对样本不足冻结要求')
            effects.append(dict(comparison_id=item['comparison_id'],**effect))
        if not cached_weekly:
            month_predictions = predict_frozen_fits(signals,monthly,fits,models)
            month_predictions.write_parquet(root/'monthly_predictions.parquet')
            for name in models:
                score = month_predictions.filter(pl.col('model')==name).select(pl.col('date').alias('signal_date'),pl.col('asset').alias('security_id'),pl.col('prediction').alias('factor_value'))
                score.write_parquet(root/f'monthly_scores_{name}.parquet')
        conflicts = [dict(model=f['model'],prediction_start=f['prediction_start'],feature=feature,beta=beta)
            for f in fits for feature,beta in zip(f['features'],f['beta'])
            if feature in r.get('expected_signs',{}) and beta*(1 if r['expected_signs'][feature]=='positive' else -1)<0]
        write_json(root/'coefficient_direction_conflicts.json',conflicts)
        _verify_inputs(c,required);_verify_code(code)
        write_json(root/'summary.json',dict(status='completed',models=len(models),fits=len(fits),effects=effects,skipped=skipped,
            monthly_signal_dates=len(monthly),model_ic=ic.group_by('model').agg(pl.col('rank_ic').mean(),pl.len().alias('weeks')).sort('model').to_dicts(),
            qualified_additions=0,test_consumed=True,sealed_oos=False,cost_tested=False,standalone_ic_filter=False,
            horizon=plan.execution.holding_sessions,coefficient_direction_conflicts=len(conflicts),
            yearly_model_ic=ic.with_columns(pl.col('date').dt.year().alias('year')).group_by('model','year').agg(pl.col('rank_ic').mean(),pl.len().alias('weeks')).sort('model','year').to_dicts(),
            scope='冻结共同截面的预测增量；不能替代FaVOR联合资格、完整归因或成本检验。'))
        write_json(root/'completion.json',dict(status='completed',summary_sha256=file_hash(root/'summary.json')))
    except Exception as error:
        write_json(root/'failure.json',dict(error=str(error)));raise
    return root
