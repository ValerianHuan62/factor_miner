"""长历史映射的状态边界与相同预测起点下的训练长度比较。"""
from pathlib import Path
import json
from datetime import date, timedelta
import polars as pl
import pytest
from factor_miner.local_data import publish_mapped_history
from factor_miner.ridge_strategy import file_hash, walk_forward


def mapped_config(tmp_path, count=10):
    dates = [date(2000, 1, 3)+timedelta(days=i) for i in range(count)]
    market = pl.DataFrame(dict(session=dates, ticker=['A']*count, o=[10.]*(count-1)+[11.], h=[12.]*count,
        l=[9.]*count, c=[10.]*count, v=[100.]*count))
    state = market.select('session','ticker').with_columns(*(pl.lit(True).alias(k) for k in
        ['compute','rank','buy','sell','label']))
    state = state.with_columns((pl.col('session') != dates[1]).alias('buy'))
    calendar = pl.DataFrame(dict(session=dates, is_open=[True]*count))
    terminal = pl.DataFrame(schema={'security_id':pl.String,'event_date':pl.Date,'available_at':pl.Date,'terminal_value':pl.Float64,'source':pl.String})
    for name, frame in [('market',market),('state',state),('calendar',calendar),('terminal_values',terminal)]:
        frame.write_parquet(tmp_path/f'{name}.parquet')
    (tmp_path/'source.json').write_text(json.dumps(dict(release_id='fixture')))
    panels = dict(market=dict(columns=dict(date='session',asset='ticker',open='o',high='h',low='l',close='c',volume='v')),
        state=dict(columns=dict(date='session',asset='ticker',valid_for_factor_compute='compute',valid_for_factor_rank='rank',
            can_open_long='buy',can_close_long='sell',valid_for_o2o_label='label')),
        calendar=dict(columns=dict(date='session'),equals=dict(is_open=True)),
        terminal_values=dict(columns={n:n for n in terminal.columns}))
    for name in panels:panels[name]['path']=str(tmp_path/f'{name}.parquet')
    release=dict(market_id='us_equity',release_id='history-fixture',source='synthetic',adjustment='split_adjusted_price_only',
        calendar_version='fixture',state_version='fixture',as_of_date=str(dates[-1]),state_as_of_date=str(dates[-1]))
    c=dict(version='mapped-history-v1',start=str(dates[0]),end=str(dates[-1]),holding_sessions=5,release=release,
        source_manifest_path=str(tmp_path/'source.json'),source_release_id='fixture',panels=panels,output_root=str(tmp_path/'out'))
    c['input_sha256']={str(p):file_hash(p) for p in [*tmp_path.glob('*.parquet'),tmp_path/'source.json']}
    config=tmp_path/'config.json';config.write_text(json.dumps(c));return c,config


def test_history_mapping_keeps_untradable_label_null_and_source_unchanged(tmp_path):
    c, config=mapped_config(tmp_path)
    root=publish_mapped_history(config)
    labels=pl.read_parquet(root/'label.parquet')
    assert labels[0,'label_o2o_5d'] is None
    assert labels[3,'label_o2o_5d'] == pytest.approx(.1)
    assert labels[-1,'label_o2o_5d'] is None
    assert json.loads((root/'manifest.json').read_text())['rows']==10
    assert all(file_hash(Path(p))==h for p,h in c['input_sha256'].items())


def test_history_mapping_rejects_changed_upstream(tmp_path):
    c, config=mapped_config(tmp_path)
    with (tmp_path/'market.parquet').open('ab') as stream:stream.write(b'changed')
    with pytest.raises(ValueError,match='来源变化'):publish_mapped_history(config)
    assert (Path(c['output_root'])/'failure.json').exists()
    assert not (Path(c['output_root'])/'manifest.json').exists()


def test_twenty_session_release_keeps_fixed_endpoints_and_unknowns(tmp_path):
    """二十日发布必须取T+21，买不到及尾部不足仍为空，不偷用五日收益。"""
    c, config = mapped_config(tmp_path, 26)
    c.update(version='mapped-history-v2', holding_sessions=20)
    config.write_text(json.dumps(c))
    root = publish_mapped_history(config)
    labels = pl.read_parquet(root/'label.parquet')
    assert 'label_o2o_5d' not in labels.columns
    assert labels[0, 'label_o2o_20d'] is None
    assert labels[1, 'label_o2o_20d'] == 0
    assert labels[4, 'label_o2o_20d'] == pytest.approx(.1)
    assert labels[4, 'label_exit_date'] == date(2000, 1, 28)
    assert labels[5, 'label_o2o_20d'] is None
    assert all(file_hash(Path(p)) == h for p, h in c['input_sha256'].items())


def test_training_windows_use_more_history_without_moving_prediction_dates():
    import numpy as np
    days=[date(2000,1,7)+timedelta(weeks=i) for i in range(540)]
    rng=np.random.default_rng(17)
    frame=pl.DataFrame([dict(date=d,asset=str(a),f=float(rng.normal()),target_z=float(rng.normal()),
        label_o2o_5d=float(rng.normal()),label_exit_date=d+timedelta(days=8)) for d in days for a in range(25)])
    outputs=[]
    for weeks in (156,520):
        prediction,audit=walk_forward(frame,days,{'baseline':['f']},dict(train_weeks=weeks,embargo_weeks=1,retrain_weeks=4,alpha=10),days[522])
        assert audit[0]['train_first_signal_date']==days[522-1-weeks]
        assert all(a['train_last_label_exit']<a['prediction_start'] for a in audit)
        outputs.append(prediction.select('date','asset'))
    assert outputs[0].equals(outputs[1])


def test_training_study_cli_artifacts_and_actual_windows(tmp_path):
    import numpy as np
    from factor_miner.favor_demo import make_plan, expressions
    from factor_miner.favor_workflow import register_favor, submit_favor, favor_generation_payload
    from factor_miner.horizon_models import run_training_window_study
    from tests.test_research_efficiency import bounded
    fixture_root=tmp_path/'fixture';fixture_root.mkdir()
    plan, config=make_plan(fixture_root);registered=fixture_root/'registered';register_favor(config,registered)
    identity=favor_generation_payload(registered,'F1','gpt6')['submission_identity']
    receipt=submit_favor(registered,dict(identity,expression=expressions()[0].model_dump(mode='json')))
    spec=registered/'submissions/F1/spec.json'
    days=[date(1997,1,3)+timedelta(weeks=i) for i in range(850)]
    rng=np.random.default_rng(19)
    market=pl.DataFrame([dict(date=d,asset=str(a),open=10.,high=12.,low=8.,close=float(10+rng.uniform(-1,1)),volume=100.) for d in days for a in range(25)])
    state=market.select('date','asset').with_columns(pl.lit(True).alias('valid_for_factor_compute'),pl.lit(True).alias('valid_for_factor_rank'))
    labels=market.select('date','asset').with_columns(pl.Series('label_o2o_5d',rng.normal(size=market.height)),(pl.col('date')+timedelta(days=42)).alias('label_exit_date'))
    dataset=tmp_path/'dataset';dataset.mkdir()
    for n, frame in [('market',market),('state',state),('calendar',pl.DataFrame({'date':days})),('label',labels)]:frame.write_parquet(dataset/f'{n}.parquet')
    (dataset/'manifest.json').write_text('{}')
    context=tmp_path/'context.parquet';pl.DataFrame(dict(date=days,code=['index']*len(days),ret=[.01]*len(days))).write_parquet(context)
    budget=bounded(plan.search_budget);budget['validation_target']='portfolio_increment'
    c=dict(version='training-window-study-v1',output_root=str(tmp_path/'study'),train_windows=[156,520],test_consumed=True,budget=budget,
        dataset_root=str(dataset),history_start='2000-01-01',evaluation_start='2011-01-01',evaluation_end=str(days[-8]),
        model_policy=dict(embargo_weeks=1,retrain_weeks=4,alpha=10),market_context_source=str(context),context_id_column='code',context_id='index',
        context_date_column='date',context_return_column='ret',factors=[dict(factor_id='fixed',candidate_id=receipt['source_candidate_id'],spec_path=str(spec))])
    c['input_sha256']={str(p):file_hash(p) for p in [*dataset.iterdir(),context,spec]}
    path=tmp_path/'study.json';path.write_text(json.dumps(c))
    root=run_training_window_study(path);summary=json.loads((root/'summary.json').read_text())
    short,long=summary['models']
    assert short['prediction_dates']==long['prediction_dates']
    assert short['first_train_signal']>long['first_train_signal']
    assert short['total_fitted_rows']<long['total_fitted_rows']
    assert all(m['training_and_prediction_seconds']>0 for m in summary['models'])
    assert summary['selected_window'] is None and summary['effect']['weeks']>=60
