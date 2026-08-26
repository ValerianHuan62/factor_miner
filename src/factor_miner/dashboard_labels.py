"""Dashboard 面向人的中文标签。"""

from __future__ import annotations


_LABELS: dict[tuple[str, str], str] = {
    ("expected_sign", "positive"): "正向",
    ("expected_sign", "negative"): "负向",
    ("availability", "next_open"): "当日收盘观察，下一交易日开盘使用",
    ("availability", "next_close"): "下一交易日收盘使用",
    ("mechanism_status", "mechanism_unverified"): "机制尚未独立验证",
    ("quality_status", "not_assessed"): "待评价",
    ("quality_status", "evaluated"): "评价完整",
    ("quality_status", "failed"): "计算失败",
    ("source_kind", "human"): "人工",
    ("source_kind", "deepseek"): "LLM",
    ("source_kind", "llm"): "LLM",
    ("source_kind", "shadow"): "确定性对照",
    ("status", "evaluated"): "评价完整",
    ("status", "redundant"): "高冗余",
    ("status", "failed"): "计算失败",
    ("status", "not_available"): "数据不可用",
    ("status", "available"): "可用",
}


def chinese_candidate_label(field: str, value: object) -> str:
    """翻译稳定描述值；未知技术值保持原样，避免猜测含义。"""

    if value is None:
        return "—"
    text = str(value)
    return _LABELS.get((field, text), text)
