"""API 调用经过相同登记、隐私、预算、编译和真实合成诊断链。"""
import json
from pathlib import Path
import pytest
from factor_miner.favor_api import api_preflight,run_api_research
from factor_miner.favor_demo import expressions
from factor_miner.favor_workflow import register_favor
from tests.test_favor_exploration import exploration_plan


def registered(tmp_path,market='us_equity',two=False,**limits):
    _,config=exploration_plan(tmp_path,market,two=two)
    payload=json.loads(config.read_text())
    payload['search_budget'].update(version='bounded-research-v2',research_question='合成问题',primary_comparison='合成固定对照',
        information_gain_rationale='验证入口一致性',validation_target='signal_exploration',stop_rule='有限名额完成后停止',
        resource_limits=dict(model_responses=2,output_tokens=10000,wall_seconds=600,artifact_bytes=100000000,**limits))
    config.write_text(json.dumps(payload));root=tmp_path/'run';register_favor(config,root)
    return root


class Transport:
    def __init__(self,*,unknown=False,wrong=False):self.count=0;self.unknown=unknown;self.wrong=wrong
    def post(self,body,key):
        self.count+=1
        request=json.loads(body)
        payload=json.loads(request['messages'][-1]['content'])
        assert 'dataset_root' not in payload and 'input_sha256' not in payload
        identity=payload['submission_identity']
        if self.wrong:identity['trial_id']='OTHER'
        output=dict(identity,expression=expressions()[self.count-1].model_dump(mode='json'))
        return json.dumps(dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(output)))],
            usage={} if self.unknown else dict(completion_tokens=30,prompt_tokens=100))).encode()


@pytest.mark.parametrize('market',['a_share','us_equity'])
def test_api_full_shared_pipeline_and_no_repeated_call(tmp_path,monkeypatch,market):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path,market)
    preview=api_preflight(root,market)
    transport=Transport()
    state=run_api_research(root,market,preview['plan_sha256'],transport=transport)
    assert state['status']=='completed',state
    assert state['output_tokens']==30 and transport.count==1
    assert (root/'summary.json').exists()
    assert json.loads((root/'submissions/F1/receipt.json').read_text())['status']=='compiled'
    assert 'synthetic-secret' not in ''.join(p.read_text() for p in (root/'api').rglob('*.json'))
    with pytest.raises(ValueError):run_api_research(root,market,preview['plan_sha256'],transport=transport)
    assert transport.count==1


def test_cross_market_and_changed_identity_do_not_call(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path);transport=Transport()
    with pytest.raises(ValueError,match='市场'):api_preflight(root,'a_share')
    with pytest.raises(ValueError,match='变化'):run_api_research(root,'us_equity','0'*64,transport=transport)
    assert transport.count==0 and not (root/'api').exists()


def test_unknown_usage_stops_without_next_call_or_evaluation(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path,two=True);preview=api_preflight(root,'us_equity');transport=Transport(unknown=True)
    state=run_api_research(root,'us_equity',preview['plan_sha256'],transport=transport)
    assert state['status']=='failed' and state['output_tokens'] is None
    assert transport.count==1 and not (root/'evaluation_started.json').exists()


def test_wrong_slot_cannot_be_imported(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path);preview=api_preflight(root,'us_equity');transport=Transport(wrong=True)
    state=run_api_research(root,'us_equity',preview['plan_sha256'],transport=transport)
    assert state['status']=='failed' and not (root/'submissions/OTHER').exists()


def test_transport_failure_keeps_unknown_usage_and_no_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path);preview=api_preflight(root,'us_equity')
    class Broken:
        def post(self,body,key):raise OSError('synthetic network failure')
    result=run_api_research(root,'us_equity',preview['plan_sha256'],transport=Broken())
    assert result['status']=='failed' and result['output_tokens'] is None
    assert result['attempted_calls']==1 and not (root/'evaluation_started.json').exists()


def test_stop_request_preserves_submission_and_does_not_evaluate(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-secret')
    root=registered(tmp_path,two=True);preview=api_preflight(root,'us_equity')
    class Stop(Transport):
        def post(self,body,key):
            response=super().post(body,key)
            (root/'api/stop.json').write_text('{}')
            return response
    transport=Stop()
    result=run_api_research(root,'us_equity',preview['plan_sha256'],transport=transport)
    assert result['status']=='stopped' and transport.count==1 and result['submitted']==1
    assert not (root/'evaluation_started.json').exists()
