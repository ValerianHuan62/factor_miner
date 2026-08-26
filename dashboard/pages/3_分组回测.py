"""目标多头组合回测页面。"""

import streamlit as st

from dashboard.read import load_server_snapshot
from dashboard.ui import apply_theme, candidate_ids, candidate_name, daily_rows, hero, make_drawdown_figure, make_wealth_figure, metric_card, number, pct, portfolio_values, section


apply_theme(st, page_title="Factor Miner｜组合回测")
try:
    snapshot = load_server_snapshot()
except Exception as error:
    st.error(str(error))
else:
    ids = candidate_ids(snapshot)
    hero(
        st,
        kicker="PORTFOLIO LENS",
        title="目标多头的收益路径怎样？",
        copy="这里只展示 A 股可实际持有的目标多头，不展示多空组合。收益使用服务器侧真实交易日日历、开到开执行和双边万分之十四成本；开发区间表现不代表可以实盘。",
        meta=f"运行 {snapshot.run_id}  ·  基准：沪深300",
    )
    if not ids:
        st.info("没有组合指标产物。")
    else:
        candidate_id = st.selectbox("查看候选", ids, format_func=candidate_name)
        metrics = portfolio_values(snapshot, candidate_id)
        series = metrics.get("series", {}) if isinstance(metrics, dict) else {}
        spread = series.get("target_long_net_return", {}) if isinstance(series, dict) else {}
        cards = st.columns(4)
        with cards[0]:
            metric_card(st, "目标多头年化收益", pct(spread.get("annualized_return")), "扣费后")
        with cards[1]:
            metric_card(st, "目标多头 Sharpe", number(spread.get("sharpe")), "净收益序列")
        with cards[2]:
            metric_card(st, "信息比率", number(spread.get("information_ratio")), "相对沪深300")
        with cards[3]:
            metric_card(st, "最大回撤", f"−{pct(spread.get('max_drawdown'))}" if isinstance(spread.get("max_drawdown"), (int, float)) else "—", "风险提示")
        section(st, "路径", "累计收益与逐期节奏")
        left, right = st.columns(2)
        with left:
            st.plotly_chart(make_wealth_figure(snapshot, candidate_id), width="stretch", config={"displaylogo": False})
        with right:
            st.plotly_chart(make_drawdown_figure(snapshot, candidate_id), width="stretch", config={"displaylogo": False})
        if isinstance(series, dict):
            section(st, "组合对比", "目标多头毛收益、净收益与基准")
            table = []
            for series_name, series_metrics in series.items():
                if isinstance(series_metrics, dict):
                    table.append({
                        "组合": series_name.replace("_net_return", "").replace("_gross_return", "（毛）"),
                        "观测数": series_metrics.get("observations"),
                        "区间收益": series_metrics.get("period_return") * 100 if isinstance(series_metrics.get("period_return"), (int, float)) else None,
                        "年化收益": series_metrics.get("annualized_return") * 100 if isinstance(series_metrics.get("annualized_return"), (int, float)) else None,
                        "Sharpe": series_metrics.get("sharpe"),
                        "最大回撤": series_metrics.get("max_drawdown") * 100 if isinstance(series_metrics.get("max_drawdown"), (int, float)) else None,
                    })
            st.dataframe(table, width="stretch", hide_index=True, column_config={
                "区间收益": st.column_config.NumberColumn(format="%.2f%%"),
                "年化收益": st.column_config.NumberColumn(format="%.2f%%"),
                "Sharpe": st.column_config.NumberColumn(format="%.2f"),
                "最大回撤": st.column_config.NumberColumn(format="%.2f%%"),
            })
        rows = daily_rows(snapshot, candidate_id)
        if rows:
            with st.expander("查看逐期收益明细"):
                keep = ["entry_date", "exit_date", "target_long_gross_return", "target_long_turnover", "target_long_cost", "target_long_net_return", "benchmark_return"]
                detail = [{key: row.get(key) for key in keep if key in row} for row in rows]
                st.dataframe(detail, width="stretch", hide_index=True)
