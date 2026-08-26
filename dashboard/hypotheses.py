"""Dashboard 假设草案的只读校验与展示模型。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.research_evolution import (
    LogicalEvolutionHypothesisDraft,
    validate_chinese_narrative,
)


_EXPECTED_SLOTS = tuple(f"H{index:02d}" for index in range(1, 11))
_PROVENANCE_FIELDS = (
    "request_sha256",
    "report_sha256",
    "gap_card_sha256",
    "context_sha256",
    "discovery_family_id",
)


@dataclass(frozen=True, slots=True)
class HypothesisDraftBatchView:
    """供 Dashboard 使用的十槽假设草案视图，不包含模型原始响应。"""

    request_sha256: str
    report_sha256: str
    gap_card_sha256: dict[str, str]
    context_sha256: str
    discovery_family_id: str
    draft_batch_sha256: str
    drafts: tuple[LogicalEvolutionHypothesisDraft, ...]
    source_path: Path
    status: str = "待审批"


def _error(message: str) -> FactorMinerError:
    """构造不暴露原始文件内容的 Dashboard 读取错误。"""

    return FactorMinerError(FailureCode.EVOLUTION_HASH_MISMATCH, message)


def _require_hash(value: object, *, field: str) -> str:
    """要求内容身份字段为小写 SHA-256。"""

    if not isinstance(value, str) or len(value) != 64:
        raise _error(f"假设批次 {field} 不是合法 SHA-256")
    try:
        int(value, 16)
    except ValueError:
        raise _error(f"假设批次 {field} 不是合法 SHA-256") from None
    return value


def _batch_identity(payload: dict[str, Any], drafts: list[dict[str, Any]]) -> dict[str, object]:
    """按正式生成器的规则重建草案批次身份。"""

    return {
        "request_sha256": payload.get("request_sha256"),
        "report_sha256": payload.get("report_sha256"),
        "gap_card_sha256": payload.get("gap_card_sha256"),
        "context_sha256": payload.get("context_sha256"),
        "discovery_family_id": payload.get("discovery_family_id"),
        "draft_sha256": [item.get("draft_sha256") for item in drafts],
    }


def load_hypothesis_draft_batch(path: Path) -> HypothesisDraftBatchView:
    """读取并核验服务器正式十槽草案，不读取模型原始响应。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _error("假设草案文件无法读取或不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise _error("假设草案文件根节点必须是 object")

    for field in _PROVENANCE_FIELDS:
        if field == "discovery_family_id":
            value = payload.get(field)
            if not isinstance(value, str) or not value.startswith("llmfamily_"):
                raise _error("假设批次 discovery_family_id 非法")
        elif field == "gap_card_sha256":
            continue
        else:
            _require_hash(payload.get(field), field=field)

    raw_drafts = payload.get("drafts")
    if not isinstance(raw_drafts, list) or len(raw_drafts) != 10:
        raise _error("假设草案必须正好包含 H01-H10 十个槽位")
    if any(not isinstance(item, dict) for item in raw_drafts):
        raise _error("假设草案条目必须是 object")
    draft_payloads = [item for item in raw_drafts if isinstance(item, dict)]
    try:
        drafts = tuple(
            LogicalEvolutionHypothesisDraft.model_validate(item)
            for item in draft_payloads
        )
    except ValueError:
        raise _error("假设草案条目未通过正式 Schema") from None
    try:
        validate_chinese_narrative(drafts)
    except FactorMinerError:
        raise _error("假设草案的自然语言叙事必须使用中文") from None
    slots = tuple(item.logical_slot_id for item in drafts)
    if slots != _EXPECTED_SLOTS:
        raise _error("假设草案必须按 H01-H10 顺序完整提供")
    if len({item.draft_sha256 for item in drafts}) != 10:
        raise _error("假设草案不能包含重复内容")

    gap_hashes = payload.get("gap_card_sha256")
    if not isinstance(gap_hashes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or len(value) != 64
        for key, value in gap_hashes.items()
    ):
        raise _error("假设批次 gap_card_sha256 非法")
    identity = _batch_identity(payload, draft_payloads)
    expected_batch_hash = sha256_json(identity)
    if payload.get("draft_batch_sha256") != expected_batch_hash:
        raise _error("假设批次 draft_batch_sha256 与内容不一致")

    return HypothesisDraftBatchView(
        request_sha256=str(payload["request_sha256"]),
        report_sha256=str(payload["report_sha256"]),
        gap_card_sha256={str(key): str(value) for key, value in gap_hashes.items()},
        context_sha256=str(payload["context_sha256"]),
        discovery_family_id=str(payload["discovery_family_id"]),
        draft_batch_sha256=expected_batch_hash,
        drafts=drafts,
        source_path=path,
    )
