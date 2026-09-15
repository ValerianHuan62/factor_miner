"""Streamlit 只读研究驾驶舱入口。"""

from __future__ import annotations

from factor_miner.errors import FactorMinerError
from dashboard.market_profiles import load_market_profiles
from dashboard.market_page import render_market_page
from dashboard.read import load_optional_snapshot
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
    render_no_published_run,
    section,
)


def main() -> None:
    """读取当前市场正式产物并展示，不提供研究写操作。"""

    try:
        import streamlit as st  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("运行 Dashboard 前请安装 Streamlit") from error

    apply_theme(st, page_title="Factor Miner｜研究驾驶舱")
    try:
        snapshot = load_optional_snapshot()
    except FactorMinerError as error:
        hero(st, kicker="FACTOR MINER / READ MODEL", title="研究驾驶舱暂时不可用", copy=str(error))
        return
    if snapshot is None:
        render_no_published_run(st, lens="RESEARCH COCKPIT")
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
        metric_card(st, "主期限 RankIC", number(first_ic.get("rank_ic_mean"), 4), "原始小数 · 冻结 5 日标签")
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
            "IC 均值": ic.get("ic_mean") if isinstance(ic.get("ic_mean"), (int, float)) else None,
            "RankIC 均值": ic.get("rank_ic_mean") if isinstance(ic.get("rank_ic_mean"), (int, float)) else None,
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
            "IC 均值": st.column_config.NumberColumn(format="%.6f"),
            "RankIC 均值": st.column_config.NumberColumn(format="%.4f"),
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
        metric_card(st, "IC 均值", number(selected_ic.get("ic_mean"), 4), "原始小数 · 主期限 5 日")
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
        st.write("真实行情、标签、IC 和组合评价可在 macOS、Linux 或其他受支持环境执行；页面从当前市场的正式 JSON/JSONL/Parquet 产物读取图表，PostgreSQL 只提供可重建的因子索引和指标。")
        st.write(f"产物数量：{len(snapshot.artifact_refs)}；清单哈希：{snapshot.artifact_manifest_sha256}")


def market_data_main() -> None:
    """展示当前研究系统的数据合同与独立产物空间。"""

    import streamlit as st  # type: ignore[import-not-found]

    render_market_page(st, str(st.session_state.get("fm_market_id", "a_share")))


if __name__ == "__main__":
    import streamlit as st
    from dashboard.market_profiles import default_market_id
    from dashboard.results import render_results
    from dashboard.settings import render_settings

    try:
        profiles = load_market_profiles()
    except (ValueError, OSError) as error:
        st.error("市场配置无法读取，请检查本地配置。")
        with st.expander("具体原因"):
            st.text(str(error))
        st.stop()
    if profiles:
        profile_ids = [profile.market_id for profile in profiles]
        current = st.session_state.get("fm_market_id", default_market_id(profiles))
        if current not in profile_ids:
            current = default_market_id(profiles)
        st.sidebar.selectbox(
            "研究市场", profile_ids, index=profile_ids.index(current),
            format_func=lambda value: next(profile.display_name for profile in profiles if profile.market_id == value),
            key="fm_market_id",
        )
    st.sidebar.caption("FACTOR MINER · 研究工作台")
    navigation = st.navigation([
        st.Page(render_results, title="看结果", icon=":material/show_chart:", default=True),
        st.Page("pages/7_研究运行台.py", title="挖因子", icon=":material/science:"),
        st.Page("pages/12_研究代表库.py", title="研究代表库", icon=":material/filter_alt:"),
        st.Page(render_settings, title="设置", icon=":material/settings:"),
    ])
    navigation.run()
