"""Dashboard 自主研究启动、全文审批与恢复运行台。"""

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Literal

import plotly.graph_objects as go
import streamlit as st

from dashboard.research_control import (
    build_review_decision,
    load_hypothesis_rows,
    load_semantic_coverage_view,
    submit_approve_remaining_and_freeze,
    submit_freeze_command,
    submit_resume_command,
    submit_review_decision,
    submit_start_command,
)
from dashboard.ui import (
    apply_research_cockpit_theme,
    apply_theme,
    research_status_header,
)
from factor_miner.lightweight_hypotheses import LightweightHypothesisDecision
from factor_miner.research_control import ResearchControlStore
from factor_miner.semantic_coverage import (
    CONTEXT_LABELS,
    DIRECTION_LABELS,
    EVENT_LABELS,
    OUTPUT_LABELS,
    QUALITY_LABELS,
    SemanticContext,
    SemanticEvent,
)


def _configuration() -> Path:
    """读取正式文件产物根目录；运行台不依赖数据库控制字段。"""

    artifact_root = os.environ.get("FM_ARTIFACT_ROOT", "").strip()
    if not artifact_root:
        raise RuntimeError("服务器尚未配置 FM_ARTIFACT_ROOT")
    return Path(artifact_root)


def _current_state(artifact_root: Path):
    """读取活动运行；若最近运行失败，仍允许用户续跑。"""

    control = ResearchControlStore(artifact_root)
    active = control.active_state()
    if active is not None:
        return active
    finished = []
    if control.runs_root.is_dir():
        for path in control.runs_root.iterdir():
            if not path.is_dir():
                continue
            state = control.load_state(path.name)
            finished.append(state)
    if not finished:
        return None
    return max(finished, key=lambda item: item.updated_at)


def _intent_time(key: str) -> datetime:
    """同一次按钮意图复用时间，使重复点击保持同一命令 ID。"""

    if key not in st.session_state:
        st.session_state[key] = datetime.now(timezone.utc)
    return st.session_state[key]


def _decision(
    row: dict[str, object],
    run_id: str,
    value: Literal["approved", "rejected"],
) -> LightweightHypothesisDecision:
    """从已投影且通过 Schema 的身份列重建审批决定。"""

    return build_review_decision(
        run_id=run_id,
        context_sha256=str(row["context_sha256"]),
        logical_slot_id=str(row["logical_slot_id"]),
        draft_sha256=str(row["draft_sha256"]),
        decision=value,
        approval_role="research_owner",
        decided_at=_intent_time(f"decision_time_{run_id}_{row['logical_slot_id']}"),
    )


def _show_detail(row: dict[str, object]) -> None:
    """逐字段展示完整正文，不截断假设内容。"""

    sections = (
        ("主张", row["claim_zh"]),
        ("机制", row["mechanism_zh"]),
        ("预期方向", row["expected_direction"]),
        ("可观察代理", row["observable_proxy_zh"]),
        (
            "机制验证方案（不改变统一回测协议）",
            row["independent_verification_zh"],
        ),
        ("竞争解释", row["competing_explanations_zh"]),
        ("失效方式", row["failure_modes_zh"]),
        ("证伪路径", row["falsification_path_zh"]),
        ("来源与边界", row["source_records_zh"]),
        ("金融语义标签", row["semantic_labels_zh"]),
    )
    for label, value in sections:
        st.markdown(f"#### {label}")
        if isinstance(value, (list, tuple)):
            for item in value:
                st.markdown(f"- {item}")
        else:
            st.write(value)


def _show_semantic_coverage(artifact_root: Path, rows: tuple[dict[str, object], ...]) -> None:
    """展示累计 E×C 热图与精确语义重复区域。"""

    coverage = load_semantic_coverage_view(artifact_root, rows)
    st.subheader("金融语义覆盖")
    st.caption(
        "只统计事前 E/C/Q/D/O 标签，不读取 IC、收益或候选通过状态；空白越多，"
        "表示该语义区域越少被明确提出，不代表那里一定存在 Alpha。"
    )
    metrics = st.columns(3)
    metrics[0].metric("已标注假设", coverage.tagged_hypothesis_count)
    metrics[1].metric("历史未标注", coverage.untagged_hypothesis_count)
    metrics[2].metric("精确重复组合", len(coverage.duplicate_regions))

    events = tuple(item.value for item in SemanticEvent)
    contexts = tuple(item.value for item in SemanticContext)
    figure = go.Figure(
        data=go.Heatmap(
            z=[
                [coverage.event_context_counts[event][context] for context in contexts]
                for event in events
            ],
            x=[CONTEXT_LABELS[item] for item in contexts],
            y=[EVENT_LABELS[item] for item in events],
            colorscale=[[0, "#0d1929"], [0.35, "#155a66"], [1, "#43d7c5"]],
            colorbar={"title": "假设数"},
            hovertemplate="事件=%{y}<br>上下文=%{x}<br>假设数=%{z}<extra></extra>",
        )
    )
    figure.update_layout(
        height=470,
        margin={"l": 20, "r": 20, "t": 20, "b": 80},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#e8f1fb"},
        xaxis={"tickangle": -35},
    )
    st.plotly_chart(figure, use_container_width=True)

    dense_cells = sorted(
        (
            {
                "事件": EVENT_LABELS[event],
                "上下文": CONTEXT_LABELS[context],
                "假设数": count,
            }
            for event, values in coverage.event_context_counts.items()
            for context, count in values.items()
            if count >= 2
        ),
        key=lambda item: (-int(item["假设数"]), str(item["事件"]), str(item["上下文"])),
    )
    exact_duplicates = []
    for region in coverage.duplicate_regions:
        plan = region.semantic_plan
        exact_duplicates.append(
            {
                "事件": EVENT_LABELS[plan.event_tag.value],
                "上下文": CONTEXT_LABELS[plan.context_tag.value],
                "质量条件": "、".join(QUALITY_LABELS[item.value] for item in plan.quality_tags) or "无",
                "方向语义": DIRECTION_LABELS[plan.direction_tag.value],
                "输出形式": OUTPUT_LABELS[plan.output_tag.value],
                "重复数": region.count,
            }
        )
    left, right = st.columns(2)
    with left:
        st.markdown("#### 高密度区域")
        st.dataframe(dense_cells, width="stretch", hide_index=True)
    with right:
        st.markdown("#### 完全相同的语义组合")
        st.dataframe(exact_duplicates, width="stretch", hide_index=True)


def _render_console(artifact_root: Path, state: object | None) -> None:
    """渲染一次运行台；状态读取和自动轮询由外层控制。"""

    if state is None:
        research_status_header(
            st,
            run_id="尚未创建",
            stage_label="空闲",
            decision_count=0,
        )
        st.info("点击后只创建研究命令；Worker 才会准备上下文并调用假设模型。")
        if st.button("新建研究批次", type="primary"):
            command = submit_start_command(
                artifact_root,
                requested_by="research_owner",
                requested_at=_intent_time("start_command_time"),
            )
            st.success(f"启动命令已进入队列：{command.command_id}")
            st.session_state["research_auto_refresh"] = True
            st.rerun()
        return

    run_id = state.run_id
    stage = state.stage.value
    rows = load_hypothesis_rows(artifact_root, run_id)
    decision_count = sum(
        row.get("decision") in {"approved", "rejected"} for row in rows
    )
    research_status_header(
        st,
        run_id=run_id,
        stage_label=state.stage_label,
        decision_count=decision_count,
    )
    if state.last_error:
        st.error(state.last_error)
    if stage == "completed":
        st.success("本批研究已完成评价、发布、数据库投影以及记忆与图谱刷新。")
        if st.button("新建下一批研究", type="primary"):
            command = submit_start_command(
                artifact_root,
                requested_by="research_owner",
                requested_at=_intent_time("start_command_time"),
            )
            st.success(f"启动命令已进入队列：{command.command_id}")
            st.session_state["research_auto_refresh"] = True
            st.rerun()
        return
    if stage == "failed":
        if st.button("从当前阶段继续", type="primary"):
            command = submit_resume_command(
                artifact_root,
                run_id=run_id,
                failed_state_sha256=state.state_sha256,
                requested_by="research_owner",
                requested_at=_intent_time(f"resume_time_{run_id}_{state.state_sha256}"),
            )
            st.success(f"继续命令已进入队列：{command.command_id}")
            st.session_state["research_auto_refresh"] = True
            st.rerun()

    if not rows:
        st.info("Worker 正在准备或生成假设，页面会在状态投影后显示 H01–H10。")
        return
    _show_semantic_coverage(artifact_root, rows)
    by_slot = {str(row["logical_slot_id"]): row for row in rows}
    left, center, right = st.columns((0.9, 2.4, 1.0), gap="large")
    with left:
        st.subheader("假设目录")
        selected = st.radio(
            "选择假设",
            tuple(by_slot),
            format_func=lambda slot: (
                f"{slot}  "
                + {"approved": "已批准", "rejected": "已拒绝"}.get(
                    str(by_slot[slot].get("decision")), "待决定"
                )
            ),
            label_visibility="collapsed",
        )
    row = by_slot[selected]
    with center:
        st.subheader(f"{selected} · 完整假设")
        st.caption(
            "事前叙事已冻结；所有候选共用同一套回测区间和评价结构；"
            "当前页面不读取候选结果。"
        )
        st.info(
            "机制验证方案尚未执行，只用于以后检验经济机制；"
            "它不会改变统一回测协议，也不参与主回测指标和候选通过判定。"
        )
        _show_detail(row)
    with right:
        st.subheader("审批")
        decisions = sum(item.get("decision") in {"approved", "rejected"} for item in rows)
        st.metric("已决定", f"{decisions}/10")
        st.write("观察时点：当日收盘后")
        st.write("最早交易：下一交易日开盘")
        if stage == "awaiting_review" and decisions < 10:
            st.info("快速模式会批准全部未决定项；已有批准或拒绝保持不变。")
            if st.button(
                "一键批准未决定项并开始挖掘",
                type="primary",
                use_container_width=True,
            ):
                commands, freeze = submit_approve_remaining_and_freeze(
                    artifact_root,
                    run_id=run_id,
                    rows=rows,
                    approval_role="research_owner",
                    requested_by="research_owner",
                    requested_at=_intent_time(f"bulk_approve_time_{run_id}"),
                )
                st.success(
                    f"已提交 {len(commands)} 条批量批准和冻结命令："
                    f"{freeze.command_id}"
                )

        with st.expander("逐条审批（可选）", expanded=False):
            if row.get("decision"):
                label = "已批准" if row["decision"] == "approved" else "已拒绝"
                st.info(f"本条状态：{label}")
            elif stage == "awaiting_review":
                if st.button("批准此假设", use_container_width=True):
                    decision = _decision(row, run_id, "approved")
                    command = submit_review_decision(
                        artifact_root,
                        decision=decision,
                        requested_by="research_owner",
                        requested_at=decision.decided_at,
                    )
                    st.success(f"批准命令已进入队列：{command.command_id}")
                if st.button("拒绝此假设", use_container_width=True):
                    decision = _decision(row, run_id, "rejected")
                    command = submit_review_decision(
                        artifact_root,
                        decision=decision,
                        requested_by="research_owner",
                        requested_at=decision.decided_at,
                    )
                    st.success(f"拒绝命令已进入队列：{command.command_id}")
        if decisions == 10 and stage == "awaiting_review":
            hashes = tuple(str(item["decision_sha256"]) for item in rows)
            if st.button("冻结十条审批并继续", type="primary", use_container_width=True):
                command = submit_freeze_command(
                    artifact_root,
                    run_id=run_id,
                    hypothesis_batch_sha256=str(rows[0]["hypothesis_batch_sha256"]),
                    decision_sha256=hashes,
                    requested_by="research_owner",
                    requested_at=_intent_time(f"freeze_time_{run_id}"),
                )
                st.success(f"冻结命令已进入队列：{command.command_id}")


@st.fragment(run_every="5s")
def _live_console(artifact_root: Path) -> None:
    """活动阶段每五秒重读状态；进入终态后退回静态页面。"""

    state = _current_state(artifact_root)
    pending = ResearchControlStore(artifact_root).pending_commands()
    if state is None and not pending:
        st.session_state.pop("research_auto_refresh", None)
        st.rerun()
    if (
        state is not None
        and state.stage.value in {"failed", "completed"}
        and not pending
    ):
        st.session_state.pop("research_auto_refresh", None)
        st.rerun()
    _render_console(artifact_root, state)


def main() -> None:
    """渲染自主研究运行台，并在活动阶段自动刷新。"""

    apply_theme(st, page_title="Factor Miner｜自主研究运行台")
    apply_research_cockpit_theme(st)
    try:
        artifact_root = _configuration()
        state = _current_state(artifact_root)
    except Exception as error:
        st.error(f"运行台配置或文件账本不可用：{error}")
        return

    auto_refresh = bool(st.session_state.get("research_auto_refresh"))
    if auto_refresh or (
        state is not None and state.stage.value not in {"failed", "completed"}
    ):
        _live_console(artifact_root)
    else:
        _render_console(artifact_root, state)


main()
