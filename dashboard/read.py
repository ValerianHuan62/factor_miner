"""Dashboard 本地或远端只读快照加载辅助。"""

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
from dashboard.market_profiles import current_market_id, dashboard_dsn, load_market_profiles, profile_by_id
from dashboard.pg_store import PostgresDashboardStore


_RUN_ID = re.compile(r"^run_[0-9a-f]{24}$")


def _artifact_layout(configured_root: Path) -> tuple[Path, Path]:
    """解析研究根与其中的正式发布产物根。"""

    return configured_root, configured_root


def dashboard_artifact_root() -> Path | None:
    """解析当前市场产物根；跨市场时不接受无市场身份的全局覆盖。"""

    market_id = current_market_id()
    specific_key = f"FM_DASHBOARD_ARTIFACT_ROOT_{market_id.upper()}"
    configured = os.environ.get(specific_key, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    profile = profile_by_id(market_id)
    if profile is not None:
        return profile.artifact_root
    legacy = (
        os.environ.get("FM_DASHBOARD_ARTIFACT_ROOT", "").strip()
        or os.environ.get("FM_ARTIFACT_ROOT", "").strip()
    )
    if legacy:
        return Path(legacy).expanduser().resolve(strict=False)
    profiles = load_market_profiles()
    return profiles[0].artifact_root if profiles else None


def _configured_run_id(root: Path) -> str | None:
    """选择当前产物根中最新已发布运行或显式指定运行。"""

    market_id = current_market_id()
    configured = (
        os.environ.get(f"FM_DASHBOARD_RUN_ID_{market_id.upper()}", "").strip()
        or (os.environ.get("FM_DASHBOARD_RUN_ID", "").strip() if not load_market_profiles() else "")
    )
    return _latest_projected_run(root, root) or (configured if _RUN_ID.fullmatch(configured) else None)


def load_optional_snapshot() -> DashboardSnapshot | None:
    """没有本市场运行时返回空；已存在但损坏的产物仍按原错误暴露。"""

    root = dashboard_artifact_root()
    if root is None or _configured_run_id(root) is None:
        return None
    return load_server_snapshot()


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

    aliases = PostgresDashboardStore(dsn, market_id=current_market_id()).load_factor_aliases()
    sources = _snapshot_source_ids(snapshot)
    return snapshot.model_copy(
        update={"candidate_aliases": {key: value for key, value in aliases.items() if key in sources}}
    )


def load_server_snapshot() -> DashboardSnapshot:
    """从正式文件读取图表，并从 PostgreSQL 附加稳定业务编号。"""

    dsn = dashboard_dsn()
    if not dsn:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            f"当前市场必须配置独立的 FM_DASHBOARD_DSN_{current_market_id().upper()}",
        )
    artifact_root = dashboard_artifact_root()
    if artifact_root is None:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前市场尚未配置独立产物目录",
        )
    private_root, artifact_root = _artifact_layout(artifact_root)
    run_id = _configured_run_id(artifact_root)
    if not run_id:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前市场还没有已发布研究运行",
        )
    snapshot = _load_artifact_snapshot(str(artifact_root), run_id)
    return _attach_factor_aliases(snapshot, dsn)


def load_server_run_id() -> str:
    """只解析当前主运行身份，不读取任何大型研究产物。"""

    artifact_root = dashboard_artifact_root()
    if artifact_root is None:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前市场尚未配置独立产物目录",
        )
    private_root, artifact_root = _artifact_layout(artifact_root)
    run_id = _configured_run_id(artifact_root)
    if run_id is None:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前市场还没有已发布研究运行",
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

    market_id = current_market_id()
    fallback_key = f"FM_DASHBOARD_BARRA_RUN_ID_{market_id.upper()}"
    fallback_run_id = os.environ.get(fallback_key, "").strip()
    if not fallback_run_id and not load_market_profiles():
        fallback_run_id = os.environ.get("FM_DASHBOARD_BARRA_RUN_ID", "").strip()
    configured_path = dashboard_artifact_root()
    configured_root = str(configured_path) if configured_path is not None else ""
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
    dsn = dashboard_dsn()
    if not dsn:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            f"当前市场必须配置独立的 FM_DASHBOARD_DSN_{current_market_id().upper()}",
        )
    return _attach_factor_aliases(fallback, dsn)


def load_server_hypothesis_batch() -> HypothesisDraftBatchView:
    """从显式路径读取当前待审批的正式假设草案。"""

    market_id = current_market_id()
    path_text = (
        os.environ.get(f"FM_DASHBOARD_HYPOTHESIS_PATH_{market_id.upper()}", "")
        or (os.environ.get("FM_DASHBOARD_HYPOTHESIS_PATH", "") if not load_market_profiles() else "")
    )
    if not path_text:
        raise FactorMinerError(
            FailureCode.RUNTIME_BOUNDARY_ERROR,
            "当前市场还没有配置待审批假设文件 FM_DASHBOARD_HYPOTHESIS_PATH",
        )
    return load_hypothesis_draft_batch(Path(path_text))
