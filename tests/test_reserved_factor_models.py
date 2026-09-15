"""月末应用冻结周度系数的因果时点回归。"""
from datetime import date

import polars as pl
import pytest

from factor_miner.reserved_factor_models import predict_frozen_fits


def test_month_end_uses_latest_available_fit_and_ignores_future_fit():
    days = [date(2024, 1, 31), date(2024, 2, 29)]
    signals = pl.DataFrame([dict(date=d, asset=str(i), f=float(i)) for d in days for i in range(30)])
    fits = [dict(model='baseline', features=['f'], prediction_start='2024-01-05', train_last_label_exit='2024-01-04', beta=[1.], intercept=0.),
            dict(model='baseline', features=['f'], prediction_start='2024-02-02', train_last_label_exit='2024-02-01', beta=[2.], intercept=1.)]
    predicted = predict_frozen_fits(signals, days, fits, {'baseline': ['f']})
    jan = predicted.filter(pl.col('date') == days[0])
    feb = predicted.filter(pl.col('date') == days[1])
    assert jan.filter(pl.col('asset') == '3')['prediction'].item() == 3.
    assert feb.filter(pl.col('asset') == '3')['prediction'].item() == 7.
    attacked = [fits[0], {**fits[1], 'beta': [1e9]}]
    assert predict_frozen_fits(signals, days, attacked, {'baseline': ['f']}).filter(pl.col('date') == days[0]).equals(jan)
    with pytest.raises(ValueError, match='没有可用'):
        predict_frozen_fits(signals, days, fits[1:], {'baseline': ['f']})
    with pytest.raises(ValueError, match='退出时点'):
        predict_frozen_fits(signals, days, [{**fits[0], 'train_last_label_exit': '2024-01-31'}], {'baseline': ['f']})


def test_cached_ranks_are_reranked_on_current_common_mask_without_labels():
    from factor_miner.reserved_factor_models import join_cached_features
    day = date(2024,1,5)
    cached = pl.DataFrame(dict(date=[day]*4,asset=list('ABCD'),old=[-.25,0.,.25,.5]))
    raw = pl.DataFrame(dict(date=[day]*4,asset=list('ABCD'),raw_factor=[1.,None,3.,4.],valid_for_factor_compute=[True]*4))
    state = cached.select('date','asset').with_columns((pl.col('asset')!='D').alias('valid_for_factor_rank'))
    result = join_cached_features(cached.lazy(),{'new':raw.lazy()},state.lazy(),[day],['old','new'])
    assert result['asset'].to_list()==['A','C']
    assert result['old'].to_list()==[0.,.5]
    assert result['new'].to_list()==[0.,.5]
    # 单调变换缓存数值不改变共同截面结果；不沿用原四只股票的分母。
    transformed=cached.with_columns((pl.col('old')*20+10).alias('old'))
    assert join_cached_features(transformed.lazy(),{'new':raw.lazy()},state.lazy(),[day],['old','new']).equals(result)
    with pytest.raises(Exception,match='1:1'):
        join_cached_features(pl.concat([cached,cached]).lazy(),{'new':raw.lazy()},state.lazy(),[day],['old','new'])
    with pytest.raises(ValueError,match='信号日'):
        join_cached_features(cached.lazy(),{'new':raw.lazy()},state.lazy(),[day,date(2024,1,12)],['old','new'])


def test_cached_weekly_runner_preserves_predictions_and_purges_training(tmp_path,monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    import json
    import numpy as np
    import factor_miner.reserved_factor_models as module
    from factor_miner.ridge_strategy import file_hash
    days=[date(2018,1,5)+timedelta(weeks=i) for i in range(260)]
    rng=np.random.default_rng(81)
    keys=pl.DataFrame([dict(date=d,asset=str(a)) for d in days for a in range(25)])
    cached=keys.with_columns(pl.Series('base',rng.normal(size=keys.height)))
    raw=keys.with_columns(pl.Series('raw_factor',rng.normal(size=keys.height)),pl.lit(True).alias('valid_for_factor_compute'))
    state=keys.with_columns(pl.lit(True).alias('valid_for_factor_rank'))
    labels=keys.with_columns(pl.Series('label_o2o_5d',rng.normal(size=keys.height)),(pl.col('date')+timedelta(days=8)).alias('label_exit_date'))
    labels=labels.with_columns(pl.when(pl.col('date')==days[-1]).then(None).otherwise(pl.col('label_o2o_5d')).alias('label_o2o_5d'))
    dataset=tmp_path/'data';dataset.mkdir()
    for name,frame in [('market',keys),('state',state),('label',labels),('calendar',pl.DataFrame({'date':days}))]:
        frame.write_parquet(dataset/f'{name}.parquet')
    cached_path=tmp_path/'cache.parquet';cached.write_parquet(cached_path)
    raw_path=tmp_path/'raw.parquet';raw.write_parquet(raw_path)
    hashes={str(p):file_hash(p) for p in dataset.iterdir()}
    protocol=tmp_path/'cache_protocol.json';protocol.write_text(json.dumps(dict(dataset_root=str(dataset),model_policy=dict(embargo_weeks=1,retrain_weeks=4,alpha=10),factors=[dict(factor_id='base')],input_sha256=hashes)))
    reservation=tmp_path/'reservation.json'
    comps=[dict(comparison_id='ridge_baseline',kind='model',features=['base']),dict(comparison_id='ridge_joint',kind='model',features=['base','F1']),dict(comparison_id='delta',kind='paired_weekly_ic',left='joint',right='baseline')]
    r=dict(version='reserved-factor-models-v2',capacity=3,comparisons=comps,comparison_ids=[x['comparison_id'] for x in comps],diagnostic_family_size=15,inherited_diagnostic_family_size=10,candidate_capacity=2,factor_inputs=[],factor_input_sha256={str(p):file_hash(p) for p in [cached_path,protocol]},baseline_features=['base'],baseline_panel_path=str(cached_path),baseline_protocol_path=str(protocol),training_sampling='weekly_last_session',ic_sampling='weekly_last_session',model_policy=dict(train_weeks=156,embargo_weeks=1,retrain_weeks=4,alpha=10),history_start=str(days[0]),evaluation_start=str(days[180]),evaluation_end=str(days[-1]),ic_hac_lags=1,ic_min_paired_observations=60,expected_signs={'F1':'positive'})
    reservation.write_text(json.dumps(r))
    plan_path=tmp_path/'plan.json';plan_path.write_text('{}')
    (tmp_path/'registration.json').write_text(json.dumps(dict(plan_sha256='fixture')))
    summary=tmp_path/'summary.json';summary.write_text(json.dumps(dict(status='completed',plan_sha256='fixture',factors={'F1':dict(empirical=dict(passed=True),synthetic=dict(status='基础检验符合'),raw_path=str(raw_path))})))
    plan=SimpleNamespace(dataset_root=str(dataset),input_sha256={**hashes,str(reservation):file_hash(reservation)},execution=SimpleNamespace(holding_sessions=5))
    monkeypatch.setattr(module,'load_plan',lambda path:plan)
    paths=[*dataset.iterdir(),reservation,plan_path,summary,cached_path,protocol,raw_path]
    c=dict(output_root=str(tmp_path/'output'),reservation_path=str(reservation),favor_plan_path=str(plan_path),favor_summary_path=str(summary),input_sha256={str(p):file_hash(p) for p in paths})
    config=tmp_path/'config.json';config.write_text(json.dumps(c))
    root=module.run_reserved_factor_models(config)
    predicted=pl.read_parquet(root/'weekly_predictions.parquet')
    assert predicted.filter(pl.col('date')==days[-1]).height==50
    assert predicted.filter(pl.col('date')==days[-1])['label_o2o_5d'].null_count()==50
    fits=json.loads((root/'fit_audit.json').read_text())
    assert all(f['train_last_label_exit']<f['prediction_start'] for f in fits)
    assert fits[0]['train_first_signal_date']==str(days[23])
    result=json.loads((root/'summary.json').read_text())
    assert result['horizon']==5 and result['effects'][0]['weeks']>=60
    assert not (root/'monthly_predictions.parquet').exists()
    assert not (root/'signal_panel.parquet').exists()
    # 只收窄当时排名资格的子发布可复用原缓存，但须重新建立共同截面。
    child=tmp_path/'subset';child.mkdir()
    for name in ['market','calendar','label']:
        (child/f'{name}.parquet').symlink_to(dataset/f'{name}.parquet')
    state.with_columns((pl.col('asset')!='0').alias('valid_for_factor_rank')).write_parquet(child/'state.parquet')
    lineage=dict(base_dataset_root=str(dataset),base_input_sha256=hashes,base_fields_unchanged=True,ranking_subset_only=True,labels_unchanged=True)
    manifest=child/'manifest.json';manifest.write_text(json.dumps(lineage))
    plan.dataset_root=str(child)
    plan.input_sha256.update({str(p):file_hash(p) for p in child.iterdir()})
    c['input_sha256'].update(plan.input_sha256);c['output_root']=str(tmp_path/'subset_output')
    config.write_text(json.dumps(c));subset_root=module.run_reserved_factor_models(config)
    assert pl.read_parquet(subset_root/'weekly_predictions.parquet').filter(pl.col('date')==days[-1]).height==48
    manifest.write_text(json.dumps(dict(lineage,labels_unchanged=False)))
    plan.input_sha256[str(manifest)]=file_hash(manifest);c['input_sha256'][str(manifest)]=file_hash(manifest)
    c['output_root']=str(tmp_path/'invalid_subset');config.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='排名收窄'):module.run_reserved_factor_models(config)
