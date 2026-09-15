"""阶段 B 受控操作台。

这是独立于只读结果 Dashboard 的操作界面。所有写操作都通过正式
``factor-miner pilot`` CLI 完成；页面不读取行情、不计算指标，也不把
DeepSeek 原始响应展示到浏览器。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def _required_env(name: str) -> Path:
    """读取必须显式提供的研究路径。"""

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少阶段 B 操作台环境变量：{name}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"阶段 B 操作台路径必须是绝对路径：{name}")
    return path


def _required_text_env(name: str) -> str:
    """读取必须显式提供的文本配置。"""

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少阶段 B 操作台环境变量：{name}")
    return value


def _optional_env(name: str) -> str | None:
    """读取可选文本配置。"""

    value = os.environ.get(name, "").strip()
    return value or None


def _latest_snapshot(stage_root: Path) -> dict[str, Any] | None:
    """读取一个阶段运行的最新不可变状态快照。"""

    snapshots = sorted((stage_root / "snapshots").glob("*.json"))
    if not snapshots:
        return None
    payload = json.loads(snapshots[-1].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"阶段 B 状态不是 JSON object：{snapshots[-1]}")
    return payload


def _available_runs(artifact_root: Path) -> list[tuple[str, dict[str, Any]]]:
    """列出阶段 B 状态，不读取任何运行结果文件。"""

    state_root = artifact_root / "state" / "pilot_stage_b"
    result: list[tuple[str, dict[str, Any]]] = []
    if not state_root.is_dir():
        return result
    for child in sorted(state_root.iterdir()):
        if not child.is_dir():
            continue
        payload = _latest_snapshot(child)
        if payload is not None:
            result.append((child.name, payload))
    return result


def _read_json(path: Path) -> dict[str, Any] | None:
    """只读取控制产物中的 JSON object。"""

    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _run_cli(args: list[str]) -> str:
    """通过当前 Python 环境调用正式 CLI，并只返回摘要输出。"""

    result = subprocess.run(
        [sys.executable, "-m", "factor_miner.cli", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"CLI 失败，退出码：{result.returncode}")
    return output


def _show_public_hypothesis(st: Any, payload: dict[str, Any]) -> None:
    """展示可人工审阅的假设字段。"""

    hypothesis = payload.get("hypothesis")
    if not isinstance(hypothesis, dict):
        return
    st.subheader("待审阅假设")
    left, right = st.columns(2)
    with left:
        st.write("主张", hypothesis.get("claim", ""))
        st.write("机制", hypothesis.get("mechanism", ""))
        st.write("预期方向", hypothesis.get("expected_sign", ""))
        st.write("可观察代理", hypothesis.get("observable_proxy", ""))
    with right:
        st.write(
            "机制验证方案（不改变统一回测协议）",
            hypothesis.get("independent_verification", ""),
        )
        st.write("竞争解释", hypothesis.get("competing_explanations", []))
        st.write("失效方式", hypothesis.get("failure_modes", []))
        st.write("证伪路径", hypothesis.get("falsification_path", ""))
    st.caption("机制状态固定为 mechanism_unverified；这不是因果结论。")


def main() -> None:
    """运行阶段 B 的生成、批准和 Pilot 操作台。"""

    try:
        import streamlit as st  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("运行操作台前请安装 Streamlit") from error

    st.set_page_config(page_title="Factor Miner 阶段 B 操作台", layout="wide")
    st.title("阶段 B：DeepSeek 假设 → 批准 → 三候选 → Pilot")
    st.caption(
        "操作台与只读结果 Dashboard 分离。所有真实数据、因子、IC 和回测均在显式配置的本地或远端研究环境执行。"
    )

    try:
        artifact_root = _required_env("FM_PILOT_STAGE_B_ROOT")
        policy_path = _required_env("FM_PILOT_POLICY_PATH")
        field_registry_path = _required_env("FM_PILOT_FIELD_REGISTRY_PATH")
        record_root = _required_env("FM_PILOT_RECORD_ROOT")
        hypothesis_request_path = _required_env("FM_PILOT_HYPOTHESIS_REQUEST_PATH")
        hypothesis_authorization_path = _required_env("FM_PILOT_HYPOTHESIS_AUTHORIZATION_PATH")
        hypothesis_response_path = _required_env("FM_PILOT_HYPOTHESIS_RESPONSE_PATH")
        approval_path = _required_env("FM_PILOT_APPROVAL_PATH")
        expression_request_path = _required_env("FM_PILOT_EXPRESSION_REQUEST_PATH")
        expression_authorization_path = _required_env("FM_PILOT_EXPRESSION_AUTHORIZATION_PATH")
        pilot_config_path = _required_env("FM_PILOT_CONFIG_PATH")
        approver_role = _required_text_env("FM_PILOT_APPROVER_ROLE")
    except RuntimeError as error:
        st.error(str(error))
        st.info("请先在当前研究环境中显式配置阶段 B 路径。")
        return

    runs = _available_runs(artifact_root)
    if not runs:
        st.info("尚未发现阶段 B 状态。先准备 hypothesis request。")
        st.code(
            "factor-miner pilot prepare-hypothesis <public_brief.json> "
            f"--artifact-root {artifact_root} --output {hypothesis_request_path}",
            language="bash",
        )
        return

    selected_id = st.selectbox(
        "阶段 B 运行",
        [item[0] for item in runs],
        index=len(runs) - 1,
    )
    state = dict(next(payload for run_id, payload in runs if run_id == selected_id))
    st.metric("当前状态", str(state.get("status", "unknown")))
    st.write(
        {
            "stage_run_id": state.get("stage_run_id"),
            "request_id": state.get("request_id"),
            "request_sha256": state.get("request_sha256"),
            "approval_id": state.get("approval_id"),
            "provider_call_id": state.get("provider_call_id"),
            "pilot_run_id": state.get("pilot_run_id"),
        }
    )

    hypothesis_response = _read_json(hypothesis_response_path)
    if hypothesis_response is not None:
        _show_public_hypothesis(st, hypothesis_response)

    st.subheader("外发授权闸门")
    if not hypothesis_authorization_path.is_file():
        hypothesis_request = _read_json(hypothesis_request_path) or {}
        st.warning("假设请求还没有精确外发授权；授权命令不会由页面自动生成。")
        st.code(
            "factor-miner llm authorize-export "
            f"{hypothesis_request_path} {policy_path} "
            f"--approved-request-sha256 {hypothesis_request.get('request_sha256', '<DeepSeek请求哈希>')} "
            f"--approver-role {approver_role} --output {hypothesis_authorization_path}",
            language="bash",
        )
    elif hypothesis_response is None:
        if st.button("生成假设（调用 DeepSeek）", type="primary"):
            with st.spinner("正在研究环境执行脱敏 DeepSeek 请求……"):
                st.code(
                    _run_cli(
                        [
                            "pilot",
                            "generate-hypothesis",
                            str(hypothesis_request_path),
                            str(policy_path),
                            str(hypothesis_authorization_path),
                            "--artifact-root",
                            str(artifact_root),
                            "--record-root",
                            str(record_root),
                            "--output",
                            str(hypothesis_response_path),
                        ]
                    ),
                    language="json",
                )
            st.rerun()
        else:
            st.info("精确外发授权已存在；点击按钮才会调用 DeepSeek。")

    if hypothesis_response is not None and not approval_path.is_file():
        st.subheader("人工批准闸门")
        st.warning("批准后才允许生成表达式；请先审阅主张、机制、方向、竞争解释和证伪路径。")
        if st.button("批准假设并冻结", type="primary"):
            with st.spinner("正在写入不可变批准记录……"):
                st.code(
                    _run_cli(
                        [
                            "pilot",
                            "approve-hypothesis",
                            str(hypothesis_response_path),
                            "--artifact-root",
                            str(artifact_root),
                            "--output",
                            str(approval_path),
                            "--approver-role",
                            approver_role,
                        ]
                    ),
                    language="json",
                )
            st.rerun()

    approval = _read_json(approval_path)
    if approval is not None and not expression_request_path.is_file():
        if st.button("准备三表达式请求"):
            with st.spinner("正在绑定字段能力、算子白名单和三个槽位……"):
                st.code(
                    _run_cli(
                        [
                            "pilot",
                            "prepare-expression",
                            str(approval_path),
                            str(field_registry_path),
                            "--artifact-root",
                            str(artifact_root),
                            "--output",
                            str(expression_request_path),
                        ]
                    ),
                    language="json",
                )
            st.rerun()

    if expression_request_path.is_file() and not expression_authorization_path.is_file():
        expression_request = _read_json(expression_request_path) or {}
        st.warning("三表达式请求还没有精确外发授权。")
        st.code(
            "factor-miner llm authorize-export "
            f"{expression_request_path} {policy_path} "
            f"--approved-request-sha256 {expression_request.get('request_sha256', '<DeepSeek请求哈希>')} "
            f"--approver-role {approver_role} --output {expression_authorization_path}",
            language="bash",
        )

    if (
        approval is not None
        and expression_request_path.is_file()
        and expression_authorization_path.is_file()
        and not state.get("pilot_run_id")
    ):
        st.subheader("表达式硬校验与阶段 A Pilot")
        st.info("本按钮会调用 DeepSeek 生成恰好三个表达式，随后在本地硬校验并在当前研究环境运行阶段 A。")
        if st.button("生成表达式并运行 Pilot", type="primary"):
            with st.spinner("正在生成、编译、计算 IC/组合并发布……"):
                output = _run_cli(
                    [
                        "pilot",
                        "run-approved",
                        str(expression_request_path),
                        str(approval_path),
                        str(policy_path),
                        str(expression_authorization_path),
                        str(field_registry_path),
                        str(pilot_config_path),
                        "--artifact-root",
                        str(artifact_root),
                        "--record-root",
                        str(record_root),
                    ]
                )
                st.code(output, language="json")
            st.rerun()


if __name__ == "__main__":
    main()
