"""Streamlit 只读研究驾驶舱入口。"""

from __future__ import annotations

from factor_miner.errors import FactorMinerError
from dashboard.read import load_server_snapshot
from dashboard.ui import (
    apply_theme,
    candidate_payload,
    candidate_ids,
    candidate_name,
    candidate_source_id,
    daily_rows,
    direction_values,
    hero,
    ic_values,
    make_ic_decay_figure,
    make_ic_sequence_figure,
    make_wealth_figure,
    metric_card,
    number,
    pct,
    portfolio_values,
    recent_portfolio_values,
    section,
)


def main() -> None:
    """读取服务器正式产物并展示，不提供研究写操作。"""

    try:
        import streamlit as st  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("运行 Dashboard 前请在服务器环境安装 Streamlit") from error

    apply_theme(st, page_title="Factor Miner｜研究驾驶舱")
    try:
        snapshot = load_server_snapshot()
    except FactorMinerError as error:
        hero(st, kicker="FACTOR MINER / READ MODEL", title="研究驾驶舱暂时不可用", copy=str(error))
        return

    ids = candidate_ids(snapshot)
    hero(
        st,
        kicker="FACTOR MINER / READ-ONLY RESEARCH COCKPIT",
        title="从候选信号，到可信可见验证",
        copy="这里展示的是已发布研究批次：先看方向、稳定性与组合曲线，再到审计页核对不可变产物。相关性不是因果，开发区间的通过也不是生产结论。",
        meta=f"运行 {snapshot.run_id}  ·  {len(ids)} 个候选  ·  图表来自正式发布产物",
    )

    if not ids:
        st.info("当前运行没有候选结果。")
        return

    first_id = ids[0]
    first_ic = ic_values(snapshot, first_id)
    first_portfolio = portfolio_values(snapshot, first_id)
    first_series = first_portfolio.get("series", {})
    long_short = first_series.get("target_long_net_return", {}) if isinstance(first_series, dict) else {}
    section(st, "运行摘要")
    cards = st.columns(4)
    with cards[0]:
        metric_card(st, "候选数量", str(len(ids)), "本次发布的完整候选集合")
    with cards[1]:
        metric_card(st, "主期限 RankIC", pct(first_ic.get("rank_ic_mean")), "冻结的 5 日标签")
    with cards[2]:
        metric_card(st, "目标多头 Sharpe", number(long_short.get("sharpe")), "扣费后净收益")
    with cards[3]:
        metric_card(st, "最大回撤", f"−{pct(long_short.get('max_drawdown'))}" if isinstance(long_short.get("max_drawdown"), (int, float)) else "—", "目标多头净值")

    section(st, "候选横向比较", "先比较，再钻取")
    comparison = []
    for candidate_id in ids:
        ic = ic_values(snapshot, candidate_id)
        portfolio = portfolio_values(snapshot, candidate_id)
        recent_portfolio = recent_portfolio_values(snapshot, candidate_id)
        direction = direction_values(snapshot, candidate_id)
        series = portfolio.get("series", {}) if isinstance(portfolio, dict) else {}
        spread = series.get("target_long_net_return", {}) if isinstance(series, dict) else {}
        recent_series = recent_portfolio.get("series", {}) if isinstance(recent_portfolio, dict) else {}
        recent_target = recent_series.get("target_long_net_return", {}) if isinstance(recent_series, dict) else {}
        selected_direction = {"positive": "正向", "negative": "负向"}.get(direction.get("selected_direction"), "—")
        relation = {"supported": "与事前假设一致", "reversed": "与事前假设相反"}.get(direction.get("hypothesis_relation"), "—")
        comparison.append({
            "候选": candidate_name(candidate_id),
            "发现方向": selected_direction,
            "假设关系": relation,
            "RankIC 均值": ic.get("rank_ic_mean") * 100 if isinstance(ic.get("rank_ic_mean"), (int, float)) else None,
            "HAC t 值": ic.get("rank_ic_hac_t"),
            "5 日有效日期": next((item.get("valid_dates") for item in ic.get("decay", []) if isinstance(item, dict) and item.get("horizon") == 5), None),
            "目标多头年化收益": spread.get("annualized_return") * 100 if isinstance(spread.get("annualized_return"), (int, float)) else None,
            "目标多头 Sharpe": spread.get("sharpe"),
            "目标多头最大回撤": spread.get("max_drawdown") * 100 if isinstance(spread.get("max_drawdown"), (int, float)) else None,
            "近期年化收益": recent_target.get("annualized_return") * 100 if isinstance(recent_target.get("annualized_return"), (int, float)) else None,
        })
    st.dataframe(
        comparison,
        width="stretch",
        hide_index=True,
        column_config={
            "RankIC 均值": st.column_config.NumberColumn(format="%.2f%%"),
            "HAC t 值": st.column_config.NumberColumn(format="%.2f"),
            "目标多头年化收益": st.column_config.NumberColumn(format="%.2f%%"),
            "目标多头 Sharpe": st.column_config.NumberColumn(format="%.2f"),
            "目标多头最大回撤": st.column_config.NumberColumn(format="%.2f%%"),
            "近期年化收益": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    selected = st.selectbox("查看候选详情", ids, format_func=candidate_name)
    selected_ic = ic_values(snapshot, selected)
    selected_portfolio = portfolio_values(snapshot, selected)
    selected_series = selected_portfolio.get("series", {})
    section(st, "候选详情", candidate_name(selected))
    detail_cards = st.columns(4)
    with detail_cards[0]:
        metric_card(st, "IC 均值", pct(selected_ic.get("ic_mean")), "主期限 5 日")
    with detail_cards[1]:
        metric_card(st, "RankIC IR", number(selected_ic.get("rank_ic_ir")), "均值 / 样本波动")
    with detail_cards[2]:
        metric_card(st, "HAC t 值", number(selected_ic.get("rank_ic_hac_t")), "处理时间依赖")
    with detail_cards[3]:
        metric_card(st, "观测日期", str(len(daily_rows(snapshot, selected))), "组合收益序列")

    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.markdown("#### IC 多期限衰减")
        st.plotly_chart(make_ic_decay_figure(snapshot, selected), width="stretch", config={"displaylogo": False})
    with chart_right:
        st.markdown("#### 目标多头累计收益")
        st.plotly_chart(make_wealth_figure(snapshot, selected), width="stretch", config={"displaylogo": False})
    st.markdown("#### 主期限每日 IC / RankIC")
    st.plotly_chart(make_ic_sequence_figure(snapshot, selected), width="stretch", config={"displaylogo": False})

    if snapshot.barra_attribution:
        barra = candidate_payload(snapshot.barra_attribution).get(candidate_source_id(snapshot, selected), {})
        if isinstance(barra, dict) and barra.get("status") not in (None, "not_available"):
            st.caption(f"Barra 状态：{barra.get('status')}")
        elif isinstance(barra, dict):
            st.info("Barra 暂未启用：当前页面保留可见状态，不把缺失数据解释成零暴露。")

    with st.expander("研究边界与数据血缘"):
        st.write("真实行情、标签、IC 和组合评价均在公司 Linux 服务器执行；页面从正式 JSON/JSONL/Parquet 产物读取图表，PostgreSQL 只提供简洁因子索引和指标。")
        st.write(f"产物数量：{len(snapshot.artifact_refs)}；清单哈希：{snapshot.artifact_manifest_sha256}")


if __name__ == "__main__":
    import streamlit as st  # type: ignore[import-not-found]

    navigation = st.navigation(
        {
            "研究": [
                st.Page(main, title="研究驾驶舱", icon=":material/dashboard:", default=True),
                st.Page("pages/1_批次总览.py", title="批次总览", icon=":material/view_list:"),
                st.Page("pages/6_假设工作台.py", title="假设工作台", icon=":material/lightbulb:"),
                st.Page("pages/7_研究运行台.py", title="研究运行台", icon=":material/science:"),
            ],
            "诊断": [
                st.Page("pages/2_IC诊断.py", title="IC 诊断", icon=":material/timeline:"),
                st.Page("pages/3_分组回测.py", title="目标多头回测", icon=":material/show_chart:"),
                st.Page("pages/4_Barra归因.py", title="Barra 归因", icon=":material/account_tree:"),
            ],
            "治理": [
                st.Page("pages/5_运行审计.py", title="运行审计", icon=":material/verified:"),
            ],
        },
        position="sidebar",
    )
    navigation.run()
