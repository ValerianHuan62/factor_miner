"""Barra 暴露和已实现收益归因页面。"""

import streamlit as st

from dashboard.barra_view import normalize_barra_view
from dashboard.read import load_optional_snapshot, load_server_barra_snapshot, load_server_run_id
from dashboard.ui import apply_theme, candidate_ids, candidate_name, candidate_payload, candidate_source_id, hero, metric_card, number, pct, render_no_published_run, section


apply_theme(st, page_title="Factor Miner｜Barra 归因")
try:
    available = load_optional_snapshot()
    if available is None:
        render_no_published_run(st, lens="BARRA LENS")
        st.stop()
    main_run_id = load_server_run_id()
    snapshot = load_server_barra_snapshot()
except Exception as error:
    st.error(str(error))
else:
    ids = candidate_ids(snapshot)
    values = candidate_payload(snapshot.barra_attribution)
    hero(
        st,
        kicker="BARRA LENS",
        title="收益里有多少来自风险暴露？",
        copy="Barra 是可选的风险解释层。未提供合格暴露、因子收益或完整风险输入时，页面明确显示未启用，不把缺失数据误报成零暴露。",
        meta=(
            f"运行 {snapshot.run_id}  ·  仅展示已发布归因结果"
            if snapshot.run_id == main_run_id
            else f"最近 Barra 验证运行 {snapshot.run_id}  ·  主研究运行 {main_run_id}"
        ),
    )
    if not values:
        st.info("没有 Barra 归因产物。")
    else:
        candidate_id = st.selectbox("查看候选", ids, format_func=candidate_name)
        result = values.get(candidate_source_id(snapshot, candidate_id), {})
        if isinstance(result, dict):
            view = normalize_barra_view(result)
            status = view["status"]
            exposure = view["exposure_summary"]
            attribution = view["attribution"]
            risk = view["risk_decomposition"]
            status_label = {
                "complete": "完整",
                "realized_attribution_only": "仅收益归因",
                "not_available": "不可用",
            }.get(status, "未知")
            cards = st.columns(3)
            with cards[0]:
                metric_card(st, "风险分解", status_label, "不代表 Alpha 或机制证明")
            with cards[1]:
                metric_card(st, "暴露记录", str(len(exposure)) if isinstance(exposure, list) else "—", "信号日 × 组合")
            with cards[2]:
                metric_card(st, "贡献记录", str(len(attribution)) if isinstance(attribution, list) else "—", "因子收益贡献")
            if status in ("not_available", "unknown"):
                st.warning("Barra 当前未启用或输入不完整；请看“缺失输入”，不要将此结果解释为风险为零。")
            else:
                section(st, "归因结果", "主动暴露与因子收益贡献")
                if isinstance(exposure, list) and exposure:
                    st.dataframe(exposure, width="stretch", hide_index=True)
                if isinstance(attribution, list) and attribution:
                    st.dataframe(attribution, width="stretch", hide_index=True)
                if isinstance(risk, list) and risk:
                    section(st, "风险分解", "因子方差、特异方差、总方差与波动率")
                    st.dataframe(risk, width="stretch", hide_index=True)
            missing = view["missing_inputs"]
            if isinstance(missing, list) and missing:
                with st.expander("查看缺失输入"):
                    st.write(missing)
            if view.get("reason"):
                st.caption(str(view["reason"]))
