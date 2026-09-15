"""批次总览页面。"""

import streamlit as st

from dashboard.read import load_optional_snapshot
from dashboard.ui import apply_theme, candidate_ids, candidate_name, candidate_payload, candidate_source_id, direction_values, hero, ic_values, metric_card, number, pct, portfolio_values, render_no_published_run, section
from factor_miner.dashboard_labels import chinese_candidate_label


apply_theme(st, page_title="Factor Miner｜批次总览")
try:
    snapshot = load_optional_snapshot()
except Exception as error:
    st.error(str(error))
else:
    if snapshot is None:
        render_no_published_run(st, lens="BATCH OVERVIEW")
        st.stop()
    ids = candidate_ids(snapshot)
    hero(
        st,
        kicker="BATCH OVERVIEW",
        title="这次研究跑出了什么？",
        copy="用一张清晰的批次卡片，先看候选是否完整、结果是否可见，再进入 IC、组合和 Barra 页面。候选表现不会被自动删除或晋级。",
        meta=f"运行 {snapshot.run_id}  ·  发布状态：{'已发布' if snapshot.published else '未发布'}",
    )
    cards = st.columns(4)
    with cards[0]:
        metric_card(st, "候选数量", str(len(ids)), "保留完整候选集合")
    with cards[1]:
        metric_card(st, "产物数量", str(len(snapshot.artifact_refs)), "已纳入发布清单")
    with cards[2]:
        metric_card(st, "主期限", "5 日", "冻结标签期限")
    with cards[3]:
        metric_card(st, "数据状态", "已发布", "正式发布产物")

    if snapshot.campaign_metadata:
        campaign = snapshot.campaign_metadata
        st.info(
            "正式研究族："
            f"{campaign.get('family_id', '未知')} · "
            f"seal {campaign.get('generation_seal_id', '未知')} · "
            f"预登记 {campaign.get('registered_slot_count', 0)} 槽 · "
            f"失败/未执行 {campaign.get('failed_slot_count', 0)} 槽"
        )

    if getattr(snapshot, "evolution_metadata", None):
        evolution = snapshot.evolution_metadata
        st.info(
            "记忆辅助演化："
            f"上下文 {evolution.get('context_id', '未知')} · "
            f"批准 {evolution.get('approval_count', 0)}/10 · "
            f"终态槽位 {evolution.get('terminal_slot_count', 0)}/120"
        )

    if not ids:
        st.info("该运行尚未投影候选汇总。")
    else:
        section(st, "候选状态", "全部候选，一张表看清")
        rows = []
        for candidate_id in ids:
            source_id = candidate_source_id(snapshot, candidate_id)
            metrics = candidate_payload(snapshot.candidate_metrics).get(source_id, {})
            definition = candidate_payload(snapshot.candidate_definitions).get(source_id, {})
            ic = ic_values(snapshot, candidate_id)
            portfolio = portfolio_values(snapshot, candidate_id)
            direction = direction_values(snapshot, candidate_id)
            series = portfolio.get("series", {}) if isinstance(portfolio, dict) else {}
            spread = series.get("target_long_net_return", {}) if isinstance(series, dict) else {}
            rows.append({
                "因子编号": candidate_name(candidate_id),
                "因子名称": definition.get("factor_name_zh", "未命名候选因子"),
                "事前方向": chinese_candidate_label("expected_sign", definition.get("expected_sign")),
                "发现方向": {"positive": "正向", "negative": "负向"}.get(direction.get("selected_direction"), "未完成"),
                "方向关系": {"supported": "一致", "reversed": "反转"}.get(direction.get("hypothesis_relation"), "未完成"),
                "来源": chinese_candidate_label("source_kind", definition.get("source_kind")),
                "IC 均值": ic.get("ic_mean") if isinstance(ic.get("ic_mean"), (int, float)) else None,
                "RankIC 均值": ic.get("rank_ic_mean") if isinstance(ic.get("rank_ic_mean"), (int, float)) else None,
                "HAC t 值": ic.get("rank_ic_hac_t"),
                "目标多头 Sharpe": spread.get("sharpe"),
                "质量状态": chinese_candidate_label("quality_status", metrics.get("quality_status", "not_assessed")),
                "Barra 状态": chinese_candidate_label("status", metrics.get("barra_status", "available")),
            })
        st.dataframe(
            rows,
            width="stretch",
            hide_index=True,
            column_config={
                "IC 均值": st.column_config.NumberColumn(format="%.4f"),
                "RankIC 均值": st.column_config.NumberColumn(format="%.4f"),
                "HAC t 值": st.column_config.NumberColumn(format="%.2f"),
                "目标多头 Sharpe": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(f"产物清单哈希：{snapshot.artifact_manifest_sha256}")
