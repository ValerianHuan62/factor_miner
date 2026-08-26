"""V0.5 不可变文档与独立单写入者哈希链账本。"""

from __future__ import annotations

import json
from pathlib import Path

from factor_miner.canonical import canonical_json_bytes
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import _HashChainJsonlStore, _atomic_write_immutable
from factor_miner.llm_brief import RegisteredLLMCoverageBrief
from factor_miner.llm_schema import (
    DiscoveryArmSpec,
    RegisteredLLMDiscoveryResearchFamily,
)
from factor_miner.llm_seal import RegisteredGenerationSeal
from factor_miner.llm_state import (
    LLMDiscoveryEvent,
    LLMDiscoveryFamilyState,
    validate_transition,
    project_discovery_family_state,
)


QUARANTINED_DISCOVERY_FAMILY_IDS = frozenset(
    {"llmfamily_1bae19965638a6ac9620e0b0"}
)


class LLMDiscoveryLedger:
    """为每个 discovery family 隔离文档、事件锁和哈希链。"""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.expanduser().resolve(strict=False)
        self.state_root = self.artifact_root / "state"
        self.families_root = self.state_root / "llm_discovery_families"
        self.briefs_root = self.state_root / "llm_briefs"

    def family_root(self, discovery_family_id: str) -> Path:
        """返回单个 family 的状态目录。"""

        return self.families_root / discovery_family_id

    @staticmethod
    def _assert_family_not_quarantined(discovery_family_id: str) -> None:
        """拒绝触碰已知污染 family，保持其原始字节不变。"""

        if discovery_family_id in QUARANTINED_DISCOVERY_FAMILY_IDS:
            raise FactorMinerError(
                FailureCode.AUDIT_QUARANTINED,
                "该 discovery family 已永久隔离，禁止修复、继续生成或封存",
            )

    def events_path(self, discovery_family_id: str) -> Path:
        """返回单个 family 的独立事件文件。"""

        return self.family_root(discovery_family_id) / "llm_events.jsonl"

    def _store(
        self,
        discovery_family_id: str,
    ) -> _HashChainJsonlStore[LLMDiscoveryEvent]:
        path = self.events_path(discovery_family_id)
        return _HashChainJsonlStore(
            path,
            path.with_suffix(path.suffix + ".lock"),
            LLMDiscoveryEvent,
        )

    def register_family(
        self,
        family: RegisteredLLMDiscoveryResearchFamily,
    ) -> Path:
        """登记 family spec，并同时登记四个 arm spec。"""

        self._assert_family_not_quarantined(family.discovery_family_id)
        root = self.family_root(family.discovery_family_id)
        path = root / "family_spec.json"
        _atomic_write_immutable(
            path,
            canonical_json_bytes(family.model_dump(mode="json")),
        )
        for arm in family.spec.arm_specs:
            self.register_arm_spec(family.discovery_family_id, arm)
        return path

    def register_arm_spec(
        self,
        discovery_family_id: str,
        arm: DiscoveryArmSpec,
    ) -> Path:
        """在 family 下按 arm ID 原子登记生成合同。"""

        self._assert_family_not_quarantined(discovery_family_id)
        path = self.family_root(discovery_family_id) / "arm_specs" / f"{arm.arm_id}.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(arm.model_dump(mode="json")),
        )

    def register_brief(self, brief: RegisteredLLMCoverageBrief) -> Path:
        """登记本地来源身份和脱敏外发体绑定的 brief。"""

        path = self.briefs_root / brief.brief_id / "brief.json"
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(brief.model_dump(mode="json")),
        )

    def register_generation_seal(
        self,
        seal: RegisteredGenerationSeal,
    ) -> Path:
        """在对应 family 下原子登记不可逆 generation seal。"""

        self._assert_family_not_quarantined(seal.manifest.discovery_family_id)
        path = (
            self.family_root(seal.manifest.discovery_family_id)
            / "generation_seal.json"
        )
        return _atomic_write_immutable(
            path,
            canonical_json_bytes(seal.model_dump(mode="json")),
        )

    def load_generation_seal(
        self,
        discovery_family_id: str,
    ) -> RegisteredGenerationSeal:
        """读取并验证 family 已登记的 generation seal。"""

        self._assert_family_not_quarantined(discovery_family_id)
        path = self.family_root(discovery_family_id) / "generation_seal.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        seal = RegisteredGenerationSeal.model_validate(payload)
        if seal.manifest.discovery_family_id != discovery_family_id:
            raise ValueError("generation seal 路径与 family 身份不一致")
        return seal

    def append_event(self, event: LLMDiscoveryEvent) -> LLMDiscoveryEvent:
        """向该事件所属 family 的独立哈希链追加事件。"""

        return self.append_validated_event(event.discovery_family_id, event)

    def append_validated_event(
        self,
        family_id: str,
        event: LLMDiscoveryEvent,
    ) -> LLMDiscoveryEvent:
        """在同一单写入者锁内预检状态转移后追加事件。

        状态历史非法时返回隔离错误，且不会写入修复事件；候选新事件非法
        时在写入前拒绝，确保事件文件字节和行数保持不变。
        """

        self._assert_family_not_quarantined(family_id)
        if event.discovery_family_id != family_id:
            raise FactorMinerError(
                FailureCode.LLM_STATE_TRANSITION_INVALID,
                "事件 family 身份与追加目标不一致",
            )
        family = self.load_family(family_id)

        def pre_append(
            events: tuple[LLMDiscoveryEvent, ...],
            candidate: LLMDiscoveryEvent,
        ) -> None:
            try:
                current = project_discovery_family_state(family, events)
            except FactorMinerError as error:
                raise FactorMinerError(
                    FailureCode.AUDIT_QUARANTINED,
                    "现有 discovery family 状态历史非法，已只读隔离",
                ) from error
            validate_transition(current, candidate)

        return self._store(family_id).append(
            event,
            pre_append_validator=pre_append,
        )

    def verify(
        self,
        discovery_family_id: str,
    ) -> tuple[LLMDiscoveryEvent, ...]:
        """验证并返回完整 V0.5 事件链。"""

        self._assert_family_not_quarantined(discovery_family_id)
        return self._store(discovery_family_id).verify()

    def load_family(
        self,
        discovery_family_id: str,
    ) -> RegisteredLLMDiscoveryResearchFamily:
        """读取并复核内容寻址 family 文档。"""

        self._assert_family_not_quarantined(discovery_family_id)
        path = self.family_root(discovery_family_id) / "family_spec.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        family = RegisteredLLMDiscoveryResearchFamily.model_validate(payload)
        if family.discovery_family_id != discovery_family_id:
            raise ValueError("family 路径与内容身份不一致")
        return family

    def project_state(self, discovery_family_id: str) -> LLMDiscoveryFamilyState:
        """先验证完整事件链，再执行纯函数状态投影。"""

        family = self.load_family(discovery_family_id)
        events = self.verify(discovery_family_id)
        try:
            return project_discovery_family_state(family, events)
        except FactorMinerError as error:
            raise FactorMinerError(
                FailureCode.AUDIT_QUARANTINED,
                "现有 discovery family 状态历史非法，已只读隔离",
            ) from error
