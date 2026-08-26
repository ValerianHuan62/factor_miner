"""把已发布 Barra 包装结构收窄为页面可直接展示的中文读模型。"""

from __future__ import annotations

from typing import Any


def _rows(value: object) -> list[object]:
    """只展示最新信号日截面，避免把完整历史发送给浏览器。"""

    rows = value if isinstance(value, list) else []
    dates = [
        str(row.get("signal_date"))
        for row in rows
        if isinstance(row, dict) and row.get("signal_date")
    ]
    if not dates:
        return rows
    latest = max(dates)
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("signal_date")) == latest
    ]


def normalize_barra_view(value: object) -> dict[str, Any]:
    """兼容可用包装和不可用状态，不把缺失数据解释成零风险。"""

    wrapper = value if isinstance(value, dict) else {}
    attribution = wrapper.get("attribution")
    detail = attribution if isinstance(attribution, dict) else {}
    status = (
        str(detail.get("risk_decomposition_status"))
        if detail.get("risk_decomposition_status") is not None
        else str(wrapper.get("status", "unknown"))
    )
    return {
        "status": status,
        "exposure_summary": _rows(detail.get("exposure_summary")),
        "attribution": _rows(detail.get("attribution")),
        "risk_decomposition": _rows(detail.get("risk_decomposition")),
        "missing_inputs": _rows(wrapper.get("missing_inputs")),
        "reason": wrapper.get("reason"),
    }
