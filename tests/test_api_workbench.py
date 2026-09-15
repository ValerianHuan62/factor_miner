"""工作台的市场隔离、凭据与审核状态展示。"""
import json
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from tests.test_dashboard_simple import profile_file
from tests.test_joint_library import make_study
from factor_miner.joint_library import adopt_library
from dashboard.api_connection import save_settings,read_settings,key_available


def test_settings_keep_key_private_and_blank_preserves_it(tmp_path,monkeypatch):
    path=tmp_path/'api.local.json';monkeypatch.setattr('dashboard.api_connection.settings_path',lambda:path)
    save_settings('synthetic-secret',{})
    assert path.stat().st_mode & 0o777==0o600
    save_settings('',{})
    assert read_settings()['api_key']=='synthetic-secret' and key_available()
    with patch.dict('os.environ',{'FM_MARKET_PROFILES_PATH':str(profile_file(tmp_path))}):
        page=AppTest.from_string('from dashboard.api_workbench import render_api_settings\nrender_api_settings()').run()
        assert not page.exception
        key=next(x for x in page.text_input if x.label=='DeepSeek API Key')
        assert key.value==''
        assert all('synthetic-secret' not in str(x.value) for x in page.markdown)


def test_library_uses_global_market_and_separates_review_from_statistics(tmp_path,monkeypatch):
    study=make_study(tmp_path);library=tmp_path/'library';adopt_library(study,library)
    profiles=profile_file(tmp_path);data=json.loads(profiles.read_text())
    data['markets'][0].update(joint_library_path=str(library),cost_review_path=str(tmp_path/'collection'))
    profiles.write_text(json.dumps(data))
    rows=[dict(编号=f'huan00{i}',名称='合成测量',状态='待正式确认',公式='delta(close,20)',
        IC均值=.01,RankIC均值=.02,参考年化=.1,参考最大回撤=.2,参考Sharpe=.5,双边成本bps=14,未确定持仓=1,同类关系='合成关系') for i in (2,3)]
    monkeypatch.setattr('dashboard.favor_cost_page.candidate_rows',lambda *_:rows)
    with patch.dict('os.environ',{'FM_MARKET_PROFILES_PATH':str(profiles)}):
        page=AppTest.from_file('dashboard/pages/12_研究代表库.py');page.session_state['fm_market_id']='a_share';page.run()
        assert not page.exception
        assert page.dataframe[0].value['审核状态'].tolist()==['已审核 · 已采纳']
        page.radio[0].set_value('同类替补').run()
        assert page.dataframe[0].value['审核状态'].tolist()==['已审核 · 同类替补']
        page.session_state['fm_market_id']='us_equity';page.run()
        assert not page.exception and not page.dataframe
        assert '当前市场' in page.info[0].value
    assert all(row['状态']=='待正式确认' for row in rows)
