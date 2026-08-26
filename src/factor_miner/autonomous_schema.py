"""Dashboard 自主研究的命令、授权和中文运行状态合同。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json


_HASH = r"^[0-9a-f]{64}$"
_CJK = re.compile(r"[\u3400-\u9fff]")


class AutonomousStage(StrEnum):
    """Dashboard 展示和 Worker 恢复共用的自主研究阶段。"""

    CONTEXT_PREPARING = "context_preparing"
    HYPOTHESIS_GENERATING = "hypothesis_generating"
    AWAITING_REVIEW = "awaiting_review"
    REVIEW_FROZEN = "review_frozen"
    EXPRESSION_GENERATING = "expression_generating"
    MANIFEST_FROZEN = "manifest_frozen"
    EVALUATING = "evaluating"
    PUBLISHED = "published"
    PROJECTED = "projected"
    EVOLUTION_REFRESHED = "evolution_refreshed"
    COMPLETED = "completed"
    NO_APPROVED_HYPOTHESIS = "no_approved_hypothesis"
    FAILED = "failed"

    @property
    def chinese_label(self) -> str:
        """返回稳定的中文阶段名。"""

        return {
            self.CONTEXT_PREPARING: "准备上下文",
            self.HYPOTHESIS_GENERATING: "生成假设",
            self.AWAITING_REVIEW: "等待审批",
            self.REVIEW_FROZEN: "审批已冻结",
            self.EXPRESSION_GENERATING: "生成表达式",
            self.MANIFEST_FROZEN: "候选已登记",
            self.EVALUATING: "计算与评价",
            self.PUBLISHED: "产物已发布",
            self.PROJECTED: "数据库已投影",
            self.EVOLUTION_REFRESHED: "记忆与图谱已刷新",
            self.COMPLETED: "运行完成",
            self.NO_APPROVED_HYPOTHESIS: "没有批准假设",
            self.FAILED: "运行失败",
        }[self]

    @property
    def terminal(self) -> bool:
        """判断该阶段是否已经结束活动批次。"""

        return self in {
            self.COMPLETED,
            self.NO_APPROVED_HYPOTHESIS,
            self.FAILED,
        }


class ResearchCommandType(StrEnum):
    """Dashboard 可以提交的四类控制命令。"""

    START = "start"
    REVIEW_DECISION = "review_decision"
    FREEZE_REVIEW = "freeze_review"
    RESUME = "resume"


def _utc(value: datetime, *, field_name: str) -> datetime:
    """统一拒绝无时区时间并规范到 UTC。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区")
    return value.astimezone(timezone.utc)


class ResearchCommand(BaseModel):
    """Dashboard 写入原子收件箱的内容寻址命令。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(pattern=r"^researchcmd_[0-9a-f]{24}$")
    command_sha256: str = Field(pattern=_HASH)
    command_type: ResearchCommandType
    target_run_id: str | None = Field(default=None, pattern=r"^autrun_[0-9a-f]{24}$")
    requested_by: str = Field(min_length=1)
    requested_at: datetime
    body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        """命令时间必须可审计。"""

        return _utc(value, field_name="requested_at")

    @model_validator(mode="after")
    def validate_identity(self) -> ResearchCommand:
        """命令 ID 必须完全由规范 payload 决定。"""

        payload = self.model_dump(
            mode="json",
            exclude={"command_id", "command_sha256"},
        )
        digest = sha256_json(payload)
        if self.command_sha256 != digest or self.command_id != f"researchcmd_{digest[:24]}":
            raise ValueError("研究命令内容身份不一致")
        if self.command_type is ResearchCommandType.START and self.target_run_id is not None:
            raise ValueError("启动命令不能预先指定 run_id")
        if self.command_type is not ResearchCommandType.START and self.target_run_id is None:
            raise ValueError("非启动命令必须绑定 target_run_id")
        return self

    @classmethod
    def build(
        cls,
        *,
        command_type: ResearchCommandType,
        requested_by: str,
        requested_at: datetime,
        target_run_id: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> ResearchCommand:
        """构造规范命令和稳定身份。"""

        payload = {
            "command_type": command_type,
            "target_run_id": target_run_id,
            "requested_by": requested_by,
            "requested_at": _utc(requested_at, field_name="requested_at"),
            "body": dict(body or {}),
        }
        draft = cls.model_construct(
            command_id="researchcmd_" + "0" * 24,
            command_sha256="0" * 64,
            **payload,
        )
        digest = sha256_json(
            draft.model_dump(
                mode="json",
                exclude={"command_id", "command_sha256"},
            )
        )
        return cls(
            command_id=f"researchcmd_{digest[:24]}",
            command_sha256=digest,
            **payload,
        )

    @classmethod
    def start(cls, *, requested_by: str, requested_at: datetime) -> ResearchCommand:
        """构造 Dashboard 的新建研究批次命令。"""

        return cls.build(
            command_type=ResearchCommandType.START,
            requested_by=requested_by,
            requested_at=requested_at,
        )


class ResearchStartAuthorization(BaseModel):
    """启动按钮绑定的本批脱敏模型角色授权。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(pattern=r"^researchauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_HASH)
    start_command_id: str = Field(pattern=r"^researchcmd_[0-9a-f]{24}$")
    allowed_agent_roles: tuple[Literal["hypothesis", "expression"], ...]
    model: str = Field(min_length=1)
    approver_role: str = Field(min_length=1)
    authorized_at: datetime
    expires_at: datetime

    @field_validator("authorized_at", "expires_at")
    @classmethod
    def validate_time(cls, value: datetime, info: Any) -> datetime:
        """授权起止时间必须带时区。"""

        return _utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_authorization(self) -> ResearchStartAuthorization:
        """固定两个角色、有效期和内容身份。"""

        if self.allowed_agent_roles != ("hypothesis", "expression"):
            raise ValueError("启动授权只能允许 hypothesis 和 expression")
        if self.expires_at <= self.authorized_at:
            raise ValueError("启动授权 expires_at 必须晚于 authorized_at")
        payload = self.model_dump(
            mode="json",
            exclude={"authorization_id", "authorization_sha256"},
        )
        digest = sha256_json(payload)
        if (
            self.authorization_sha256 != digest
            or self.authorization_id != f"researchauth_{digest[:24]}"
        ):
            raise ValueError("启动授权内容身份不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        command: ResearchCommand,
        model: str,
        approver_role: str,
        expires_at: datetime,
    ) -> ResearchStartAuthorization:
        """从启动命令构造本批模型范围授权。"""

        if command.command_type is not ResearchCommandType.START:
            raise ValueError("启动授权只能绑定 start 命令")
        payload = {
            "start_command_id": command.command_id,
            "allowed_agent_roles": ("hypothesis", "expression"),
            "model": model,
            "approver_role": approver_role,
            "authorized_at": command.requested_at,
            "expires_at": _utc(expires_at, field_name="expires_at"),
        }
        draft = cls.model_construct(
            authorization_id="researchauth_" + "0" * 24,
            authorization_sha256="0" * 64,
            **payload,
        )
        digest = sha256_json(
            draft.model_dump(
                mode="json",
                exclude={"authorization_id", "authorization_sha256"},
            )
        )
        return cls(
            authorization_id=f"researchauth_{digest[:24]}",
            authorization_sha256=digest,
            **payload,
        )


class AutonomousResearchState(BaseModel):
    """不含原始研究数据的不可变中文运行状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "autonomous-research-state-v1"
    run_id: str = Field(pattern=r"^autrun_[0-9a-f]{24}$")
    sequence: int = Field(ge=0)
    stage: AutonomousStage
    stage_label: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    command_ids: tuple[str, ...] = Field(min_length=1)
    stage_refs: dict[str, str]
    last_error: str | None = None
    state_sha256: str = Field(pattern=_HASH)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_state_time(cls, value: datetime, info: Any) -> datetime:
        """状态时间必须带时区。"""

        return _utc(value, field_name=info.field_name)

    @field_validator("last_error")
    @classmethod
    def validate_chinese_error(cls, value: str | None) -> str | None:
        """面向 Dashboard 的错误必须可直接用中文阅读。"""

        if value is not None and not _CJK.search(value):
            raise ValueError("运行错误必须使用中文")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> AutonomousResearchState:
        """核对阶段名、时间、引用和内容身份。"""

        if self.stage_label != self.stage.chinese_label:
            raise ValueError("中文阶段名与阶段代码不一致")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if len(set(self.command_ids)) != len(self.command_ids):
            raise ValueError("状态包含重复命令 ID")
        if any(not re.fullmatch(_HASH, digest) for digest in self.stage_refs.values()):
            raise ValueError("阶段引用必须是 sha256")
        payload = self.model_dump(mode="json", exclude={"state_sha256"})
        if self.state_sha256 != sha256_json(payload):
            raise ValueError("自主研究状态内容身份不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        sequence: int,
        stage: AutonomousStage,
        created_at: datetime,
        updated_at: datetime,
        command_ids: tuple[str, ...],
        stage_refs: dict[str, str],
        last_error: str | None = None,
    ) -> AutonomousResearchState:
        """构造带中文阶段名和内容哈希的状态。"""

        payload = {
            "version": "autonomous-research-state-v1",
            "run_id": run_id,
            "sequence": sequence,
            "stage": stage,
            "stage_label": stage.chinese_label,
            "created_at": _utc(created_at, field_name="created_at"),
            "updated_at": _utc(updated_at, field_name="updated_at"),
            "command_ids": command_ids,
            "stage_refs": dict(stage_refs),
            "last_error": last_error,
        }
        draft = cls.model_construct(
            state_sha256="0" * 64,
            **payload,
        )
        digest = sha256_json(
            draft.model_dump(mode="json", exclude={"state_sha256"})
        )
        return cls(**payload, state_sha256=digest)
