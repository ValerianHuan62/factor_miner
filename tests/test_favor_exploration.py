"""探索入口跨市场回归；旧严格协议保持，未知与低收益不伪装通过。"""
from datetime import date, timedelta
import json

import polars as pl
import pytest

from factor_miner.exploration_contract import ExplorationMeasure
from factor_miner.favor_demo import make_plan, expressions, conditions, field
from factor_miner.favor_schema import FavorExplorationPlan, FavorPlan, parse_favor_plan
from factor_miner.favor_exploration import validate_exploration_construct, observation_frame, observation_dates
from factor_miner.favor_workflow import register_favor, submit_favor, run_favor, favor_generation_payload, file_sha, load_plan
from factor_miner.favor_preflight import inspect_data_feasibility
from factor_miner.favor_validation import ConstructPolicy, validate_empirical_construct
from tests.test_regime import state_claim


def rule(kind='continuous', **overrides):
    kwargs = dict(kind=kind, rationale='合成样本用于验证事前类型化测量规则', min_dates=2, min_assets=2, min_group_samples=2)
    if kind != 'continuous':
        kwargs.update(state_values=(0.,1.), active_values=(1.,))
    return ExplorationMeasure(**(kwargs | overrides))


def exploration_plan(tmp_path, market='us_equity', two=False):
    old, path = make_plan(tmp_path, market)
    data = old.model_dump(mode='json')
    if not two:
        data['conditions'] = data['conditions'][:1]
        data['slots'] = data['slots'][:1]
    data.update(version='favor-exploration-v1', regimes={c['condition_id']:state_claim().model_dump(mode='json') for c in data['conditions']},
        exploration={c['condition_id']:rule(min_dates=60,min_assets=20,min_group_samples=20).model_dump(mode='json') for c in data['conditions']})
    data['integration']['validation_thresholds'] = [[.9]*len(data['conditions'])]
    plan = parse_favor_plan(data)
    path.write_text(plan.model_dump_json())
    return plan, path


def panel(binary=False):
    days = [date(2021,1,1)+timedelta(days=i) for i in range(80)]
    rows = [dict(date=d,asset=f'S{j:03d}',raw_factor=float(j%2 if binary else j//20+1),
                 valid_for_factor_compute=True) for d in days for j in range(100)]
    raw = pl.DataFrame(rows)
    market = raw.select('date','asset',pl.col('raw_factor').alias('close'))
    state = raw.select('date','asset',pl.lit(True).alias('valid_for_factor_rank'))
    condition = conditions()[0]
    measurement = condition.state_measurements[0].model_copy(update={'expression':field('close')})
    condition = condition.model_copy(update={'state_measurements':(measurement,)})
    return raw, market, state, condition, days


def test_constant_tail_width_is_not_a_universal_economic_requirement():
    raw,market,state,c,days = panel()
    old = validate_empirical_construct(raw,market,state,c,days[0],days[-1],ConstructPolicy())
    assert not old['passed'] and not old['measurements'][0]['checks']['tail_variation']
    new = validate_exploration_construct(raw,market,state,c,days[0],days[-1],rule(),days)
    assert new['passed'] and not new['return_labels_used']
    constant = raw.with_columns(pl.lit(1.).alias('raw_factor'))
    assert not validate_exploration_construct(constant,market,state,c,days[0],days[-1],rule(),days)['passed']
    reversed_c = c.model_copy(update={'state_measurements':(c.state_measurements[0].model_copy(update={'expected_direction':'decrease'}),)})
    assert validate_exploration_construct(raw,market,state,reversed_c,days[0],days[-1],rule(),days)['status'] == 'measurement_mismatch'


def test_binary_state_does_not_need_five_bins_and_unknown_is_not_zero():
    raw,market,state,c,days = panel(True)
    assert validate_exploration_construct(raw,market,state,c,days[0],days[-1],rule('state'),days)['passed']
    sparse = raw.filter(pl.col('asset') < 'S010')
    result = validate_exploration_construct(sparse,market,state,c,days[0],days[-1],rule('state'),days)
    assert result['status'] == 'insufficient_sample' and result['signal_coverage'] == .1
    bad = raw.with_columns(pl.lit(2.).alias('raw_factor'))
    with pytest.raises(ValueError,match='未登记'):
        validate_exploration_construct(bad,market,state,c,days[0],days[-1],rule('state'),days)


def test_event_counts_onsets_not_daily_replication_and_gap_is_unknown():
    days = [date(2021,1,1)+timedelta(days=i) for i in range(8)]
    raw = pl.DataFrame(dict(date=days,asset=['A']*8,raw_factor=[0.,1.,1.,0.,None,1.,0.,1.], valid_for_factor_compute=[True]*8))
    state = raw.select('date','asset',pl.lit(True).alias('valid_for_factor_rank'))
    result = observation_frame(raw,state,days,rule('event'),'high')
    assert result.filter(pl.col('_trigger'))['date'].to_list() == [days[1],days[7]]
    missing = observation_frame(raw.filter(pl.col('date') != days[4]),state.filter(pl.col('date') != days[4]),days,rule('event'),'high')
    assert missing.filter(pl.col('_trigger'))['date'].to_list() == [days[1],days[7]]


def test_monthly_observation_is_not_replicated_and_final_month_not_certified():
    days = [date(2021,1,1)+timedelta(days=i) for i in range(70)]
    assert observation_dates(days,rule(observation_frequency='monthly_last_session')) == [date(2021,1,31),date(2021,2,28)]


@pytest.mark.parametrize('market',['a_share','us_equity'])
def test_single_condition_cli_workflow_both_markets_and_producers(tmp_path,market):
    plan,path = exploration_plan(tmp_path,market)
    preview = inspect_data_feasibility(plan)
    assert 'proxy_joint_events' not in preview and not preview['return_labels_used']
    root = tmp_path/'run'
    register_favor(path,root)
    request = favor_generation_payload(root,'F1','deepseek_api' if market=='a_share' else 'gpt6')
    assert request['exploration_measurement']['kind'] == 'continuous'
    assert submit_favor(root,{**request['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})['status'] == 'compiled'
    run_favor(root)
    summary = json.loads((root/'summary.json').read_text())
    assert summary['exploration_eligible'] == 1
    assert summary['retained_components'] == summary['retained_combinations'] == 0
    assert summary['test_evaluated'] is False and summary['sealed_oos'] is False
    assert summary['factors']['F1']['ic_diagnostic_status'] == 'evaluated'
    assert (root/'factors/F1/validation_diagnostics.json').exists()
    from factor_miner.favor_store import projection_payload
    _, kept, trials = projection_payload(root)
    assert not kept and len(trials) == 1
    frozen = json.loads((root/'exploration_freeze.json').read_text())
    assert not frozen['outcome_used_for_eligibility']


def test_failed_other_condition_does_not_block_exploration(tmp_path):
    _,path = exploration_plan(tmp_path,two=True)
    root = tmp_path/'run'; register_favor(path,root)
    first = favor_generation_payload(root,'F1','gpt6')
    submit_favor(root,{**first['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})
    second = favor_generation_payload(root,'F2','gpt6')
    assert submit_favor(root,{**second['submission_identity'],'expression':field('close').model_dump(mode='json')})['status']=='compile_failed'
    run_favor(root)
    result = json.loads((root/'summary.json').read_text())
    assert result['exploration_eligible']==1 and result['registered']==2
    assert result['factors']['F2']['status']=='compile_failed'


def test_old_serialization_and_new_frozen_contract_cannot_be_changed(tmp_path):
    plan,path = exploration_plan(tmp_path)
    assert 'exploration' not in FavorPlan.model_fields
    data = plan.model_dump(mode='json')
    assert parse_favor_plan(data).model_dump(mode='json') == data
    root=tmp_path/'run';register_favor(path,root)
    data['exploration']['recovery']['min_dates']=2
    (root/'plan.json').write_text(json.dumps(data))
    with pytest.raises(ValueError,match='变化'):
        load_plan(root)
    with pytest.raises(ValueError):
        rule(min_signal_coverage=.2)


def test_test_labels_cannot_affect_exploration(tmp_path):
    plan,path = exploration_plan(tmp_path)
    outputs=[]
    for index in range(2):
        if index:
            label_path=tmp_path/'dataset/label.parquet'
            labels=pl.read_parquet(label_path).with_columns(pl.when(pl.col('date')>=plan.splits.test_start).then(999.).otherwise(pl.col('label_o2o_5d')).alias('label_o2o_5d'))
            labels.write_parquet(label_path)
            data=plan.model_dump(mode='json');data['input_sha256'][str(label_path)]=file_sha(label_path)
            path.write_text(parse_favor_plan(data).model_dump_json())
        root=tmp_path/f'run{index}';register_favor(path,root)
        request=favor_generation_payload(root,'F1','gpt6')
        submit_favor(root,{**request['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})
        run_favor(root)
        summary=json.loads((root/'summary.json').read_text())
        outputs.append((summary['factors']['F1']['discovery'],json.loads((root/'factors/F1/validation_diagnostics.json').read_text())))
    assert outputs[0]==outputs[1]


def test_unrecoverable_history_runs_diagnostics_without_statistical_pass(tmp_path):
    from tests.test_research_efficiency import bounded
    from factor_miner.research_pool import validate_search_budget
    plan, path = exploration_plan(tmp_path, 'a_share')
    data = plan.model_dump(mode='json')
    budget = dict(bounded(data['search_budget']), validation_target='signal_exploration',
        historical_budget_status='unrecoverable', historical_attempt_count=None,
        inherited_diagnostic_family_size=None, history_loss_reason='用户确认完整历史预算已删除，无法恢复')
    data.update(run_kind='research', search_budget=budget)
    path.write_text(parse_favor_plan(data).model_dump_json())
    root = tmp_path/'run'
    register_favor(path, root)
    request = favor_generation_payload(root, 'F1', 'gpt6')
    submit_favor(root, {**request['submission_identity'], 'expression':expressions()[0].model_dump(mode='json')})
    run_favor(root)
    summary = json.loads((root/'summary.json').read_text())
    report = summary['factors']['F1']
    assert summary['diagnostic_family_size'] is None
    assert report['status'] == 'exploration_evaluated'
    assert report['discovery']['raw_p_value'] is not None
    assert report['discovery']['bonferroni_p_value'] is None
    assert report['standalone_statistical_pass'] is None
    assert report['statistical_inference_status'] == 'unavailable_historical_budget'
    assert report['validation'] and not summary['test_evaluated']
    from factor_miner.favor_store import projection_payload
    assert not projection_payload(root)[1]
    for changes in ({'validation_target':'joint_trigger'}, {'validation_target':'portfolio_increment'},
                    {'historical_attempt_count':0}, {'inherited_diagnostic_family_size':0},
                    {'history_loss_reason':''}, {'historical_budget_status':'complete'}):
        with pytest.raises(ValueError):
            validate_search_budget(dict(budget, **changes), [])


def test_real_registration_requires_explicit_exploration_budget_and_preflight_is_label_free(tmp_path):
    from tests.test_research_efficiency import bounded
    plan,path=exploration_plan(tmp_path)
    data=plan.model_dump(mode='json');data['run_kind']='research'
    data['search_budget']=bounded(data['search_budget'])
    with pytest.raises(ValueError,match='signal_exploration'):
        parse_favor_plan(data)
    data['search_budget']['validation_target']='signal_exploration'
    data['search_budget']['resource_limits']['output_tokens']=None
    plan=parse_favor_plan(data);path.write_text(plan.model_dump_json())
    label=tmp_path/'dataset/label.parquet'
    original=label.read_bytes();label.unlink()
    assert not inspect_data_feasibility(plan)['return_labels_used']
    label.write_bytes(original)
    register_favor(path,tmp_path/'run')
    assert load_plan(tmp_path/'run').search_budget['resource_limits']['output_tokens'] is None


def test_schema_cli_defaults_to_exploration_and_explicit_old_schema_remains(tmp_path):
    from typer.testing import CliRunner
    from factor_miner.cli import app
    runner=CliRunner()
    for protocol in ('exploration','regime','legacy'):
        path=tmp_path/f'{protocol}.json'
        result=runner.invoke(app,['favor-schema',str(path),'--protocol',protocol])
        assert result.exit_code==0,result.output
        schema=json.loads(path.read_text())
        assert ('exploration' in schema['properties']) == (protocol=='exploration')


def test_monthly_ic_insufficiency_does_not_block_fixed_validation(tmp_path):
    plan,path=exploration_plan(tmp_path)
    data=plan.model_dump(mode='json')
    data['exploration']['recovery'].update(observation_frequency='monthly_last_session',min_dates=12)
    data['execution']['frequency']='monthly_last_session'
    path.write_text(parse_favor_plan(data).model_dump_json())
    root=tmp_path/'run';register_favor(path,root)
    request=favor_generation_payload(root,'F1','gpt6')
    submit_favor(root,{**request['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})
    run_favor(root)
    report=json.loads((root/'summary.json').read_text())['factors']['F1']
    assert report['status']=='exploration_evaluated'
    assert report['ic_diagnostic_status']=='insufficient_sample'
    assert report['standalone_statistical_pass'] is None
    assert len(report['validation'])==3


@pytest.mark.parametrize('kind',['state','event'])
@pytest.mark.parametrize('market',['a_share','us_equity'])
def test_discrete_candidate_reaches_shared_end_to_end_execution(tmp_path,kind,market):
    from factor_miner.schema import FactorNode
    from factor_miner.favor_demo import rule as ast_rule
    plan,path=exploration_plan(tmp_path,market)
    expression=FactorNode(op='gt',args=(expressions()[0],FactorNode(op='const',value=.005)))
    data=plan.model_dump(mode='json')
    data['conditions'][0]['expression_constraint']=ast_rule(expression).model_dump(mode='json')
    data['exploration']['recovery']=rule(kind,min_dates=20,min_assets=20,min_group_samples=20).model_dump(mode='json')
    data['execution']['frequency']='daily'
    path.write_text(parse_favor_plan(data).model_dump_json())
    root=tmp_path/'run';register_favor(path,root)
    request=favor_generation_payload(root,'F1','gpt6')
    assert submit_favor(root,{**request['submission_identity'],'expression':expression.model_dump(mode='json')})['status']=='compiled'
    run_favor(root)
    result=json.loads((root/'summary.json').read_text())
    assert result['exploration_eligible']==1,result['factors']
    assert result['factors']['F1']['status']=='exploration_evaluated'
    if kind=='event':
        assert result['factors']['F1']['ic_diagnostic_status']=='not_applicable'


def test_sparse_synthetic_change_does_not_need_half_the_dates_to_move():
    from factor_miner.schema import FactorNode
    from factor_miner.construct_validation import validate_construct
    expression=FactorNode(op='gt',args=(expressions()[0],FactorNode(op='const',value=.04)))
    condition=conditions()[0].observation.model_copy(update={'response_test':'positive_intraday_jump'})
    old=validate_construct(expression,condition)
    new=validate_construct(expression,condition,response_aggregation='directional_changes')
    assert old['status']=='偏离'
    assert new['status']=='基础检验符合'
    assert new['checks'][-1]['directional_changes']>=2
    assert new['checks'][-1]['median_response']==0
    assert validate_construct(expression,condition.model_copy(update={'expected_response':'decrease'}),
        response_aggregation='directional_changes')['status']=='偏离'


def test_a_share_cli_register_request_submit_run(tmp_path):
    from typer.testing import CliRunner
    from factor_miner.cli import app
    _,config=exploration_plan(tmp_path,'a_share')
    root=tmp_path/'run';runner=CliRunner()
    result=runner.invoke(app,['register-favor',str(config),str(root)])
    assert result.exit_code==0,result.output
    request_path=tmp_path/'request.json'
    result=runner.invoke(app,['favor-request',str(root),'F1',str(request_path),'--producer','gpt6'])
    assert result.exit_code==0,result.output
    request=json.loads(request_path.read_text())
    submission=tmp_path/'submission.json'
    submission.write_text(json.dumps({**request['submission_identity'],'expression':expressions()[0].model_dump(mode='json')}))
    result=runner.invoke(app,['submit-favor',str(root),str(submission)])
    assert result.exit_code==0,result.output
    result=runner.invoke(app,['run-favor',str(root)])
    assert result.exit_code==0,result.output
    assert json.loads((root/'summary.json').read_text())['exploration_eligible']==1
