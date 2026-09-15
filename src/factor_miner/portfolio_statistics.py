"""组合回测指标：实际交易日日历、净 Sharpe、回撤和基准超额。"""

from __future__ import annotations

from datetime import date
import math
from typing import Iterable

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


class PortfolioSeriesMetrics(BaseModel):
    """一个组合收益序列的完整指标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: int = Field(ge=2)
    period_return: float
    annualized_return: float
    excess_return: float
    excess_annualized_return: float
    annualized_volatility: float
    excess_annualized_volatility: float
    sharpe: float
    information_ratio: float
    max_drawdown: float = Field(ge=0)
    excess_max_drawdown: float = Field(ge=0)
    annualization_factor: float = Field(gt=0)
    return_basis: str = "net_or_gross_as_named"


class PortfolioMetrics(BaseModel):
    """目标多头毛净收益和基准序列的指标集合。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar_version: str
    calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark: str = "CSI300"
    series: dict[str, PortfolioSeriesMetrics]
    metrics_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        calendar_version: str,
        calendar_sha256: str,
        series: dict[str, PortfolioSeriesMetrics],
    ) -> PortfolioMetrics:
        """构造内容寻址的指标对象。"""

        payload = {
            "calendar_version": calendar_version,
            "calendar_sha256": calendar_sha256,
            "benchmark": "CSI300",
            "series": {key: value.model_dump(mode="json") for key, value in series.items()},
        }
        return cls(
            calendar_version=calendar_version,
            calendar_sha256=calendar_sha256,
            series=series,
            metrics_sha256=sha256_json(payload),
        )


def _statistics_error(message: str) -> FactorMinerError:
    """构造统计输入合同错误。"""

    return FactorMinerError(FailureCode.INSUFFICIENT_VALID_DATES, message)


def _compound(values: Iterable[float]) -> float:
    """计算连续持有的复合收益。"""

    wealth = 1.0
    for value in values:
        if not math.isfinite(value) or value <= -1.0:
            raise _statistics_error("收益序列包含非有限值或小于等于 -100% 的值")
        wealth *= 1.0 + value
    return wealth - 1.0


def _max_drawdown(values: list[float]) -> float:
    """返回财富曲线最大回撤的正数幅度。"""

    wealth = 1.0
    peak = wealth
    maximum = 0.0
    for value in values:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        maximum = max(maximum, 1.0 - wealth / peak)
    return maximum


def _sample_std(values: list[float]) -> float:
    """返回样本标准差，避免静默把单点序列当作零风险。"""

    if len(values) < 2:
        raise _statistics_error("收益序列至少需要两个观测")
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _calendar_sessions(calendar: pl.DataFrame) -> tuple[date, ...]:
    """提取真实开放交易日并核验唯一性。"""

    if set(calendar.columns) != {"trade_date", "is_open"}:
        raise _statistics_error("交易日日历必须精确包含 trade_date 和 is_open")
    if calendar.schema["trade_date"] != pl.Date or calendar.schema["is_open"] != pl.Boolean:
        raise _statistics_error("交易日日历字段类型不正确")
    if calendar.select(pl.col("trade_date").is_duplicated().any()).item():
        raise _statistics_error("交易日日历存在重复日期")
    dates = tuple(
        calendar.filter(pl.col("is_open")).select("trade_date").to_series().to_list()
    )
    if len(dates) < 2:
        raise _statistics_error("交易日日历开放日不足")
    return tuple(sorted(dates))


def _period_session_counts(
    entry_dates: list[date],
    exit_dates: list[date],
    sessions: tuple[date, ...],
) -> list[int]:
    """按每个 open-to-open 区间数真实开放交易日。"""

    counts: list[int] = []
    for entry, exit_date in zip(entry_dates, exit_dates, strict=True):
        if entry >= exit_date:
            raise _statistics_error("收益区间日期顺序错误")
        if entry not in sessions or exit_date not in sessions:
            raise _statistics_error("收益区间端点不在真实交易日日历")
        count = sum(entry < session <= exit_date for session in sessions)
        if count < 1:
            raise _statistics_error("收益区间没有真实交易日")
        counts.append(count)
    return counts


def _series_metrics(
    values: list[float],
    benchmark_values: list[float],
    session_counts: list[int],
    annual_sessions: float,
) -> PortfolioSeriesMetrics:
    """计算一个收益序列的毛/净、超额和风险指标。"""

    if len(values) != len(benchmark_values) or len(values) != len(session_counts):
        raise _statistics_error("组合、基准和交易日区间长度不一致")
    observations = len(values)
    if observations < 2:
        raise _statistics_error("收益序列观测数不足")
    period_sessions = sum(session_counts)
    if period_sessions <= 0:
        raise _statistics_error("收益序列没有真实交易日")
    annualization_factor = annual_sessions / (period_sessions / observations)
    scale = math.sqrt(annualization_factor)
    excess = [
        (1.0 + value) / (1.0 + benchmark) - 1.0
        for value, benchmark in zip(values, benchmark_values, strict=True)
    ]
    volatility = _sample_std(values)
    excess_volatility = _sample_std(excess)
    sharpe = 0.0 if volatility == 0.0 else (sum(values) / observations) / volatility * scale
    information_ratio = (
        0.0
        if excess_volatility == 0.0
        else (sum(excess) / observations) / excess_volatility * scale
    )
    return PortfolioSeriesMetrics(
        observations=observations,
        period_return=_compound(values),
        annualized_return=(1.0 + _compound(values)) ** (annual_sessions / period_sessions) - 1.0,
        excess_return=_compound(excess),
        excess_annualized_return=(1.0 + _compound(excess)) ** (annual_sessions / period_sessions) - 1.0,
        annualized_volatility=volatility * scale,
        excess_annualized_volatility=excess_volatility * scale,
        sharpe=sharpe,
        information_ratio=information_ratio,
        max_drawdown=_max_drawdown(values),
        excess_max_drawdown=_max_drawdown(excess),
        annualization_factor=annualization_factor,
    )


def calculate_portfolio_metrics(
    daily_returns: pl.DataFrame,
    benchmark_returns: pl.DataFrame,
    calendar: pl.DataFrame,
    *,
    calendar_version: str,
    calendar_sha256: str,
) -> PortfolioMetrics:
    """按实际交易日日历计算各组合收益序列指标。

    `daily_returns` 只能包含目标多头的毛净收益和基准收益；
    `benchmark_returns` 必须含唯一的逐交易日 entry_date、exit_date 和 benchmark_return。
    """

    required = {"entry_date", "exit_date"}
    if not required.issubset(daily_returns.columns):
        raise _statistics_error("组合收益缺少 entry_date 或 exit_date")
    if set(benchmark_returns.columns) != {"entry_date", "exit_date", "benchmark_return"}:
        raise _statistics_error(
            "基准收益必须精确包含 entry_date、exit_date 和 benchmark_return"
        )
    if daily_returns.height == 0 or benchmark_returns.height == 0:
        raise _statistics_error("组合或基准收益为空")
    if daily_returns.select(pl.col("exit_date").is_duplicated().any()).item():
        raise _statistics_error("组合收益 exit_date 重复")
    if benchmark_returns.select(pl.col("exit_date").is_duplicated().any()).item():
        raise _statistics_error("基准收益 exit_date 重复")
    sessions = _calendar_sessions(calendar)
    dates = daily_returns.sort("exit_date")
    entry_dates = dates["entry_date"].to_list()
    exit_dates = dates["exit_date"].to_list()
    session_counts = _period_session_counts(entry_dates, exit_dates, sessions)
    first_date = min(entry_dates)
    last_date = max(exit_dates)
    visible_sessions = [item for item in sessions if first_date <= item <= last_date]
    years = len({item.year for item in visible_sessions})
    if years < 1:
        raise _statistics_error("收益区间无法从真实交易日日历确定年份")
    annual_sessions = len(visible_sessions) / years
    benchmark_map = dict(
        zip(
            benchmark_returns["exit_date"].to_list(),
            benchmark_returns["benchmark_return"].to_list(),
            strict=True,
        )
    )
    try:
        benchmark_values = [benchmark_map[item] for item in exit_dates]
    except KeyError:
        raise _statistics_error("基准收益缺少组合收益对应的 exit_date") from None
    result: dict[str, PortfolioSeriesMetrics] = {}
    return_columns = {column for column in dates.columns if column.endswith("_return")}
    expected_return_columns = {
        "target_long_gross_return",
        "target_long_net_return",
        "benchmark_return",
    }
    if return_columns != expected_return_columns:
        raise _statistics_error("组合收益只能包含目标多头毛净收益和基准收益")
    for column in ("target_long_gross_return", "target_long_net_return"):
        result[column] = _series_metrics(
            dates[column].to_list(),
            benchmark_values,
            session_counts,
            annual_sessions,
        )
    result["CSI300"] = _series_metrics(
        benchmark_values,
        benchmark_values,
        session_counts,
        annual_sessions,
    )
    return PortfolioMetrics.build(
        calendar_version=calendar_version,
        calendar_sha256=calendar_sha256,
        series=result,
    )
