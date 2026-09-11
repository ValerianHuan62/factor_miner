"""跨市场探索资格：测量类型和检验依据必须先于公式与收益冻结。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class ExplorationMeasure(BaseModel):
    """样本支持与信号激活分开；事件不要求在每只股票上反复出现。"""

    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['continuous', 'state', 'event']
    rationale: str = Field(min_length=10)
    observation_frequency: Literal['daily', 'weekly_last_session', 'monthly_last_session'] = 'daily'
    min_dates: int = Field(ge=2)
    min_assets: int = Field(ge=2)
    min_group_samples: int = Field(ge=2)
    min_signal_coverage: float = Field(default=.8, ge=.8, le=1)
    min_rank_correlation: float = Field(default=.3, gt=0, le=1)
    # 连续值只冻结一档探索持仓；状态与事件直接使用有经济含义的原始值。
    activation_quantile: float = Field(default=.9, gt=.5, lt=1)
    state_values: tuple[FiniteFloat, ...] = ()
    active_values: tuple[FiniteFloat, ...] = ()

    @model_validator(mode='after')
    def coherent(self):
        if self.kind == 'continuous':
            if self.state_values or self.active_values:
                raise ValueError('连续测量不能同时指定离散状态')
        elif (len(set(self.state_values)) != len(self.state_values)
              or len(set(self.active_values)) != len(self.active_values)
              or len(self.state_values) < 2 or not self.active_values
              or not set(self.active_values) < set(self.state_values)):
            raise ValueError('状态和事件必须冻结唯一取值、激活组及非空对照组')
        return self
