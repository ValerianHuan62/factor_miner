"""十槽假设草案只读工作台。"""

import streamlit as st

from dashboard.read import load_server_hypothesis_batch
from dashboard.ui import apply_theme, hero, metric_card, section
from factor_miner.errors import FactorMinerError


def _direction(value: object) -> str:
    """把模型方向标签转换为中文展示。"""

    labels = {
        "positive": "正向",
        "negative": "负向",
        "neutral": "中性",
        "正向": "正向",
        "负向": "负向",
        "中性": "中性",
    }
    return labels.get(str(value), str(value) if value else "未说明")


def main() -> None:
    """展示待审批假设，不提供批准、生成因子或回测操作。"""

    apply_theme(st, page_title="Factor Miner｜假设工作台")
    try:
        batch = load_server_hypothesis_batch()
    except Exception as error:
        hero(
            st,
            kicker="HYPOTHESIS DESK / READ-ONLY",
            title="还没有可展示的假设批次",
            copy="当前市场还没有待审批假设文件。生成草案后，可用可选的 FM_DASHBOARD_HYPOTHESIS_PATH 指向 hypothesis_drafts_*.json；此页不会把草案自动变成因子。",
        )
        if not isinstance(error, FactorMinerError):
            st.error(f"假设文件存在但无法读取：{error}")
        else:
            st.caption("等待当前市场生成并登记正式假设草案。")
        return

    hero(
        st,
        kicker="HYPOTHESIS DESK / PRE-APPROVAL",
        title="先看假设，再决定要不要生成因子",
        copy="这里展示的是模型根据现有因子图谱缺口提出的事前假设。机制仍未验证，草案尚未批准；只有你明确批准后，才会进入三种不同结构的因子表达式和后续回测。",
        meta=(
            f"研究族 {batch.discovery_family_id}  ·  {batch.status}  ·  "
            f"草案批次 {batch.draft_batch_sha256[:16]}…  ·  来源 {batch.source_path.name}"
        ),
    )

    cards = st.columns(4)
    with cards[0]:
        metric_card(st, "假设数量", str(len(batch.drafts)), "固定 H01–H10")
    with cards[1]:
        metric_card(st, "当前状态", batch.status, "不会自动进入因子流程")
    with cards[2]:
        metric_card(st, "覆盖缺口", str(len(batch.gap_card_sha256)), "来自当前脱敏 gap report")
    with cards[3]:
        metric_card(st, "机制状态", "未验证", "结果出来前不改写事前叙事")

    st.warning(
        "阅读提示：同一个缺口可以承载多个不同机制；这不代表它们是同一个因子。"
        "后续每个假设的三个候选必须在结构上有实质差异，不能只更换时间窗口。"
    )

    section(st, "假设索引", "先扫一遍十个方向")
    rows = [
        {
            "槽位": draft.logical_slot_id,
            "事前主张": draft.prior_claim,
            "预期方向": _direction(draft.expected_direction),
            "缺口": "、".join(draft.gap_ids),
            "来源任务": str(len(draft.source_records)),
            "事件语义": (
                draft.semantic_plan.chinese_labels()["事件"]
                if draft.semantic_plan is not None
                else "未标注"
            ),
            "上下文语义": (
                draft.semantic_plan.chinese_labels()["上下文"]
                if draft.semantic_plan is not None
                else "未标注"
            ),
        }
        for draft in batch.drafts
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    section(st, "逐条阅读", "展开查看机制、竞争解释与证伪路径")
    for index, draft in enumerate(batch.drafts):
        title = f"{draft.logical_slot_id}  ·  {draft.prior_claim}"
        with st.expander(title, expanded=index == 0):
            st.caption(
                f"状态：待审批　|　机制：未验证　|　缺口：{'、'.join(draft.gap_ids)}　|　"
                f"预期方向：{_direction(draft.expected_direction)}"
            )
            st.markdown("**金融语义标签（非通过条件）**")
            st.write(
                draft.semantic_plan.chinese_labels()
                if draft.semantic_plan is not None
                else "未标注；不影响审批、候选生成或统一评价。"
            )
            left, right = st.columns(2)
            with left:
                st.markdown("**事前主张**")
                st.write(draft.prior_claim)
                st.markdown("**可能机制**")
                st.write(draft.mechanism)
                st.markdown("**可观测代理**")
                st.write(draft.observable_proxy)
                st.markdown("**机制验证方案（不改变统一回测协议）**")
                st.write(draft.independent_verification)
                st.caption(
                    "尚未执行；不改变统一回测区间、标签、交易时点或评价结构，"
                    "也不参与候选通过判定。"
                )
            with right:
                st.markdown("**竞争解释**")
                for value in draft.competing_explanations:
                    st.markdown(f"- {value}")
                st.markdown("**失效方式**")
                for value in draft.failure_modes:
                    st.markdown(f"- {value}")
                st.markdown("**证伪路径**")
                st.write(draft.falsification_path)

            st.markdown("**来源检索任务**")
            source_rows = [
                {
                    "任务编号": source.source_record_id,
                    "来源主张片段": source.claim_fragment,
                    "检索理由": source.rationale,
                    "关键词": "、".join(source.query_terms),
                    "年份": f"{source.year_start}–{source.year_end}",
                    "最多结果": source.result_limit,
                }
                for source in draft.source_records
            ]
            st.dataframe(source_rows, width="stretch", hide_index=True)

    with st.expander("研究边界与身份信息"):
        st.write("页面只读取服务器生成的结构化脱敏草案，不读取原始模型响应、原始行情、个股样本或回测结果。")
        st.write({
            "请求哈希": batch.request_sha256,
            "上下文哈希": batch.context_sha256,
            "gap report 哈希": batch.report_sha256,
            "草案批次哈希": batch.draft_batch_sha256,
        })


main()
