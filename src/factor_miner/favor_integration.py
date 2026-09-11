"""多条件 AND 触发、三档方向选择性和固定阈值的因果组合执行。"""
from __future__ import annotations

from datetime import date
import math

import numpy as np
import polars as pl

from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.favor_validation import require_panel
from factor_miner.trading_schedule import RebalanceWindow


def percentile_scores(raw: pl.DataFrame, state: pl.DataFrame, activation: str) -> pl.DataFrame:
    """只用信号日可排名股票，按条件触发方向生成截面百分位；同值不拆散。"""
    if activation not in {"high", "low"}:
        raise ValueError("未知条件触发方向")
    require_panel(raw, {"date", "asset", "raw_factor", "valid_for_factor_compute"}, "因子")
    require_panel(state, {"date", "asset", "valid_for_factor_rank"}, "状态")
    frame = raw.join(state.select("date", "asset", "valid_for_factor_rank"), on=["date", "asset"], how="left", validate="1:1")
    if frame["valid_for_factor_rank"].null_count():
        raise ValueError("信号缺少排名状态")
    frame = frame.filter(pl.col("valid_for_factor_rank") & pl.col("valid_for_factor_compute") & pl.col("raw_factor").is_finite())
    multiplier = 1 if activation == "high" else -1
    return frame.with_columns((pl.col("raw_factor")*multiplier).alias("oriented")).select("date", "asset",
        ((pl.col("oriented").rank(method="average").over("date")-.5)/pl.len().over("date")).alias("score"))


def joint_signals(scores: dict[str, pl.DataFrame], members: tuple[str, ...], thresholds: tuple[float, ...]) -> pl.DataFrame:
    """每个观察条件恰选一个因子，所有条件同时触发才入场；不读取标签。"""
    if len(members) < 2 or len(members) != len(thresholds) or len(members) != len(set(members)):
        raise ValueError("联合信号需要至少两个不同条件的因子及逐条件阈值")
    if any(not 0 < q < 1 for q in thresholds):
        raise ValueError("联合阈值必须在零一之间")
    frame = scores[members[0]].rename({"score": "score_0"})
    for i, member in enumerate(members[1:], 1):
        frame = frame.join(scores[member].rename({"score": f"score_{i}"}), on=["date", "asset"], how="inner", validate="1:1")
    return frame.select("date", "asset", pl.all_horizontal(
        *(pl.col(f"score_{i}").is_finite() & (pl.col(f"score_{i}") >= q) for i, q in enumerate(thresholds))).alias("trigger"))


def selectivity_feasibility(ladder: dict[float, pl.DataFrame], *, start: date, end: date,
                            min_events: int, min_tickers: int, support_threshold: float) -> dict:
    """只数触发次数，给出收益检验的乐观上限；缺标签和 purge 只会降低支持。"""
    if len(ladder) < 3 or start > end or min_events < 1 or min_tickers < 1 or not 0 < support_threshold <= 1:
        raise ValueError('事件数量预检参数不合法')
    counts = []
    for level in sorted(ladder):
        frame = ladder[level]
        require_panel(frame, {'date', 'asset', 'trigger'}, '联合信号')
        if frame.schema['trigger'] != pl.Boolean or frame['trigger'].null_count():
            raise ValueError('触发状态必须为非空布尔值')
        counts.append(dict(frame.filter(pl.col('date').is_between(start, end) & pl.col('trigger'))
                           .group_by('asset').len().iter_rows()))
    base = set(counts[0])
    sufficient = sum(all(rows.get(asset, 0) >= min_events for rows in counts) for asset in base)
    upper = sufficient / len(base) if base else 0.
    return dict(feasible=len(base) >= min_tickers and upper >= support_threshold,
                tickers=len(base), sufficient_tickers=sufficient, support_upper_bound=upper,
                required_support=support_threshold, return_labels_used=False,
                scope='仅为事件数量的必要条件，不证明方向选择性、机制或收益')


def directional_selectivity(ladder: dict[float, pl.DataFrame], labels: pl.DataFrame, *,
                            start: date, end: date, boundary: date, min_events: int,
                            min_tickers: int, support_threshold: float, tolerance: float = 1e-8,
                            label_column: str = "label_o2o_5d") -> dict:
    """训练期逐证券检查收紧阈值后平均收益或胜率改善，标签退出跨界则 purge。"""
    if not start <= end < boundary:
        raise ValueError('方向选择性要求独立后续分区')
    feasibility = selectivity_feasibility(ladder, start=start, end=end, min_events=min_events,
        min_tickers=min_tickers, support_threshold=support_threshold)
    if not feasibility['feasible']:
        return dict(version='favor-selectivity-v1', passed=False, status='insufficient_event_capacity',
                    feasibility=feasibility, ticker_support_rate=None, tickers=feasibility['tickers'],
                    ticker_reports=[], missing_or_purged_by_level={}, return_labels_used=False)
    require_panel(labels, {"date", "asset", label_column, "label_exit_date"}, "标签")
    levels = sorted(ladder)
    counts = []
    base_assets = set()
    for level in levels:
        signals = ladder[level].filter(pl.col("date").is_between(start, end) & pl.col("trigger"))
        if level == levels[0]:
            base_assets = set(signals["asset"].to_list())
        joined = signals.join(labels, on=["date", "asset"], how="left", validate="1:1")
        usable = joined.filter(pl.col(label_column).is_finite() & (pl.col("label_exit_date") < boundary))
        stats = usable.group_by("asset").agg(pl.len().alias("events"), pl.col(label_column).mean().alias("mean_return"),
            (pl.col(label_column) > 0).sum().alias("wins"), (pl.col(label_column) < 0).sum().alias("losses"))
        counts.append((level, {x["asset"]: x for x in stats.to_dicts()}, joined.height-usable.height))
    reports = []
    for asset in sorted(base_assets):
        rows = [values.get(asset) for _, values, _ in counts]
        enough = all(r is not None and r["events"] >= min_events and r["wins"]+r["losses"] > 0 for r in rows)
        mean_up = win_up = False
        if enough:
            means = np.array([r["mean_return"] for r in rows])
            wins = np.array([r["wins"]/(r["wins"]+r["losses"]) for r in rows])
            mean_up = bool(np.all(np.diff(means) >= -tolerance) and means[-1]-means[0] > tolerance and means[-1] > 0)
            win_up = bool(np.all(np.diff(wins) >= -tolerance) and wins[-1]-wins[0] > tolerance and means[-1] > 0)
        reports.append(dict(asset=asset, sufficient_support=bool(enough), mean_improves=mean_up,
                            win_rate_improves=win_up, passed=mean_up or win_up,
                            ladder=[dict(level=q, statistics=values.get(asset)) for q, values, _ in counts]))
    rate = sum(r["passed"] for r in reports)/len(reports) if reports else 0.
    return dict(version="favor-selectivity-v1", passed=len(reports) >= min_tickers and rate >= support_threshold,
                ticker_support_rate=rate, tickers=len(reports), ticker_reports=reports,
                missing_or_purged_by_level={str(q): n for q, _, n in counts},
                scope="发现期结构筛选；不是交易成本后收益，也不是独立机制证明")


def execute_joint(signals: pl.DataFrame, market: pl.DataFrame, state: pl.DataFrame,
                  schedule: tuple[RebalanceWindow, ...], *, cost_bps: float,
                  terminal_policy: str, terminal_events: pl.DataFrame | None):
    """触发证券全部等权，无触发日持现金；复用因子回测订单和终止价值规则。"""
    if {w.signal_date for w in schedule} - set(signals["date"].to_list()):
        raise ValueError("信号日缺少完整联合测量截面；不能把缺数当成无触发持现金")
    panel = signals.filter(pl.col("trigger")).select(pl.col("date").alias("signal_date"),
        pl.col("asset").alias("security_id"), pl.lit(1.).alias("factor_value"))
    return simulate_causal_extreme_portfolio(panel, market, state, schedule, group_count=1,
        round_trip_cost_bps=cost_bps, terminal_policy=terminal_policy, terminal_events=terminal_events,
        allow_noncontiguous_schedule=True, allow_empty_signals=True, retain_daily_holdings=True)


def portfolio_metrics(result, benchmark=None) -> dict:
    """未知终值不参与阈值寻优；实际指标与参考估值严格分开。"""
    from factor_miner.research_report import performance
    benchmark_rows = {r["exit_date"]: r["target_long_net_return"] for r in benchmark.daily_returns} if benchmark else None
    if benchmark_rows is not None and any(r["exit_date"] not in benchmark_rows for r in result.daily_returns):
        raise ValueError("策略和基准的净值日期不一致")
    rows = [{**r, "benchmark_return": benchmark_rows[r["exit_date"]] if benchmark_rows is not None else 0.} for r in result.daily_returns]
    if len(rows) < 2:
        raise ValueError("组合逐日净值不足")
    reference = performance(rows)
    actual = None if result.unresolved_positions else dict(reference)
    if actual is not None and benchmark and benchmark.unresolved_positions:
        actual["information_ratio"] = None
    calmar = None
    if actual is not None and actual["max_drawdown"] > 0:
        calmar = actual["annualized_return"]/actual["max_drawdown"]
    if calmar is not None and not math.isfinite(calmar):
        raise ValueError("Calmar 非有限")
    return dict(actual=actual, reference=reference, calmar=calmar,
                calmar_reason=None if calmar is not None else "终值未确定或最大回撤为零，Calmar 不可用于寻优")
