"""目标多头组合、内部极端组治理诊断、换手和成本计算。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Mapping

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
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
                "governance": governance.model_dump(mode="json"),
            }
        )
        return cls(
            policy_id=policy_id,
            direction=direction,
            daily_returns=daily_returns,
            weights=weights,
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


def _validate_panel(panel: pl.LazyFrame) -> None:
    """检查已对齐 open-to-open 面板的最小字段。"""

    required = {
        "signal_date",
        "entry_date",
        "exit_date",
        "security_id",
        "factor_value",
        "asset_return",
    }
    missing = required.difference(panel.collect_schema().names())
    if missing:
        raise _portfolio_error(f"组合面板缺少字段：{sorted(missing)}")


def _calculate_turnover(
    positions: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
) -> pl.LazyFrame:
    """按同一持仓组合的前一期权重计算单边换手。"""

    entries = [item.entry_date for item in schedule]
    previous_map = pl.DataFrame(
        {"entry_date": entries, "previous_entry_date": [None, *entries[:-1]]}
    ).lazy()
    positions_with_previous = positions.join(previous_map, on="entry_date", how="left")
    previous_positions = positions.select(
        [
            pl.col("entry_date").alias("previous_entry_date"),
            "portfolio",
            "security_id",
            pl.col("weight").alias("previous_weight"),
        ]
    )
    return (
        positions_with_previous.join(
            previous_positions,
            on=["previous_entry_date", "portfolio", "security_id"],
            how="left",
        )
        .with_columns(
            pl.when(pl.col("previous_weight").is_null())
            .then(pl.lit(0.0))
            .otherwise(pl.col("previous_weight"))
            .alias("previous_weight_effective")
        )
        .group_by(["entry_date", "portfolio"])
        .agg(
            pl.when(pl.col("previous_entry_date").first().is_null())
            .then(pl.lit(1.0))
            .otherwise(
                1.0 - (pl.min_horizontal("weight", "previous_weight_effective")).sum()
            )
            .alias("turnover")
        )
    )


def run_target_long_backtest(
    panel: pl.LazyFrame,
    benchmark: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    policy: PortfolioEvaluationPolicy,
    *,
    direction: Literal["positive", "negative"] = "positive",
) -> PortfolioBacktestResult:
    """执行冻结的目标多头回测和内部极端组胜率诊断。

    `benchmark` 必须包含 `exit_date` 与 `benchmark_return`；当前函数保留该列
    供组合结果对账，超额收益和信息比率在统计层计算。
    """

    _validate_panel(panel)
    if direction not in {"positive", "negative"}:
        raise _portfolio_error("因子方向只能是 positive 或 negative")
    if not schedule:
        raise _portfolio_error("组合回测调仓窗口不能为空")
    if set(benchmark.collect_schema().names()) != {
        "exit_date",
        "benchmark_return",
    }:
        raise _portfolio_error("benchmark 必须精确包含 exit_date 和 benchmark_return")
    benchmark_duplicate = (
        benchmark.group_by("exit_date").len().filter(pl.col("len") > 1).limit(1).collect()
    )
    if benchmark_duplicate.height:
        raise _portfolio_error("benchmark exit_date 重复")

    data = panel.filter(
        pl.col("factor_value").is_not_null()
        & pl.col("asset_return").is_not_null()
    )
    ordered = (
        data.sort(
            ["entry_date", "factor_value", "security_id"],
            descending=[False, True, False],
        )
        .with_columns(
            pl.col("factor_value")
            .rank(method="ordinal", descending=True)
            .over("entry_date")
            .alias("_ordinal_rank"),
            pl.len().over("entry_date").alias("_name_count"),
        )
        .with_columns(
            (
                ((pl.col("_ordinal_rank") - 1) * policy.group_count / pl.col("_name_count"))
                .floor()
                + 1
            )
            .cast(pl.Int64)
            .alias("_group")
        )
    )
    invalid_counts = (
        ordered.group_by("entry_date")
        .agg(pl.col("_name_count").first().alias("name_count"))
        .filter(pl.col("name_count") < policy.group_count)
        .limit(1)
        .collect()
    )
    if invalid_counts.height:
        raise _portfolio_error("某个调仓窗口可分组股票少于 10 只")
    empty_groups = (
        ordered.group_by(["entry_date", "_group"])
        .len()
        .group_by("entry_date")
        .agg(pl.len().alias("group_count"))
        .filter(pl.col("group_count") != policy.group_count)
        .limit(1)
        .collect()
    )
    if empty_groups.height:
        raise _portfolio_error("某个调仓窗口存在空分组")

    grouped_positions = (
        ordered.with_columns(
            (
                1.0 / pl.len().over(["entry_date", "_group"])
            ).alias("weight"),
        )
        .select(
            "signal_date",
            "entry_date",
            "exit_date",
            "security_id",
            "_group",
            "weight",
            "asset_return",
        )
    )
    target_group = 1 if direction == "positive" else policy.group_count
    target_positions = grouped_positions.filter(pl.col("_group") == target_group).with_columns(
        pl.lit("target_long").alias("portfolio")
    )
    target_returns = target_positions.group_by(["entry_date", "exit_date"]).agg(
        (pl.col("weight") * pl.col("asset_return")).sum().alias("target_long_gross_return")
    )
    target_turnover = _calculate_turnover(target_positions, schedule).select(
        ["entry_date", pl.col("turnover").alias("target_long_turnover")]
    )
    cost_rate = policy.round_trip_cost_bps / 10_000.0
    final = (
        target_returns.join(target_turnover, on="entry_date", how="inner")
        .with_columns(
            (pl.col("target_long_turnover") * cost_rate).alias("target_long_cost"),
        )
        .with_columns(
            (
                pl.col("target_long_gross_return")
                - pl.col("target_long_cost")
            ).alias("target_long_net_return")
        )
        .join(benchmark, on="exit_date", how="inner")
        .select(
            "entry_date",
            "exit_date",
            "target_long_gross_return",
            "target_long_turnover",
            "target_long_cost",
            "target_long_net_return",
            "benchmark_return",
        )
    )
    rows = tuple(final.sort("entry_date").collect().to_dicts())
    weight_rows = tuple(
        target_positions.select(
            "signal_date", "entry_date", "exit_date", "security_id", "portfolio", "weight"
        )
        .sort(["entry_date", "security_id"])
        .collect()
        .to_dicts()
    )
    if not rows:
        raise _portfolio_error("组合回测没有可用收益窗口")
    governance_positions = grouped_positions.filter(pl.col("_group").is_in([1, policy.group_count])).with_columns(
        pl.when(pl.col("_group") == 1)
        .then(pl.lit("high"))
        .otherwise(pl.lit("low"))
        .alias("portfolio")
    )
    governance_returns = (
        governance_positions.group_by(["entry_date", "exit_date", "portfolio"])
        .agg((pl.col("weight") * pl.col("asset_return")).sum().alias("gross_return"))
        .collect()
        .pivot(on="portfolio", index=["entry_date", "exit_date"], values="gross_return")
        .rename({"high": "high_gross_return", "low": "low_gross_return"})
        .lazy()
    )
    governance_turnover = (
        _calculate_turnover(governance_positions, schedule)
        .collect()
        .pivot(on="portfolio", index="entry_date", values="turnover")
        .rename({"high": "high_turnover", "low": "low_turnover"})
        .lazy()
    )
    governance_rows = tuple(
        governance_returns.join(governance_turnover, on="entry_date", how="inner")
        .with_columns(
            (
                pl.col("high_gross_return")
                - pl.col("low_gross_return")
                - (pl.col("high_turnover") + pl.col("low_turnover")) / 2.0 * cost_rate
            ).alias("extreme_spread_net_return")
        )
        .select("entry_date", "exit_date", "extreme_spread_net_return")
        .sort("entry_date")
        .collect()
        .to_dicts()
    )
    governance = PortfolioGovernanceSummary.build(
        tuple(ExtremeSpreadDiagnostic.model_validate(item) for item in governance_rows)
    )
    return PortfolioBacktestResult.build(
        policy_id=portfolio_policy_id(policy),
        direction=direction,
        daily_returns=rows,
        weights=weight_rows,
        governance=governance,
    )


def run_portfolio_backtest(
    panel: pl.LazyFrame,
    benchmark: pl.LazyFrame,
    schedule: tuple[RebalanceWindow, ...],
    policy: PortfolioEvaluationPolicy,
    *,
    direction: Literal["positive", "negative"] = "positive",
) -> PortfolioBacktestResult:
    """保留模块入口名称，执行目标多头而非旧多空协议。"""

    return run_target_long_backtest(panel, benchmark, schedule, policy, direction=direction)
