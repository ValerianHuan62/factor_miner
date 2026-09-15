"""Dashboard 回测情景分析的纯计算函数。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from statistics import mean, stdev
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ScenarioMetrics:
    """选定区间与成本假设下的只读情景指标。"""

    observations: int
    period_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe: float
    calmar_ratio: float | None
    information_ratio: float
    max_drawdown: float
    average_turnover: float
    total_cost: float


def _date(value: object) -> date:
    """把正式逐期产物中的日期收窄为 date。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError("逐期收益缺少有效日期")


def _finite(row: dict[str, Any], field: str) -> float:
    """读取有限数值，缺失时硬失败而不是补零。"""

    value = row.get(field)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"逐期收益缺少有限数值 {field}")
    return float(value)


def infer_published_cost_bps(rows: Iterable[dict[str, Any]]) -> float | None:
    """由正式成本与换手率反推冻结成本率，仅用于设置 UI 默认值。"""

    total_cost = 0.0
    total_turnover = 0.0
    for row in rows:
        cost = row.get("target_long_cost")
        turnover = row.get("target_long_turnover")
        if not isinstance(cost, (int, float)) or not isinstance(turnover, (int, float)):
            continue
        if float(turnover) < 0 or float(cost) < 0:
            continue
        total_cost += float(cost)
        total_turnover += float(turnover)
    return total_cost / total_turnover * 10_000.0 if total_turnover > 0 else None


def scenario_rows(
    rows: Iterable[dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    cost_bps: float,
    slippage_bps: float,
) -> list[dict[str, Any]]:
    """按退出日筛选，并用显式总成本率重算净收益。"""

    if start_date > end_date:
        raise ValueError("回测开始日期不能晚于结束日期")
    if cost_bps < 0 or slippage_bps < 0:
        raise ValueError("成本与滑点不能为负")
    rate = (cost_bps + slippage_bps) / 10_000.0
    result: list[dict[str, Any]] = []
    for source in rows:
        exit_date = _date(source.get("exit_date"))
        if not start_date <= exit_date <= end_date:
            continue
        entry_date = _date(source.get("entry_date"))
        gross = _finite(source, "target_long_gross_return")
        turnover = _finite(source, "target_long_turnover")
        benchmark = _finite(source, "benchmark_return")
        if turnover < 0:
            raise ValueError("逐期换手率不能为负")
        row_rate = rate / float(source.get("cost_turnover_sides", 1))
        net = gross - turnover * row_rate
        if net < -1.0 - 1e-12 or benchmark < -1.0 - 1e-12:
            raise ValueError("情景收益不能小于 -100%")
        net = max(-1.0, net)
        result.append(
            {
                "entry_date": entry_date,
                "exit_date": exit_date,
                "gross_return": gross,
                "turnover": turnover,
                "scenario_cost": turnover * row_rate,
                "scenario_net_return": net,
                "published_net_return": source.get("target_long_net_return"),
                "benchmark_return": benchmark,
            }
        )
    return sorted(result, key=lambda row: (row["exit_date"], row["entry_date"]))


def nav_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐期收益转换为情景净值、基准净值和回撤路径。"""

    scenario_nav = 1.0
    benchmark_nav = 1.0
    published_nav = 1.0
    peak = 1.0
    result: list[dict[str, Any]] = []
    for row in rows:
        scenario_nav *= 1.0 + float(row["scenario_net_return"])
        benchmark_nav *= 1.0 + float(row["benchmark_return"])
        published = row.get("published_net_return")
        published_nav = (
            published_nav * (1.0 + float(published))
            if isinstance(published, (int, float))
            else math.nan
        )
        peak = max(peak, scenario_nav)
        result.append(
            {
                "date": row["exit_date"],
                "scenario_nav": scenario_nav,
                "benchmark_nav": benchmark_nav,
                "published_nav": published_nav,
                "drawdown": scenario_nav / peak - 1.0,
            }
        )
    return result


def calculate_scenario_metrics(rows: Iterable[dict[str, Any]]) -> ScenarioMetrics:
    """使用所选期间的实际端点计算情景年化与风险指标。"""

    values = list(rows)
    if len(values) < 2:
        raise ValueError("情景回测至少需要两个逐期观测")
    returns = [float(row["scenario_net_return"]) for row in values]
    benchmarks = [float(row["benchmark_return"]) for row in values]
    excess = [value - benchmark for value, benchmark in zip(returns, benchmarks, strict=True)]
    elapsed_days = (_date(values[-1]["exit_date"]) - _date(values[0]["entry_date"])).days
    if elapsed_days <= 0:
        raise ValueError("情景回测日期跨度必须为正")
    observations_per_year = len(values) * 365.25 / elapsed_days
    scale = math.sqrt(observations_per_year)
    wealth = math.prod(1.0 + value for value in returns)
    annualized_return = wealth ** (365.25 / elapsed_days) - 1.0
    volatility = stdev(returns)
    excess_volatility = stdev(excess)
    path = nav_rows(values)
    max_drawdown = abs(min(float(row["drawdown"]) for row in path))
    return ScenarioMetrics(
        observations=len(values),
        period_return=wealth - 1.0,
        annualized_return=annualized_return,
        annualized_volatility=volatility * scale,
        sharpe=0.0 if volatility == 0.0 else mean(returns) / volatility * scale,
        calmar_ratio=(
            None if max_drawdown == 0.0 else annualized_return / max_drawdown
        ),
        information_ratio=(
            0.0 if excess_volatility == 0.0 else mean(excess) / excess_volatility * scale
        ),
        max_drawdown=max_drawdown,
        average_turnover=mean(float(row["turnover"]) for row in values),
        total_cost=sum(float(row["scenario_cost"]) for row in values),
    )
