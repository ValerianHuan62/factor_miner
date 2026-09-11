"""FaVOR 五分位真实构念验证：语义映射事前冻结，统计判定由程序执行。"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.compiler import build_polars_expr
from factor_miner.hypothesis_constraints import ConditionContract


class ConstructPolicy(BaseModel):
    """五组统计的有限、事前冻结容差与支持要求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    min_assets_per_date: int = Field(default=20, ge=5)
    min_dates: int = Field(default=60, ge=2)
    min_bin_samples: int = Field(default=20, ge=2)
    min_signal_coverage: float = Field(default=.8, gt=0, le=1)
    relative_tolerance: float = Field(default=1e-6, gt=0, lt=1)
    min_bin_correlation: float = Field(default=.5, gt=0, le=1)


def require_panel(frame: pl.DataFrame, columns: set[str], name: str) -> None:
    """主键、类型、空主键与列缺失是输入错误，不是构念失败。"""
    if columns - set(frame.columns):
        raise ValueError(f"{name} 缺少字段：{sorted(columns-set(frame.columns))}")
    if frame.schema.get("date") != pl.Date:
        raise ValueError(f"{name} date 必须为 Date")
    if frame.select(pl.struct("date", "asset").is_duplicated().any()).item():
        raise ValueError(f"{name} 主键重复")
    if frame.select(pl.any_horizontal(pl.col("date").is_null(), pl.col("asset").is_null()).any()).item():
        raise ValueError(f"{name} 主键为空")


def signal_feasibility(raw: pl.DataFrame, state: pl.DataFrame, start: date, end: date,
                       policy: ConstructPolicy) -> dict:
    """正式候选只看发现期的有效覆盖，低于冻结门槛时无需进行昂贵测量。"""
    require_panel(raw, {'date', 'asset', 'raw_factor', 'valid_for_factor_compute'}, '因子')
    require_panel(state, {'date', 'asset', 'valid_for_factor_rank'}, '状态')
    universe = state.filter(pl.col('date').is_between(start, end) & pl.col('valid_for_factor_rank'))
    eligible = universe.select('date', 'asset').join(raw, on=['date', 'asset'], how='left', validate='1:1').filter(
        pl.col('valid_for_factor_compute') & pl.col('raw_factor').is_finite())
    counts = universe.group_by('date').len(name='universe').join(
        eligible.group_by('date').len(name='usable'), on='date', how='left').with_columns(pl.col('usable').fill_null(0))
    coverage = counts.select((pl.col('usable') / pl.col('universe')).median()).item()
    dates = counts.filter(pl.col('usable') >= policy.min_assets_per_date).height
    return dict(feasible=coverage is not None and coverage >= policy.min_signal_coverage and dates >= policy.min_dates,
                signal_coverage=coverage, sufficient_dates=dates, return_labels_used=False)


def validate_empirical_construct(raw: pl.DataFrame, market: pl.DataFrame, state: pl.DataFrame,
                                 condition: ConditionContract, start: date, end: date,
                                 policy: ConstructPolicy) -> dict:
    """只用发现期当期市场状态；保持相同因子值同组，禁止按证券编号拆散并列值。"""
    require_panel(raw, {"date", "asset", "raw_factor", "valid_for_factor_compute"}, "因子")
    require_panel(market, {"date", "asset"}, "市场")
    require_panel(state, {"date", "asset", "valid_for_factor_rank"}, "状态")
    names = [f"state_{i}" for i in range(len(condition.state_measurements))]
    # 窗口在完整历史上计算，统计范围随后限制于发现期。
    observed = market.filter(pl.col("date") <= end).sort("asset", "date").with_columns(
        *(build_polars_expr(m.expression).alias(n) for n, m in zip(names, condition.state_measurements, strict=True)))
    frame = raw.filter(pl.col("date").is_between(start, end)).join(
        state.select("date", "asset", "valid_for_factor_rank"), on=["date", "asset"], how="left", validate="1:1")
    if frame["valid_for_factor_rank"].null_count():
        raise ValueError("构念面板缺少信号日排名状态")
    frame = frame.filter(pl.col("valid_for_factor_compute") & pl.col("valid_for_factor_rank") & pl.col("raw_factor").is_finite())
    universe = state.filter(pl.col("date").is_between(start,end) & pl.col("valid_for_factor_rank")).group_by("date").len(name="universe")
    coverage = universe.join(frame.group_by("date").len(name="usable"), on="date", how="left").select(
        (pl.col("usable").fill_null(0)/pl.col("universe")).median()).item()
    frame = frame.with_columns(pl.len().over("date").alias("cross_section_count")).filter(
        pl.col("cross_section_count") >= policy.min_assets_per_date)
    bin_expression = ((pl.col("raw_factor").rank(method="average").over("date") - .5) / pl.len().over("date") * 5)
    frame = frame.with_columns(bin_expression.floor().clip(0, 4).cast(pl.Int64).alias("factor_bin"))
    frame = frame.join(observed.select("date", "asset", *names), on=["date", "asset"], how="left", validate="1:1")
    results = []
    for column, measurement in zip(names, condition.state_measurements, strict=True):
        usable = frame.filter(pl.col(column).is_finite())
        profile = usable.group_by("factor_bin").agg(pl.len().alias("count"),
            pl.col(column).mean().alias("mean"), pl.col(column).median().alias("median"),
            pl.col(column).quantile(.1, interpolation="linear").alias("q10"),
            pl.col(column).quantile(.9, interpolation="linear").alias("q90"),
            pl.col(column).std().alias("std")).sort("factor_bin")
        support = (usable["date"].n_unique() >= policy.min_dates and profile.height == 5
                   and profile["count"].min() >= policy.min_bin_samples)
        checks = {"central_tendency": False, "tail_variation": False, "statistical_agreement": False, "semantic_direction": False}
        if support:
            means, medians = profile["mean"].to_numpy(), profile["median"].to_numpy()
            spread = profile["q90"].to_numpy() - profile["q10"].to_numpy()
            scale = max(float(usable[column].std() or 0), abs(float(usable[column].mean() or 0)), 1e-12)
            epsilon = policy.relative_tolerance * scale
            direction = 1 if measurement.expected_direction == "increase" else -1
            corr = float(np.corrcoef(np.arange(5), means)[0, 1]) if np.std(means) > epsilon else 0.
            dm, dd = np.diff(means), np.diff(medians)
            aligned = bool(np.all(np.sign(np.where(abs(dm) <= epsilon, 0, dm)) == np.sign(np.where(abs(dd) <= epsilon, 0, dd))))
            semantic = bool(np.all(direction*dm >= -epsilon) and np.all(direction*dd >= -epsilon)
                            and direction*(means[-1]-means[0]) > epsilon and direction*(medians[-1]-medians[0]) > epsilon)
            checks = dict(central_tendency=bool(math.isfinite(corr) and abs(corr) >= policy.min_bin_correlation),
                          tail_variation=bool(np.ptp(spread) > epsilon), statistical_agreement=aligned,
                          semantic_direction=semantic)
        results.append(dict(name=measurement.name, expected_direction=measurement.expected_direction,
                            sufficient_support=bool(support), checks=checks, profile=profile.to_dicts(),
                            passed=bool(support and all(checks.values()))))
    # 所有事前指定的观察量都要通过；不得看结果后挑一个支持叙事的变量。
    return dict(version="favor-empirical-v1", passed=bool(coverage is not None and coverage >= policy.min_signal_coverage and all(x["passed"] for x in results)),
                signal_coverage=coverage, measurements=results,
                start=start.isoformat(), end=end.isoformat(), return_labels_used=False,
                mechanism_status="mechanism_unverified")
