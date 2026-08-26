"""Dashboard 只读投影的存储端口与合成内存实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from factor_miner.errors import FactorMinerError, FailureCode


class DashboardStore(Protocol):
    """PostgreSQL 适配器必须实现的最小投影端口。"""

    def replace_run_snapshot(self, run_id: str, snapshot: dict[str, object]) -> None:
        """幂等写入已发布运行的只读摘要。"""

    def load_run_snapshot(self, run_id: str) -> dict[str, object] | None:
        """读取已投影运行；不能执行回测或改变账本。"""


@dataclass
class InMemoryDashboardStore:
    """Mac 合成测试使用的非生产内存投影。"""

    snapshots: dict[str, dict[str, object]] = field(default_factory=dict)
    write_count: int = 0

    def replace_run_snapshot(self, run_id: str, snapshot: dict[str, object]) -> None:
        """按摘要内容幂等替换。"""

        current = self.snapshots.get(run_id)
        if current is not None and current != snapshot:
            raise FactorMinerError(
                FailureCode.LEDGER_CORRUPT,
                "Dashboard 同一 run_id 的投影内容不一致",
            )
        self.snapshots[run_id] = dict(snapshot)
        self.write_count += 1

    def load_run_snapshot(self, run_id: str) -> dict[str, object] | None:
        """返回只读副本。"""

        value = self.snapshots.get(run_id)
        return None if value is None else dict(value)
