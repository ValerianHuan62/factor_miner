"""自主研究命令收件箱和单写入者状态 Store。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from factor_miner.autonomous_schema import (
    AutonomousResearchState,
    ResearchCommand,
    ResearchCommandType,
)
from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.ledger import _atomic_write_immutable


class ResearchControlStore:
    """以不可变 JSON 管理 Dashboard 命令和 Worker 状态。"""

    def __init__(self, artifact_root: Path) -> None:
        """固定自主研究状态根目录。"""

        root = artifact_root.expanduser().resolve(strict=False)
        self.root = root / "state" / "autonomous_research"
        self.inbox_root = self.root / "inbox"
        self.processed_root = self.root / "processed"
        self.runs_root = self.root / "runs"

    def submit(self, command: ResearchCommand) -> Path:
        """幂等提交命令；存在活动批次时拒绝新的启动命令。"""

        path = self.inbox_root / f"{command.command_id}.json"
        payload = canonical_json_bytes(command.model_dump(mode="json"))
        if path.is_file():
            return _atomic_write_immutable(path, payload)
        if (
            command.command_type is ResearchCommandType.START
            and self.active_state() is not None
        ):
            raise ValueError("已有活动研究批次，不能创建第二个批次")
        if command.command_type is ResearchCommandType.START and any(
            item.command_type is ResearchCommandType.START for item in self.pending_commands()
        ):
            raise ValueError("已有等待启动的研究批次，请先连接生成服务")
        return _atomic_write_immutable(path, payload)

    def pending_commands(self) -> tuple[ResearchCommand, ...]:
        """按请求时间和命令 ID 返回尚未处理的命令。"""

        if not self.inbox_root.is_dir():
            return ()
        commands = []
        for path in sorted(self.inbox_root.glob("researchcmd_*.json")):
            if (self.processed_root / path.name).is_file():
                continue
            commands.append(ResearchCommand.model_validate_json(path.read_bytes()))
        return tuple(sorted(commands, key=lambda item: (item.requested_at, item.command_id)))

    def mark_processed(self, command_id: str, *, processed_at: datetime) -> Path:
        """为已消费命令写入不可变回执。"""

        command_path = self.inbox_root / f"{command_id}.json"
        if not command_path.is_file():
            raise ValueError("待处理研究命令不存在")
        command = ResearchCommand.model_validate_json(command_path.read_bytes())
        if processed_at.tzinfo is None or processed_at.utcoffset() is None:
            raise ValueError("processed_at 必须带时区")
        payload = {
            "command_id": command.command_id,
            "command_sha256": command.command_sha256,
            "processed_at": processed_at.isoformat(),
        }
        payload["receipt_sha256"] = sha256_json(payload)
        return _atomic_write_immutable(
            self.processed_root / f"{command_id}.json",
            canonical_json_bytes(payload),
        )

    def publish_state(self, state: AutonomousResearchState) -> Path:
        """按连续 sequence 发布一个运行的新状态快照。"""

        snapshots = self.runs_root / state.run_id / "snapshots"
        path = snapshots / f"{state.sequence:020d}.json"
        payload = canonical_json_bytes(state.model_dump(mode="json"))
        if path.is_file():
            return _atomic_write_immutable(path, payload)
        latest = self._latest_state(state.run_id)
        if latest is None:
            if state.sequence != 0:
                raise ValueError("首个状态 sequence 必须为 0")
            active = self.active_state()
            if active is not None and active.run_id != state.run_id:
                raise ValueError("已有活动研究批次，不能发布第二个活动状态")
        else:
            if state.sequence != latest.sequence + 1:
                raise ValueError("状态 sequence 必须连续递增")
            if state.created_at != latest.created_at:
                raise ValueError("同一运行的 created_at 不得改变")
        return _atomic_write_immutable(path, payload)

    def load_state(self, run_id: str) -> AutonomousResearchState:
        """读取指定运行的最新状态。"""

        state = self._latest_state(run_id)
        if state is None:
            raise ValueError("自主研究运行不存在")
        return state

    def active_state(self) -> AutonomousResearchState | None:
        """返回唯一非终态运行；检测到多个时硬失败。"""

        if not self.runs_root.is_dir():
            return None
        active = []
        for child in sorted(self.runs_root.iterdir()):
            if not child.is_dir():
                continue
            latest = self._latest_state(child.name)
            if latest is not None and not latest.stage.terminal:
                active.append(latest)
        if len(active) > 1:
            raise ValueError("检测到多个活动研究批次")
        return active[0] if active else None

    def _latest_state(self, run_id: str) -> AutonomousResearchState | None:
        """读取单个运行最后一个规范快照。"""

        snapshots = self.runs_root / run_id / "snapshots"
        paths = sorted(snapshots.glob("*.json")) if snapshots.is_dir() else []
        if not paths:
            return None
        try:
            payload = json.loads(paths[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("自主研究状态文件损坏") from error
        return AutonomousResearchState.model_validate(payload)
