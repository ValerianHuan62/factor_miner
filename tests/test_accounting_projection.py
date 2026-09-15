"""最新记账报告不能混用历史收益或把未确定回收填成零。"""

from dashboard.accounting_projection import overlay_accounting_metrics


def test_unresolved_report_replaces_legacy_metrics_without_mutating_source():
    source = {"factor_id": "huan004", "annualized_return": 9.9}
    evaluation = dict.fromkeys(("valid_dates", "ic_mean", "rank_ic_mean", "ic_std",
        "rank_ic_std", "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t", "median_coverage"), 1)
    report = ("huan004", evaluation, {}, "unresolved", "resolved", "new_run", "today")
    row = overlay_accounting_metrics([source], [report])[0]
    assert row["annualized_return"] is None
    assert row["information_ratio"] is None
    assert row["ic_mean"] == 1
    assert row["run_id"] == "new_run"
    assert source["annualized_return"] == 9.9


def test_resolved_target_with_unknown_benchmark_has_no_information_ratio():
    evaluation = dict.fromkeys(("valid_dates", "ic_mean", "rank_ic_mean", "ic_std",
        "rank_ic_std", "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t", "median_coverage"), 1)
    performance = {"annualized_return": .1, "max_drawdown": .2, "sharpe": .3}
    report = ("huan004", evaluation, performance, "resolved", "unresolved", "new_run", "today")
    row = overlay_accounting_metrics([{"factor_id": "huan004"}], [report])[0]
    assert row["annualized_return"] == .1
    assert row["information_ratio"] is None
