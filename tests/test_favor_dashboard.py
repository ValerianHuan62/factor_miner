"""真实 Streamlit 事件验证 FaVOR 默认入口、市场隔离、三档统计和成本切换。"""
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.test_dashboard_simple import profile_file
from factor_miner.favor_demo import make_plan, expressions
from factor_miner.favor_workflow import register_favor, favor_generation_payload, submit_favor, run_favor


def test_workbench_defaults_to_shared_favor_and_preserves_history(tmp_path):
    with patch.dict("os.environ", {"FM_MARKET_PROFILES_PATH":str(profile_file(tmp_path))}):
        page=AppTest.from_file("dashboard/pages/7_研究运行台.py",default_timeout=30).run()
        assert not page.exception
        assert page.radio[0].value=="API 研究"
        assert any("还没有登记" in x.value for x in page.info)
        assert not any("生成服务未连接" in x.value for x in page.warning)
        page.radio[0].set_value("历史批次流程").run()
        assert not page.exception
        assert [x.label for x in page.tabs]==["研究批次","自然语言分区"]


def test_favor_page_renders_ladder_and_cost_scenarios(tmp_path):
    _,config=make_plan(tmp_path)
    root=tmp_path/"us_equity/favor/synthetic"
    register_favor(config,root)
    for i in range(2):
        identity=favor_generation_payload(root,f"F{i+1}","gpt6")["submission_identity"]
        submit_favor(root,{**identity,"expression":expressions()[i].model_dump(mode="json")})
    run_favor(root)
    with patch.dict("os.environ", {"FM_MARKET_PROFILES_PATH":str(profile_file(tmp_path))}):
        page=AppTest.from_file("dashboard/pages/7_研究运行台.py",default_timeout=30).run()
        assert not page.exception
        page.radio[0].set_value("FaVOR（A 股 / 美股统一）").run()
        assert next(x for x in page.metric if x.label=="保留因子").value=="2"
        assert next(x for x in page.metric if x.label=="保留组合").value=="1"
        assert any("合成演示" in x.value for x in page.warning)
        costs=next(x for x in page.selectbox if x.label=="双边成本（基点）")
        costs.set_value(40.).run()
        assert not page.exception
        assert len(page.get("plotly_chart"))==1
        assert not page.json
        import json
        summary=json.loads((root/'summary.json').read_text())
        table=next(x.value for x in page.dataframe if 'ic_mean' in x.value.columns)
        assert table['ic_mean'].tolist()==[r['discovery']['ic_mean'] for r in summary['factors'].values() if r['status']=='retained_component']
        page.session_state["fm_market_id"]="a_share"
        page.run()
        assert not page.exception
        assert any("还没有登记" in x.value for x in page.info)


def test_exploration_visible_without_false_retention(tmp_path):
    from tests.test_favor_exploration import exploration_plan
    _,config=exploration_plan(tmp_path)
    root=tmp_path/'us_equity/favor/exploration'
    register_favor(config,root)
    identity=favor_generation_payload(root,'F1','gpt6')['submission_identity']
    submit_favor(root,{**identity,'expression':expressions()[0].model_dump(mode='json')})
    run_favor(root)
    with patch.dict('os.environ',{'FM_MARKET_PROFILES_PATH':str(profile_file(tmp_path))}):
        page=AppTest.from_file('dashboard/pages/7_研究运行台.py',default_timeout=30).run()
        assert not page.exception
        page.radio[0].set_value('FaVOR（A 股 / 美股统一）').run()
        assert next(x for x in page.metric if x.label=='可继续探索').value=='1'
        assert next(x for x in page.metric if x.label=='正式保留').value=='0'
        assert any('尚未进行测试期确认' in x.value for x in page.info)
        import json
        summary=json.loads((root/'summary.json').read_text())
        table=next(x.value for x in page.dataframe if 'ic_mean' in x.value.columns)
        assert table.iloc[0]['ic_mean']==summary['factors']['F1']['discovery']['ic_mean']
        assert table.iloc[0]['RankIC']==summary['factors']['F1']['discovery']['rank_ic_mean']
