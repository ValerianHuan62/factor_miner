"""设置页只展示连接状态，技术配置按需展开。"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from dashboard.market_profiles import current_market_id, market_profiles_path, profile_by_id
from dashboard.results import reports_for_market
from dashboard.ui import apply_theme
from dashboard.worker_connection import connect_worker, research_root, worker_config_path, worker_running


def render_settings() -> None:
    """在一个页面说明结果读取与自动研究的独立就绪状态。"""

    apply_theme(st, page_title="Factor Miner｜设置")
    st.title("设置")
    market_id = current_market_id()
    profile = profile_by_id(market_id)
    if profile is None:
        st.info("尚未配置市场，请按项目说明添加数据与结果目录。")
        return
    root = research_root(market_id)
    online = worker_running(root) if root else False
    from dashboard.api_workbench import render_api_settings
    render_api_settings()
    with st.expander("历史批次服务与高级连接", expanded=False):
        st.subheader(f"{profile.display_name}数据与历史服务")
        try:
            reports = reports_for_market(market_id)
            st.success(f"研究结果：已连接 · {len(reports)} 份报告") if reports else st.info("研究结果：还没有已发布的回测报告")
        except (ValueError, OSError, KeyError):
            st.error("研究结果：无法读取，请检查高级连接中的结果目录。")
        path = worker_config_path(market_id)
        if online:
            st.success("生成服务：已启动")
        elif path is None:
            st.warning("历史批次服务：未配置。上方 API 研究入口可独立使用。")
            st.write("历史批次服务需要当前市场研究配置，连接模型、标准面板数据和冻结的评价协议。")
        else:
            st.info("生成服务：已配置，尚未启动")
            if st.button("连接生成服务", type="primary"):
                try:
                    process = connect_worker(market_id)
                    st.session_state[f"worker_process_{market_id}"] = process
                    st.info("正在连接，请刷新连接状态。")
                except (ValueError, OSError) as error:
                    st.error(f"未能连接：{error}")
        process = st.session_state.get(f"worker_process_{market_id}")
        if process is not None and process.poll() is not None:
            st.error("生成服务已退出。请检查研究配置和模型、数据库连接。")
        if st.button("刷新连接状态"):
            st.rerun()
        with st.expander("高级连接设置"):
            st.caption("这里只设置连接，不修改行情或研究协议。")
            configured = st.text_input("美股研究配置文件" if market_id == "us_equity" else "研究配置文件",
                                       value=str(path) if path else "", key=f"worker_path_{market_id}")
            if st.button("保存生成服务配置"):
                try:
                    from factor_miner.server_research_dependencies import ServerResearchConfig

                    config_path = Path(configured).expanduser().resolve()
                    config = ServerResearchConfig.model_validate_json(config_path.read_bytes())
                    if root is None or config.artifact_root.resolve() != root.resolve():
                        raise ValueError("研究配置的产物目录必须与当前市场的批次目录一致。")
                    if config.dashboard_dsn_env != f"FM_DASHBOARD_DSN_{market_id.upper()}":
                        raise ValueError("研究配置必须使用当前市场的独立数据库连接。")
                    source = market_profiles_path()
                    payload = json.loads(source.read_text())
                    for item in payload["markets"]:
                        if item["market_id"] == market_id:
                            item["worker_config_path"] = str(config_path)
                    from factor_miner.ledger import atomic_write_bytes

                    atomic_write_bytes(source, json.dumps(payload, ensure_ascii=False, indent=2).encode())
                    st.success("已保存，刷新连接状态后可以连接生成服务。")
                except (ValueError, OSError) as error:
                    st.error(f"配置未保存：{error}")
            st.text(f"批次目录：{root}")
            st.text("结果目录：" + "、".join(str(item) for item in profile.backtest_roots))
            st.caption("模型密钥与数据库连接使用启动环境配置，不在页面展示或保存。")
