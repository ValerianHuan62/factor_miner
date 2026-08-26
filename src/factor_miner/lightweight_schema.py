"""个人研究模式的轻量批次冻结合同。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import sha256_json


class LightweightResearchConfig(BaseModel):
    """运行前冻结的假设数、候选数和 LLM 开关。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_count: int = Field(default=10, gt=0)
    candidates_per_hypothesis: int = Field(default=3, gt=0)
    allow_external_llm: bool = True

    @property
    def slot_count(self) -> int:
        """返回本批完整统计检验族规模。"""

        return self.hypothesis_count * self.candidates_per_hypothesis


class LightweightCandidateBinding(BaseModel):
    """一个轻量槽位和不可变候选 Spec 的绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_id: str = Field(min_length=1)
    terminal_status: Literal["ready", "failed"] = "ready"
    source_candidate_id: str | None = Field(default=None, min_length=1)
    candidate_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal_binding(self) -> LightweightCandidateBinding:
        """ready 必须绑定真实候选，failed 不得伪造候选身份。"""

        if self.terminal_status == "ready":
            if self.source_candidate_id is None or self.candidate_spec_hash is None:
                raise ValueError("ready 槽必须绑定候选身份")
            if self.failure_reason is not None:
                raise ValueError("ready 槽不得包含失败原因")
        else:
            if self.source_candidate_id is not None or self.candidate_spec_hash is not None:
                raise ValueError("failed 槽不得伪造候选身份")
            if self.failure_reason is None:
                raise ValueError("failed 槽必须记录失败原因")
        return self


class LightweightBatchManifest(BaseModel):
    """读取任何研究结果前登记的轻量批次清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "lightweight-batch-v1"
    hypothesis_count: int = Field(gt=0)
    candidates_per_hypothesis: int = Field(gt=0)
    family_size: int = Field(gt=0)
    allow_external_llm: bool
    candidate_bindings: tuple[LightweightCandidateBinding, ...] = Field(min_length=1)
    evaluation_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_graph_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_frozen_family(self) -> LightweightBatchManifest:
        """槽位、候选和统计分母必须完整且唯一。"""

        expected = self.hypothesis_count * self.candidates_per_hypothesis
        if self.family_size != expected or len(self.candidate_bindings) != expected:
            raise ValueError("轻量批次槽位数与冻结配置不一致")
        slot_ids = tuple(item.slot_id for item in self.candidate_bindings)
        candidate_ids = tuple(
            item.source_candidate_id
            for item in self.candidate_bindings
            if item.source_candidate_id is not None
        )
        spec_hashes = tuple(
            item.candidate_spec_hash
            for item in self.candidate_bindings
            if item.candidate_spec_hash is not None
        )
        if len(set(slot_ids)) != expected:
            raise ValueError("轻量批次包含重复槽位")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("轻量批次包含重复候选身份")
        if len(set(spec_hashes)) != len(spec_hashes):
            raise ValueError("轻量批次包含重复候选 Spec")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != sha256_json(payload):
            raise ValueError("轻量批次 manifest_sha256 与内容不一致")
        return self


def build_lightweight_batch_manifest(
    *,
    config: LightweightResearchConfig,
    candidate_bindings: tuple[LightweightCandidateBinding, ...],
    evaluation_policy_hash: str,
    memory_snapshot_hash: str,
    coverage_graph_hash: str,
) -> LightweightBatchManifest:
    """构造内容寻址轻量清单，不自动补齐缺失候选。"""

    payload = {
        "version": "lightweight-batch-v1",
        "hypothesis_count": config.hypothesis_count,
        "candidates_per_hypothesis": config.candidates_per_hypothesis,
        "family_size": config.slot_count,
        "allow_external_llm": config.allow_external_llm,
        "candidate_bindings": [item.model_dump(mode="json") for item in candidate_bindings],
        "evaluation_policy_hash": evaluation_policy_hash,
        "memory_snapshot_hash": memory_snapshot_hash,
        "coverage_graph_hash": coverage_graph_hash,
    }
    return LightweightBatchManifest(
        **payload,
        manifest_sha256=sha256_json(payload),
    )
