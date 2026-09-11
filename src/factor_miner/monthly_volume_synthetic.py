"""月度观察的确定性反例；不接触市场数据、标签或候选登记。"""
from datetime import date, timedelta

import numpy as np
import polars as pl

from factor_miner.monthly_volume_state import MonthlyVolumeGamma, raw_monthly_volume, monthly_volume_states


def synthetic_monthly_panel():
    """不等长自然月，日均成交从每月已知常数生成。"""
    start, stop = date(2000, 1, 1), date(2003, 1, 1)
    days = [start + timedelta(days=i) for i in range((stop-start).days) if (start+timedelta(days=i)).weekday() < 5]
    rows = []
    for asset in ("a", "b"):
        for d in days:
            month = (d.year-2000)*12+d.month-1
            rows.append(dict(date=d, asset=asset, capitalization_volume_raw=float(100+month), capitalization_price_raw=10., close=10.))
    market = pl.DataFrame(rows)
    state = market.select("date", "asset").with_columns(pl.lit(True).alias("valid_for_factor_compute"), pl.lit(True).alias("valid_for_factor_rank"))
    return market, state, pl.DataFrame({"date": [*days, date(2003, 1, 2)]})


def inspect_monthly_volume_contract(gamma: MonthlyVolumeGamma) -> dict:
    market, state, calendar = synthetic_monthly_panel()
    end = date(2002, 12, 31)
    raw = raw_monthly_volume(market, state, calendar, gamma, through=end)
    target = raw.filter((pl.col("asset") == "a") & (pl.col("month_id") == 2000*12+18)).row(0, named=True)
    checks = [dict(test="月均量及隔六个月的十二月基准", passed=bool(np.isclose(target["monthly_volume"], 118.) and
        np.isclose(target["baseline_volume"], 105.5) and np.isclose(target["atv"], np.log(118/105.5))))]
    scaled = market.with_columns((pl.col("capitalization_volume_raw")/10).alias("capitalization_volume_raw"),
        (pl.col("capitalization_price_raw")*10).alias("capitalization_price_raw"), (pl.col("close")*10).alias("close"))
    scaled_raw = raw_monthly_volume(scaled, state, calendar, gamma, through=end)
    checks.append(dict(test="股份单位重标", passed=bool(np.allclose(raw["atv"].to_numpy(), scaled_raw["atv"].to_numpy(), equal_nan=True))))
    split = market.with_columns(pl.when(pl.col("date") < date(2002, 1, 1)).then(pl.col("capitalization_volume_raw")/2)
        .otherwise(pl.col("capitalization_volume_raw")).alias("capitalization_volume_raw"),
        pl.when(pl.col("date") < date(2002, 1, 1)).then(pl.col("capitalization_price_raw")*2)
        .otherwise(pl.col("capitalization_price_raw")).alias("capitalization_price_raw"))
    split_raw = raw_monthly_volume(split, state, calendar, gamma, through=end)
    checks.append(dict(test="拆股前后可比成交单位", passed=bool(np.allclose(raw["atv"].to_numpy(), split_raw["atv"].to_numpy(), equal_nan=True))))
    boundary = date(2002, 7, 15)
    altered = market.with_columns(pl.when(pl.col("date") > boundary).then(pl.col("capitalization_volume_raw")*99)
        .otherwise(pl.col("capitalization_volume_raw")).alias("capitalization_volume_raw"))
    past = raw_monthly_volume(market, state, calendar, gamma, through=boundary)
    future = raw_monthly_volume(altered, state, calendar, gamma, through=boundary)
    checks.append(dict(test="未来扰动及未完月隔离", passed=past.equals(future) and past["date"].max().month == 6))
    ties = raw.with_columns(pl.lit(0.).alias("atv"))
    states = monthly_volume_states(ties, gamma)
    checks.append(dict(test="常数截面不能伪造极端分组", passed=states["extreme"].to_list() == [0]*states.height))
    return dict(passed=all(c["passed"] for c in checks), checks=checks, return_labels_used=False,
        scope="只验证原始观察和分组状态定义；不证明情绪机制、独立信息或交易收益")
