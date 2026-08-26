"""DeepSeek 固定传输、不可变调用录制与离线重放。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.llm_online import (
    DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
    DEEPSEEK_MODEL,
    LLMCampaignScopeAuthorization,
    LLMExportAuthorization,
    PreparedDeepSeekRequest,
    verify_export_authorization,
    verify_scope_export_authorization,
)
from factor_miner.llm_privacy import CorporateExternalResearchPolicy


class DeepSeekTransport(Protocol):
    """可替换的最小 HTTP 传输端口。"""

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        """向固定 DeepSeek endpoint 发送请求并返回原始响应字节。"""


class UrllibDeepSeekTransport:
    """只访问固定官方 endpoint 的标准库 HTTP 实现。"""

    def post(self, request_bytes: bytes, api_key: str) -> bytes:
        """发送 JSON 请求；错误信息不包含 header、密钥或响应正文。"""

        request = Request(
            DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
            data=request_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except HTTPError as error:
            raise FactorMinerError(
                FailureCode.LLM_PROVIDER_UNAVAILABLE,
                f"DeepSeek HTTP 请求失败，状态码 {error.code}",
            ) from None
        except (URLError, TimeoutError, OSError):
            raise FactorMinerError(
                FailureCode.LLM_PROVIDER_UNAVAILABLE,
                "DeepSeek 网络请求失败",
            ) from None


class LLMCallRecord(BaseModel):
    """不含密钥和隐藏推理内容的调用审计清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(pattern=r"^llmcall_[0-9a-f]{24}$")
    campaign_id: str
    agent_role: str
    slot_ids: tuple[str, ...]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    system_fingerprint: str | None
    finish_reason: str
    usage: dict[str, int]
    completed_at: datetime


class ProviderToolCall(BaseModel):
    """DeepSeek 返回且已经过固定工具名与 JSON 参数校验的调用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecordedLLMResponse:
    """已录制响应的审计记录、最终 JSON 内容和本地目录。"""

    record: LLMCallRecord
    content_json: dict[str, Any] | None
    tool_calls: tuple[ProviderToolCall, ...]
    assistant_message: dict[str, Any]
    record_directory: Path


def _normalized_usage(value: Any) -> dict[str, int]:
    """只保留顶层整数 token 计数，兼容供应商嵌套详情字段。"""

    if not isinstance(value, dict):
        raise TypeError
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, int) and not isinstance(item, bool)
    }


def _response_parts(
    response_bytes: bytes,
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    tuple[ProviderToolCall, ...],
    dict[str, Any],
]:
    """解析最终 JSON 或唯一批准的文献工具调用，失败时不猜测。"""

    try:
        envelope = json.loads(response_bytes)
        if not isinstance(envelope, dict):
            raise TypeError
        choices = envelope["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise TypeError
        choice = choices[0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError
        content = message.get("content")
        raw_tool_calls = message.get("tool_calls", [])
        content_json: dict[str, Any] | None = None
        tool_calls: list[ProviderToolCall] = []
        if isinstance(content, str) and content.strip():
            content_json = json.loads(content)
            if not isinstance(content_json, dict) or raw_tool_calls:
                raise TypeError
        elif isinstance(raw_tool_calls, list) and raw_tool_calls:
            for raw_call in raw_tool_calls:
                function = raw_call["function"]
                if (
                    raw_call.get("type") != "function"
                    or function.get("name") != "search_literature"
                ):
                    raise TypeError
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise TypeError
                tool_calls.append(
                    ProviderToolCall(
                        tool_call_id=raw_call["id"],
                        name=function["name"],
                        arguments=arguments,
                    )
                )
        else:
            raise TypeError
        finish_reason = choice["finish_reason"]
        if not isinstance(finish_reason, str) or not finish_reason:
            raise TypeError
        usage = _normalized_usage(envelope.get("usage", {}))
        if any(value < 0 for value in usage.values()):
            raise TypeError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise FactorMinerError(
            FailureCode.LLM_RESPONSE_INVALID,
            "DeepSeek 响应不是有效的单选项 JSON object 或文献工具调用",
        ) from None
    return envelope, content_json, tuple(tool_calls), message


def _build_record(
    *,
    prepared: PreparedDeepSeekRequest,
    response_bytes: bytes,
    envelope: dict[str, Any],
    completed_at: datetime,
) -> LLMCallRecord:
    """从已验证 envelope 构造内容寻址调用清单。"""

    response_hash = sha256(response_bytes).hexdigest()
    call_hash = sha256_json(
        {
            "campaign_id": prepared.campaign_id,
            "request_sha256": prepared.request_sha256,
            "response_sha256": response_hash,
        }
    )
    choice = envelope["choices"][0]
    return LLMCallRecord(
        call_id=f"llmcall_{call_hash[:24]}",
        campaign_id=prepared.campaign_id,
        agent_role=prepared.agent_role,
        slot_ids=prepared.slot_ids,
        request_sha256=prepared.request_sha256,
        response_sha256=response_hash,
        model=DEEPSEEK_MODEL,
        system_fingerprint=envelope.get("system_fingerprint"),
        finish_reason=choice["finish_reason"],
        usage=_normalized_usage(envelope.get("usage", {})),
        completed_at=completed_at,
    )


def _build_invalid_response_record(
    *,
    prepared: PreparedDeepSeekRequest,
    response_bytes: bytes,
    completed_at: datetime,
) -> LLMCallRecord:
    """为无法解析的响应建立可审计、不可当作结果的记录。"""

    response_hash = sha256(response_bytes).hexdigest()
    call_hash = sha256_json(
        {
            "campaign_id": prepared.campaign_id,
            "request_sha256": prepared.request_sha256,
            "response_sha256": response_hash,
        }
    )
    return LLMCallRecord(
        call_id=f"llmcall_{call_hash[:24]}",
        campaign_id=prepared.campaign_id,
        agent_role=prepared.agent_role,
        slot_ids=prepared.slot_ids,
        request_sha256=prepared.request_sha256,
        response_sha256=response_hash,
        model=DEEPSEEK_MODEL,
        system_fingerprint=None,
        finish_reason="invalid_response",
        usage={},
        completed_at=completed_at,
    )


def _record_call(
    *,
    directory: Path,
    request_bytes: bytes,
    response_bytes: bytes,
    record: LLMCallRecord,
) -> None:
    """原子保存请求、响应和审计清单。"""

    _atomic_write_immutable(directory / "request.json", request_bytes)
    _atomic_write_immutable(directory / "response.json", response_bytes)
    _atomic_write_immutable(
        directory / "call_record.json",
        canonical_json_bytes(record.model_dump(mode="json")),
    )


def execute_recorded_call(
    *,
    prepared: PreparedDeepSeekRequest,
    authorization: LLMExportAuthorization | LLMCampaignScopeAuthorization,
    policy: CorporateExternalResearchPolicy,
    record_root: Path,
    transport: DeepSeekTransport,
    now: datetime,
    scope_request_count: int = 1,
) -> RecordedLLMResponse:
    """只发送获准字节，并原子保存请求、原始响应和审计清单。"""

    if isinstance(authorization, LLMCampaignScopeAuthorization):
        request_bytes = verify_scope_export_authorization(
            prepared,
            policy,
            authorization,
            now,
            request_count=scope_request_count,
        )
    else:
        request_bytes = verify_export_authorization(
            prepared,
            policy,
            authorization,
            now,
        )
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise FactorMinerError(
            FailureCode.LLM_PROVIDER_UNAVAILABLE,
            "服务器私有环境尚未设置 DeepSeek 凭据",
        )
    try:
        response_bytes = transport.post(request_bytes, api_key)
    except FactorMinerError:
        raise
    except Exception:
        raise FactorMinerError(
            FailureCode.LLM_PROVIDER_UNAVAILABLE,
            "DeepSeek 传输实现发生未分类错误",
        ) from None

    try:
        envelope, content_json, tool_calls, assistant_message = _response_parts(
            response_bytes
        )
    except FactorMinerError as error:
        if error.code is not FailureCode.LLM_RESPONSE_INVALID:
            raise
        record = _build_invalid_response_record(
            prepared=prepared,
            response_bytes=response_bytes,
            completed_at=now,
        )
        _record_call(
            directory=record_root / "calls" / record.call_id,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            record=record,
        )
        raise
    record = _build_record(
        prepared=prepared,
        response_bytes=response_bytes,
        envelope=envelope,
        completed_at=now,
    )
    directory = record_root / "calls" / record.call_id
    _record_call(
        directory=directory,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        record=record,
    )
    return RecordedLLMResponse(
        record=record,
        content_json=content_json,
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        record_directory=directory,
    )


def replay_recorded_call(record_directory: Path) -> RecordedLLMResponse:
    """逐字核验已录制调用并重新解析最终 JSON，不访问网络。"""

    try:
        request_bytes = (record_directory / "request.json").read_bytes()
        response_bytes = (record_directory / "response.json").read_bytes()
        record = LLMCallRecord.model_validate_json(
            (record_directory / "call_record.json").read_bytes()
        )
    except (OSError, ValueError):
        raise FactorMinerError(
            FailureCode.LEDGER_CORRUPT,
            "LLM 调用录制缺失或清单无效",
        ) from None
    request_hash = sha256(request_bytes).hexdigest()
    response_hash = sha256(response_bytes).hexdigest()
    if (
        request_hash != record.request_sha256
        or response_hash != record.response_sha256
        or record_directory.name != record.call_id
    ):
        raise FactorMinerError(
            FailureCode.LEDGER_CORRUPT,
            "LLM 调用录制哈希或目录身份不一致",
        )
    envelope, content_json, tool_calls, assistant_message = _response_parts(
        response_bytes
    )
    expected = _build_record(
        prepared=PreparedDeepSeekRequest(
            campaign_id=record.campaign_id,
            agent_role=record.agent_role,
            slot_ids=record.slot_ids,
            body=json.loads(request_bytes),
            export_payload={"information_class": "replay_only"},
            request_sha256=record.request_sha256,
        ),
        response_bytes=response_bytes,
        envelope=envelope,
        completed_at=record.completed_at,
    )
    if expected != record:
        raise FactorMinerError(
            FailureCode.LEDGER_CORRUPT,
            "LLM 调用录制内容与审计清单不一致",
        )
    return RecordedLLMResponse(
        record=record,
        content_json=content_json,
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        record_directory=record_directory,
    )
