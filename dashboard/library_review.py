"""把显式配置的复核清单叠加为读模型，保留原报告身份与统计结论。"""

import hashlib
import json
from pathlib import Path


def apply_library_review(reports: list[dict], path: Path, market_id: str) -> list[dict]:
    """拒绝跨市场、缺失因子或报告内容变化后的陈旧筛选清单。"""
    review = json.loads(path.read_text(encoding="utf-8"))
    if review.get("schema_version") != "library-review-v1" or review.get("market_id") != market_id:
        raise ValueError("复核清单版本或市场不匹配")
    entries = review["by_factor"]
    missing = set(entries) - {r["factor_id"] for r in reports}
    if missing:
        raise ValueError(f"复核清单引用的因子不在当前结果目录：{sorted(missing)}")
    result = []
    for report in reports:
        entry = entries.get(report["factor_id"])
        if entry is None:
            result.append(report)
            continue
        source = Path(report["report_batch"]) / f"{report['factor_id']}.json"
        if (entry["source_candidate_id"] != report.get("source_candidate_id")
                or hashlib.sha256(source.read_bytes()).hexdigest() != entry["report_sha256"]):
            raise ValueError(f"{report['factor_id']} 的复核与原始报告身份不一致")
        result.append({**report, "library_review": entry, "library_summary": review["message"],
                       "library_default_view": review.get("default_view")})
    return result


def apply_research_pool(reports: list[dict], path: Path, market_id: str) -> list[dict]:
    """独立叠加组合研究资格；原单因子结论仍显示并保持原值。"""
    pool = json.loads(path.read_text())
    if pool.get('schema_version') != 'research-pool-dashboard-v1' or pool.get('market_id') != market_id:
        raise ValueError('组合研究池版本或市场不一致')
    if set(pool['by_factor']) - {r['factor_id'] for r in reports}:
        raise ValueError('组合研究池引用缺失报告')
    result = []
    for report in reports:
        entry = pool['by_factor'].get(report['factor_id'])
        if entry is None:
            result.append(report)
            continue
        source = Path(report['report_batch'])/f"{report['factor_id']}.json"
        if (entry['source_candidate_id'] != report['source_candidate_id']
                or hashlib.sha256(source.read_bytes()).hexdigest() != entry['report_sha256']):
            raise ValueError('组合研究池与原报告身份不符')
        result.append({**report, 'research_pool': entry, 'research_pool_summary': pool['message']})
    return result
