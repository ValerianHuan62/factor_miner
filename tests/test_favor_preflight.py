"""在候选和标签前发现观察方向错误，预检不改变金融方向。"""
import json
import pytest
from factor_miner.favor_demo import make_plan,field
from factor_miner.favor_schema import FavorPlan
from factor_miner.favor_preflight import inspect_measurement_contracts,preflight_favor
from factor_miner.schema import FactorNode as N

def low_anchor_plan(tmp_path, response='increase', relationship='increase'):
    plan,_=make_plan(tmp_path)
    raw=plan.model_dump(mode='json');c=raw['conditions'][0]
    expression=N(op='div',args=(field('close'),N(op='rolling_min',args=(field('low'),),window=40)))
    c['observation'].update(observation='价格脱离历史低点',measurement='价格除历史低点',response_test='near_low',expected_response=response)
    c['state_measurements'][0].update(expression=expression.model_dump(mode='json'),expected_direction=relationship)
    return FavorPlan.model_validate(raw)

def test_low_anchor_wrong_response_rejected_and_correct_direction_not_flipped(tmp_path):
    plan=low_anchor_plan(tmp_path)
    before=plan.model_dump(mode='json')
    bad=inspect_measurement_contracts(plan)
    assert not bad['passed'] and not bad['return_labels_used']
    condition=plan.conditions[0]
    corrected=condition.model_copy(update={'observation':condition.observation.model_copy(update={'expected_response':'decrease'})})
    good=inspect_measurement_contracts(plan.model_copy(update={'conditions':(corrected,*plan.conditions[1:])}))
    assert good['passed']
    assert before==plan.model_dump(mode='json')
    assert corrected.expected_return_sign==condition.expected_return_sign

def test_inverse_measurement_maps_response_and_never_opens_data(tmp_path):
    plan=low_anchor_plan(tmp_path,relationship='decrease')
    plan=plan.model_copy(update={'dataset_root':'/not/available','input_sha256':{}})
    config=tmp_path/'contract.json';config.write_text(plan.model_dump_json())
    output=tmp_path/'check.json';result=preflight_favor(config,output)
    assert result['passed']
    assert not json.loads(output.read_text())['real_market_data_used']
    with pytest.raises(FileExistsError):preflight_favor(config,output)

def test_invalid_state_operator_is_reported_before_registration(tmp_path):
    plan,_=make_plan(tmp_path);condition=plan.conditions[0]
    bad=N(op='hlc_spread',args=(field('high'),field('low'),field('close')),window=20)
    state=condition.state_measurements[0].model_copy(update={'expression':bad})
    plan=plan.model_copy(update={'conditions':(condition.model_copy(update={'state_measurements':(state,)}),*plan.conditions[1:])})
    report=inspect_measurement_contracts(plan)
    assert not report['passed']
    assert report['measurements'][0]['stage']=='structure'
