"""跨市场 FaVOR 研究的事前计划、分区、预算和候选提交合同。"""
from __future__ import annotations

from datetime import date
from math import prod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from factor_miner.favor_validation import ConstructPolicy
from factor_miner.hypothesis_constraints import ConditionContract
from factor_miner.schema import FactorNode, HypothesisSpec


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FavorSlot(FrozenModel):
    trial_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    condition_id: str
    mechanism_id: str
    information_source: str


class FavorSplit(FrozenModel):
    discovery_start: date
    discovery_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    test_consumed: bool

    @model_validator(mode="after")
    def chronological(self):
        if not self.discovery_start < self.discovery_end < self.validation_start < self.validation_end < self.test_start < self.test_end:
            raise ValueError("FaVOR 发现、验证、测试分区必须严格按时间分离")
        return self


class FavorExecution(FrozenModel):
    frequency: Literal["daily", "weekly_last_session", "every_5_sessions", "monthly_last_session"] = "every_5_sessions"
    holding_sessions: Literal[1, 5, 20] = 5
    round_trip_cost_bps: FiniteFloat = Field(default=10., ge=0)
    stress_cost_bps: tuple[FiniteFloat, ...] = (20., 40.)
    terminal_policy: Literal["fail_closed", "report_unresolved"] = "report_unresolved"
    position_side: Literal["long"] = "long"

    @model_validator(mode="after")
    def costs(self):
        if any(x < 0 for x in self.stress_cost_bps) or len(set(self.stress_cost_bps)) != len(self.stress_cost_bps):
            raise ValueError("成本情景必须非负且不能重复")
        return self


class FavorIntegration(FrozenModel):
    selectivity_levels: tuple[float, ...] = (.5, .7, .9)
    # 每个向量分别指定各条件的最终激活百分位；仅在验证期选一个。
    validation_thresholds: tuple[tuple[float, ...], ...] = Field(min_length=1)
    min_events_per_ticker: int = Field(default=5, ge=2)
    min_tickers: int = Field(default=20, ge=2)
    ticker_support_threshold: float = Field(default=.5, gt=0, le=1)
    redundancy_threshold: float = Field(default=.75, gt=0, le=1)

    @model_validator(mode="after")
    def levels(self):
        if self.selectivity_levels != (.5, .7, .9):
            raise ValueError("favor-v1 固定 q50/q70/q90 三档，不看结果更改")
        if len(set(self.validation_thresholds)) != len(self.validation_thresholds):
            raise ValueError("阈值试验向量重复")
        if any(not .5 < q < 1 for row in self.validation_thresholds for q in row):
            raise ValueError("验证期阈值必须在 (0.5, 1) 内")
        return self


class FavorDataRelease(FrozenModel):
    """数据所有者显式提供的发布身份；三类 mask 和成交限制来自状态面板。"""
    market_id: Literal["a_share", "us_equity"]
    release_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    adjustment: str = Field(min_length=1)
    calendar_version: str = Field(min_length=1)
    state_version: str = Field(min_length=1)
    as_of_date: date
    state_as_of_date: date
    availability: Literal["after_close_t"] = "after_close_t"

    @model_validator(mode="after")
    def ready(self):
        if self.state_as_of_date != self.as_of_date:
            raise ValueError("状态表与发布截止日不一致")
        if any(v.strip().lower() in {"unknown", "未知", "none", ""} for v in
               (self.release_id, self.source, self.adjustment, self.calendar_version, self.state_version)):
            raise ValueError("数据发布身份不能未知")
        return self


class FavorPlan(FrozenModel):
    version: Literal["favor-gamma-v1", "favor-gamma-v2", "favor-gamma-v3"] = "favor-gamma-v1"
    run_kind: Literal["research", "synthetic_demo"] = "research"
    market_id: Literal["a_share", "us_equity"]
    hypothesis: HypothesisSpec
    conditions: tuple[ConditionContract, ...] = Field(min_length=2)
    slots: tuple[FavorSlot, ...] = Field(min_length=2)
    splits: FavorSplit
    construct_policy: ConstructPolicy = Field(default_factory=ConstructPolicy)
    integration: FavorIntegration
    execution: FavorExecution = Field(default_factory=FavorExecution)
    dataset_root: str
    data_contract_path: str
    terminal_events_path: str | None = None
    input_sha256: dict[str, str]
    allowed_fields: tuple[str, ...] = Field(min_length=1)
    search_budget: dict
    # 历史规范 AST 只用于识别已尝试身份，不能充当有效组合代表。
    historical_ast_hashes: tuple[str, ...]

    @property
    def gamma_version(self) -> str:
        """显式版本绑定日历月观察，属性不进入旧计划的规范序列化。"""
        return "hypothesis-gamma-calendar-month-v1" if self.version == "favor-gamma-v3" else "hypothesis-gamma-v1"

    @model_validator(mode="after")
    def frozen_search(self):
        from factor_miner.research_pool import validate_search_budget
        if self.version == "favor-gamma-v1" and (self.execution.holding_sessions != 5
                or self.execution.frequency not in {"weekly_last_session", "every_5_sessions"}):
            raise ValueError("日频、月频或非五日目标必须显式登记favor-gamma-v2；旧协议不自动迁移")
        ids = [x.condition_id for x in self.conditions]
        if len(ids) != len(set(ids)) or set(ids) != {s.condition_id for s in self.slots}:
            raise ValueError("观察条件必须唯一，且每个条件有明确候选槽")
        if any(len(q) != len(ids) for q in self.integration.validation_thresholds):
            raise ValueError("阈值向量必须覆盖每一个条件")
        if self.hypothesis.expected_sign != "positive":
            raise ValueError("favor-v1 复用多头成交协议，组合假设须为正向；不模拟未授权做空")
        validate_search_budget(self.search_budget, [s.model_dump(exclude={"condition_id"}) for s in self.slots])
        target = 'signal_exploration' if self.version == 'favor-exploration-v1' else 'joint_trigger'
        if self.search_budget.get('version') == 'bounded-research-v2' and self.search_budget['validation_target'] != target:
            if target == 'signal_exploration':
                raise ValueError('探索协议要求 validation_target=signal_exploration；不得冒充联合确认或组合增量')
            raise ValueError('FaVOR 只检验事前联合触发主张；普通组合增量须使用独立冻结的组合对照入口')
        combinations = prod(sum(s.condition_id == cid for s in self.slots) for cid in ids)
        comparisons = combinations * (3 + len(self.integration.validation_thresholds) + 1 + len(self.execution.stress_cost_bps)) + 2*len(self.slots) + 1
        if self.version == 'favor-exploration-v1':
            comparisons = len(self.slots) * (3 + len(self.execution.stress_cost_bps)) + 1
        if comparisons > self.search_budget["model_capacity"]:
            raise ValueError(f"联合笛卡尔积、三档筛选、阈值及成本情景需预留 {comparisons} 个模型名额")
        return self


class FavorSubmission(FrozenModel):
    trial_id: str
    plan_sha256: str
    gamma_sha256: str
    producer: Literal["deepseek_api", "gpt6"]
    expression: FactorNode


class FavorRegimePlan(FavorPlan):
    """新版显式绑定状态；旧计划类型与序列化保持原样。"""
    version: Literal['favor-regime-v1'] = 'favor-regime-v1'
    regimes: dict[str, 'RegimeHypothesisSpec']

    @property
    def gamma_version(self) -> str:
        return 'hypothesis-gamma-calendar-month-v1'

    @model_validator(mode='after')
    def state_contract(self):
        if set(self.regimes) != {c.condition_id for c in self.conditions}:
            raise ValueError('每个观察条件必须登记状态主张或明确无条件主张')
        for spec in self.regimes.values():
            if spec.has_claim and spec.data_available and not set(spec.required_fields).issubset(self.allowed_fields):
                raise ValueError('状态代理字段不在已冻结数据范围内')
        return self


from factor_miner.regime import RegimeHypothesisSpec
FavorRegimePlan.model_rebuild()

from factor_miner.exploration_contract import ExplorationMeasure


class FavorExplorationPlan(FavorRegimePlan):
    """探索独立记录；不继承旧联合策略的通过资格，也不重写旧计划。"""
    version: Literal['favor-exploration-v1'] = 'favor-exploration-v1'
    conditions: tuple[ConditionContract, ...] = Field(min_length=1)
    slots: tuple[FavorSlot, ...] = Field(min_length=1)
    exploration: dict[str, ExplorationMeasure]

    @model_validator(mode='after')
    def exploration_contract(self):
        if set(self.exploration) != {c.condition_id for c in self.conditions}:
            raise ValueError('每个条件必须冻结探索测量类型与支持依据')
        if len(self.integration.validation_thresholds) != 1:
            raise ValueError('探索不搜索联合阈值，仅保留一个合同占位向量')
        for index, condition in enumerate(self.conditions):
            rule = self.exploration[condition.condition_id]
            if self.integration.validation_thresholds[0][index] != rule.activation_quantile:
                raise ValueError('探索激活阈值必须与计划中唯一向量一致')
            if rule.kind != 'continuous' and condition.activation_direction != 'high':
                raise ValueError('离散激活由 active_values 定义，不能再翻转排名方向')
        return self


def parse_favor_plan(payload: dict) -> FavorPlan:
    cls = {'favor-regime-v1': FavorRegimePlan, 'favor-exploration-v1': FavorExplorationPlan}.get(payload.get('version'), FavorPlan)
    return cls.model_validate(payload)


class FavorRegimeSubmission(FavorSubmission):
    regime_sha256: str
