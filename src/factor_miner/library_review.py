"""既有候选的事后分层复核，不修改假设方向或原始评价结果。"""

from __future__ import annotations

import math


def review_candidate(report: dict, gates: dict) -> dict:
    """区分统计失败、构念待补与可去重候选；收益不充当模型输入闸门。"""
    reasons = []
    sign = {"positive": 1, "negative": -1}[report["hypothesis_direction"]]
    for field, label in (("discovery", "发现期"), ("evaluation", "确认期")):
        metrics = report[field]
        for key in ("rank_ic_mean", "bonferroni_p_value", "median_coverage"):
            value = metrics.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{report['factor_id']} {label}缺少有效 {key}")
        if metrics["rank_ic_mean"] * sign <= 0:
            reasons.append(f"{label}方向与原假设相反；不事后翻转")
        elif metrics["rank_ic_mean"] * sign < gates["each_period_abs_rank_ic_min"]:
            reasons.append(f"{label} RankIC 未达到 {gates['each_period_abs_rank_ic_min']}")
        if metrics["bonferroni_p_value"] >= gates["each_period_bonferroni_p_max_exclusive"]:
            reasons.append(f"{label}未通过原 {gates['family_size']} 槽多重检验")
        if metrics["median_coverage"] < gates["each_period_coverage_min"]:
            reasons.append(f"{label}覆盖率不足")
    statistical_pass = not reasons
    construct = report["construct_validation"]["status"]
    if construct != gates["construct_status"]:
        reasons.append(f"公式基础检验：{construct}；不能视为机制已经验证")
    status = ("eligible" if construct == gates["construct_status"] else "watch") if statistical_pass else "rejected"
    if construct == "偏离":
        status = "rejected"
    return {"source_candidate_id": report["candidate_id"], "status": status,
            "statistical_pass": statistical_pass, "reasons": reasons,
            "mechanism_status": "mechanism_unverified", "sealed_oos": False}


def select_representatives(reports: list[dict], reviews: dict, pairs: dict, threshold: float) -> list[str]:
    """按发现期强度挑代表；同假设或与已选代表高相关的候选留作替补。"""
    ordered = sorted((r for r in reports if reviews[r["factor_id"]]["status"] == "eligible"),
                     key=lambda r: (-abs(r["discovery"]["rank_ic_hac_t"]), r["slot"]))
    selected = []
    for report in ordered:
        fid = report["factor_id"]
        duplicate = None
        for prior in selected:
            pair = pairs[tuple(sorted((fid, prior["factor_id"])))]
            if pair["valid_dates"] < 60 or pair["p95_abs_spearman"] is None:
                raise ValueError("发现期重叠不足，不能宣称低相关")
            if report["hypothesis_id"] == prior["hypothesis_id"] or pair["p95_abs_spearman"] >= threshold:
                duplicate = prior["factor_id"]
                break
        if duplicate:
            reviews[fid].update(status="reserve", representative=duplicate)
            reviews[fid]["reasons"].append(f"与 {duplicate} 同假设或高度相关，保留档案作为替补")
        else:
            reviews[fid]["status"] = "retained"
            selected.append(report)
    labels = {"retained": "保留基底", "reserve": "同类替补", "watch": "观察池", "rejected": "未保留"}
    for review in reviews.values():
        review["label"] = labels[review["status"]]
    return [report["factor_id"] for report in selected]
