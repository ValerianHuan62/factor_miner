"""组合回测、交易日历和成本口径的冻结合同。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json


class TradingCalendarIdentity(BaseModel):
    """真实交易日日历的内容身份，不把年化天数写成常量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar_version: str = Field(min_length=1)
    calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange_scope: Literal["SSE_SZSE"] = "SSE_SZSE"
    source: Literal["server_trading_calendar"] = "server_trading_calendar"

    @field_validator("calendar_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        """日历版本不能是空白。"""

        if not value.strip():
            raise ValueError("交易日日历版本不能为空")
        return value


class PortfolioEvaluationPolicy(BaseModel):
    """目标多头组合评价的单一冻结口径。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["3"] = "3"
    rebalance_weekday: Literal["Tuesday"] = "Tuesday"
    signal_timing: Literal["prior_trade_close"] = "prior_trade_close"
    entry_timing: Literal["scheduled_open"] = "scheduled_open"
    exit_timing: Literal["next_scheduled_open"] = "next_scheduled_open"
    selection_timing: Literal["signal_day_before_future_execution"] = (
        "signal_day_before_future_execution"
    )
    unfilled_entry_policy: Literal["cash_no_replacement_no_retry"] = (
        "cash_no_replacement_no_retry"
    )
    delayed_exit_policy: Literal["retry_each_market_session"] = (
        "retry_each_market_session"
    )
    terminal_policy: Literal["fail_closed"] = "fail_closed"
    return_interval: Literal["open_to_open"] = "open_to_open"
    group_count: Literal[10] = 10
    group_weighting: Literal["equal_weight"] = "equal_weight"
    benchmark: Literal["CSI300"] = "CSI300"
    round_trip_cost_bps: Literal[14] = 14
    cost_definition: Literal["turnover_times_round_trip_bps"] = (
        "turnover_times_round_trip_bps"
    )
    annualization_basis: Literal["actual_trading_calendar"] = (
        "actual_trading_calendar"
    )
    sharpe_basis: Literal["net_of_cost"] = "net_of_cost"
    metric_return_basis: Literal["gross_and_net"] = "gross_and_net"

def portfolio_policy_id(policy: PortfolioEvaluationPolicy) -> str:
    """根据完整组合政策生成内容寻址身份。"""

    return f"portpolicy_{sha256_json(policy.model_dump(mode='json'))[:24]}"
