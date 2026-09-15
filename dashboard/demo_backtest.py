"""单因子合成回测演示数据，不进入正式候选或研究账本。"""

from __future__ import annotations

from datetime import date, timedelta
import math
from statistics import mean
from typing import Any

from factor_miner.statistics import FrozenStatisticalPolicy, hac_mean_test_frozen


DEMO_FACTOR_ID = "demo_momentum_20d"
DEMO_FACTOR_NAME = "演示：20 日动量"
FROZEN_COST_BPS = 14.0
FROZEN_HAC_MAX_LAGS = 5


def _business_days(start: date, count: int) -> list[date]:
    """生成稳定的工作日序列，仅供 UI 演示。"""

    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def demo_daily_rows() -> list[dict[str, Any]]:
    """生成一个可复现的单因子逐期回测样例。"""

    sessions = _business_days(date(2023, 1, 3), 456)
    rows: list[dict[str, Any]] = []
    for index in range(0, 450, 5):
        entry = sessions[index]
        exit_date = sessions[index + 5]
        cycle = math.sin(index / 17.0)
        shock = math.sin(index * 1.73) * 0.008
        gross = 0.0032 + cycle * 0.010 + shock
        turnover = 0.36 + (math.sin(index / 11.0) + 1.0) * 0.18
        cost = turnover * FROZEN_COST_BPS / 10_000.0
        benchmark = 0.0014 + math.sin(index / 20.0) * 0.007 + math.cos(index / 8.0) * 0.003
        rows.append(
            {
                "entry_date": entry,
                "exit_date": exit_date,
                "target_long_gross_return": gross,
                "target_long_turnover": turnover,
                "target_long_cost": cost,
                "target_long_net_return": gross - cost,
                "benchmark_return": benchmark,
            }
        )
    return rows


def demo_ic_rows() -> list[dict[str, Any]]:
    """生成与演示回测同期间的 IC/RankIC 序列。"""

    return [
        {
            "date": row["exit_date"],
            "ic": 0.018 + math.sin(index / 9.0) * 0.028,
            "rank_ic": 0.024 + math.sin(index / 8.0) * 0.032,
        }
        for index, row in enumerate(demo_daily_rows())
    ]


def demo_ic_summary() -> dict[str, float]:
    """按冻结 HAC 长度计算演示 IC 聚合值。"""

    rows = demo_ic_rows()
    ic = [float(row["ic"]) for row in rows]
    rank_ic = [float(row["rank_ic"]) for row in rows]
    inference = hac_mean_test_frozen(
        rank_ic,
        FrozenStatisticalPolicy(
            policy_id="dashboard_demo_hac_v1",
            alpha=0.05,
            hac_max_lags=FROZEN_HAC_MAX_LAGS,
            min_valid_dates=60,
            bonferroni_denominator=1,
            multiplicity_policy="single_demo_only",
        ),
    )
    return {
        "ic_mean": mean(ic),
        "rank_ic_mean": mean(rank_ic),
        "rank_ic_hac_t": inference.t_value,
    }


def demo_barra_rows() -> dict[str, list[dict[str, object]]]:
    """提供小型归因展示结构，明确不代表真实 Barra 输出。"""

    return {
        "exposure": [
            {"风险因子": "Momentum", "主动暴露": 0.42},
            {"风险因子": "Size", "主动暴露": -0.11},
            {"风险因子": "Value", "主动暴露": 0.06},
            {"风险因子": "Volatility", "主动暴露": -0.08},
        ],
        "attribution": [
            {"来源": "风格因子", "收益贡献": 0.021},
            {"来源": "行业", "收益贡献": -0.004},
            {"来源": "特异收益", "收益贡献": 0.037},
        ],
        "risk": [
            {"来源": "风格与行业风险", "年化风险": 0.082},
            {"来源": "特异风险", "年化风险": 0.116},
        ],
    }
