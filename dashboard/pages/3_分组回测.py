"""单因子回测、IC 与 Barra 一体化演示工作台。"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go
import streamlit as st

from dashboard.backtest_scenario import (
    calculate_scenario_metrics,
    nav_rows,
    scenario_rows,
)
from dashboard.demo_backtest import (
    DEMO_FACTOR_ID,
    DEMO_FACTOR_NAME,
    FROZEN_COST_BPS,
    FROZEN_HAC_MAX_LAGS,
    demo_daily_rows,
    demo_ic_rows,
    demo_ic_summary,
)
from dashboard.market_profiles import current_market_id, profile_by_id
from dashboard.real_backtest import load_real_backtest_catalog
from dashboard.ui import (
    apply_theme,
    hero,
    metric_card,
    number,
    pct,
    section,
)


def _nav_figure(rows: list[dict[str, object]]) -> go.Figure:
    """绘制情景、冻结成本与基准 NAV。"""

    path = nav_rows(rows)
    figure = go.Figure()
    for field, label, color, width in (
        ("scenario_nav", "情景净值", "#c7f36b", 3.2),
        ("published_nav", "冻结成本净值", "#70d7c4", 1.8),
        ("benchmark_nav", "基准净值", "#91a096", 1.5),
    ):
        figure.add_trace(
            go.Scatter(
                x=[row["date"] for row in path],
                y=[row[field] for row in path],
                mode="lines",
                name=label,
                line={"color": color, "width": width},
            )
        )
    figure.update_layout(
        height=440,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111814",
        font={"family": "SFMono-Regular, Menlo, monospace", "color": "#dce8de"},
        legend={"orientation": "h", "y": 1.1},
        margin={"l": 20, "r": 20, "t": 45, "b": 20},
        hovermode="x unified",
    )
    figure.update_xaxes(gridcolor="#26342c", title="退出日期")
    figure.update_yaxes(gridcolor="#26342c", title="NAV", tickformat=".3f")
    return figure


def _drawdown_figure(rows: list[dict[str, object]]) -> go.Figure:
    """绘制当前情景的水下回撤曲线。"""

    path = nav_rows(rows)
    figure = go.Figure(
        go.Scatter(
            x=[row["date"] for row in path],
            y=[float(row["drawdown"]) * 100 for row in path],
            mode="lines",
            line={"color": "#ff8e72", "width": 2.4},
            fill="tozeroy",
            fillcolor="rgba(255,142,114,.16)",
            name="情景回撤",
        )
    )
    figure.update_layout(
        height=280,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111814",
        font={"family": "SFMono-Regular, Menlo, monospace", "color": "#dce8de"},
        margin={"l": 20, "r": 20, "t": 20, "b": 20},
    )
    figure.update_xaxes(gridcolor="#26342c")
    figure.update_yaxes(gridcolor="#26342c", title="回撤（%）")
    return figure


def _ic_figure() -> go.Figure:
    """绘制演示因子的 IC 与 RankIC 序列。"""

    rows = demo_ic_rows()
    figure = go.Figure()
    figure.add_hline(y=0, line_color="#526057", line_width=1)
    figure.add_trace(
        go.Scatter(
            x=[row["date"] for row in rows],
            y=[row["ic"] for row in rows],
            mode="lines",
            name="IC",
            line={"color": "#70d7c4", "width": 1.8},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[row["date"] for row in rows],
            y=[row["rank_ic"] for row in rows],
            mode="lines",
            name="RankIC",
            line={"color": "#c7f36b", "width": 2.4},
        )
    )
    figure.update_layout(
        height=390,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111814",
        font={"family": "SFMono-Regular, Menlo, monospace", "color": "#dce8de"},
        legend={"orientation": "h", "y": 1.1},
        hovermode="x unified",
        margin={"l": 20, "r": 20, "t": 40, "b": 20},
    )
    figure.update_xaxes(gridcolor="#26342c")
    figure.update_yaxes(gridcolor="#26342c", tickformat=".4f")
    return figure


apply_theme(st, page_title="Factor Miner｜回测系统")

market_id = current_market_id()
profile = profile_by_id(market_id)
market_name = profile.display_name if profile is not None else market_id
if market_id == "us_equity":
    if profile is None:
        raise RuntimeError("美股系统尚未配置市场 Profile")
    real_backtests = load_real_backtest_catalog(
        backtest_path=profile.backtest_path,
        backtest_roots=profile.backtest_roots,
    )
    factor_names = {
        str(item["factor_id"]): str(item["factor_name"])
        for item in real_backtests
    }
    factor_options = list(factor_names)
    selected_factor_id = str(st.session_state.get("fm_backtest_factor_id", factor_options[0]))
    if selected_factor_id not in factor_names:
        selected_factor_id = factor_options[0]
        st.session_state["fm_backtest_factor_id"] = selected_factor_id
    real_backtest = next(
        item for item in real_backtests
        if str(item["factor_id"]) == selected_factor_id
    )
    source_rows = list(real_backtest["daily_rows"])
    evaluation = real_backtest["evaluation"]
    protocol = real_backtest["protocol"]
    ic_summary = {
        "ic_mean": evaluation.get("ic_mean"),
        "rank_ic_mean": evaluation["rank_ic_mean"],
        "rank_ic_hac_t": evaluation["rank_ic_hac_t"],
    }
    factor_id = str(real_backtest["factor_id"])
    factor_name = str(real_backtest["factor_name"])
    published_cost_bps = float(protocol["base_cost_bps"])
    page_scope = str(real_backtest["scope"])
    hero_copy = (
        "本页读取真实美股 split-adjusted open、显式交易状态与聚合后的逐期组合收益。"
        "信号在收盘后形成，下一交易日开盘成交；候选与各自回测产物按稳定 factor_id 联动。"
    )
else:
    real_backtest = None
    source_rows = demo_daily_rows()
    ic_summary = demo_ic_summary()
    factor_id = DEMO_FACTOR_ID
    factor_name = DEMO_FACTOR_NAME
    published_cost_bps = FROZEN_COST_BPS
    page_scope = "确定性合成 UI 演示，不进入正式候选或研究账本"
    hero_copy = (
        "本页只使用一个确定性合成示例演示逐期收益、成本、滑点、区间、NAV、回撤和 HAC 统计。"
        "它不会计算 367 个历史因子，也不会写入正式候选、PostgreSQL 或不可变研究账本。"
    )
    factor_names = {factor_id: factor_name}
    factor_options = [factor_id]
exits = [
    value if isinstance(value := row["exit_date"], date) else date.fromisoformat(str(value)[:10])
    for row in source_rows
]

hero(
    st,
    kicker=f"BACKTEST LAB / {market_id.upper()} / SINGLE FACTOR {'REAL DATA' if real_backtest else 'DEMO'}",
    title="一个因子，完整走完回测与诊断。",
    copy=hero_copy,
    meta=f"当前系统：{market_name} · {factor_id} · {len(source_rows)} 条收益观测 · {page_scope}",
)
if real_backtest is None:
    st.warning("当前为合成 UI 演示。以后接入某个正式候选时，页面结构不变，但数据必须来自该运行的不可变逐期产物。")
else:
    if real_backtest["mode"] == "Smoke":
        st.warning("当前是开发期真实数据 Smoke，不是密封 OOS 或生产结论；调整日期和成本只属于事后情景观察。")
    else:
        st.warning("当前是已使用确认期的重算诊断，不是新的密封 OOS；调整日期和成本只属于事后情景观察。")
    with st.expander("真实数据与交易协议", expanded=False):
        st.json({"数据身份": real_backtest["data_identity"], "交易协议": protocol})

st.selectbox(
    "回测候选",
    factor_options,
    key="fm_backtest_factor_id" if real_backtest else None,
    format_func=lambda item: f"{item} · {factor_names[item]}",
)
if real_backtest and any(str(item["factor_id"]).startswith("strategy_") for item in real_backtests):
    st.caption("候选菜单同时包含本次 30 个原始因子和末尾的简单组合策略；策略不占用 huan 因子编号。")
    if real_backtest.get("strategy_contract"):
        with st.expander("简单策略的冻结选因子与交易规则", expanded=True):
            st.write(real_backtest["inference"]["selected_factors"])
            st.json(real_backtest["strategy_contract"])
if real_backtest and real_backtest.get("schema_version") == "dashboard-real-backtest-v3-accounting":
    quality = real_backtest.get("data_quality_audit", {})
    if quality.get("status") == "review_required":
        st.error(f"数据质量待核查：发现 {quality['anomaly_count']} 个极端报价断点。本页是记账诊断，不是可信收益结论；零回收压力情景也不能修复报价错误。")
        with st.expander("价格断点证据（保留原数据，不事后删除交易）"):
            st.write(quality["rule"])
            st.dataframe(quality["anomalies"], width="stretch", hide_index=True)
    elif quality.get("status") == "no_unresolved_eligible_discontinuity":
        st.caption("价格连续性与 L2 联合检查：可用样本中未裁决断点为 0；隔离证据及已核实真实暴跌保留。此检查不是全部数据质量或收益有效性的保证。")
    audit = real_backtest["execution_audit"]["target"]
    pending = real_backtest.get("unresolved_positions", [])
    st.subheader("持仓记账与损失")
    account_columns = st.columns(3)
    account_columns[0].metric("实际总收益", "价值未确定" if pending else pct(real_backtest["reference_metrics"]["period_return"]))
    account_columns[1].metric("未确定证券数", audit["unresolved_security_count"])
    account_columns[2].metric("已结算终止持仓笔数", audit["terminal_settlement_count"])
    if pending:
        st.warning("仍有持仓无法确定最终回收价值。最后报价只用于参考估值，不代表能按该价卖出；这些交易没有被删除。")
        with st.expander("价值未确定的持仓明细", expanded=True):
            st.caption("金额以初始组合净值为 1 的归一化资金单位展示。最后报价日期可用于识别数据断档。")
            st.dataframe(pending, width="stretch", hide_index=True)
    with st.expander("已有依据的退市／并购结算"):
        st.dataframe(real_backtest.get("terminal_settlements", []), width="stretch", hide_index=True)
    with st.expander("等权基准的未确定持仓"):
        st.dataframe(real_backtest.get("benchmark_unresolved_positions", []), width="stretch", hide_index=True)
    valuation = st.radio("估值展示", ["最后报价参考估值", "未确定持仓全部零回收压力情景"], key=f"valuation_{factor_id}", horizontal=True)
    if valuation.startswith("未确定"):
        source_rows = list(real_backtest["zero_recovery_daily_rows"])
        st.info("仅在报告末日假设未确定持仓全部零回收；基准采用同样情景。该假设不改变之前的选股或交易，不是实际退市损失记录。")
    else:
        st.info("下方净值及收益指标采用最后报价参考估值。存在未确定持仓时，它们不是最终实际收益。")
    with st.expander("构念验证：公式是否测量了声称的状态", expanded=True):
        construct = real_backtest["construct_validation"]
        st.write(f"检验结果：{construct['status']}；经济机制仍未独立验证。")
        st.write(construct.get("condition", {}).get("observation", ""))
        st.dataframe(construct.get("checks", []), width="stretch", hide_index=True)
        st.caption("当前 30 个候选属于事后审计。基础检验符合不代表经济因果成立，也不代表 IC 或回测通过。")
        st.dataframe(construct.get("training_condition_profile", []), width="stretch", hide_index=True)
    with st.expander("统计检验与因子冗余"):
        st.json(real_backtest.get("inference", {}))
        st.caption("IC 仅在固定端点标签可计算的样本内评价；实际选股不按未来标签是否存在过滤。IC 不能替代包含不可成交和未确定持仓的组合表现。")
top = st.columns(5)
with top[0]:
    metric_card(st, "IC 均值", number(ic_summary["ic_mean"], 4), "原始小数")
with top[1]:
    metric_card(st, "RankIC 均值", number(ic_summary["rank_ic_mean"], 4), "原始小数")
with top[2]:
    metric_card(
        st,
        "RankIC HAC t",
        number(ic_summary["rank_ic_hac_t"]),
        f"冻结 maxlags={evaluation.get('hac_max_lags', FROZEN_HAC_MAX_LAGS) if real_backtest else FROZEN_HAC_MAX_LAGS}",
    )
with top[3]:
    metric_card(st, "冻结成本", f"{published_cost_bps:.1f} bps", "美股成本模型 · 逐期换手率计费")
with top[4]:
    metric_card(
        st,
        "持有周期",
        "5 日",
        "计划 5 日；退出失败顺延" if real_backtest else "确定性演示协议",
    )

backtest_tab, data_tab = st.tabs(["交互回测", "逐期数据"])

with backtest_tab:
    controls = st.columns([1.6, 1, 1])
    with controls[0]:
        selected_range = st.date_input(
            "估值日期区间" if real_backtest and real_backtest.get("schema_version") == "dashboard-real-backtest-v3-accounting" else "退出日区间",
            value=(min(exits), max(exits)),
            min_value=min(exits),
            max_value=max(exits),
        )
    with controls[1]:
        cost_bps = st.number_input(
            "交易成本（bps）",
            min_value=0.0,
            max_value=200.0,
            value=published_cost_bps,
            step=1.0,
            help="按每期换手率乘以总成本率。",
        )
    with controls[2]:
        slippage_bps = st.number_input(
            "额外滑点（bps）",
            min_value=0.0,
            max_value=200.0,
            value=0.0,
            step=1.0,
            help="额外叠加到每期换手率上的压力假设。",
        )
    if not isinstance(selected_range, (tuple, list)) or len(selected_range) != 2:
        st.info("请选择完整的开始与结束日期。")
        st.stop()
    try:
        scenario = scenario_rows(
            source_rows,
            start_date=selected_range[0],
            end_date=selected_range[1],
            cost_bps=float(cost_bps),
            slippage_bps=float(slippage_bps),
        )
        metrics = calculate_scenario_metrics(scenario)
    except ValueError as error:
        st.warning(str(error))
    else:
        st.caption(
            f"当前情景总成本 {cost_bps + slippage_bps:.2f} bps。区间选择属于事后切片，只用于交互与稳健性观察。"
        )
        cards = st.columns(7)
        values = (
            ("区间收益", pct(metrics.period_return), f"{metrics.observations} 期"),
            ("情景年化", pct(metrics.annualized_return), "按实际日期跨度"),
            ("年化波动", pct(metrics.annualized_volatility), "情景净收益"),
            ("Sharpe", number(metrics.sharpe), "零无风险利率"),
            ("Calmar", number(metrics.calmar_ratio), "年化收益 / 最大回撤"),
            (
                "信息比率",
                number(metrics.information_ratio),
                "相对同池等权基准" if real_backtest else "相对演示基准",
            ),
            ("最大回撤", pct(metrics.max_drawdown), "正数幅度"),
        )
        for column, (label, value, note) in zip(cards, values, strict=True):
            with column:
                metric_card(st, label, value, note)
        st.plotly_chart(_nav_figure(scenario), width="stretch", config={"displaylogo": False})
        st.plotly_chart(_drawdown_figure(scenario), width="stretch", config={"displaylogo": False})
        detail = st.columns(3)
        detail[0].metric("平均换手率", pct(metrics.average_turnover))
        detail[1].metric("逐期成本合计", pct(metrics.total_cost))
        detail[2].metric("区间观测", str(metrics.observations))

with data_tab:
    st.dataframe(source_rows, width="stretch", hide_index=True)
    st.caption(
        "逐期数据来自真实美股聚合回测产物；调整参数不会修改源记录。"
        if real_backtest
        else "演示逐期数据由固定公式生成；调整参数不会修改源记录。"
    )
