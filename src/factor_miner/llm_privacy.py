"""V0.5 外发信息分类、授权与规范字节敏感信息扫描。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import math
import re
from typing import Any
import unicodedata

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


ABSOLUTELY_FORBIDDEN_PATTERNS = (
    re.compile(rb"(?:^|[\s\"'])/(?:Users|data|home|private|var|etc)/"),
    re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(rb"(?i)\b(?:bearer|api[_-]?key|token)\b"),
    re.compile(rb"(?i)\bfactor_(?:internal|prod|real)_[A-Za-z0-9_-]+\b"),
)
TOKEN_PATTERN = re.compile(rb"[A-Za-z0-9+/=_-]{24,}")
CONTENT_IDENTITY_PATTERN = re.compile(
    r"^(?:[a-z][a-z0-9]*_)?[0-9a-f]{24,64}$"
)
OFFICIAL_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
OFFICIAL_DEEPSEEK_MODEL = "deepseek-v4-pro"


def _policy_identity_payload(
    *,
    provider: str,
    allowed_endpoint: str,
    allowed_information_classes: tuple[str, ...],
    forbidden_information_classes: tuple[str, ...],
    allowed_models: tuple[str, ...],
    maximum_authorized_campaigns: int,
    valid_from: datetime,
    valid_until: datetime,
    approver_role: str,
    approval_reference: str,
) -> dict[str, Any]:
    """用同一时间格式构造政策内容身份，避免序列化器格式漂移。"""

    return {
        "provider": provider,
        "allowed_endpoint": allowed_endpoint,
        "allowed_information_classes": allowed_information_classes,
        "forbidden_information_classes": forbidden_information_classes,
        "allowed_models": allowed_models,
        "maximum_authorized_campaigns": maximum_authorized_campaigns,
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
        "approver_role": approver_role,
        "approval_reference": approval_reference,
    }


class CorporateExternalResearchPolicy(BaseModel):
    """公司对聚合衍生研究信息的条件外发授权。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(pattern=r"^extpolicy_[0-9a-f]{24}$")
    provider: str
    allowed_endpoint: str
    allowed_information_classes: tuple[str, ...]
    forbidden_information_classes: tuple[str, ...]
    allowed_models: tuple[str, ...] = Field(min_length=1)
    maximum_authorized_campaigns: int = Field(ge=1)
    valid_from: datetime
    valid_until: datetime
    approver_role: str
    approval_reference: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "policy_id",
        "provider",
        "allowed_endpoint",
        "approver_role",
        "approval_reference",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        """政策关键文本不得为空。"""

        if not value.strip():
            raise ValueError("外发政策文本不能为空")
        return value

    @field_validator(
        "allowed_information_classes",
        "forbidden_information_classes",
    )
    @classmethod
    def validate_classes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """信息类别必须非空、唯一且稳定排序。"""

        if any(not value.strip() for value in values):
            raise ValueError("信息类别不能包含空值")
        if tuple(sorted(set(values))) != values:
            raise ValueError("信息类别必须唯一且按字典序排序")
        return values

    @field_validator("allowed_models")
    @classmethod
    def validate_models(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """模型集合必须唯一排序，且只能包含当前批准模型。"""

        if tuple(sorted(set(values))) != values:
            raise ValueError("allowed_models 必须唯一且按字典序排序")
        if values != (OFFICIAL_DEEPSEEK_MODEL,):
            raise ValueError("公司外发政策只允许冻结的 DeepSeek 模型")
        return values

    @field_validator("valid_from", "valid_until")
    @classmethod
    def validate_policy_time(cls, value: datetime) -> datetime:
        """政策时间必须带时区。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("公司外发政策时间必须带时区")
        return value

    @model_validator(mode="after")
    def validate_scope_and_identity(self) -> CorporateExternalResearchPolicy:
        """政策必须绑定官方范围、正向有效期和完整内容身份。"""

        if self.provider != "deepseek":
            raise ValueError("公司外发政策 provider 必须为 deepseek")
        if self.allowed_endpoint != OFFICIAL_DEEPSEEK_ENDPOINT:
            raise ValueError("公司外发政策 endpoint 不是冻结官方端点")
        if self.valid_until <= self.valid_from:
            raise ValueError("公司外发政策有效期必须正向")
        payload = _policy_identity_payload(
            provider=self.provider,
            allowed_endpoint=self.allowed_endpoint,
            allowed_information_classes=self.allowed_information_classes,
            forbidden_information_classes=self.forbidden_information_classes,
            allowed_models=self.allowed_models,
            maximum_authorized_campaigns=self.maximum_authorized_campaigns,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            approver_role=self.approver_role,
            approval_reference=self.approval_reference,
        )
        expected = sha256_json(payload)
        if self.policy_sha256 != expected:
            raise ValueError("公司外发政策哈希与内容不一致")
        if self.policy_id != f"extpolicy_{expected[:24]}":
            raise ValueError("公司外发政策 ID 与内容不一致")
        return self


def registered_corporate_external_research_policy(
    *,
    provider: str,
    allowed_endpoint: str,
    allowed_information_classes: tuple[str, ...],
    forbidden_information_classes: tuple[str, ...],
    allowed_models: tuple[str, ...],
    maximum_authorized_campaigns: int,
    valid_from: datetime | str,
    valid_until: datetime | str,
    approver_role: str,
    approval_reference: str,
) -> CorporateExternalResearchPolicy:
    """由完整政策内容生成不可伪改的 ID 和 SHA-256。"""

    normalized_valid_from = (
        datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        if isinstance(valid_from, str)
        else valid_from
    )
    normalized_valid_until = (
        datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        if isinstance(valid_until, str)
        else valid_until
    )
    payload = _policy_identity_payload(
        provider=provider,
        allowed_endpoint=allowed_endpoint,
        allowed_information_classes=allowed_information_classes,
        forbidden_information_classes=forbidden_information_classes,
        allowed_models=allowed_models,
        maximum_authorized_campaigns=maximum_authorized_campaigns,
        valid_from=normalized_valid_from,
        valid_until=normalized_valid_until,
        approver_role=approver_role,
        approval_reference=approval_reference,
    )
    policy_hash = sha256_json(payload)
    return CorporateExternalResearchPolicy(
        policy_id=f"extpolicy_{policy_hash[:24]}",
        policy_sha256=policy_hash,
        **payload,
    )


class ExportPreview(BaseModel):
    """用户授权前看到的规范外发字节摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_size_bytes: int = Field(ge=2)
    field_count: int = Field(ge=1)
    information_classes: tuple[str, ...] = Field(min_length=1)


def _information_classes(value: Any) -> set[str]:
    """递归收集 payload 声明的信息类别。"""

    found: set[str] = set()
    if isinstance(value, dict):
        information_class = value.get("information_class")
        if isinstance(information_class, str) and information_class.strip():
            found.add(information_class)
        for child in value.values():
            found.update(_information_classes(child))
    elif isinstance(value, list | tuple):
        for child in value:
            found.update(_information_classes(child))
    return found


def _contains_unsafe_unicode(value: Any) -> bool:
    """递归识别序列化会隐藏的控制字符和格式字符。"""

    if isinstance(value, str):
        return any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    if isinstance(value, dict):
        return any(
            _contains_unsafe_unicode(key) or _contains_unsafe_unicode(child)
            for key, child in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_unsafe_unicode(child) for child in value)
    return False


def _shannon_entropy(value: bytes) -> float:
    """计算长 token 的字节香农熵。"""

    counts = {byte: value.count(byte) for byte in set(value)}
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _without_content_identities(value: Any, *, key: str = "") -> Any:
    """熵扫描忽略固定格式的本地内容 ID，但不忽略普通正文中的同一字符串。"""

    if isinstance(value, dict):
        return {
            child_key: _without_content_identities(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list | tuple):
        return [_without_content_identities(child, key=key) for child in value]
    is_identity_key = key.endswith(("_id", "_hash", "_sha256"))
    if (
        is_identity_key
        and isinstance(value, str)
        and CONTENT_IDENTITY_PATTERN.fullmatch(value)
    ):
        return "<content-identity>"
    return value


def scan_export_payload(
    payload: dict[str, Any],
    policy: CorporateExternalResearchPolicy,
) -> bytes:
    """先核验条件授权，再扫描规范 JSON 的绝对禁止信息。"""

    classes = _information_classes(payload)
    if not classes:
        raise FactorMinerError(
            FailureCode.LLM_POLICY_NOT_AUTHORIZED,
            "外发 payload 没有声明 information_class",
        )
    unauthorized = classes.difference(policy.allowed_information_classes)
    forbidden = classes.intersection(policy.forbidden_information_classes)
    if unauthorized or forbidden:
        rejected = tuple(sorted(unauthorized | forbidden))
        raise FactorMinerError(
            FailureCode.LLM_POLICY_NOT_AUTHORIZED,
            f"公司政策未授权信息类别：{rejected}",
        )

    if _contains_unsafe_unicode(payload):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            "外发 payload 包含控制字符或零宽格式字符",
        )
    payload_bytes = canonical_json_bytes(payload)
    if any(pattern.search(payload_bytes) for pattern in ABSOLUTELY_FORBIDDEN_PATTERNS):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            "外发 payload 命中绝对禁止信息模式",
        )
    entropy_bytes = canonical_json_bytes(_without_content_identities(payload))
    for match in TOKEN_PATTERN.finditer(entropy_bytes):
        token = match.group(0)
        if _shannon_entropy(token) >= 4.3:
            raise FactorMinerError(
                FailureCode.LLM_PRIVACY_VIOLATION,
                "外发 payload 包含疑似高熵凭据",
            )
    if any(
        byte < 32 and byte not in {9, 10, 13}
        for byte in payload_bytes
    ):
        raise FactorMinerError(
            FailureCode.LLM_PRIVACY_VIOLATION,
            "外发 payload 包含控制字符",
        )
    return payload_bytes


def build_export_preview(
    payload: dict[str, Any],
    policy: CorporateExternalResearchPolicy,
) -> ExportPreview:
    """对已经通过扫描的精确规范字节生成授权预览。"""

    payload_bytes = scan_export_payload(payload, policy)
    return ExportPreview(
        payload_sha256=sha256(payload_bytes).hexdigest(),
        payload_size_bytes=len(payload_bytes),
        field_count=len(payload),
        information_classes=tuple(sorted(_information_classes(payload))),
    )
