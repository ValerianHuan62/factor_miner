"""两个市场共用的 FaVOR 研究与结果页面，默认只显示假设、关键指标和图。"""
from __future__ import annotations

import json
from pathlib import Path

from dashboard.market_profiles import current_market_id, profile_by_id


def available_runs(base: Path, market_id: str) -> list[Path]:
    """只读配置产物根下的明确 FaVOR 目录，不扫描整个磁盘。"""
    result = []
    for registration in sorted((base/"favor").glob("*/registration.json"), reverse=True):
        root = registration.parent
        plan = json.loads((root/"plan.json").read_text())
        if plan["market_id"] == market_id:
            result.append(root)
    return result


def selectivity_rows(report: dict) -> list[dict]:
    """按事件数汇总三档统计；不把构念统计当成回测收益。"""
    totals = {}
    for ticker in report["selectivity"]["ticker_reports"]:
        for item in ticker["ladder"]:
            row = item["statistics"]
            entry = totals.setdefault(item["level"], dict(events=0, returns=0., wins=0, losses=0))
            if row:
                entry["events"] += row["events"]
                entry["returns"] += row["events"]*row["mean_return"]
                entry["wins"] += row["wins"]
                entry["losses"] += row["losses"]
    return [{"阈值": f"q{round(level*100)}", "有效触发次数": row["events"],
             "平均标签收益": row["returns"]/row["events"] if row["events"] else None,
             "胜率": row["wins"]/(row["wins"]+row["losses"]) if row["wins"]+row["losses"] else None}
            for level,row in sorted(totals.items())]


def render_favor(st, *, workbench=False):
    """展示同一冻结协议的条件、三档阈值和净值，并可运行已提交的候选。"""
    import plotly.graph_objects as go
    from factor_miner.favor_workflow import load_plan, run_favor
    profile = profile_by_id(current_market_id())
    st.subheader("假设研究与 FaVOR 联合策略")
    st.caption("论文与假设 → 冻结测量合同 → 类型化探索或已登记的联合验证 → 因果诊断")
    if profile is None:
        st.info("请先配置研究市场。")
        return
    try:
        from dashboard.api_connection import run_roots
        roots = list(dict.fromkeys(r for base in run_roots(profile) for r in available_runs(base, profile.market_id)))
    except (OSError, ValueError, KeyError) as error:
        st.error(f"FaVOR 研究目录无法读取：{error}")
        return
    if not roots:
        st.info("当前市场还没有登记 FaVOR 研究。先冻结假设与研究计划，再生成各观察条件的候选公式。")
        st.caption("A 股 DeepSeek 和美股 GPT‑6 使用相同流程；历史因子不会自动标记为通过新协议。")
        return
    root = st.selectbox("研究", roots, format_func=lambda p:p.name, key=f"favor_run_{workbench}_{profile.market_id}")
    try:
        plan = load_plan(root)
        if plan.run_kind == "synthetic_demo":
            st.warning("合成演示：用于验证系统功能，不是真实市场因子或投资结果。")
        st.write(plan.hypothesis.claim)
        from factor_miner.favor_schema import FavorRegimePlan
        if isinstance(plan,FavorRegimePlan):
            st.dataframe([{'观察条件':c.condition_id,'预期适用状态':plan.regimes[c.condition_id].label,
                '状态检验':'未开展' if not plan.regimes[c.condition_id].has_claim else '待检验' if plan.regimes[c.condition_id].data_available else '待数据'}
                for c in plan.conditions],hide_index=True,width='stretch')
            with st.expander('完整状态主张与证伪办法'):
                for condition_id, spec in plan.regimes.items():
                    st.write(condition_id+'：'+spec.claim)
                    st.write('可能失效：'+spec.failure_condition)
                    st.write('证伪：'+spec.falsification)
        else:
            st.caption('预期适用状态：未登记 · 历史计划保留原记录')
        with st.expander("经济依据与失效条件"):
            st.write(plan.hypothesis.mechanism)
            st.write("竞争解释："+"；".join(plan.hypothesis.competing_explanations))
            st.write("失效条件："+"；".join(plan.hypothesis.failure_modes))
            st.write("独立验证："+plan.hypothesis.independent_verification)
        st.dataframe([{"观察条件": c.observation.observation, "测量":c.observation.measurement,
            "触发方向":"高值" if c.activation_direction=="high" else "低值"} for c in plan.conditions], hide_index=True, width="stretch")
        if not (root/"summary.json").exists():
            submitted = len(list((root/"submissions").glob("*/receipt.json")))
            st.write(f"已登记 {len(plan.slots)} 个候选名额，已提交 {submitted} 个。")
            if (root/"failure.json").exists():
                st.error(json.loads((root/"failure.json").read_text())["reason"])
            elif (root/"evaluation_started.json").exists():
                st.info("研究已启动；完成后刷新查看。中断运行保留记录，不覆盖重跑。")
            elif workbench and submitted:
                st.caption("开始后即封存当前提交，未提交槽仍保留记录并计入预留预算。")
                exploring = plan.version == 'favor-exploration-v1'
                if st.button("封存候选并运行探索诊断" if exploring else "封存候选并运行完整验证", type="primary"):
                    with st.spinner("正在执行类型化测量和固定方案诊断…" if exploring else "正在执行构念、联合阈值和因果回测…"):
                        run_favor(root)
                    st.rerun()
            return
        summary = json.loads((root/"summary.json").read_text())
        if summary['version'] == 'favor-exploration-v1':
            st.info('这是探索结果：尚未进行测试期确认或组合增量验证，不计入正式保留因子。')
            if plan.search_budget.get('historical_budget_status') == 'unrecoverable':
                st.caption('历史试验预算已丢失：展示测量与收益诊断，完整多重检验校正和统计通过状态不可确定。')
            columns = st.columns(3)
            for col, label, key in zip(columns, ('已提交','可继续探索','正式保留'), ('submitted','exploration_eligible','retained_components')):
                col.metric(label, summary[key])
            names = {c.condition_id:c.observation.measurement for c in plan.conditions}
            st.dataframe([{'测量':names[r['condition_id']], '状态':r.get('reason',r['status']),
                'ic_mean':r.get('discovery',{}).get('ic_mean'),
                'RankIC':r.get('discovery',{}).get('rank_ic_mean'),
                '单因子统计通过':r.get('standalone_statistical_pass')} for r in summary['factors'].values()],
                hide_index=True, width='stretch', column_config={
                    'ic_mean':st.column_config.NumberColumn('IC 均值（ic_mean）',format='%.6f'),
                    'RankIC':st.column_config.NumberColumn(format='%.6f')})
            st.caption('IC 均值为逐日截面 Pearson IC 的均值；RankIC 为逐日截面 Spearman IC 的均值。未评价或不适用时保留空值。')
            evaluated = [t for t,r in summary['factors'].items() if r['status']=='exploration_evaluated']
            if evaluated:
                trial = st.selectbox('探索信号', evaluated, format_func=lambda t:names[summary['factors'][t]['condition_id']])
                outcomes = json.loads((root/'factors'/trial/'validation_diagnostics.json').read_text())
                st.caption('事前固定激活方案的验证期成本诊断；没有用测试期选优，也没有自动翻转方向。')
                st.dataframe([{'双边成本（基点）':x['cost_bps'],
                    '年化收益':(x['metrics']['actual'] or {}).get('annualized_return'),
                    '最大回撤':(x['metrics']['actual'] or {}).get('max_drawdown'),
                    '未确定持仓':len(x['unresolved_positions'])} for x in outcomes], hide_index=True,width='stretch')
            return
        columns = st.columns(4)
        for column,label,key in zip(columns,("累计提交","构念通过","保留因子","保留组合"),
                                    ("submitted","construct_passed","retained_components","retained_combinations")):
            column.metric(label,summary[key])
        if summary["test_consumed"]:
            st.caption("这是已消费样本上的探索性结果；训练与阈值选择按时间分离。")
        combinations = summary["combinations"]
        if combinations:
            cid = st.selectbox("联合假设组合",list(combinations), format_func=lambda c:" + ".join(
                next(x.observation.measurement for x in plan.conditions if x.condition_id==summary["factors"][t]["condition_id"])
                for t in combinations[c]["members"]))
            report = combinations[cid]
            st.write(f"方向选择性支持比例：{report['selectivity']['ticker_support_rate']:.1%}")
            st.dataframe(selectivity_rows(report),hide_index=True,width="stretch",column_config={
                "平均标签收益":st.column_config.NumberColumn(format="%.4f"),"胜率":st.column_config.NumberColumn(format="%.4f")})
            st.caption("上表是发现期结构筛选，尚未扣交易成本；每个条件必须同时触发。")
            if report["status"] == "retained_combination":
                st.write("冻结的逐条件阈值："+" / ".join(f"q{q*100:g}" for q in report["thresholds"]))
                outcomes=json.loads((root/"strategies"/cid/"backtest.json").read_text())
                cost=st.selectbox("双边成本（基点）",[x["cost_bps"] for x in outcomes])
                result=next(x for x in outcomes if x["cost_bps"]==cost)
                metrics=result["metrics"]["actual"]
                if metrics is None:
                    st.info("存在终值未确定的持仓。下图为最后报价参考净值，不能视为实际回收收益。")
                else:
                    cols=st.columns(3)
                    cols[0].metric("年化收益",f"{metrics['annualized_return']:.2%}")
                    cols[1].metric("最大回撤",f"{metrics['max_drawdown']:.2%}")
                    cols[2].metric("Sharpe",f"{metrics['sharpe']:.3f}")
                chart=go.Figure()
                for key,label in (("daily_returns","组合净值" if metrics is not None else "最后报价参考"),
                                  ("zero_recovery_daily_returns","零回收压力")):
                    wealth=1.; values=[]
                    for row in result[key]:
                        wealth*=1+row["target_long_net_return"];values.append(wealth)
                    chart.add_trace(go.Scatter(x=[r["exit_date"] for r in result[key]],y=values,name=label))
                st.plotly_chart(chart,width="stretch")
            else:
                st.info("该组合未通过方向选择性或没有有效的验证期阈值，未进入测试期选优。")
        archived=st.checkbox("查看未保留候选及原因",value=False)
        names={c.condition_id:c.observation.measurement for c in plan.conditions}
        st.dataframe([{"测量":names[r["condition_id"]],"结果":"保留为组合组成" if r["status"]=="retained_component" else r.get("reason",r["status"]),
                       "单因子统计通过":r.get("standalone_statistical_pass",False),"ic_mean":r.get("discovery",{}).get("ic_mean"),"RankIC":r.get("discovery",{}).get("rank_ic_mean")}
                      for r in summary["factors"].values() if archived or r["status"]=="retained_component"],hide_index=True,width="stretch",
                     column_config={"ic_mean":st.column_config.NumberColumn('IC 均值（ic_mean）',format="%.6f"),
                                    "RankIC":st.column_config.NumberColumn(format="%.6f")})
    except (ValueError,OSError,KeyError) as error:
        st.error(f"FaVOR 研究未完成：{error}")
