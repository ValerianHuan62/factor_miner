"""交易所研究范围只改变排名资格，成交状态与未来标签不得参与观察。"""
from pathlib import Path
import json
import polars as pl
import pytest
from factor_miner.local_data import publish_mapped_history,publish_observation_overlay
from factor_miner.ridge_strategy import file_hash
from factor_miner.schema import FactorNode as N
from factor_miner.construct_validation import ObservableCondition,validate_construct
from tests.test_history_training_study import mapped_config


def test_overlay_preserves_exits_and_labels_and_does_not_fill_unknown_counts(tmp_path):
    _,config=mapped_config(tmp_path);base=publish_mapped_history(config)
    original=pl.read_parquet(base/'market.parquet')
    obs=original.select('date','asset').with_columns(pl.lit('Q').alias('primary_exchange'),pl.lit(12.).alias('trade_count'))
    days=obs['date'].to_list()
    obs=obs.with_columns(pl.when(pl.col('date')==days[-1]).then(pl.lit('N')).otherwise(pl.col('primary_exchange')).alias('primary_exchange'),pl.when(pl.col('date')==days[3]).then(None).otherwise(pl.col('trade_count')).alias('trade_count'))
    source=tmp_path/'counts.parquet';obs.write_parquet(source)
    c=dict(version='nasdaq-trade-count-overlay-v1',availability='after_close_t',revision_policy='annual_historical_release_no_daily_vintage_guarantee',universe_exchange='Q',base_dataset_root=str(base),source_path=str(source),source_columns={k:k for k in obs.columns},output_root=str(tmp_path/'overlay'),release_id='overlay',input_sha256={str(p):file_hash(p) for p in [*base.glob('*.parquet'),base/'manifest.json',base/'release.json',source]})
    path=tmp_path/'overlay.json';path.write_text(json.dumps(c));root=publish_observation_overlay(path)
    market=pl.read_parquet(root/'market.parquet');state=pl.read_parquet(root/'state.parquet');old=pl.read_parquet(base/'state.parquet')
    assert market.select(original.columns).equals(original)
    assert market.filter(pl.col('date')==days[3])['trade_count'].item() is None
    assert state[-1,'valid_for_factor_rank'] is False
    assert state[-1,'can_close_long']==old[-1,'can_close_long']
    assert file_hash(root/'label.parquet')==file_hash(base/'label.parquet')
    assert (root/'label.parquet').is_symlink()
    # 缺交易所身份时拒绝，不能用未来交易所回填。
    obs.with_columns(pl.lit(None,dtype=pl.String).alias('primary_exchange')).write_parquet(source)
    c['input_sha256'][str(source)]=file_hash(source);c['output_root']=str(tmp_path/'missing')
    path.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='交易所身份'):publish_observation_overlay(path)
    assert not (tmp_path/'missing'/'completion.json').exists()


def test_trade_count_constructs_have_units_and_direction_counterexamples():
    f=lambda x:N(op='field',field=x)
    n=lambda op,*args,**kw:N(op=op,args=args,**kw)
    ret=n('div',n('delta',f('close'),period=1),n('delay',f('close'),period=1))
    expressions=[n('div',n('rolling_mean',f('trade_count'),window=5),n('rolling_mean',f('trade_count'),window=60)),n('div',n('rolling_cov',ret,f('trade_count'),window=20),n('rolling_mean',f('trade_count'),window=20))]
    for expression,test in zip(expressions,['trade_frequency_growth','trade_return_coupling']):
        condition=ObservableCondition(observation='事前固定的成交状态',measurement='笔数响应',response_test=test)
        assert validate_construct(expression,condition)['status']=='基础检验符合'
        assert validate_construct(expression,condition.model_copy(update={'expected_response':'decrease'}))['status']!='基础检验符合'


def test_terminal_conversion_uses_strict_prior_price_and_preserves_unknown():
    from datetime import date
    from factor_miner.local_data import normalize_terminal_recoveries
    d=date(2024,1,5)
    events=pl.DataFrame(dict(security_id=['A','B','C','D'],event_date=[d]*4,available_at=[d]*4,
        delisting_total_return=[-.5,None,None,-1.],cash_amount=[None,8.,None,None],source=['fixture']*4,action_type=['terminal']*4))
    prices=pl.DataFrame(dict(security_id=['A','A','B','C','D'],last_session=[date(2024,1,4),d,date(2024,1,4),date(2024,1,4),date(2024,1,4)],
        split_adjusted_close=[20.,999.,10.,10.,10.],split_adjustment_factor=[2.,100.,.5,1.,1.]))
    known,unknown=normalize_terminal_recoveries(events,prices)
    assert dict(zip(known['security_id'],known['terminal_value']))=={'A':10.,'B':4.,'D':0.}
    assert unknown['security_id'].to_list()==['C']
    assert all(x<d for x in known['last_session'])


def test_failed_terminal_replay_keeps_candidate_identity_and_failure_chain(tmp_path):
    from factor_miner.favor_demo import make_plan,expressions
    from factor_miner.favor_workflow import register_favor,submit_favor,favor_generation_payload,repair_favor_terminals,load_plan
    from factor_miner.ledger import JsonlLedger,TrialEvent,EventType
    plan,config=make_plan(tmp_path);failed=tmp_path/'failed';register_favor(config,failed)
    payload=favor_generation_payload(failed,'F1','gpt6')['submission_identity']
    candidate=submit_favor(failed,dict(payload,expression=expressions()[0].model_dump(mode='json')))
    (failed/'failure.json').write_text(json.dumps(dict(reason='终止结算 缺少terminal_value')))
    ledger=JsonlLedger(failed/'ledger');ledger.append_event(TrialEvent(event_type=EventType.EVALUATION_FAILED,status='failed'))
    before=ledger.verify();recovery=tmp_path/'recovery';recovery.mkdir()
    terminal=recovery/'terminal_values.parquet'
    pl.DataFrame(schema={'security_id':pl.String,'event_date':pl.Date,'available_at':pl.Date,'terminal_value':pl.Float64,'source':pl.String}).write_parquet(terminal)
    (recovery/'completion.json').write_text(json.dumps(dict(status='completed',terminal_values_sha256=file_hash(terminal))))
    root=repair_favor_terminals(failed,recovery,tmp_path/'repair');after=JsonlLedger(root/'ledger').verify()
    assert ledger.verify()==before and after[:-1]==before
    assert after[-1].supersedes_event_id==before[-1].event_id
    assert json.loads((root/'submissions/F1/receipt.json').read_text())==candidate
    assert load_plan(root).search_budget==plan.search_budget
    assert load_plan(root).conditions==plan.conditions


def test_favor_rejects_raw_terminal_schema_before_research_registration(tmp_path):
    from factor_miner.favor_demo import make_plan
    from factor_miner.favor_workflow import verify_inputs
    plan,_=make_plan(tmp_path)
    path=tmp_path/'raw_terminal.parquet'
    pl.DataFrame(schema={'security_id':pl.String,'event_date':pl.Date,'available_at':pl.Date,'cash_amount':pl.Float64,'source':pl.String}).write_parquet(path)
    plan=plan.model_copy(update={'terminal_events_path':str(path),'input_sha256':dict(plan.input_sha256,**{str(path):file_hash(path)})})
    with pytest.raises(Exception,match='terminal_value'):verify_inputs(plan)
