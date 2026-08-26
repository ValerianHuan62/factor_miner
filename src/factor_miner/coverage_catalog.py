"""把现有生产因子 YAML 安全转换为 V0.4 结构节点。"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError
import yaml

from factor_miner.coverage_schema import CoverageFactorNode
from factor_miner.errors import FactorMinerError, FailureCode


_OPERATOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("abs", re.compile(r"\babs\s*\(", re.IGNORECASE)),
    ("corr", re.compile(r"\bcorr(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("cov", re.compile(r"\bcov(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("decay", re.compile(r"\bdecay(?:_\w+)?(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("delta", re.compile(r"\bdelta(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("log", re.compile(r"\blog\s*\(", re.IGNORECASE)),
    ("ma", re.compile(r"\bma(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("rank_cs", re.compile(r"\brank_cs\s*\(", re.IGNORECASE)),
    ("rank_ts", re.compile(r"\brank_ts(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("sign", re.compile(r"\bsign\s*\(", re.IGNORECASE)),
    ("std", re.compile(r"\bstd(?:_\d+)?\s*\(", re.IGNORECASE)),
    ("sum", re.compile(r"\bsum(?:_\d+)?\s*\(", re.IGNORECASE)),
)
_WINDOW_PATTERN = re.compile(
    r"\b(?:corr|cov|decay(?:_\w+)?|delta|ma|rank_ts|std|sum)_(\d+)\b",
    re.IGNORECASE,
)


def load_legacy_factor_catalog(path: Path) -> tuple[CoverageFactorNode, ...]:
    """使用 safe YAML 读取生产目录，不执行其中任何公式。"""

    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as error:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 YAML 无法读取：{source}",
        ) from error
    if not isinstance(payload, list):
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "生产因子目录必须是 YAML list",
        )
    nodes: list[CoverageFactorNode] = []
    seen: set[str] = set()
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"生产因子 YAML 第 {index + 1} 项必须是 mapping",
            )
        status = str(record.get("status", "active")).strip().lower()
        if status not in {"active", "inactive"}:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"生产因子状态不受支持：{status}",
            )
        factor_id = _required_text(record, "factor_id", index)
        if factor_id in seen:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"生产因子 ID 重复：{factor_id}",
            )
        seen.add(factor_id)
        nodes.append(_legacy_node(record, index, source.name, status))
    if not nodes:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            "生产因子 YAML 不能为空",
        )
    return tuple(sorted(nodes, key=lambda item: item.factor_id))


def _legacy_node(
    record: dict[str, Any],
    index: int,
    source_name: str,
    status: str,
) -> CoverageFactorNode:
    """将一条自由文本目录记录转换成不可执行结构元数据。"""

    formula = " ".join(_required_text(record, "formula_expr", index).split())
    fields_raw = record.get("input_fields")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 YAML 第 {index + 1} 项 input_fields 必须是非空 list",
        )
    fields = tuple(sorted({_nonempty_item(value, "input_fields") for value in fields_raw}))
    mean_ic = _optional_finite(record.get("mean_ic"), "mean_ic")
    icir = _optional_finite(record.get("icir"), "icir")
    orientation = -1 if mean_ic is not None and mean_ic < 0 else 1
    windows = set(int(value) for value in _WINDOW_PATTERN.findall(formula))
    param_n = record.get("param_n")
    if param_n is not None:
        try:
            parsed_param = int(param_n)
        except (TypeError, ValueError) as error:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                f"生产因子 param_n 非法：{param_n}",
            ) from error
        if parsed_param <= 0:
            raise FactorMinerError(
                FailureCode.SPEC_SCHEMA_INVALID,
                "生产因子 param_n 必须为正整数",
            )
        windows.add(parsed_param)
    try:
        return CoverageFactorNode(
            factor_id=_required_text(record, "factor_id", index),
            factor_name_cn=_required_text(record, "factor_name_cn", index),
            node_status=status,
            structure_source="legacy_formula_metadata",
            formula_expr=formula,
            formula_hash=hashlib.sha256(formula.encode("utf-8")).hexdigest(),
            ast_hash=None,
            input_fields=fields,
            operator_tags=tuple(
                name for name, pattern in _OPERATOR_PATTERNS if pattern.search(formula)
            ),
            windows=tuple(sorted(windows)),
            lookback_window=_nonnegative_int(record, "lookback_window", index),
            lag_days=_nonnegative_int(record, "lag_days", index),
            category=_required_text(record, "category", index),
            subcategory=_required_text(record, "subcategory", index),
            description=_required_text(record, "description", index),
            preprocess_method=_required_text(record, "preprocess_method", index),
            neutralization=_required_text(record, "neutralization", index),
            orientation_sign=orientation,
            orientation_source="legacy_visible_mean_ic",
            legacy_mean_ic=mean_ic,
            legacy_icir=icir,
            source=source_name,
        )
    except ValidationError as error:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 YAML 第 {index + 1} 项合同非法",
        ) from error


def _required_text(record: dict[str, Any], key: str, index: int) -> str:
    """读取不可为空的 YAML 文本。"""

    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 YAML 第 {index + 1} 项缺少 {key}",
        )
    return value.strip()


def _nonempty_item(value: Any, field: str) -> str:
    """把序列项规范化为非空文本。"""

    text = str(value).strip()
    if not text:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 {field} 不能包含空项",
        )
    return text


def _nonnegative_int(record: dict[str, Any], key: str, index: int) -> int:
    """读取非负整数。"""

    try:
        value = int(record.get(key))
    except (TypeError, ValueError) as error:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 YAML 第 {index + 1} 项 {key} 非法",
        ) from error
    if value < 0:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 {key} 不能为负数",
        )
    return value


def _optional_finite(value: Any, key: str) -> float | None:
    """读取可空有限历史指标。"""

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 {key} 必须是数值",
        ) from error
    if not math.isfinite(result):
        raise FactorMinerError(
            FailureCode.SPEC_SCHEMA_INVALID,
            f"生产因子 {key} 必须是有限数",
        )
    return result
