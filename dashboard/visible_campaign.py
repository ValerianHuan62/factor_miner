"""把仅完成开发期 IC/HAC 的可见候选转换为 Dashboard 读模型行。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from factor_miner.dashboard_projection import _expression_details


STATUS_LABELS = {
    "visible_passed": "开发期可见通过",
    "visible_failed": "开发期统计未通过",
    "redundancy_failed": "开发期冗余未通过",
}


def _read_object(path: Path) -> dict[str, Any]:
    """读取一个 JSON 对象；缺失或结构异常时显式失败。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 必须是对象：{path}")
    return payload


def load_visible_campaign_rows(
    *,
    run_root: Path,
    candidate_state_root: Path,
    pre_registration_path: Path,
    horizon_days: int,
) -> tuple[str, tuple[dict[str, object], ...]]:
    """读取已发布 Smoke 运行，只产生实际存在的定义和 IC/HAC 字段。"""

    if horizon_days <= 0:
        raise ValueError("标签期限必须为正整数")
    manifest = _read_object(run_root / "run_manifest.json")
    run_id = str(manifest.get("run_id", ""))
    statuses = manifest.get("statuses")
    if not run_id.startswith("run_") or not isinstance(statuses, dict):
        raise ValueError("可见运行 manifest 缺少 run_id 或 statuses")
    pre_registration = _read_object(pre_registration_path)
    names = {
        str(item["candidate_id"]): str(item["name"])
        for item in pre_registration.get("candidates", ())
        if isinstance(item, dict) and item.get("candidate_id") and item.get("name")
    }
    evaluated_at = datetime.fromtimestamp(
        (run_root / "run_manifest.json").stat().st_mtime,
        tz=timezone.utc,
    )
    rows: list[dict[str, object]] = []
    for candidate_id in sorted(str(value) for value in statuses):
        candidate_root = run_root / "candidates" / candidate_id
        state = _read_object(candidate_state_root / f"{candidate_id}.json")
        evaluation = _read_object(candidate_root / "evaluation.json")
        inference = _read_object(candidate_root / "inference.json")
        package = _read_object(candidate_root / "candidate_package.json")
        if state.get("candidate_id") != candidate_id or package.get("candidate_id") != candidate_id:
            raise ValueError(f"候选身份不一致：{candidate_id}")
        spec = state.get("spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("hypothesis"), dict):
            raise ValueError(f"候选缺少冻结 Spec：{candidate_id}")
        hypothesis = spec["hypothesis"]
        expression = spec.get("expression")
        formula = str(_expression_details(expression).get("formula_text") or "未提供公式")
        rank_ic_mean = float(evaluation["mean_rank_ic"])
        expected_sign = str(hypothesis.get("expected_sign", "neutral"))
        discovered_direction = "正向" if rank_ic_mean >= 0 else "负向"
        hypothesis_direction = {"positive": "正向", "negative": "负向"}.get(
            expected_sign,
            "中性",
        )
        name = names.get(candidate_id, candidate_id)
        category = (
            "波动率" if "volatility" in name
            else "反转" if "reversal" in name
            else "动量" if "strength" in name or "momentum" in name
            else "未分类"
        )
        rows.append(
            {
                "source_candidate_id": candidate_id,
                "hypothesis": str(hypothesis.get("claim") or "尚未提供中文假设"),
                "mechanism": str(hypothesis.get("mechanism") or "机制尚未独立验证"),
                "formula": formula,
                "calculation": f"按公式 {formula} 计算；收盘观察，下一交易日开盘后使用。",
                "hypothesis_direction": hypothesis_direction,
                "discovered_direction": discovered_direction,
                "direction_relation": (
                    "与假设一致" if hypothesis_direction == discovered_direction else "与假设相反"
                ),
                "category": category,
                "trading_timing": "下一交易日开盘",
                "status": STATUS_LABELS.get(str(package.get("status")), "开发期评价未完成"),
                "evaluation_scope": "开发期 Smoke，仅完成 RankIC/HAC，未执行组合回测",
                "horizon_days": horizon_days,
                "valid_dates": int(evaluation["valid_dates"]),
                "coverage_mean": float(evaluation["median_coverage"]),
                "rank_ic_mean": rank_ic_mean,
                "rank_ic_std": float(evaluation["std_rank_ic"]),
                "rank_ic_ir": float(evaluation["icir"]),
                "rank_ic_hac_t": float(inference["t_value"]),
                "raw_p_value": float(inference["raw_p_value"]),
                "bonferroni_p_value": float(inference["bonferroni_p_value"]),
                "evaluated_at": evaluated_at,
            }
        )
    return run_id, tuple(rows)
