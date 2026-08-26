"""IC 与 RankIC 诊断页面。"""

import streamlit as st

from dashboard.read import load_server_snapshot
from dashboard.ui import apply_theme, candidate_ids, candidate_name, hero, ic_values, make_ic_decay_figure, make_ic_sequence_figure, metric_card, number, pct, section


apply_theme(st, page_title="Factor Miner｜IC 诊断")
try:
    snapshot = load_server_snapshot()
except Exception as error:
    st.error(str(error))
else:
    ids = candidate_ids(snapshot)
    hero(
        st,
        kicker="IC DIAGNOSTICS",
        title="信号稳定吗？",
        copy="主期限使用冻结的 5 日标签；页面同时展示多期限衰减、每日序列和 HAC t 值。显著性只是统计证据，不等于经济机制成立。",
        meta=f"运行 {snapshot.run_id}  ·  时间依赖使用冻结 HAC 参数",
    )
    if not ids:
        st.info("没有 IC 诊断产物。")
    else:
        candidate_id = st.selectbox("查看候选", ids, format_func=candidate_name)
        diagnostics = ic_values(snapshot, candidate_id)
        cards = st.columns(4)
        with cards[0]:
            metric_card(st, "IC 均值", pct(diagnostics.get("ic_mean")), "主期限 5 日")
        with cards[1]:
            metric_card(st, "RankIC 均值", pct(diagnostics.get("rank_ic_mean")), "主期限 5 日")
        with cards[2]:
            metric_card(st, "RankIC HAC t", number(diagnostics.get("rank_ic_hac_t")), "考虑时间依赖")
        with cards[3]:
            metric_card(st, "RankIC IR", number(diagnostics.get("rank_ic_ir")), "均值 / 样本波动")
        section(st, "形状", "多期限衰减与每日稳定性")
        left, right = st.columns(2)
        with left:
            st.plotly_chart(make_ic_decay_figure(snapshot, candidate_id), width="stretch", config={"displaylogo": False})
        with right:
            st.plotly_chart(make_ic_sequence_figure(snapshot, candidate_id), width="stretch", config={"displaylogo": False})
        section(st, "审计摘要")
        annual = diagnostics.get("annual_summary", {})
        if isinstance(annual, dict):
            annual_rows = [{"年份": year, **value} for year, value in annual.items() if isinstance(value, dict)]
            st.dataframe(annual_rows, width="stretch", hide_index=True)
        with st.expander("查看统计细节（非原始 JSON）"):
            st.write({
                "IC 左尾概率": pct(diagnostics.get("p_ic_lt_neg_002")),
                "IC 右尾概率": pct(diagnostics.get("p_ic_gt_pos_002")),
                "IC 自相关滞后数": len(diagnostics.get("autocorrelation", [])) if isinstance(diagnostics.get("autocorrelation"), list) else 0,
            })
