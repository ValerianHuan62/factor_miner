"""受限 Crossref 文献元数据查询、清洗与内容身份。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import unescape
import json
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


CROSSREF_WORKS_ENDPOINT = "https://api.crossref.org/works"
URL_OR_IP = re.compile(
    r"(?i)(?:https?://|www\.|\b(?:\d{1,3}\.){3}\d{1,3}\b)"
)
HTML_TAG = re.compile(r"<[^>]+>")


class LiteratureQuery(BaseModel):
    """模型可提交给受限文献网关的唯一查询形态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_terms: tuple[str, ...] = Field(min_length=1, max_length=8)
    year_start: int = Field(ge=1900, le=2100)
    year_end: int = Field(ge=1900, le=2100)
    result_limit: int = Field(ge=1, le=5)

    @field_validator("query_terms")
    @classmethod
    def validate_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """查询词不得为空、重复或包含控制字符。"""

        if any(
            not value.strip()
            or any(ord(character) < 32 for character in value)
            for value in values
        ):
            raise ValueError("文献查询词不能为空或包含控制字符")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("文献查询词不能重复")
        return values

    @model_validator(mode="after")
    def validate_years(self) -> LiteratureQuery:
        """文献年份必须正向。"""

        if self.year_end < self.year_start:
            raise ValueError("文献检索结束年份不能早于开始年份")
        return self


class LiteratureResult(BaseModel):
    """只含公开身份和有界摘要的单条文献元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_identifier: str
    title: str
    authors: tuple[str, ...]
    publication_year: int
    abstract_excerpt: str
    source_name: str
    canonical_url: str
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_status: str


class LiteratureSearchRecord(BaseModel):
    """一次查询及其原始响应身份的不可变公开元数据记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    normalized_query: str
    retrieved_at: datetime
    results: tuple[LiteratureResult, ...]
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiteratureSourceRecordTask(BaseModel):
    """基于逻辑假设预先冻结的 source-record 检索任务。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(pattern=r"^llmsrctask_[0-9a-f]{24}$")
    logical_slot_id: str = Field(pattern=r"^H(0[1-9]|10)$")
    source_record_id: str
    claim_fragment: str
    rationale: str
    query: LiteratureQuery
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_record_id", "claim_fragment", "rationale")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """任务说明文本不得为空。"""

        if not value.strip():
            raise ValueError("来源记录任务文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> LiteratureSourceRecordTask:
        """任务 ID 与 hash 必须绑定逻辑假设和查询内容。"""

        payload = self.model_dump(mode="json", exclude={"task_id", "task_sha256"})
        expected = sha256(canonical_json_bytes(payload)).hexdigest()
        if self.task_sha256 != expected:
            raise ValueError("来源记录任务 hash 与内容不一致")
        if self.task_id != f"llmsrctask_{expected[:24]}":
            raise ValueError("来源记录任务 ID 与内容不一致")
        return self


def build_literature_source_record_tasks(
    draft: object,
) -> tuple[LiteratureSourceRecordTask, ...]:
    """把逻辑假设草案显式投影成后续检索任务，不重生成第二套假设。"""

    logical_slot_id = getattr(draft, "logical_slot_id", None)
    source_records = getattr(draft, "source_records", None)
    if not isinstance(logical_slot_id, str) or not logical_slot_id.strip():
        raise ValueError("逻辑假设草案缺少 logical_slot_id")
    if not isinstance(source_records, tuple) or not source_records:
        raise ValueError("逻辑假设草案缺少 source_records")
    tasks: list[LiteratureSourceRecordTask] = []
    for source_record in source_records:
        payload = {
            "logical_slot_id": logical_slot_id,
            "source_record_id": getattr(source_record, "source_record_id"),
            "claim_fragment": getattr(source_record, "claim_fragment"),
            "rationale": getattr(source_record, "rationale"),
            "query": LiteratureQuery(
                query_terms=tuple(getattr(source_record, "query_terms")),
                year_start=getattr(source_record, "year_start"),
                year_end=getattr(source_record, "year_end"),
                result_limit=getattr(source_record, "result_limit"),
            ).model_dump(mode="json"),
        }
        task_hash = sha256(canonical_json_bytes(payload)).hexdigest()
        tasks.append(
            LiteratureSourceRecordTask(
                task_id=f"llmsrctask_{task_hash[:24]}",
                task_sha256=task_hash,
                logical_slot_id=logical_slot_id,
                source_record_id=payload["source_record_id"],
                claim_fragment=payload["claim_fragment"],
                rationale=payload["rationale"],
                query=LiteratureQuery.model_validate(payload["query"]),
            )
        )
    return tuple(tasks)


def validate_literature_query(
    query: LiteratureQuery,
    allowed_vocabulary: frozenset[str],
) -> LiteratureQuery:
    """拒绝 URL、IP 和冻结公开词表以外的查询内容。"""

    normalized_allowed = {item.casefold() for item in allowed_vocabulary}
    for term in query.query_terms:
        if URL_OR_IP.search(term):
            raise FactorMinerError(
                FailureCode.LLM_PRIVACY_VIOLATION,
                "文献查询不得包含 URL 或 IP",
            )
        tokens = tuple(
            token
            for token in re.split(r"[^A-Za-z0-9_]+", term.casefold())
            if token
        )
        if not tokens or any(token not in normalized_allowed for token in tokens):
            raise FactorMinerError(
                FailureCode.LLM_PRIVACY_VIOLATION,
                "文献查询包含冻结公开词表之外的内容",
            )
    return query


def _first_text(value: Any) -> str:
    """从 Crossref 列表或文本字段提取首个有界字符串。"""

    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0].strip()[:300]
    if isinstance(value, str):
        return value.strip()[:300]
    return ""


def _publication_year(item: dict[str, Any]) -> int | None:
    """按固定优先级读取 Crossref 年份。"""

    for key in ("published", "published-print", "published-online", "issued"):
        parts = item.get(key, {}).get("date-parts", [])
        if (
            isinstance(parts, list)
            and parts
            and isinstance(parts[0], list)
            and parts[0]
            and isinstance(parts[0][0], int)
        ):
            return parts[0][0]
    return None


def _clean_abstract(value: Any) -> str:
    """去除 HTML 并把摘要限制在 500 个字符。"""

    if not isinstance(value, str):
        return ""
    plain = unescape(HTML_TAG.sub(" ", value))
    return " ".join(plain.split())[:500]


def _parse_result(item: dict[str, Any]) -> LiteratureResult | None:
    """把合法 DOI 元数据投影成不会触发任意网页访问的结果。"""

    doi = item.get("DOI")
    title = _first_text(item.get("title"))
    year = _publication_year(item)
    if not isinstance(doi, str) or not doi.strip() or not title or year is None:
        return None
    normalized_doi = doi.strip().casefold()
    authors = tuple(
        " ".join(
            part
            for part in (author.get("given", ""), author.get("family", ""))
            if isinstance(part, str) and part.strip()
        )[:120]
        for author in item.get("author", [])[:8]
        if isinstance(author, dict)
    )
    metadata = {
        "public_identifier": normalized_doi,
        "title": title,
        "authors": authors,
        "publication_year": year,
        "abstract_excerpt": _clean_abstract(item.get("abstract")),
        "source_name": _first_text(item.get("container-title")),
        "canonical_url": f"https://doi.org/{normalized_doi}",
        "verification_status": "identity_verified",
    }
    return LiteratureResult(
        **metadata,
        metadata_sha256=sha256_json(metadata),
    )


def parse_crossref_response(
    *,
    query: LiteratureQuery,
    response_payload: dict[str, Any],
    retrieved_at: datetime,
) -> LiteratureSearchRecord:
    """解析 Crossref JSON；只保留前 `result_limit` 个合法 DOI 结果。"""

    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("文献检索时间必须带时区")
    try:
        items = response_payload["message"]["items"]
        if not isinstance(items, list):
            raise TypeError
    except (KeyError, TypeError):
        raise FactorMinerError(
            FailureCode.LLM_RESPONSE_INVALID,
            "Crossref 响应结构无效",
        ) from None
    results: list[LiteratureResult] = []
    for item in items:
        if isinstance(item, dict):
            parsed = _parse_result(item)
            if parsed is not None:
                results.append(parsed)
        if len(results) == query.result_limit:
            break
    return LiteratureSearchRecord(
        provider="crossref",
        normalized_query=" ".join(query.query_terms),
        retrieved_at=retrieved_at,
        results=tuple(results),
        response_sha256=sha256(canonical_json_bytes(response_payload)).hexdigest(),
    )


class CrossrefMetadataAdapter:
    """只访问 Crossref 固定 works endpoint 的公开元数据适配器。"""

    def search(
        self,
        query: LiteratureQuery,
        *,
        retrieved_at: datetime,
    ) -> LiteratureSearchRecord:
        """执行一次有界 Crossref 查询，不跟随结果中的 URL。"""

        parameters = urlencode(
            {
                "query": " ".join(query.query_terms),
                "filter": (
                    f"from-pub-date:{query.year_start}-01-01,"
                    f"until-pub-date:{query.year_end}-12-31"
                ),
                "rows": query.result_limit,
                "select": (
                    "DOI,title,author,published,container-title,abstract"
                ),
            }
        )
        request = Request(
            f"{CROSSREF_WORKS_ENDPOINT}?{parameters}",
            headers={"User-Agent": "factor-miner/0.1 metadata-only"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, json.JSONDecodeError):
            raise FactorMinerError(
                FailureCode.LLM_PROVIDER_UNAVAILABLE,
                "Crossref 元数据服务不可用",
            ) from None
        if not isinstance(payload, dict):
            raise FactorMinerError(
                FailureCode.LLM_RESPONSE_INVALID,
                "Crossref 响应不是 JSON object",
            )
        return parse_crossref_response(
            query=query,
            response_payload=payload,
            retrieved_at=retrieved_at,
        )
