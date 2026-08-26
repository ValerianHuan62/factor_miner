"""阶段 A 固定候选运行的只读 Dashboard 投影。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from factor_miner.dashboard_projection import (
    DashboardSnapshot,
    _read_candidate_definitions,
)
from factor_miner.dashboard_store import DashboardStore
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_artifacts import PublishedRunManifest, verify_published_run


_REQUIRED_PILOT_JSON = (
    "run/metrics.json",
    "run/input_manifest.json",
    "ic/diagnostics.json",
    "portfolio/daily.json",
    "portfolio/metrics.json",
    "barra/attribution.json",
    "visualization/pilot.json",
)


def _projection_error(message: str) -> FactorMinerError:
    """构造投影合同错误。"""

    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)


def _read_referenced_json(
    root: Path,
    manifest: PublishedRunManifest,
    relative_path: str,
) -> dict[str, Any]:
    """只读取已由运行清单引用的 JSON object。"""

    if relative_path not in {item.relative_path for item in manifest.artifacts}:
        raise _projection_error(f"Pilot 清单缺少引用：{relative_path}")
    path = root / relative_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _projection_error(f"Pilot JSON 产物无法解析：{relative_path}") from None
    if not isinstance(payload, dict):
        raise _projection_error(f"Pilot JSON 产物根节点不是 object：{relative_path}")
    return payload


def _candidate_metrics(run_metrics: dict[str, Any]) -> dict[str, object]:
    """将发布器的候选摘要数组转成数据库候选维度。"""

    summaries = run_metrics.get("candidate_summaries")
    if not isinstance(summaries, list):
        return run_metrics
    result: dict[str, object] = {}
    for summary in summaries:
        if not isinstance(summary, dict) or not isinstance(summary.get("candidate_id"), str):
            raise _projection_error("Pilot candidate_summaries 结构无效")
        candidate_id = str(summary["candidate_id"])
        result[candidate_id] = dict(summary)
    return result


def _candidate_map(payload: dict[str, Any], name: str) -> dict[str, object]:
    """提取候选维度并拒绝错误根结构。"""

    candidates = payload.get("candidates")
    if not isinstance(candidates, dict):
        raise _projection_error(f"{name} 缺少 candidates object")
    return {str(key): value for key, value in candidates.items()}


def _portfolio_daily(payload: dict[str, Any]) -> dict[str, object]:
    """提取组合日常收益长表。"""

    candidates = _candidate_map(payload, "portfolio/daily.json")
    result: dict[str, object] = {}
    for candidate_id, value in candidates.items():
        if not isinstance(value, dict) or not isinstance(value.get("daily"), list):
            raise _projection_error("portfolio/daily.json 候选结构无效")
        result[candidate_id] = value["daily"]
    return result


def _portfolio_metrics(payload: dict[str, Any]) -> dict[str, object]:
    """提取组合指标摘要。"""

    candidates = _candidate_map(payload, "portfolio/metrics.json")
    if any(not isinstance(value, dict) for value in candidates.values()):
        raise _projection_error("portfolio/metrics.json 候选结构无效")
    return candidates


def _barra_attribution(payload: dict[str, Any]) -> dict[str, object]:
    """将 Barra 可选包装转换为 Dashboard 归因或 not_available。"""

    candidates = _candidate_map(payload, "barra/attribution.json")
    result: dict[str, object] = {}
    for candidate_id, value in candidates.items():
        if not isinstance(value, dict):
            raise _projection_error("barra/attribution.json 候选结构无效")
        attribution = value.get("attribution")
        if attribution is not None:
            if not isinstance(attribution, dict):
                raise _projection_error("Barra attribution 结构无效")
            result[candidate_id] = attribution
        else:
            result[candidate_id] = {
                "status": value.get("status", "not_available"),
                "missing_inputs": value.get("missing_inputs", []),
                "identity": value.get("identity"),
                "reason": value.get("reason"),
            }
    return result


def project_pilot_run(
    artifact_root: Path,
    pilot_run_id: str,
    store: DashboardStore,
) -> DashboardSnapshot:
    """核验并投影已发布 Pilot；数据库失败不回写或删除本地产物。"""

    manifest = verify_published_run(artifact_root, pilot_run_id)
    run_root = artifact_root.expanduser().resolve(strict=False) / "artifacts" / "runs" / pilot_run_id
    referenced = {item.relative_path for item in manifest.artifacts}
    missing = set(_REQUIRED_PILOT_JSON).difference(referenced)
    if missing:
        raise _projection_error(f"Pilot 发布产物不完整：{sorted(missing)}")
    run_metrics = _read_referenced_json(run_root, manifest, "run/metrics.json")
    candidate_metrics = _candidate_metrics(run_metrics)
    candidate_definitions = _read_candidate_definitions(
        run_root,
        manifest,
        candidate_metrics,
    )
    _read_referenced_json(run_root, manifest, "run/input_manifest.json")
    ic = _candidate_map(
        _read_referenced_json(run_root, manifest, "ic/diagnostics.json"),
        "ic/diagnostics.json",
    )
    recent_ic = (
        _read_referenced_json(run_root, manifest, "ic/recent.json")
        if "ic/recent.json" in referenced else None
    )
    direction_decisions = (
        _read_referenced_json(run_root, manifest, "direction/decisions.json")
        if "direction/decisions.json" in referenced else None
    )
    portfolio_daily = _portfolio_daily(
        _read_referenced_json(run_root, manifest, "portfolio/daily.json")
    )
    portfolio_metrics = _portfolio_metrics(
        _read_referenced_json(run_root, manifest, "portfolio/metrics.json")
    )
    recent_portfolio_metrics = (
        _portfolio_metrics(_read_referenced_json(
            run_root, manifest, "portfolio/recent_metrics.json"
        )) if "portfolio/recent_metrics.json" in referenced else None
    )
    portfolio_governance = (
        _read_referenced_json(run_root, manifest, "portfolio/governance.json")
        if "portfolio/governance.json" in referenced else None
    )
    barra = _barra_attribution(
        _read_referenced_json(run_root, manifest, "barra/attribution.json")
    )
    _read_referenced_json(run_root, manifest, "visualization/pilot.json")
    snapshot = DashboardSnapshot.build(
        run_id=pilot_run_id,
        artifact_manifest_sha256=manifest.manifest_sha256,
        artifact_refs=tuple(item.model_dump(mode="json") for item in manifest.artifacts),
        candidate_metrics=candidate_metrics,
        candidate_definitions=candidate_definitions,
        portfolio_metrics=portfolio_metrics,
        recent_portfolio_metrics=recent_portfolio_metrics,
        portfolio_governance=portfolio_governance,
        portfolio_daily=portfolio_daily,
        ic_diagnostics=ic,
        recent_ic_diagnostics=recent_ic,
        direction_decisions=direction_decisions,
        barra_attribution=barra,
    )
    store.replace_run_snapshot(pilot_run_id, snapshot.model_dump(mode="json"))
    return snapshot
