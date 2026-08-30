"""Dashboard 服务器端只读快照加载辅助。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from functools import lru_cache

from factor_miner.dashboard_projection import DashboardSnapshot, project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.errors import FactorMinerError, FailureCode
from dashboard.hypotheses import HypothesisDraftBatchView, load_hypothesis_draft_batch
from dashboard.pg_store import PostgresDashboardStore


_RUN_ID = re.compile(r"^run_[0-9a-f]{24}$")


def _artifact_layout(configured_root: Path) -> tuple[Path, Path]:
    """解析服务器私有根与其中的正式发布产物根。"""

    return configured_root, configured_root


def _latest_projected_run(private_root: Path, published_root: Path) -> str | None:
    """从 Worker 的正式投影摘要中选择最新且已发布的运行。"""

    runs_root = private_root / "state/autonomous_research/runs"
    if not runs_root.is_dir():
        return None
    projections = sorted(
        runs_root.glob("autrun_*/objects/projection.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for projection in projections:
        try:
            payload = json.loads(projection.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_id = payload.get("projected_run_id") if isinstance(payload, dict) else None
        if (
            isinstance(run_id, str)
            and _RUN_ID.fullmatch(run_id)
            and (
                published_root / "artifacts" / "runs" / run_id / "run_manifest.json"
            ).is_file()
        ):
            return run_id
    return None


@lru_cache(maxsize=2)
def _load_artifact_snapshot(artifact_root: str, run_id: str) -> DashboardSnapshot:
    """解析并缓存不可变正式产物，避免每个页面重复读取大文件。"""

    return project_run_artifacts(
        Path(artifact_root),
        run_id,
        InMemoryDashboardStore(),
    )


def _snapshot_source_ids(snapshot: DashboardSnapshot) -> set[str]:
    """收集当前快照实际出现的候选键，防止全库别名跨运行串号。"""

    result = set(snapshot.candidate_definitions)
    for value in (
        snapshot.candidate_metrics,
        snapshot.ic_diagnostics,
        snapshot.portfolio_metrics,
        snapshot.portfolio_daily,
        snapshot.barra_attribution,
    ):
        if not isinstance(value, dict):
            continue
        payload = value.get("candidates", value)
        if isinstance(payload, dict):
            result.update(str(item) for item in payload)
    return result


def _attach_factor_aliases(snapshot: DashboardSnapshot, dsn: str) -> DashboardSnapshot:
    """只附加当前快照已有候选的 huan 编号。"""

    aliases = PostgresDashboardStore(dsn).load_factor_aliases()
    sources = _snapshot_source_ids(snapshot)
    return snapshot.model_copy(
        update={"candidate_aliases": {key: value for key, value in aliases.items() if key in sources}}
    )


def load_server_snapshot() -> DashboardSnapshot:
    """从正式文件读取图表，并从 PostgreSQL 附加稳定业务编号。"""

    dsn = os.environ.get("FM_DASHBOARD_DSN", "").strip()
    if not dsn:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "完整 Dashboard 必须配置 FM_DASHBOARD_DSN",
        )
    configured_root = os.environ.get("FM_ARTIFACT_ROOT", "")
    configured_run_id = os.environ.get("FM_DASHBOARD_RUN_ID", "")
    if not configured_root:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "缺少服务器私有环境 FM_ARTIFACT_ROOT",
        )
    private_root, artifact_root = _artifact_layout(Path(configured_root))
    run_id = _latest_projected_run(private_root, artifact_root) or configured_run_id
    if not run_id:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "没有已投影运行，且未配置 FM_DASHBOARD_RUN_ID",
        )
    snapshot = _load_artifact_snapshot(str(artifact_root), run_id)
    return _attach_factor_aliases(snapshot, dsn)


def load_server_run_id() -> str:
    """只解析当前主运行身份，不读取任何大型研究产物。"""

    configured_root = os.environ.get("FM_ARTIFACT_ROOT", "")
    configured_run_id = os.environ.get("FM_DASHBOARD_RUN_ID", "")
    if not configured_root:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "缺少服务器私有环境 FM_ARTIFACT_ROOT",
        )
    private_root, artifact_root = _artifact_layout(Path(configured_root))
    run_id = _latest_projected_run(private_root, artifact_root) or configured_run_id
    if not _RUN_ID.fullmatch(run_id):
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前主运行 ID 无效",
        )
    return run_id


def _has_available_barra(snapshot: DashboardSnapshot) -> bool:
    """判断快照是否至少含一个真实可用的 Barra 候选。"""

    value = snapshot.barra_attribution
    payload = value.get("candidates", value) if isinstance(value, dict) else {}
    return any(
        isinstance(item, dict)
        and item.get("status") == "available"
        and isinstance(item.get("attribution"), dict)
        for item in payload.values()
    )


def load_server_barra_snapshot() -> DashboardSnapshot:
    """优先读取主运行；无归因时读取显式配置的最近 Barra 验证运行。"""

    fallback_run_id = os.environ.get("FM_DASHBOARD_BARRA_RUN_ID", "").strip()
    configured_root = os.environ.get("FM_ARTIFACT_ROOT", "").strip()
    current_run_id = load_server_run_id()
    current_barra = (
        Path(configured_root)
        / "artifacts"
        / "runs"
        / current_run_id
        / "barra"
        / "attribution.json"
    )
    if current_barra.is_file():
        current = load_server_snapshot()
        if _has_available_barra(current):
            return current
    if not _RUN_ID.fullmatch(fallback_run_id) or not configured_root:
        return load_server_snapshot()
    fallback = _load_artifact_snapshot(configured_root, fallback_run_id)
    dsn = os.environ.get("FM_DASHBOARD_DSN", "").strip()
    if not dsn:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "完整 Dashboard 必须配置 FM_DASHBOARD_DSN",
        )
    return _attach_factor_aliases(fallback, dsn)


def load_server_hypothesis_batch() -> HypothesisDraftBatchView:
    """从服务器私有环境读取当前待审批的正式假设草案。"""

    path_text = os.environ.get("FM_DASHBOARD_HYPOTHESIS_PATH", "")
    if not path_text:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "缺少服务器私有环境 FM_DASHBOARD_HYPOTHESIS_PATH",
        )
    return load_hypothesis_draft_batch(Path(path_text))
