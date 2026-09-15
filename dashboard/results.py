"""从当前市场正式回测文件展示精简结果，不依赖数据库。"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import plotly.graph_objects as go
import pandas as pd
import streamlit as st

from dashboard.market_profiles import current_market_id, profile_by_id
from dashboard.real_backtest import load_real_backtest_catalog
from dashboard.ui import apply_theme, number, pct
from dashboard.backtest_scenario import scenario_rows, calculate_scenario_metrics


def matches_result_kind(report: dict[str, Any], kind: str) -> bool:
    """淘汰只影响默认展示，不删除报告或缩小统计检验族。"""
    review = report.get("library_review") or {}
    if kind == "全部":
        return True
    if kind == "组合研究池":
        return (report.get('research_pool') or {}).get('status') == 'research_input'
    if kind == "恢复研究":
        pool = report.get('research_pool') or {}
        return pool.get('status') == 'research_input' and pool.get('previous_status') == 'rejected'
    if kind == "在研因子":
        return review.get("status") in {"retained", "reserve", "watch"}
    if kind == "淘汰档案":
        return review.get("status") == "rejected"
    if kind in {"保留基底", "观察池", "同类替补"}:
        return review.get("label") == kind
    if kind == "统计备选":
        return bool((report.get("screening") or review).get("statistical_pass"))
    return str(report["factor_id"]).startswith("strategy_") == (kind == "组合策略")


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """只缓存图表与研究摘要，逐证券审计继续留在原始产物。"""

    keys = (
        "factor_id", "factor_name", "market_id", "window", "scope", "mode",
        "evaluation", "direction", "reference_metrics", "zero_recovery_metrics",
        "daily_rows", "zero_recovery_daily_rows", "inference", "protocol",
        "report_batch", "source_candidate_id",
        "screening", "batch_summary",
    )
    result = {key: report.get(key) for key in keys}
    result["pending_count"] = len(report.get("unresolved_positions", []))
    result["pending_positions"] = report.get("unresolved_positions", [])
    result["benchmark_pending_count"] = len(report.get("benchmark_unresolved_positions", []))
    result["quality_status"] = report.get("data_quality_audit", {}).get("status")
    construct = report.get("construct_validation", {})
    result["observation"] = construct.get("condition", {}).get("observation", "")
    result["construct_status"] = construct.get("status", "尚未检验")
    return result


@st.cache_data(ttl=120, max_entries=4, show_spinner=False)
def _catalog(market_id: str, backtest_path: str | None, roots: tuple[str, ...]) -> list[dict[str, Any]]:
    """配置和市场共同参与缓存键，避免串用结果。"""

    from pathlib import Path

    reports = load_real_backtest_catalog(
        backtest_path=Path(backtest_path) if backtest_path else None,
        backtest_roots=tuple(Path(root) for root in roots),
    )
    if any(report["market_id"] != market_id for report in reports):
        raise ValueError("回测报告的市场与当前选择不一致，请检查设置。")
    return [compact_report(report) for report in reports]


def reports_for_market(market_id: str) -> list[dict[str, Any]]:
    """只访问当前市场显式配置的结果目录。"""

    profile = profile_by_id(market_id)
    if profile is None or not (profile.backtest_path or profile.backtest_roots):
        return []
    reports = _catalog(market_id, str(profile.backtest_path) if profile.backtest_path else None,
                       tuple(str(root) for root in profile.backtest_roots))
    if profile.library_review_path:
        from dashboard.library_review import apply_library_review
        reports = apply_library_review(reports, profile.library_review_path, market_id)
    if profile.research_pool_path:
        from dashboard.library_review import apply_research_pool
        reports = apply_research_pool(reports, profile.research_pool_path, market_id)
    if profile.regime_manifest_path:
        from factor_miner.regime import attach_records, load_manifest
        manifest = load_manifest(profile.regime_manifest_path, market_id)
        if profile.regime_context_id and profile.regime_context_id != manifest.context_id:
            raise ValueError('文件与 PostgreSQL 状态上下文配置不一致')
        reports = attach_records(reports, manifest)
    return reports


def actual_performance(report: dict[str, Any]) -> dict[str, Any]:
    """持仓价值未确定时实际表现保持缺失，参考估值不能填入实际指标。"""

    if report.get("pending_count"):
        return {}
    result = dict(report.get("reference_metrics") or {})
    if report.get("benchmark_pending_count"):
        result["information_ratio"] = None
    return result


def result_row(report: dict[str, Any]) -> dict[str, Any]:
    """固定首页指标顺序，相关系数保留原始小数。"""

    evaluation = report["evaluation"]
    performance = report.get("reference_metrics") or {}
    row = {
        "因子": report["factor_id"], "研究内容": (report.get("library_review") or {}).get("display_name") or report.get("observation") or report["factor_name"],
        "IC": evaluation.get("ic_mean"), "RankIC": evaluation.get("rank_ic_mean"),
        "t 值": evaluation.get("rank_ic_hac_t"),
        "年化收益": performance.get("annualized_return"), "Sharpe": performance.get("sharpe"),
        "最大回撤": performance.get("max_drawdown"),
        "筛选状态": "数据待核查" if report.get("quality_status") == "review_required" else (report.get("library_review") or {}).get("label") or (report.get("screening") or {}).get("label") or (
            "参考估值" if report.get("pending_count") else "已出诊断"),
    }
    from factor_miner.regime import record_fields
    fields = {**record_fields(None), **report}
    row['预期适用状态'] = fields['expected_regime_label']
    row['状态检验'] = fields['regime_validation_status']
    if report.get('research_pool'):
        row['组合研究'] = {'research_input':'研究代表', 'redundant':'同类替补', 'excluded':'暂不纳入'}[report['research_pool']['status']]
        if report['research_pool'].get('model_diagnostic_status'):
            row['组合贡献'] = report['research_pool']['model_diagnostic_status']
    leading = ('因子','研究内容','预期适用状态','状态检验')
    return {**{k:row[k] for k in leading}, **{k:v for k,v in row.items() if k not in leading}}


def wealth_figure(rows: list[dict[str, Any]], *, drawdown: bool = False) -> go.Figure:
    """从原始逐期收益计算净值或回撤，缺失值拒绝伪造。"""

    figure = go.Figure()
    for field, label, color in (("target_long_net_return", "因子组合", "#c7f36b"),
                                 ("benchmark_return", "同池等权基准", "#789d93")):
        wealth = peak = 1.0
        values = []
        for row in rows:
            value = row.get(field)
            if not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("收益序列不完整，不能绘图。")
            wealth *= 1 + value
            peak = max(peak, wealth)
            values.append(wealth / peak - 1 if drawdown else wealth)
        figure.add_trace(go.Scatter(x=[row["exit_date"] for row in rows], y=values,
                                   name=label, mode="lines", line={"color": color, "width": 2},
                                   fill="tozeroy" if drawdown and field.startswith("target") else None))
    figure.update_layout(template="plotly_dark", height=290 if drawdown else 380,
                          margin={"l": 10, "r": 10, "t": 35, "b": 10},
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          legend={"orientation": "h", "y": 1.14}, hovermode="x unified")
    figure.update_yaxes(title="回撤" if drawdown else "净值（起点为 1）",
                       tickformat=".0%" if drawdown else ".2f", gridcolor="#26342c")
    return figure


def render_results() -> None:
    """搜索候选、查看关键指标与图表。"""

    apply_theme(st, page_title="Factor Miner｜看结果")
    market_id = current_market_id()
    profile = profile_by_id(market_id)
    st.title("看结果")
    from dashboard.favor_cost_page import render_cost_candidates
    from dashboard.favor_page import render_favor
    library_label = "已审核因子库" if profile and profile.joint_library_path else "研究候选库"
    choices = ([library_label] if profile and profile.cost_review_path else []) + ["既有因子与策略", "FaVOR 联合策略"]
    mode = st.radio("查看内容", choices, horizontal=True)
    if mode == library_label:
        render_cost_candidates(st, profile)
        return
    if mode == "FaVOR 联合策略":
        render_favor(st)
        return
    st.caption(f"{profile.display_name if profile else '当前市场'} · 先看信号，再看收益与风险")
    try:
        with st.spinner("正在读取研究结果…"):
            reports = reports_for_market(market_id)
    except (ValueError, OSError, KeyError) as error:
        st.error("结果暂时无法读取。请在设置中检查结果目录。")
        with st.expander("查看具体原因"):
            st.text(str(error))
        return
    if not reports:
        st.info("还没有可展示的回测结果。可以先到“挖因子”创建研究批次。")
        st.page_link("pages/7_研究运行台.py", label="去挖因子", icon=":material/science:")
        return
    batches = list(dict.fromkeys(item.get("report_batch") for item in reports))
    if len(batches) > 1:
        labels = {}
        for index, batch in enumerate(batches):
            members = [str(item['factor_id']) for item in reports if item.get('report_batch') == batch and not str(item['factor_id']).startswith('strategy_')]
            labels[batch] = f"{'最新批次' if index == len(batches)-1 else '历史批次'} · {len(members)} 个因子" + (f"（{members[0]}–{members[-1]}）" if members else '')
        batch = st.selectbox("研究批次", [*reversed(batches), "all"],
            index=len(batches) if any(item.get('library_review') for item in reports) else 0,
            format_func=lambda value: "全部批次" if value == "all" else labels[value], key=f"result_batch_{market_id}")
        if batch != "all":
            reports = [item for item in reports if item.get('report_batch') == batch]
    controls = st.columns([3, 1, 1])
    query = controls[0].text_input("搜索因子", placeholder="输入编号、名称或研究内容")
    screened = any(item.get('screening') for item in reports)
    reviewed = any(item.get('library_review') for item in reports)
    if reviewed:
        for message in dict.fromkeys(item['library_summary'] for item in reports if item.get('library_summary')):
            st.caption(message)
    kinds = (["在研因子", "保留基底", "观察池", "同类替补", "淘汰档案"] if reviewed else []) + (["统计备选"] if screened else []) + ["因子", "组合策略", "全部"]
    default_view = next((item.get('library_default_view') for item in reports if item.get('library_default_view')), None)
    if any(item.get('research_pool') for item in reports):
        kinds = ['组合研究池', '恢复研究', *kinds]
        default_view = '组合研究池'
        for message in dict.fromkeys(item['research_pool_summary'] for item in reports if item.get('research_pool_summary')):
            st.caption(message)
    if reviewed and default_view in (None, "因子", "全部"):
        default_view = "在研因子"
    kind = controls[1].selectbox("查看", kinds, index=kinds.index(default_view) if default_view in kinds else 0)
    if screened and kind not in ("保留基底", "观察池", "同类替补"):
        messages = list(dict.fromkeys(item.get('batch_summary') for item in reports if item.get('batch_summary') and item.get('screening')))
        for message in messages:
            st.caption(message)
    regime_controls = st.columns(3)
    regime_labels = regime_controls[0].multiselect('预期适用状态', sorted({r.get('expected_regime_label','未登记') for r in reports}))
    regime_statuses = regime_controls[1].multiselect('状态检验', sorted({r.get('regime_validation_status','未开展') for r in reports}))
    regime_directions = regime_controls[2].multiselect('状态方向', sorted({r.get('regime_direction','未登记') for r in reports}))
    reports = [r for r in reports if (not regime_labels or r.get('expected_regime_label','未登记') in regime_labels)
               and (not regime_statuses or r.get('regime_validation_status','未开展') in regime_statuses)
               and (not regime_directions or r.get('regime_direction','未登记') in regime_directions)]
    sort = controls[2].selectbox("排序", ["编号", "RankIC", "t 值", "Sharpe"])
    filtered = [item for item in reports if query.casefold() in (
        str(item["factor_id"]) + str(item["factor_name"]) + str(item["observation"]) + str(item.get("expected_regime_label",""))).casefold()
        and matches_result_kind(item, kind)]
    if sort != "编号":
        filtered.sort(key=lambda item: result_row(item).get(sort) if isinstance(
            result_row(item).get(sort), (int, float)) else -math.inf, reverse=True)
    if not filtered:
        st.info("没有匹配的结果，试试缩短搜索词或切换“查看”。")
        return
    st.caption(f"{len(filtered)} 个结果 · 收益指标采用最后报价参考估值 · 点击一行查看详情")
    numeric_columns = ("IC", "RankIC", "t 值", "年化收益", "Sharpe", "最大回撤")
    table = pd.DataFrame([result_row(item) for item in filtered]).astype({key: float for key in numeric_columns})
    styled_table = table.style.format({"IC": "{:.4f}", "RankIC": "{:.4f}", "t 值": "{:.2f}",
                                       "年化收益": "{:.2%}", "Sharpe": "{:.2f}", "最大回撤": "{:.2%}"}, na_rep="—")
    event = st.dataframe(styled_table, hide_index=True,
        width="stretch", height=min(250, 38 + 35 * len(filtered)), on_select="rerun",
        selection_mode="single-row", key=f"results_table_{market_id}_{query}_{kind}_{sort}",
        column_config={"研究内容": st.column_config.TextColumn(width="medium"),
                       **{key: st.column_config.NumberColumn(width=105, format="%.4f") for key in ("IC", "RankIC")},
                       **{key: st.column_config.NumberColumn(width=90, format="%.2f") for key in ("t 值", "Sharpe")},
                       **{key: st.column_config.NumberColumn(width=105) for key in ("年化收益", "最大回撤")}})
    from dashboard.factor_explorer import factor_rows_csv
    st.download_button('下载当前列表', factor_rows_csv([{**result_row(r),
        '假设来源':r.get('regime_origin',''),'状态检验区间':r.get('regime_period',''),
        '状态方向':r.get('regime_direction',''),'可能失效情形':r.get('failure_condition_summary','')} for r in filtered]),
        file_name='factor_regimes.csv',mime='text/csv')
    choices = [item["factor_id"] for item in filtered]
    selection_key = f"result_factor_{market_id}"
    chosen_rows = event.selection.rows
    table_selection_key = f"selected_row_{market_id}"
    selection_identity = (query, kind, sort, tuple(chosen_rows))
    if chosen_rows and chosen_rows[0] < len(filtered) and st.session_state.get(table_selection_key) != selection_identity:
        st.session_state[selection_key] = choices[chosen_rows[0]]
    st.session_state[table_selection_key] = selection_identity
    if st.session_state.get(selection_key) not in choices:
        st.session_state[selection_key] = next((item['factor_id'] for item in filtered if (item.get('library_review') or {}).get('status') == 'retained'), choices[0])
    selected = st.selectbox("当前因子", choices, key=selection_key,
        format_func=lambda value: next(f"{item['factor_id']} · {(item.get('library_review') or {}).get('display_name') or item['factor_name']}" for item in filtered if item["factor_id"] == value))
    report = next(item for item in filtered if item["factor_id"] == selected)
    st.subheader((report.get("library_review") or {}).get("display_name") or report["observation"] or report["factor_name"])
    from dashboard.regime import render_regime
    render_regime(st, report.get("regime_record"))
    review = report.get("library_review")
    if review:
        st.caption(f"复核结论：{review['label']} · " + ("；".join(review['reasons']) or "可作为后续研究输入；经济机制尚未独立验证"))
    pool = report.get('research_pool')
    if pool:
        st.caption('组合研究资格：' + '；'.join(pool['reasons']))
        contribution = pool.get('joint_ablation')
        if contribution:
            st.caption(f"联合模型保留此因子的 RankIC 增量：{contribution['joint_minus_reference_rank_ic']:+.5f}；校正 p={contribution['bonferroni_p']:.4g}。")
    if report.get("direction") in ("positive", "negative"):
        st.caption("使用方向：因子数值越大，选股排名越靠前。" if report["direction"] == "positive" else "使用方向：因子数值越小，选股排名越靠前；负 RankIC 与这个方向一致。")
    window = report["window"]
    st.caption(f"信号区间：{window.get('start', '—')} 至 {window.get('end', '—')} · {report['scope']}")
    if report.get("quality_status") == "review_required":
        st.error("行情中存在待核查的价格断点，当前指标和曲线只能用于诊断。")
    view = st.radio("收益口径", ["最后报价参考估值", "零回收压力情景"], horizontal=True,
                    key=f"result_valuation_{market_id}") if report.get("zero_recovery_daily_rows") else "已发布收益"
    rows = report["zero_recovery_daily_rows"] if view == "零回收压力情景" else report["daily_rows"]
    dates = [date.fromisoformat(str(row["exit_date"])[:10]) for row in rows]
    controls = st.columns([2, 1, 1])
    interval = controls[0].date_input("估值日期区间", value=(min(dates), max(dates)),
        min_value=min(dates), max_value=max(dates), key=f"result_dates_{market_id}_{selected}")
    base_cost = float((report.get("protocol") or {}).get("base_cost_bps", 10))
    cost = controls[1].number_input("往返成本（bps）", min_value=0.0, max_value=200.0,
        value=base_cost, step=1.0, key=f"result_cost_{market_id}_{selected}")
    slippage = controls[2].number_input("额外往返滑点（bps）", min_value=0.0, max_value=200.0,
        value=0.0, step=1.0, key=f"result_slippage_{market_id}_{selected}")
    st.caption(f"{view} · 1 bps = 0.01%。区间按估值日截取；成本和滑点只调整既有成交路径，不重新选股。")
    if not isinstance(interval, (tuple, list)) or len(interval) != 2:
        st.info("请选择完整的开始和结束日期。")
        return
    try:
        adjusted = scenario_rows(rows, start_date=interval[0], end_date=interval[1], cost_bps=cost, slippage_bps=slippage)
        metrics = calculate_scenario_metrics(adjusted)
    except ValueError as error:
        st.info(str(error))
        return
    summary = result_row(report)
    summary.update({"年化收益": metrics.annualized_return, "Sharpe": metrics.sharpe, "最大回撤": metrics.max_drawdown})
    cards = st.columns(6)
    for column, key in zip(cards, ("IC", "RankIC", "t 值", "年化收益", "Sharpe", "最大回撤"), strict=True):
        column.metric(key, pct(summary[key]) if key in ("年化收益", "最大回撤") else number(summary[key], 4 if key in ("IC", "RankIC") else 2))
    st.caption("收益卡片和图表随上方设置变化；IC、RankIC、t 值始终对应完整的冻结评价区间。")
    rows = [{**row, "target_long_net_return": row["scenario_net_return"]} for row in adjusted]
    left, right = st.columns([1.5, 1])
    with left:
        st.markdown("#### 组合净值")
        st.plotly_chart(wealth_figure(rows), width="stretch", config={"displaylogo": False})
    with right:
        st.markdown("#### 回撤")
        st.plotly_chart(wealth_figure(rows, drawdown=True), width="stretch", config={"displaylogo": False})
    with st.expander("更多指标与研究说明"):
        if review:
            if review.get("formula"):
                st.write(f"公式：{review['formula']}")
            if review.get("discovery"):
                st.write(f"发现期 RankIC：{number(review['discovery']['rank_ic_mean'], 4)}；确认期 RankIC：{number(report['evaluation'].get('rank_ic_mean'), 4)}。")
            st.write("这是已使用区间的事后复核；保留代表不等于通过了独立经济机制检验。")
            if review.get("semantic_review"):
                st.write(review["semantic_review"])
            for warning in review.get("economic_warnings", []):
                st.write(warning)
            if review.get("incremental"):
                incremental = review["incremental"]
                st.write(f"相对固定研究基底的 RankIC 增量：{number(incremental.get('incremental_rank_ic'), 5)}；全库校正 p 值：{number(incremental.get('bonferroni_p'), 4)}。")
        evaluation = report["evaluation"]
        st.write(f"有效日期：{evaluation.get('valid_dates', '—')}；覆盖率：{pct(evaluation.get('median_coverage'))}。")
        st.write(f"公式基础检验：{report['construct_status']}。经济机制尚未独立验证。")
        st.write(f"多重检验校正后 p 值：{number(evaluation.get('bonferroni_p_value'), 4)}。")
        screening = report.get('screening') or {}
        if screening:
            st.write(f"筛选：{screening.get('label')}。")
            for reason in screening.get('reasons', []):
                st.write(reason)
            incremental = screening.get('incremental') or {}
            if incremental:
                st.write(f"加入原因子基底后，RankIC 变化 {number(incremental.get('incremental_rank_ic'), 5)}；校正后 p 值 {number(incremental.get('bonferroni_p'), 4)}。")
        st.write(f"所选区间收益：{pct(metrics.period_return)}；信息比率：{number(metrics.information_ratio)}。")
        st.write("最后报价参考估值不保证可成交；零回收压力情景在报告末日扣除全部未确定持仓价值。两者均不替代已确定的实际回收收益。")
        for label, key in (("交易规则", "execution"), ("基准", "benchmark"), ("股息口径", "dividend_treatment")):
            value = (report.get("protocol") or {}).get(key)
            if isinstance(value, str):
                st.write(f"{label}：{value}")
    if report["pending_count"]:
        with st.expander(f"估值说明与待结算持仓（{report['pending_count']} 笔）"):
            st.write("实际回收金额尚未确定，主页已按最后报价展示参考表现。无需继续修补行情即可查看和比较这些诊断结果。")
            st.caption("金额以初始净值为 1 的归一化资金单位展示；最后报价不等于可回收金额。")
            st.dataframe([{
                "证券": item.get("security_id"), "买入日": item.get("entry_date"),
                "计划退出日": item.get("scheduled_exit_date"), "最后报价日": item.get("last_observed_date"),
                "最后报价": item.get("last_observed_price"), "参考价值": item.get("reference_mark_value"),
                "原因": item.get("reason"),
            } for item in report["pending_positions"]], hide_index=True, width="stretch")
