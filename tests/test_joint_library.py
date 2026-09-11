"""代表库采纳、身份和默认输入范围回归。"""
import json
from pathlib import Path
import pytest

from factor_miner.joint_library import active_members, adopt_library, load_library, validate_members
from factor_miner.joint_study import digest
from factor_miner.research_report import write_json


def make_study(tmp_path):
    collection=tmp_path/'collection'; collection.mkdir()
    write_json(collection/'collection.json', {'market_id':'a_share','reviews':[]})
    study=tmp_path/'study';study.mkdir()
    members=[dict(factor_id=f'huan00{i}',source_candidate_id=f'cand{i}',group='g',qualification='research_candidate_pending_confirmation',original_statistical_pass=None) for i in (2,3)]
    roles=[dict(factor_id=r['factor_id'],group='g',representative='huan002',role='研究代表' if i==0 else '同类替补',selection_uses_returns=False) for i,r in enumerate(members)]
    write_json(study/'protocol.json',dict(candidates=members,formal_pass=None,config={'collection':str(collection)},input_sha256={str((collection/'collection.json').resolve()):digest(collection/'collection.json')}))
    write_json(study/'summary.json',dict(status='completed_diagnostic',formal_pass=None,sealed_oos=False))
    write_json(study/'representatives.json',roles)
    (study/'研究结果.md').write_text('结果');(study/'研究结果.html').write_text('结果')
    write_json(study/'completion.json',{'artifacts':{p.name:digest(p) for p in study.iterdir()}})
    return study


def test_adoption_is_complete_immutable_and_preserves_qualification(tmp_path):
    study=make_study(tmp_path); root=tmp_path/'library'
    result=adopt_library(study,root)
    assert (result['representatives'],result['reserves'])==(1,1)
    assert [r['factor_id'] for r in active_members(root)]==['huan002']
    assert len(active_members(root,True))==2
    assert all(r['original_statistical_pass'] is None for r in active_members(root,True))
    with pytest.raises(FileExistsError):adopt_library(study,root)
    (root/'library.json').write_text('{}')
    with pytest.raises(ValueError,match='身份变化'):load_library(root)


def test_reject_changed_diagnostic_and_incomplete_roles(tmp_path):
    study=make_study(tmp_path)
    (study/'representatives.json').write_text('[]')
    with pytest.raises(ValueError,match='产物身份变化'):adopt_library(study,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_reject_cross_group_and_cyclic_reserves(tmp_path):
    study=make_study(tmp_path);root=tmp_path/'library';adopt_library(study,root)
    members=load_library(root)['members'];members[1]['group']='other'
    with pytest.raises(ValueError,match='同机制代表'):validate_members(members)
    members[1]['group']='g';members[0]['representative']='huan003'
    with pytest.raises(ValueError):validate_members(members)


def test_dashboard_joins_roles_without_changing_metrics(tmp_path,monkeypatch):
    import dashboard.favor_cost_page as page
    study=make_study(tmp_path);root=tmp_path/'library';adopt_library(study,root)
    rows=[{'编号':f'huan00{i}','实际年化':None,'状态':'待正式确认'} for i in (2,3)]
    monkeypatch.setattr(page,'candidate_rows',lambda *_: rows)
    result=page.library_rows(tmp_path/'collection','a_share',root)
    assert [r['库内角色'] for r in result]==['研究代表','同类替补']
    assert all(r['实际年化'] is None and r['状态']=='待正式确认' for r in result)
    with pytest.raises(ValueError,match='市场'):page.library_rows(tmp_path/'collection','us_equity',root)
