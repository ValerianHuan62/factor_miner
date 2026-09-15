"""因子探索页的纯数据变换函数。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import csv
from io import StringIO
from typing import Any


SEARCH_FIELDS = ("factor_id", "hypothesis", "mechanism", "formula", "calculation", "expected_regime_label", "failure_condition_summary")


def text_values(rows: Iterable[dict[str, Any]], field: str) -> list[str]:
    """提取稳定排序的非空文本枚举。"""

    return sorted(
        {
            str(value).strip()
            for row in rows
            if (value := row.get(field)) is not None and str(value).strip()
        }
    )


def filter_factor_rows(
    rows: Iterable[dict[str, Any]],
    *,
    query: str = "",
    directions: Sequence[str] = (),
    relations: Sequence[str] = (),
    statuses: Sequence[str] = (),
    regime_labels: Sequence[str] = (),
    regime_statuses: Sequence[str] = (),
    regime_directions: Sequence[str] = (),
    minimum_valid_dates: int = 0,
    minimum_abs_hac_t: float = 0.0,
) -> list[dict[str, Any]]:
    """按研究定义和聚合指标筛选，不重算或改写任何指标。"""

    needle = query.strip().casefold()
    direction_set = set(directions)
    relation_set = set(relations)
    status_set = set(statuses)
    result: list[dict[str, Any]] = []
    for row in rows:
        if needle and not any(
            needle in str(row.get(field, "")).casefold() for field in SEARCH_FIELDS
        ):
            continue
        if direction_set and row.get("discovered_direction") not in direction_set:
            continue
        if relation_set and row.get("direction_relation") not in relation_set:
            continue
        if status_set and row.get("status") not in status_set:
            continue
        if regime_labels and row.get('expected_regime_label') not in regime_labels:
            continue
        if regime_statuses and row.get('regime_validation_status') not in regime_statuses:
            continue
        if regime_directions and row.get('regime_direction') not in regime_directions:
            continue
        valid_dates = row.get("valid_dates")
        if not isinstance(valid_dates, int) or valid_dates < minimum_valid_dates:
            continue
        hac_t = row.get("rank_ic_hac_t")
        if not isinstance(hac_t, (int, float)) or abs(float(hac_t)) < minimum_abs_hac_t:
            continue
        result.append(dict(row))
    return result


def sort_factor_rows(
    rows: Iterable[dict[str, Any]],
    *,
    field: str,
    descending: bool,
) -> list[dict[str, Any]]:
    """按显式字段排序；空值始终置后。"""

    values = [dict(row) for row in rows]
    populated = [row for row in values if row.get(field) is not None]
    missing = [row for row in values if row.get(field) is None]
    populated.sort(key=lambda row: row[field], reverse=descending)
    return populated + missing


def factor_rows_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    """把当前筛选结果编码为 UTF-8 CSV，不额外推导研究字段。"""

    values = [dict(row) for row in rows]
    if not values:
        return b""
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(values[0]))
    writer.writeheader()
    writer.writerows(values)
    return output.getvalue().encode("utf-8-sig")
