"""跨市场 API 研究工作台：登记、调用、后台进度与结果。"""
from __future__ import annotations
import json
from pathlib import Path
import streamlit as st

from dashboard.api_connection import key_available, read_settings, register_plan, run_roots, start_api
from dashboard.market_profiles import current_market_id, profile_by_id
from factor_miner.favor_workflow import load_plan
from factor_miner.favor_api import api_preflight, api_running
from factor_miner.errors import FactorMinerError


def render_api_settings():
    from dashboard.api_connection import save_settings
    from dashboard.market_profiles import load_market_profiles
    from factor_miner.llm_online import DEEPSEEK_MODEL
    settings=read_settings()
    st.subheader('模型 API')
    st.caption(f'DeepSeek · {DEEPSEEK_MODEL} · A 股与美股共用生成接口，研究数据和结果按市场隔离。')
    st.success('API Key 已配置') if key_available() else st.info('配置 API Key 后，可在“挖因子”直接启动研究。')
    with st.form('api_settings'):
        key=st.text_input('DeepSeek API Key',type='password',help='留空保留原值。密钥只保存在本机权限为 600 的私有配置，不写入 Git 或运行日志。')
        templates=dict(settings.get('templates',{}))
        with st.expander('各市场的研究计划模板'):
            st.caption('模板固定数据发布、切分与有限预算。工作台支持导入完整计划，再用中文表单编辑经济假设；模板不代表已批准新研究。')
            for profile in load_market_profiles():
                templates[profile.market_id]=st.text_input(profile.display_name+'计划模板路径',value=templates.get(profile.market_id,''))
        if st.form_submit_button('保存 API 设置',type='primary'):
            try:
                for market,path in templates.items():
                    if path:
                        from factor_miner.favor_schema import parse_favor_plan
                        plan=parse_favor_plan(json.loads(Path(path).expanduser().read_text()))
                        if plan.market_id != market:raise ValueError('模板市场不匹配')
                save_settings(key,templates)
                st.success('已保存。前往“挖因子”预览计划并启动。')
            except (ValueError,OSError) as error:st.error(str(error))


def new_plan(profile):
    st.write('先写清论文依据与经济假设，再冻结测量、数据和预算。')
    upload=st.file_uploader('导入研究计划',type=['json'],key='api_plan_'+profile.market_id)
    template=read_settings().get('templates',{}).get(profile.market_id)
    if upload is None and not template:
        st.info('首次使用请导入完整研究计划，或在设置中配置该市场模板。数据与统计协议只需配置一次；新问题的测量条件仍需事前核对。')
        return
    try:
        payload=json.loads(upload.getvalue() if upload else Path(template).expanduser().read_text())
        if payload.get('market_id')!=profile.market_id:raise ValueError('计划属于其他市场，请切换市场或更换计划')
    except (ValueError,OSError) as error:st.error(str(error));return
    with st.form('new_plan_'+profile.market_id):
        hypothesis=dict(payload['hypothesis'])
        for key,label in [('claim','经济假设'),('mechanism','经济机制'),('observable_proxy','可观察代理'),
                          ('independent_verification','独立验证方案'),('falsification_path','证伪路径')]:
            hypothesis[key]=st.text_area(label,value=hypothesis[key],height=80)
        for key,label in [('source_refs','论文来源（每行一项）'),('competing_explanations','竞争解释（每行一项）'),('failure_modes','失效条件（每行一项）')]:
            hypothesis[key]=[x.strip() for x in st.text_area(label,value='\n'.join(hypothesis[key]),height=80).splitlines() if x.strip()]
        with st.expander('测量、分区与预算'):
            contract=st.text_area('计划合同（不含上方经济假设）',value=json.dumps({k:v for k,v in payload.items() if k!='hypothesis'},ensure_ascii=False,indent=2),height=350)
            st.caption('新问题需同步修改观察条件及测量，不从历史收益反推方向，也不重置历史统计预算。')
        submitted=st.form_submit_button('校验并冻结本轮计划',type='primary')
    if submitted:
        try:
            draft=json.loads(contract);draft['hypothesis']=hypothesis
            with st.spinner('正在校验测量与数据可行性…'):root=register_plan(draft,profile.market_id)
            st.success('计划已冻结。到“研究任务”选择该计划，预览后启动 API。')
            st.session_state['api_registered_'+profile.market_id]=str(root)
        except (ValueError,OSError) as error:st.error(str(error))


@st.fragment(run_every='5s')
def progress(root):
    path=root/'api/status.json'
    if not path.exists():
        st.info('任务已派发，正在准备。')
        with st.expander('启动日志'):
            log=root/'api-worker.log'
            st.text(log.read_text()[-2000:] if log.exists() else '等待后台进程')
        return
    state=json.loads(path.read_text())
    st.subheader(state['stage'])
    st.progress(state['submitted']/state['total'],text=f"已校验 {state['submitted']} / {state['total']} 个候选")
    cols=st.columns(3)
    cols[0].metric('模型响应',state['model_responses'] if state['model_responses'] is not None else '未知')
    cols[1].metric('输出 Token',state['output_tokens'] if state['output_tokens'] is not None else '未知')
    cols[2].metric('已用时间',f"{state['elapsed_seconds']:.0f} 秒")
    if state.get('message'): st.warning(state['message'])
    if state['status']=='completed':st.success('本轮研究已完成，在“研究结果”查看诊断。')
    elif state['status']=='running' and not api_running(root):
        st.error('后台进程已中断。调用与试验记录保留，请检查任务；不会自动重复发送 API。')
    elif state['status']=='running':
        st.caption('任务在后台执行，关闭页面不会中断。停止请求在阶段边界生效。')
        if not (root/'api/stop.json').exists() and st.button('停止后续阶段',key='stop_'+root.name):
            from factor_miner.research_report import write_json
            write_json(root/'api/stop.json',{'requested_by':'research_owner'})
            st.info('停止请求已记录。')


def render_api_workbench():
    st.title('挖因子')
    st.caption('论文与假设 → 冻结测量 → API 生成公式 → 统计与回测 → 研究结果')
    profile=profile_by_id(current_market_id())
    if profile is None:st.info('请先配置研究市场。');return
    st.write(f'**{profile.display_name}研究空间** · DeepSeek API')
    if not key_available():st.info('API 尚未配置。可先准备研究计划，再到“设置”填写 API Key。')
    tabs=st.tabs(['研究任务','新建研究','研究结果'])
    with tabs[1]:new_plan(profile)
    with tabs[0]:
        from dashboard.favor_page import available_runs
        try:
            roots=list(dict.fromkeys(r for base in run_roots(profile) for r in available_runs(base,profile.market_id)))
            if not roots:st.info('当前市场还没有登记研究。到“新建研究”导入并冻结计划。')
            else:
                root=st.selectbox('选择研究任务',roots,format_func=lambda p:p.name,key='api_run_'+profile.market_id)
                plan=load_plan(root)
                st.markdown('#### '+plan.hypothesis.claim)
                st.caption(f'{len(plan.slots)} 个候选名额 · 持有 {plan.execution.holding_sessions} 个交易日 · 双边 {plan.execution.round_trip_cost_bps:g} bps')
                with st.expander('论文、假设与冻结规则'):
                    st.write(plan.hypothesis.mechanism)
                    st.write('来源：'+'；'.join(plan.hypothesis.source_refs))
                    st.dataframe([{'测量':c.observation.measurement,'预期方向':c.expected_return_sign} for c in plan.conditions],hide_index=True,width='stretch')
                    st.write('数据分区：'+str(plan.splits.discovery_start)+' 至 '+str(plan.splits.test_end))
                if (root/'api-dispatch.json').exists() or (root/'api/status.json').exists():progress(root)
                elif (root/'summary.json').exists():st.success('研究已完成，可在“研究结果”查看。')
                elif (root/'evaluation_started.json').exists():st.info('研究已封存，保留原任务与运行记录。')
                else:
                    try:preview=api_preflight(root,profile.market_id)
                    except (ValueError,FactorMinerError) as error:st.warning(str(error))
                    else:
                        st.write(f"本轮最多 {preview['resource_limits']['model_responses']} 次模型响应，时间上限 {preview['resource_limits']['wall_seconds']} 秒。")
                        st.caption('点击启动即授权将本轮论文、假设与测量合同发送至 DeepSeek；不发送行情、个股样本或结果。完成后按已冻结协议自动诊断。')
                        if st.button('调用 API 并开始研究',type='primary',disabled=not key_available()):
                            start_api(root,profile.market_id,preview['plan_sha256']);st.rerun()
        except (ValueError,OSError,KeyError,FactorMinerError) as error:st.error('研究任务无法读取：'+str(error))
    with tabs[2]:
        from dashboard.favor_page import render_favor
        render_favor(st)
