"""价格异常不允许静默删除，必须进入诊断记录。"""

from datetime import date
import polars as pl
from factor_miner.report_data_quality import audit_price_continuity


def test_price_jump_is_retained_as_review_evidence():
    frame = pl.DataFrame({"asset": ["A", "A"], "date": [date(2026,2,5), date(2026,2,6)], "open": [.0051, 145.26]})
    result = audit_price_continuity(frame.lazy())
    assert result["status"] == "review_required"
    assert result["anomaly_count"] == 1
    assert result["anomalies"][0]["price"] == 145.26
    assert frame.height == 2


def test_quarantined_prices_and_verified_losses_do_not_trigger_false_gate():
    """上游隔离坏价和精确裁决真实损失，报告必须正确区分。"""
    frame = pl.DataFrame({"asset": ["A", "A"], "date": [date(2026,2,5), date(2026,2,6)], "open": [10., .9]})
    state = frame.select("date", "asset").with_columns(*(pl.lit(False).alias(k) for k in ("valid_for_factor_compute", "valid_for_factor_rank", "can_open_long", "can_close_long", "valid_for_o2o_label")))
    assert audit_price_continuity(frame.lazy(), state.lazy())["eligible_unreviewed_count"] == 0
    reviewed = [{"security_id": "A", "session_date": "2026-02-06", "open_ratio": .09}]
    assert audit_price_continuity(frame.lazy(), accepted_jumps=reviewed)["eligible_unreviewed_count"] == 0
    reviewed[0]["open_ratio"] = .08
    assert audit_price_continuity(frame.lazy(), accepted_jumps=reviewed)["eligible_unreviewed_count"] == 1
