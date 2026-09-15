"""目标多头组合、内部极端组治理诊断、换手和成本计算。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Mapping

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_schema import (
    PortfolioEvaluationPolicy,
    portfolio_policy_id,
)
from factor_miner.trading_schedule import RebalanceWindow


class ExtremeSpreadDiagnostic(BaseModel):
    """仅供治理胜率使用的逐期高低极端组差。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_date: date
    exit_date: date
    extreme_spread_net_return: float


class PortfolioGovernanceSummary(BaseModel):
    """不进入公开组合日序列和指标的内部治理摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    win_rate: float = Field(ge=0.0, le=1.0)
    extreme_spread_daily: tuple[ExtremeSpreadDiagnostic, ...] = Field(min_length=1)

    @classmethod
    def build(
        cls,
        extreme_spread_daily: tuple[ExtremeSpreadDiagnostic, ...],
    ) -> PortfolioGovernanceSummary:
        """由冻结的高值减低值逐期净收益计算内部胜率。"""

        return cls(
            win_rate=(
                sum(item.extreme_spread_net_return > 0.0 for item in extreme_spread_daily)
                / len(extreme_spread_daily)
            ),
            extreme_spread_daily=extreme_spread_daily,
        )


class PortfolioBacktestResult(BaseModel):
    """可写入 JSON/Parquet 的组合回测结果摘要和长表行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    direction: Literal["positive", "negative"]
    daily_returns: tuple[dict[str, object], ...] = Field(min_length=1)
    weights: tuple[dict[str, object], ...] = Field(min_length=1)
    orders: tuple[dict[str, object], ...] = ()
    holdings_daily: tuple[dict[str, object], ...] = ()
    execution_summary: dict[str, object] = Field(default_factory=dict)
    governance: PortfolioGovernanceSummary
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        policy_id: str,
        direction: Literal["positive", "negative"],
        daily_returns: tuple[dict[str, object], ...],
        weights: tuple[dict[str, object], ...],
        governance: PortfolioGovernanceSummary,
        orders: tuple[dict[str, object], ...] = (),
        holdings_daily: tuple[dict[str, object], ...] = (),
        execution_summary: dict[str, object] | None = None,
    ) -> PortfolioBacktestResult:
        """由完整结果长表构造内容寻址结果。"""

        _validate_public_result_rows(daily_returns, weights)

        def jsonable(value: object) -> object:
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, dict):
                return {str(key): jsonable(item) for key, item in value.items()}
            if isinstance(value, tuple | list):
                return [jsonable(item) for item in value]
            return value

        result_hash = sha256_json(
            {
                "policy_id": policy_id,
                "direction": direction,
                "daily_returns": jsonable(daily_returns),
                "weights": jsonable(weights),
                "orders": jsonable(orders),
                "holdings_daily": jsonable(holdings_daily),
                "execution_summary": jsonable(execution_summary or {}),
                "governance": governance.model_dump(mode="json"),
            }
        )
        return cls(
            policy_id=policy_id,
            direction=direction,
            daily_returns=daily_returns,
            weights=weights,
            orders=orders,
            holdings_daily=holdings_daily,
            execution_summary=execution_summary or {},
            governance=governance,
            result_sha256=result_hash,
        )


_PUBLIC_DAILY_RETURN_FIELDS = frozenset(
    {
        "entry_date",
        "exit_date",
        "target_long_gross_return",
        "target_long_turnover",
        "target_long_cost",
        "target_long_net_return",
        "benchmark_return",
    }
)
_PUBLIC_WEIGHT_FIELDS = frozenset(
    {
        "signal_date",
        "entry_date",
        "exit_date",
        "security_id",
        "portfolio",
        "weight",
    }
)


def _validate_public_result_rows(
    daily_returns: tuple[dict[str, object], ...],
    weights: tuple[dict[str, object], ...],
) -> None:
    """拒绝把分组、多空或未知字段写进公开组合结果。"""

    for row in daily_returns:
        if not isinstance(row, Mapping) or set(row) != _PUBLIC_DAILY_RETURN_FIELDS:
            raise _portfolio_error("公开组合日序列字段必须精确符合目标多头合同")
    for row in weights:
        if not isinstance(row, Mapping) or set(row) != _PUBLIC_WEIGHT_FIELDS:
            raise _portfolio_error("公开组合持仓字段必须精确符合目标多头合同")
        if row.get("portfolio") != "target_long":
            raise _portfolio_error("公开组合持仓 portfolio 必须是 target_long")


def _portfolio_error(message: str) -> FactorMinerError:
    """构造组合评价合同错误。"""

    return FactorMinerError(FailureCode.PORTFOLIO_DATA_CONTRACT_INVALID, message)


def run_target_long_backtest(
    factor_panel: pl.DataFrame | pl.LazyFrame,
    market_panel: pl.DataFrame | pl.LazyFrame,
    state_panel: pl.DataFrame | pl.LazyFrame,
    benchmark: pl.DataFrame | pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    policy: PortfolioEvaluationPolicy,
    *,
    direction: Literal["positive", "negative"] = "positive",
) -> PortfolioBacktestResult:
    """先按 T 日冻结选择，再通过因果订单状态机执行目标多头。"""

    if direction not in {"positive", "negative"}:
        raise _portfolio_error("因子方向只能是 positive 或 negative")
    if not schedule:
        raise _portfolio_error("组合回测调仓窗口不能为空")
    benchmark_frame = benchmark.collect() if isinstance(benchmark, pl.LazyFrame) else benchmark
    if set(benchmark_frame.columns) != {
        "entry_date",
        "exit_date",
        "benchmark_return",
    }:
        raise _portfolio_error("benchmark 必须精确包含 entry_date、exit_date 和 benchmark_return")
    if benchmark_frame.select(pl.col("exit_date").is_duplicated().any()).item():
        raise _portfolio_error("benchmark exit_date 重复")
    target = simulate_causal_extreme_portfolio(
        factor_panel,
        market_panel,
        state_panel,
        schedule,
        direction=direction,
        group_count=policy.group_count,
        round_trip_cost_bps=policy.round_trip_cost_bps,
    )
    high = target if direction == "positive" else simulate_causal_extreme_portfolio(
        factor_panel, market_panel, state_panel, schedule,
        direction="positive", group_count=policy.group_count,
        round_trip_cost_bps=policy.round_trip_cost_bps,
    )
    low = target if direction == "negative" else simulate_causal_extreme_portfolio(
        factor_panel, market_panel, state_panel, schedule,
        direction="negative", group_count=policy.group_count,
        round_trip_cost_bps=policy.round_trip_cost_bps,
    )
    benchmark_map = {
        (row["entry_date"], row["exit_date"]): float(row["benchmark_return"])
        for row in benchmark_frame.iter_rows(named=True)
    }
    rows = tuple(
        {
            **row,
            "benchmark_return": benchmark_map[(row["entry_date"], row["exit_date"])],
        }
        for row in target.daily_returns
    )
    weight_rows = tuple(
        {
            "signal_date": row["signal_date"],
            "entry_date": row["entry_date"],
            "exit_date": row["scheduled_exit_date"],
            "security_id": row["security_id"],
            "portfolio": "target_long",
            "weight": row["target_weight"],
        }
        for row in target.selections
    )
    high_map = {
        (row["entry_date"], row["exit_date"]): float(row["target_long_net_return"])
        for row in high.daily_returns
    }
    low_map = {
        (row["entry_date"], row["exit_date"]): float(row["target_long_net_return"])
        for row in low.daily_returns
    }
    governance_rows = tuple(
        {
            "entry_date": entry_date,
            "exit_date": exit_date,
            "extreme_spread_net_return": high_return - low_map[(entry_date, exit_date)],
        }
        for (entry_date, exit_date), high_return in sorted(high_map.items())
    )
    governance = PortfolioGovernanceSummary.build(
        tuple(ExtremeSpreadDiagnostic.model_validate(item) for item in governance_rows)
    )
    return PortfolioBacktestResult.build(
        policy_id=portfolio_policy_id(policy),
        direction=direction,
        daily_returns=rows,
        weights=weight_rows,
        orders=target.orders,
        holdings_daily=target.holdings_daily,
        execution_summary=target.execution_summary,
        governance=governance,
    )


def run_portfolio_backtest(
    factor_panel: pl.DataFrame | pl.LazyFrame,
    market_panel: pl.DataFrame | pl.LazyFrame,
    state_panel: pl.DataFrame | pl.LazyFrame,
    benchmark: pl.DataFrame | pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    policy: PortfolioEvaluationPolicy,
    *,
    direction: Literal["positive", "negative"] = "positive",
) -> PortfolioBacktestResult:
    """保留模块入口名称，执行目标多头而非旧多空协议。"""

    return run_target_long_backtest(
        factor_panel,
        market_panel,
        state_panel,
        benchmark,
        schedule,
        policy,
        direction=direction,
    )
