"""PostgreSQL 因子目录的交互探索页面。"""

from __future__ import annotations

from statistics import median

import streamlit as st

from dashboard.factor_explorer import (
    factor_rows_csv,
    filter_factor_rows,
    sort_factor_rows,
    text_values,
)
from dashboard.market_profiles import current_market_id, dashboard_dsn, profile_by_id
from dashboard.pg_store import PostgresDashboardStore
from dashboard.real_backtest import load_real_backtest_catalog
from dashboard.ui import (
    apply_theme,
    hero,
    metric_card,
    number,
    pct,
    section,
)


SORT_FIELDS = {
    "因子编号": "factor_id",
    "RankIC 均值": "rank_ic_mean",
    "|RankIC HAC t|": "rank_ic_hac_t",
    "年化收益": "annualized_return",
    "Sharpe": "sharpe",
    "最大回撤": "max_drawdown",
    "有效日期": "valid_dates",
}


def _numeric(value: object) -> float | None:
    """把可选指标收窄为浮点数；未执行的评价保持为空。"""

    return float(value) if isinstance(value, (int, float)) else None


def _percent_points(value: object) -> float | None:
    """仅为表格显示把比例转换成百分点，不为缺失指标造值。"""

    number_value = _numeric(value)
    return number_value * 100 if number_value is not None else None


@st.cache_data(ttl=30, show_spinner=False)
def _load_rows(market_id: str) -> list[dict[str, object]]:
    """短时缓存 PostgreSQL 联查视图，避免每次筛选都重新连接。"""

    dsn = dashboard_dsn(market_id)
    if not dsn:
        raise RuntimeError(f"请先配置 FM_DASHBOARD_DSN_{market_id.upper()}，再打开因子探索页")
    return PostgresDashboardStore(dsn, market_id=market_id).load_factor_metrics_detail()


apply_theme(st, page_title="Factor Miner｜因子探索")

market_id = current_market_id()
profile = profile_by_id(market_id)
market_name = profile.display_name if profile is not None else market_id

try:
    all_rows = _load_rows(market_id)
except Exception as error:
    hero(st, kicker="FACTOR MINER / READ-ONLY", title="因子探索暂时不可用", copy=str(error))
    st.error(str(error))
    st.code(
        "docker compose up -d postgres\n"
        "export FM_DASHBOARD_DSN_A_SHARE='postgresql://factor_miner:factor_miner_local@127.0.0.1:5432/factor_miner'\n"
        "export FM_DASHBOARD_DSN_US_EQUITY='postgresql://factor_miner:factor_miner_local@127.0.0.1:5432/factor_miner_us'\n"
        "make dashboard",
        language="bash",
    )
    st.stop()

accounting_rows = [row for row in all_rows if str(row.get("evaluation_scope", "")).startswith("本次因果记账")]
if accounting_rows:
    if any(str(row.get("status", "")).startswith("数据待核") for row in accounting_rows):
        st.error("本次数据存在未解释价格断点。IC、参考收益及压力收益均仅供诊断，不能据此认定因子有效。详见回测系统的价格断点证据。")
    history_count = len(all_rows) - len(accounting_rows)
    include_history = st.sidebar.checkbox(f"包含旧版本历史候选（{history_count} 个，未按本次协议重跑）", value=False)
    if not include_history:
        all_rows = accounting_rows
    st.info("当前记账报告的未确定实际收益显示为 —；最后报价参考估值与零回收压力情景请在回测系统查看。旧版本指标单独保留，不与本次结果混用。")

hero(
    st,
    kicker=f"FACTOR MINER / {market_id.upper()} / RESEARCH CATALOG",
    title=f"{market_name}因子不是奖杯，是待复核的研究标本。",
    copy=f"搜索公式与金融假设，按方向和统计证据筛选，再把候选并排比较。当前展示 {len(all_rows)} 个已投影候选；筛选与排序不会重算结果。",
    meta=f"当前系统：{market_name} · 与其他市场因子、运行和产物严格隔离",
)

with st.sidebar:
    st.subheader("筛选工作台")
    query = st.text_input("搜索", placeholder="编号、假设、机制或公式")
    directions = st.multiselect(
        "发现方向",
        text_values(all_rows, "discovered_direction"),
    )
    relations = st.multiselect(
        "与事前假设关系",
        text_values(all_rows, "direction_relation"),
    )
    statuses = st.multiselect("候选状态", text_values(all_rows, "status"))
    regime_labels = st.multiselect('预期适用状态',text_values(all_rows,'expected_regime_label'))
    regime_statuses = st.multiselect('状态检验',text_values(all_rows,'regime_validation_status'))
    regime_directions = st.multiselect('状态方向',text_values(all_rows,'regime_direction'))
    maximum_dates = max((int(row["valid_dates"]) for row in all_rows), default=0)
    minimum_valid_dates = st.slider(
        "最少有效日期",
        min_value=0,
        max_value=maximum_dates,
        value=0,
    )
    minimum_abs_hac_t = st.slider(
        "最低 |RankIC HAC t|",
        min_value=0.0,
        max_value=10.0,
        value=0.0,
        step=0.25,
        help="只改变当前可见集合，不重新判定显著性或候选状态。",
    )
    sort_label = st.selectbox("排序指标", list(SORT_FIELDS), index=1)
    descending = st.toggle("从高到低", value=True)

filtered = filter_factor_rows(
    all_rows,
    query=query,
    directions=directions,
    relations=relations,
    statuses=statuses,
    regime_labels=regime_labels, regime_statuses=regime_statuses, regime_directions=regime_directions,
    minimum_valid_dates=minimum_valid_dates,
    minimum_abs_hac_t=minimum_abs_hac_t,
)
sort_field = SORT_FIELDS[sort_label]
if sort_label == "|RankIC HAC t|":
    filtered = sorted(
        filtered,
        key=lambda row: abs(float(row.get(sort_field, 0.0))),
        reverse=descending,
    )
else:
    filtered = sort_factor_rows(filtered, field=sort_field, descending=descending)

rank_ics = [value for row in filtered if (value := _numeric(row.get("rank_ic_mean"))) is not None]
sharpes = [value for row in filtered if (value := _numeric(row.get("sharpe"))) is not None]
cards = st.columns(4)
with cards[0]:
    metric_card(st, "可见候选", str(len(filtered)), f"全库 {len(all_rows)} 个")
with cards[1]:
    metric_card(st, "RankIC 中位数", number(median(rank_ics), 4) if rank_ics else "—", "原始小数")
with cards[2]:
    metric_card(st, "Sharpe 中位数", number(median(sharpes)) if sharpes else "—", "当前筛选集合")
with cards[3]:
    reversed_count = sum(row.get("direction_relation") == "与假设相反" for row in filtered)
    metric_card(st, "方向反转", str(reversed_count), "发现方向与事前假设相反")

has_portfolio_rows = any(bool(row.get("has_portfolio")) for row in filtered)
section(
    st,
    "候选图谱",
    "完整候选展示收益与信号；尚未回测的开发期候选展示 RankIC 与 HAC t",
)
if filtered:
    import plotly.express as px

    y_field = "annualized_return" if has_portfolio_rows else "rank_ic_hac_t"
    y_label = "目标多头年化收益" if has_portfolio_rows else "RankIC HAC t"
    hover_data = {
        "hypothesis": True,
        "rank_ic_hac_t": ":.2f",
        "rank_ic_mean": ":.4f",
    }
    if has_portfolio_rows:
        hover_data.update(
            {
                "sharpe": ":.2f",
                "max_drawdown": ":.2%",
                "annualized_return": ":.2%",
            }
        )
    figure = px.scatter(
        filtered,
        x="rank_ic_mean",
        y=y_field,
        color="discovered_direction",
        symbol="direction_relation",
        hover_name="factor_id",
        hover_data=hover_data,
        color_discrete_map={"正向": "#70d7c4", "负向": "#ff8e72"},
        labels={
            "rank_ic_mean": "RankIC 均值",
            y_field: y_label,
            "discovered_direction": "发现方向",
            "direction_relation": "假设关系",
        },
    )
    figure.update_traces(marker={"size": 10, "opacity": 0.82, "line": {"width": 1, "color": "#dce8de"}})
    figure.update_layout(
        height=470,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111814",
        font={"family": "SFMono-Regular, Menlo, monospace", "color": "#dce8de"},
        legend={"orientation": "h", "y": 1.12},
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
    )
    figure.update_xaxes(tickformat=".4f", gridcolor="#26342c")
    figure.update_yaxes(tickformat=".1%" if has_portfolio_rows else ".2f", gridcolor="#26342c")
    st.plotly_chart(figure, width="stretch", config={"displaylogo": False})
else:
    st.info("当前筛选条件下没有候选。")

section(st, "因子目录", "筛选、排序与导出当前集合")
table_rows = [
    {
        "因子编号": row["factor_id"],
        "金融假设": row["hypothesis"],
        "预期适用状态":row.get("expected_regime_label","未登记"),
        "状态检验":row.get("regime_validation_status","未开展"),
        "发现方向": row["discovered_direction"],
        "假设关系": row["direction_relation"],
        "RankIC": float(row["rank_ic_mean"]),
        "HAC t": row["rank_ic_hac_t"],
        "年化收益": _percent_points(row.get("annualized_return")),
        "Sharpe": row["sharpe"],
        "最大回撤": _percent_points(row.get("max_drawdown")),
        "评价范围": row.get("evaluation_scope"),
        "有效日期": row["valid_dates"],
    }
    for row in filtered
]
st.dataframe(
    table_rows,
    width="stretch",
    hide_index=True,
    column_config={
        "RankIC": st.column_config.NumberColumn(format="%.4f"),
        "HAC t": st.column_config.NumberColumn(format="%.2f"),
        "年化收益": st.column_config.NumberColumn(format="%.2f%%"),
        "Sharpe": st.column_config.NumberColumn(format="%.2f"),
        "最大回撤": st.column_config.NumberColumn(format="%.2f%%"),
    },
)
st.download_button(
    "下载当前筛选结果",
    data=factor_rows_csv(filtered),
    file_name="factor_miner_filtered_factors.csv",
    mime="text/csv",
    disabled=not filtered,
)

if filtered:
    section(st, "并排复核", "最多选择五个候选")
    options = [str(row["factor_id"]) for row in filtered]
    chosen = st.multiselect(
        "选择候选",
        options,
        default=options[: min(3, len(options))],
        max_selections=5,
    )
    selected_rows = [row for row in filtered if row["factor_id"] in chosen]
    if selected_rows:
        compare = st.columns(len(selected_rows))
        for column, row in zip(compare, selected_rows, strict=True):
            with column:
                st.markdown(f"### {row['factor_id']}")
                st.caption(str(row["status"]))
                st.metric("IC 均值", number(row["ic_mean"], 4))
                st.metric("RankIC 均值", number(row["rank_ic_mean"], 4))
                st.metric("RankIC HAC t", number(row["rank_ic_hac_t"]))
                st.metric("年化收益", pct(row["annualized_return"]))
                st.metric("Sharpe", number(row["sharpe"]))

    selected_id = st.selectbox("查看完整定义", options)
    detail = next(row for row in filtered if row["factor_id"] == selected_id)
    from dashboard.regime import render_regime
    render_regime(st, detail.get('regime_record'))
    local_backtest_ids: set[str] = set()
    if market_id == "us_equity" and profile is not None:
        local_backtest_ids = {
            str(item["factor_id"])
            for item in load_real_backtest_catalog(
                backtest_path=profile.backtest_path,
                backtest_roots=profile.backtest_roots,
            )
        }
    has_local_backtest = bool(
        market_id == "us_equity"
        and selected_id in local_backtest_ids
    )
    if detail.get("has_portfolio") or has_local_backtest:
        if has_local_backtest:
            st.session_state["fm_backtest_factor_id"] = selected_id
        st.page_link(
            "pages/3_分组回测.py",
            label="打开真实单因子回测" if has_local_backtest else "打开单因子回测演示",
            icon=":material/show_chart:",
        )
    else:
        st.caption("该候选尚未执行组合回测；系统不会用零值或其他市场结果补齐指标。")
    left, right = st.columns([1.2, 1])
    with left:
        st.markdown("#### 金融假设")
        st.write(detail["hypothesis"])
        st.markdown("#### 机制")
        st.write(detail["mechanism"])
        st.markdown("#### 计算说明")
        st.write(detail["calculation"])
    with right:
        st.markdown("#### 公式")
        st.code(str(detail["formula"]), language="text", wrap_lines=True)
        st.markdown("#### 结论边界")
        st.info("这里展示的是已投影聚合指标。确认期或最近期表现不等于未来 Alpha，也不证明经济机制成立。")
