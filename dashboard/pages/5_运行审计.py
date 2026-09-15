"""运行审计页面。"""

import streamlit as st

from dashboard.read import load_optional_snapshot
from dashboard.ui import apply_theme, hero, metric_card, render_no_published_run, section


apply_theme(st, page_title="Factor Miner｜运行审计")
try:
    snapshot = load_optional_snapshot()
except Exception as error:
    st.error(str(error))
else:
    if snapshot is None:
        render_no_published_run(st, lens="RUN AUDIT")
        st.stop()
    hero(
        st,
        kicker="RUN AUDIT",
        title="可见结果背后的审计链",
        copy="审计页保留运行身份、发布清单和每个产物的哈希。这里是核验入口，不是研究结论页。",
        meta=f"运行 {snapshot.run_id}  ·  已发布快照 · 只读",
    )
    cards = st.columns(3)
    with cards[0]:
        metric_card(st, "发布状态", "已发布" if snapshot.published else "未发布", "未发布运行不可读取")
    with cards[1]:
        metric_card(st, "产物数量", str(len(snapshot.artifact_refs)), "清单内文件")
    with cards[2]:
        metric_card(st, "快照哈希", snapshot.snapshot_sha256[:12] + "…", "内容寻址身份")
    section(st, "产物清单", "每个文件都可核验")
    artifact_rows = []
    for artifact in snapshot.artifact_refs:
        artifact_rows.append({
            "产物": artifact.get("relative_path"),
            "大小": artifact.get("size_bytes"),
            "SHA-256 前缀": str(artifact.get("sha256", ""))[:16] + "…",
        })
    st.dataframe(artifact_rows, width="stretch", hide_index=True)
    with st.expander("查看运行身份（仅审计用途）"):
        st.write({
            "运行 ID": snapshot.run_id,
            "产物清单 SHA-256": snapshot.artifact_manifest_sha256,
            "Dashboard 快照 SHA-256": snapshot.snapshot_sha256,
        })
