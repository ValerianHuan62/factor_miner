"""日历月慢变量的合成构念反例，不读取真实股票或收益标签。"""
from datetime import date, timedelta

import numpy as np
import polars as pl

from factor_miner.compiler import attach_market_sessions, build_polars_expr


def validate_monthly_construct(node, condition) -> dict:
    """检查单位、事前状态响应及未到观察时点的信息隔离。"""
    days = [date(2000, 1, 1) + timedelta(days=i) for i in range(1500)]
    days = [d for d in days if d.weekday() < 5]
    clock = np.array([(d - days[0]).days for d in days])
    calendar = pl.DataFrame({"date": days})

    def panel(changed=False, scale=1.):
        # 股数缓慢增加与价格温和上涨为对照；分别增强净收缩或价格修复。
        shares = 1e6 * np.exp((-.0006 if changed and condition.response_test == "monthly_share_change" else .0001) * clock)
        price = 50 * np.exp((.001 if changed and condition.response_test == "monthly_price_repair" else .0001) * clock)
        return pl.DataFrame({"date": days, "asset": ["synthetic"] * len(days),
                             "close": price * scale, "event_adjusted_shares": shares / scale})

    def values(frame):
        return attach_market_sessions(frame.lazy(), calendar, calendar_months=True).sort("asset", "date").with_columns(
            build_polars_expr(node).alias("value")).collect()["value"].tail(100).to_numpy()

    base, rescaled, changed = values(panel()), values(panel(scale=10.)), values(panel(changed=True))
    valid = np.isfinite(base) & np.isfinite(rescaled) & np.isfinite(changed)
    checks = []
    if condition.share_unit_invariant:
        checks.append(dict(test="股份单位重标", passed=bool(valid.sum() >= 50 and np.allclose(base[valid], rescaled[valid], rtol=1e-7, atol=1e-12)), valid_points=int(valid.sum())))
    delta = float(np.median(changed[valid] - base[valid])) if valid.any() else None
    passed = delta is not None and (delta > 1e-12 if condition.expected_response == "increase" else delta < -1e-12)
    checks.append(dict(test=condition.response_test, passed=passed, median_response=delta, expected=condition.expected_response))
    lag = 6 if condition.response_test == "monthly_share_change" else 1
    final_month = days[-1].year * 12 + days[-1].month - 1
    observed_end = max(d for d in days if d.year * 12 + d.month - 1 == final_month - lag)
    perturbed = panel().with_columns([
        pl.when(pl.col("date") > observed_end).then(pl.col(field) * 17).otherwise(pl.col(field)).alias(field)
        for field in ("close", "event_adjusted_shares")
    ])
    late = values(perturbed)[-1]
    checks.append(dict(test="观察滞后隔离", passed=bool(np.isfinite(base[-1]) and np.isfinite(late) and np.isclose(base[-1], late, rtol=1e-10, atol=1e-12))))
    return dict(version="construct-calendar-month-v1", status="基础检验符合" if all(c["passed"] for c in checks) else "偏离",
                condition=condition.model_dump(), checks=checks, mechanism_status="mechanism_unverified", return_labels_used=False,
                limitations="合成反例只验证测量响应，不证明真实股份观测的公布日期，也不证明收益机制。")
