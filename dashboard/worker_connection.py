"""研究页对正式 CLI 的本地连接，不在浏览器请求中执行研究。"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from dashboard.market_profiles import profile_by_id


def research_root(market_id: str) -> Path | None:
    """控制目录与结果目录可以独立，但必须由当前市场显式指定。"""

    profile = profile_by_id(market_id)
    return (profile.research_root or profile.artifact_root) if profile else None


def worker_config_path(market_id: str) -> Path | None:
    """读取当前市场的生成服务配置，不使用其他市场的连接。"""

    configured = os.environ.get(f"FM_RESEARCH_CONFIG_{market_id.upper()}", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    profile = profile_by_id(market_id)
    return profile.worker_config_path if profile else None


def worker_running(root: Path) -> bool:
    """只检查正式 Worker 持有的文件锁，配置存在并不表示在线。"""

    lock = root / "state" / "autonomous_research" / "worker.lock"
    if not lock.is_file() or os.name == "nt":
        return False
    import fcntl

    with lock.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def submit_start_cli(root: Path, requested_at: datetime) -> dict[str, str]:
    """通过正式 CLI 原子登记批次，使用参数数组避免命令注入。"""

    result = subprocess.run(
        [sys.executable, "-m", "factor_miner.cli", "research", "start",
         "--artifact-root", str(root), "--requested-at", requested_at.isoformat()],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("批次未创建。请检查目录权限，以及是否已有等待启动的批次。")
    return json.loads(result.stdout)


def connect_worker(market_id: str) -> subprocess.Popen:
    """先验证配置与市场隔离，再启动正式 CLI；失败由设置页展示。"""

    from factor_miner.server_research_dependencies import load_server_research_config

    path = worker_config_path(market_id)
    root = research_root(market_id)
    if path is None or root is None:
        raise ValueError("尚未配置该市场的生成服务。")
    config = load_server_research_config(path)
    if config.artifact_root.resolve() != root.resolve():
        raise ValueError("生成服务与当前市场的批次目录不一致。")
    if config.dashboard_dsn_env != f"FM_DASHBOARD_DSN_{market_id.upper()}":
        raise ValueError("生成服务必须使用当前市场的独立数据库连接。")
    if worker_running(root):
        raise ValueError("生成服务已在线。")
    log_path = root / "state" / "autonomous_research" / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "factor_miner.cli", "research", "worker", "--config", str(path)],
            cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log,
            start_new_session=True,
        )
