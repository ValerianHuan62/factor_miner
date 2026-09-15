"""状态登记与展示回归：保持旧身份，拒绝错配和事后替换。"""
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from factor_miner.canonical import sha256_json
from factor_miner.regime import RegimeHypothesisSpec, RegimeRecord, RegimeManifest, RegimeValidation, attach_records, save_manifest, load_manifest, record_fields
from factor_miner.favor_schema import FavorPlan, FavorRegimePlan, parse_favor_plan
from factor_miner.favor_demo import make_plan, expressions
from factor_miner.favor_workflow import register_favor, load_plan, favor_generation_payload, submit_favor


def state_claim(**updates):
    data = dict(has_claim=True,claim='高波动时预期增强',dimension='volatility',direction='high',effect='stronger',scope='market_time',
        measurement_ref='synthetic-vol-v1',measurement_definition='历史20日波动',required_fields=['close'],availability='前一日收盘后',
        lag_sessions=1,threshold_rule='过去252日中位数',data_available=True,unavailable_reason='',control_state='低波动',
        failure_condition='平静时期可能减弱',falsification='预登记差值不足或反向',competing_explanations=['样本构成'])
    return RegimeHypothesisSpec.model_validate({**data,**updates})


def record(**updates):
    return RegimeRecord.model_validate(dict(market_id='us_equity',source_candidate_id='cand_test',factor_id='huan999',
        hypothesis_sha256='h',mechanism='合成机制',origin='historical_addendum',registered_at=datetime.now(timezone.utc),
        results_seen=True,previously_seen=False,context_id='context',context_label='合成五日',spec=state_claim(),**updates))


def test_manifest_roundtrip_preserves_original_metrics(tmp_path):
    r=record();m=RegimeManifest(market_id=r.market_id,context_id=r.context_id,records=[r],source_files={})
    p=tmp_path/'manifest.json';save_manifest(m,p);save_manifest(m,p)
    original=dict(market_id='us_equity',source_candidate_id='cand_test',factor_id='huan999',rank_ic_mean=.012,status='原状态')
    result=attach_records([original],load_manifest(p,'us_equity'))[0]
    assert result['rank_ic_mean']==.012 and result['status']=='原状态'
    assert result['regime_validation_status']=='待检验'
    assert 'regime_record' not in original
    with pytest.raises(ValueError,match='市场'):
        load_manifest(p,'a_share')
    with pytest.raises(ValueError,match='身份'):
        attach_records([{**original,'source_candidate_id':'wrong'}],m)
    with pytest.raises(ValueError,match='重复'):
        RegimeManifest(market_id=r.market_id,context_id=r.context_id,records=[r,r],source_files={})


def test_missing_and_corrupt_sources_are_not_untested(tmp_path):
    source=tmp_path/'source.json';source.write_text('{}')
    from factor_miner.regime import file_sha
    m=RegimeManifest(market_id='us_equity',context_id='context',records=[record()],source_files={str(source):file_sha(source)})
    p=tmp_path/'manifest.json';save_manifest(m,p);source.write_text('changed')
    with pytest.raises(ValueError,match='变化'):
        load_manifest(p,'us_equity')
    assert record_fields(None)['expected_regime_label']=='未登记'


def test_no_claim_is_not_all_state_valid():
    s=state_claim(has_claim=False,claim='暂无理论依据',dimension='none',direction='none',effect='none',scope='none')
    assert s.label=='尚无明确条件假设'
    with pytest.raises(ValueError): state_claim(lag_sessions=-1)
    with pytest.raises(ValueError): state_claim(data_available=False)


def test_old_plan_hash_and_gamma_unchanged(tmp_path):
    plan,config=make_plan(tmp_path)
    raw=plan.model_dump(mode='json');identity=sha256_json(raw)
    assert sha256_json(parse_favor_plan(raw).model_dump(mode='json'))==identity
    assert 'regimes' not in FavorPlan.model_fields
    root=tmp_path/'old';register_favor(config,root)
    assert load_plan(root).model_dump(mode='json')==raw
    assert 'regime_hypothesis' not in favor_generation_payload(root,'F1','gpt6')


def test_new_plan_binds_before_submission_and_keeps_ledger(tmp_path):
    plan,config=make_plan(tmp_path)
    data=plan.model_dump(mode='json');data.update(version='favor-regime-v1',regimes={c.condition_id:state_claim().model_dump(mode='json') for c in plan.conditions})
    new=FavorRegimePlan.model_validate(data);config.write_text(new.model_dump_json())
    root=tmp_path/'new';register_favor(config,root)
    request=favor_generation_payload(root,'F1','gpt6')
    other=favor_generation_payload(root,'F1','deepseek_api')
    assert request['regime_hypothesis']==other['regime_hypothesis']
    bad={**request['submission_identity'],'regime_sha256':'wrong','expression':expressions()[0].model_dump(mode='json')}
    receipt=submit_favor(root,bad)
    assert receipt['status']=='compile_failed' and '状态' in receipt['reason']
    with pytest.raises(FileExistsError):submit_favor(root,bad)
    good=favor_generation_payload(root,'F2','gpt6')
    receipt=submit_favor(root,{**good['submission_identity'],'expression':expressions()[1].model_dump(mode='json')})
    assert receipt['status']=='compiled'
    state=RegimeRecord.model_validate_json((root/'submissions/F2/regime_record.json').read_text())
    assert state.origin=='before_expression' and not state.results_seen
    changed=json.loads((root/'plan.json').read_text());changed['regimes'][plan.conditions[0].condition_id]['claim']='结果后改写'
    (root/'plan.json').write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='变化'): load_plan(root)


def test_request_contains_no_paths_or_returns(tmp_path):
    from factor_miner.llm_hypothesis import regime_hypothesis_request
    plan,_=make_plan(tmp_path)
    request=regime_hypothesis_request(plan.model_dump(mode='json'))
    assert 'dataset_root' not in request and 'splits' not in request
    assert request['response_schema']['required']==[c.condition_id for c in plan.conditions]


@pytest.mark.parametrize('tamper', [False, True])
def test_new_regime_plan_reaches_evaluation_and_rejects_state_replacement(tmp_path, tamper):
    """新版提交必须进入真实评价入口，且评价时再次核验状态身份。"""
    from factor_miner.favor_workflow import run_favor
    plan, config = make_plan(tmp_path)
    data = plan.model_dump(mode='json')
    data.update(version='favor-regime-v1', regimes={c.condition_id: state_claim().model_dump(mode='json') for c in plan.conditions})
    config.write_text(FavorRegimePlan.model_validate(data).model_dump_json())
    root = tmp_path / 'regime_run'
    register_favor(config, root)
    for trial, expression in zip(['F1', 'F2'], expressions()):
        request = favor_generation_payload(root, trial, 'gpt6')
        receipt = submit_favor(root, {**request['submission_identity'], 'expression': expression.model_dump(mode='json')})
        assert receipt['status'] == 'compiled'
    if tamper:
        path = root / 'submissions/F1/submission.json'
        payload = json.loads(path.read_text())
        payload['regime_sha256'] = 'replaced_after_submission'
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match='冻结状态'):
            run_favor(root)
        assert (root / 'failure.json').exists()
    else:
        run_favor(root)
        assert (root / 'completion.json').exists()


def test_reject_unsupported_claim_and_inconsistent_record():
    v=dict(run_id='run',spec_sha256='wrong',context_id='context',status='not_evaluated',start='2024-01-01',end='2025-01-01',
        test_consumed=True,sealed_oos=False,target_dates=20,control_dates=20,unknown_dates=0,family_size=10,alpha=.05,
        protocol_sha256='p',source_path='s',source_sha256='s',interpretation='合成')
    with pytest.raises(ValueError,match='不匹配'):record(validation=v)
    with pytest.raises(ValueError):RegimeValidation.model_validate({**v,'status':'supported'})


def test_regime_filters_and_csv():
    from dashboard.factor_explorer import filter_factor_rows,factor_rows_csv
    row=dict(factor_id='huan999',valid_dates=30,rank_ic_hac_t=1,**record_fields(record()))
    assert filter_factor_rows([row],regime_labels=['高波动'])==[row]
    assert not filter_factor_rows([row],regime_statuses=['支持预期增强'])
    assert '历史补充假设' in factor_rows_csv([row]).decode('utf-8-sig')


def test_superseded_batch_cannot_become_current(tmp_path):
    from factor_miner.regime import import_state_batch
    (tmp_path/'correction_notice.json').write_text('{}')
    with pytest.raises(ValueError,match='替代'):
        import_state_batch(tmp_path,tmp_path/'manifest.json')
