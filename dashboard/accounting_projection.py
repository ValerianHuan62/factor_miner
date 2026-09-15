"""将明确估值口径的最新报告覆盖到展示行，不改写非空历史指标表。"""

from __future__ import annotations


def overlay_accounting_metrics(rows, reports):
    """未知回收收益返回 None；参考估值指标不得冒充实际投资表现。"""
    by_id = {row[0]: row for row in reports}
    result = []
    for source in rows:
        row = dict(source)
        report = by_id.get(row["factor_id"])
        if report is not None:
            _, evaluation, performance, target_status, benchmark_status, run_id, created_at = report
            row.update({key: evaluation[key] for key in (
                "valid_dates", "ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std",
                "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t",
            )})
            row.update(horizon_days=5, coverage_mean=evaluation["median_coverage"],
                       run_id=run_id, evaluated_at=created_at,
                       evaluation_scope="本次因果记账；旧 factor_metrics 行仅为历史结果",
                       has_portfolio=True)
            for key in ("annualized_return", "max_drawdown", "sharpe", "information_ratio"):
                unknown = target_status == "unresolved" or (key == "information_ratio" and benchmark_status == "unresolved")
                row[key] = None if unknown else performance[key]
        result.append(row)
    return result
