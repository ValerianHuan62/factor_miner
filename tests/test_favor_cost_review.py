"""成本研究资格与正式确认分开；不能以降成本恢复相反假设。"""
import pytest
from factor_miner.favor_cost_review import research_admission


def test_cost_positive_does_not_repair_wrong_direction_or_missing_construct():
    report = dict(synthetic={'status':'基础检验符合'},empirical={'passed':True},discovery={'ic_mean':-.02,'rank_ic_mean':-.03})
    metrics = {'reference':{'annualized_return':.1},'actual':None}
    assert not research_admission(report,'positive',metrics)['eligible']
    accepted = research_admission(report,'negative',metrics)
    assert accepted['eligible'] and accepted['formal_pass'] is False
    report['empirical']['passed'] = False
    assert not research_admission(report,'negative',metrics)['eligible']


@pytest.mark.parametrize('value',[0.,-.1,float('nan')])
def test_nonpositive_or_unknown_return_not_admitted(value):
    report=dict(synthetic={'status':'基础检验符合'},empirical={'passed':True},discovery={'ic_mean':.02,'rank_ic_mean':.03})
    assert not research_admission(report,'positive',{'reference':{'annualized_return':value}})['eligible']


def test_completed_review_preserves_source_and_cannot_rewrite_identity(tmp_path):
    import json
    from pathlib import Path
    from tests.test_favor_exploration import exploration_plan
    from factor_miner.favor_demo import expressions
    from factor_miner.favor_workflow import register_favor, favor_generation_payload, submit_favor, run_favor, file_sha
    from factor_miner.favor_cost_review import run_cost_review, load_cost_review
    _, plan_path = exploration_plan(tmp_path)
    source=tmp_path/'source'; register_favor(plan_path,source)
    req=favor_generation_payload(source,'F1','gpt6')
    submit_favor(source,{**req['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})
    run_favor(source)
    summary=json.loads((source/'summary.json').read_text())
    raw=Path(summary['factors']['F1']['raw_path']).resolve()
    files={str(p):file_sha(p) for p in [source/'plan.json',source/'summary.json',source/'completion.json',raw]}
    config=dict(version='favor-cost-review-v1',market_id='us_equity',round_trip_cost_bps=14,
                output_root=str(tmp_path/'review'),sources=[dict(root=str(source),trial_id='F1',files=files)])
    path=tmp_path/'cost.json';path.write_text(json.dumps(config))
    output=run_cost_review(path);result=load_cost_review(output)
    assert result['formal_candidates']==0 and not result['test_evaluated']
    assert result['results'][0]['parity_verified']
    assert all(file_sha(Path(p))==digest for p,digest in files.items())
    (output/'F1/report.json').write_text('{}')
    with pytest.raises(ValueError,match='身份变化'):load_cost_review(output)


def test_export_reuses_frozen_cost_without_execution_and_detects_source_change(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from tests.test_favor_exploration import exploration_plan
    from factor_miner.favor_demo import expressions
    from factor_miner.favor_workflow import register_favor, favor_generation_payload, submit_favor, run_favor, file_sha
    from factor_miner.favor_cost_review import export_exploration_candidates, load_cost_review
    _, path = exploration_plan(tmp_path, market='a_share')
    plan=json.loads(path.read_text());plan['execution']['round_trip_cost_bps']=14;path.write_text(json.dumps(plan))
    source=tmp_path/'source';register_favor(path,source)
    req=favor_generation_payload(source,'F1','gpt6')
    submit_favor(source,{**req['submission_identity'],'expression':expressions()[0].model_dump(mode='json')})
    run_favor(source)
    detail=source/'factors/F1/validation_diagnostics.json'
    files={str(p):file_sha(p) for p in [source/'plan.json',source/'summary.json',source/'completion.json',source/'submissions/F1/spec.json',detail]}
    config=dict(version='favor-research-export-v1',market_id='a_share',round_trip_cost_bps=14,
        output_root=str(tmp_path/'export'),sources=[dict(root=str(source),trial_id='F1',files=files)])
    config_path=tmp_path/'export.json';config_path.write_text(json.dumps(config))
    def forbidden(*args,**kwargs):raise AssertionError('复用不能重跑交易')
    monkeypatch.setattr('factor_miner.favor_cost_review.execute_joint',forbidden)
    output=export_exploration_candidates(config_path);result=load_cost_review(output)
    assert result['results'][0]['execution_reused']
    original=json.loads(detail.read_text())[0]
    assert result['results'][0]['metrics']==original['metrics']
    assert result['formal_candidates']==0 and not result['test_evaluated']
    assert all(file_sha(Path(p))==h for p,h in files.items())
    detail.write_text('[]')
    with pytest.raises(ValueError,match='源研究身份变化'):load_cost_review(output)


def test_dashboard_collection_keeps_previous_batch_and_checks_publication(tmp_path):
    import json
    from dashboard.favor_cost_page import candidate_rows
    from factor_miner.favor_workflow import file_sha
    from factor_miner.research_report import write_json
    reviews=[]
    for index in [2,3]:
        root=tmp_path/str(index);trial=f'F{index}'
        row=dict(trial_id=trial,name='合成研究候选',discovery={'ic_mean':.02,'rank_ic_mean':.03},
            round_trip_cost_bps=14,metrics={'actual':None,'reference':{'annualized_return':.1,'max_drawdown':.2,'sharpe':.4,'information_ratio':.1}},
            unresolved_positions=1,related_candidates=[],admission={'eligible':True})
        write_json(root/trial/'report.json',row);write_json(root/'protocol.json',{'fixture':index})
        write_json(root/'summary.json',dict(version='favor-cost-review-v1',market_id='a_share',results=[row],
            research_candidates=1,formal_candidates=0,test_evaluated=False,sealed_oos=False))
        write_json(root/'completion.json',dict(summary_sha256=file_sha(root/'summary.json'),protocol_sha256=file_sha(root/'protocol.json'),reports={trial:file_sha(root/trial/'report.json')}))
        write_json(root/'publication.json',dict(summary_sha256=file_sha(root/'summary.json'),aliases={trial:f'huan{index:03d}'},formulas={trial:'close'}))
        reviews.append(dict(root=str(root),summary_sha256=file_sha(root/'summary.json'),publication_sha256=file_sha(root/'publication.json')))
    collection=tmp_path/'collection';write_json(collection/'collection.json',dict(market_id='a_share',reviews=reviews))
    rows=candidate_rows(collection,'a_share')
    assert [r['编号'] for r in rows]==['huan002','huan003']
    assert all(r['实际年化'] is None and r['状态']=='待正式确认' for r in rows)
    (tmp_path/'2/publication.json').write_text('{}')
    with pytest.raises(ValueError,match='发布身份变化'):candidate_rows(collection,'a_share')
