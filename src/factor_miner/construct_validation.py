"""轻量构念验证：事前观察合同、确定性反例及独立于收益的审计。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class ObservableCondition(BaseModel):
    """生成公式前冻结要测量的状态及可证伪的响应，不包含收益标签。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    observation: str = Field(min_length=1)
    measurement: str = Field(min_length=1)
    share_unit_invariant: bool = True
    response_test: Literal["none", "trend", "volatility", "reversal", "relative_range",
        "volume_growth", "volume_instability", "trade_frequency_growth", "trade_return_coupling", "illiquidity", "overnight_gap", "close_location",
        "upside_tail", "downside_tail", "range_instability", "return_volume_coupling",
        "intraday_strength", "near_high", "positive_close_jump", "negative_close_jump",
        "positive_intraday_jump", "positive_gap_jump", "absolute_return_volume_coupling",
        "range_persistence", "market_correlation", "market_curvature", "market_delay", "price_updates", "volatility_hedge", "volatility_uncertainty_hedge", "market_downside_asymmetry", "liquidity_bad_market", "bid_ask_bounce", "overnight_up_day_down", "stress_volume_support", "recent_return_acceleration", "market_cubic_response", "dispersion_hedge", "intraday_return", "market_residual_variation", "distributed_market_delay", "drawdown_recovery", "near_low", "anchor_reversal", "residual_pressure", "joint_tail_events", "oil_uncertainty_hedge", "salience_context", "closing_quote_displacement", "quoted_spread_width", "quote_trade_location", "turnover_level", "turnover_return_coupling", "capitalization_size", "leader_catchup", "industry_leader_strength", "industry_lag_beta", "monthly_share_change", "monthly_price_repair", "annual_gross_profit", "annual_cash_flow", "annual_leverage", "annual_profit_improvement"] = "none"
    expected_response: Literal["increase", "decrease"] = "increase"
    origin: Literal["preregistered", "retrospective_audit"] = "preregistered"


def validate_construct(expression: object, condition: ObservableCondition | None, *, calendar_months: bool = False,
                       response_aggregation: str = 'median') -> dict[str, object]:
    """只用合成 OHLCV 检验量纲与方向，禁止通过预测收益反向解释机制。"""
    from factor_miner.schema import FactorNode
    from factor_miner.compiler import build_polars_expr
    from factor_miner.dsl import validate_ast, CALENDAR_MONTH_LIMITS

    if condition is None:
        return {"status": "证据不足", "reason": "没有冻结观察合同", "mechanism_status": "mechanism_unverified"}
    node = FactorNode.model_validate(expression)
    from factor_miner.pit_financials import RAW_FIELDS
    validate_ast(node, allowed_fields={*RAW_FIELDS, "open", "high", "low", "close", "volume", "trade_count", "market_return", "vix_change", "vvix_change", "ovx_change", "dispersion_change", "quote_bid_raw", "quote_ask_raw", "quote_close_raw", "market_cap_usd", "capitalization_price_raw", "capitalization_volume_raw", "stock_price_return", "leader_return", "industry_leader_return", "industry_peer_return", *({"event_adjusted_shares"} if calendar_months else set())}, forbidden_fields=(), limits=CALENDAR_MONTH_LIMITS if calendar_months else None)
    if condition.response_test in {"monthly_share_change", "monthly_price_repair"}:
        if not calendar_months:
            raise ValueError("月度构念反例需要显式日历月协议")
        from factor_miner.monthly_construct import validate_monthly_construct
        return validate_monthly_construct(node, condition)
    checks: list[dict[str, object]] = []
    sessions = [date(2000, 1, 1) + timedelta(days=i) for i in range(300)]
    index = np.arange(300)

    def panel(trend: float = 0.0007, amplitude: float = 0.01, scale: float = 1.0) -> pl.DataFrame:
        """构造价格和成交量有波动的确定性反例，尺度变换保持美元成交额。"""
        close = 50 * np.exp(trend * index + amplitude * np.sin(index * .73))
        market_path = .01 * np.sin(index * .53)
        if condition.response_test == "distributed_market_delay":
            market_path = np.random.default_rng(3101).normal(0, .01, len(index))
            returns = .8 * market_path + .001 * np.cos(index * .37)
            returns[0] = 0.
            close = 50 * np.cumprod(1 + returns)
        if condition.response_test == "drawdown_recovery":
            cycle = index % 40
            close = np.where(cycle < 3, 100., np.where(cycle < 6, 60., 70.))
        if condition.response_test == "joint_tail_events":
            market_path = .001 * np.sin(index * .53) - .05 * (index % 20 == 10)
            returns = .001 * np.cos(index * .37) - .08 * (index % 20 == 0)
            returns[0] = 0.
            close = 50 * np.cumprod(1 + returns)
        if condition.response_test == "salience_context":
            returns = .02 * (-1.) ** index + .001 * np.sin(index * .41)
            returns[0] = 0.
            market_path = .8 * np.abs(returns)
            close = 50 * np.cumprod(1 + returns)
        opening = close * np.exp(.002 * np.cos(index * .37))
        result = pl.DataFrame({"date": sessions, "asset": ["synthetic"] * 300, "__market_session": index,
            "open": opening * scale, "close": close * scale,
            "high": np.maximum(close, opening) * (1 + amplitude) * scale,
            "low": np.minimum(close, opening) * (1 - amplitude) * scale,
            "volume": (1e6 * (1 + .25 * np.sin(index * .41))) / scale,
            "trade_count": np.round(1000 * (1 + .2 * np.sin(index * .31))),
            "market_return": market_path,
            "vix_change": .8 * np.cos(index * .29) - .5 * np.sin(index * .53),
            "vvix_change": 2 * np.sin(index * .71) + .7 * np.cos(index * .29),
            "ovx_change": 1.2 * np.sin(index * .91) + .8 * np.cos(index * .17),
            "dispersion_change": .002 * np.sin(index * .83) + .001 * np.cos(index * .31),
            "quote_bid_raw": close * (1 + .001*np.sin(index*.61)) * (1-.003-.001*np.sin(index*.43)) * scale,
            "quote_ask_raw": close * (1 + .001*np.sin(index*.61)) * (1+.003+.001*np.sin(index*.43)) * scale,
            "quote_close_raw": close * scale,
            "market_cap_usd": close * 1e8,
            "capitalization_price_raw": close * scale,
            "capitalization_volume_raw": (1e6 * (1 + .25 * np.sin(index * .41))) / scale,
            "leader_return": .004 + np.random.default_rng(1091).normal(0,.01,len(index)),
            "stock_price_return": .001 * np.cos(index*.37),
            "industry_leader_return": .004 + np.random.default_rng(2047).normal(0,.012,len(index)),
            "industry_peer_return": .003 + np.random.default_rng(2741).normal(0,.008,len(index))})
        result = result.with_columns(pl.lit(1e9).alias("annual_total_assets"),
            pl.lit(2e8).alias("annual_gross_profit"), pl.lit(1e8).alias("annual_cash_flow_from_operating_activities"),
            pl.lit(4e8).alias("annual_total_liabilities"), pl.lit(1e9).alias("prior_annual_total_assets"),
            pl.lit(1.5e8).alias("prior_annual_gross_profit"))
        if condition.response_test == "leader_catchup":
            leader=result['leader_return'].to_numpy()
            lagged=np.r_[0.,leader[:-1]]
            result=result.with_columns(pl.Series('stock_price_return',.7*leader+.2*lagged+.001*np.cos(index*.37)))

        if condition.response_test == "industry_lag_beta":
            leader = result["industry_leader_return"].to_numpy()
            peer = result["industry_peer_return"].to_numpy()
            result = result.with_columns(pl.Series("stock_price_return", .7*peer + .2*np.r_[0., leader[:-1]] + .001*np.cos(index*.37)))

        if condition.response_test == "closing_quote_displacement":
            result = result.with_columns(pl.Series("close", 50 * scale * np.exp(.002 * (-1.) ** index)),
                pl.lit(50 * scale).alias("open"), pl.lit(50 * scale * np.exp(.03)).alias("high"),
                pl.lit(50 * scale * np.exp(-.03)).alias("low"))
        return result

    def values(data: pl.DataFrame) -> np.ndarray:
        """执行已验证 AST，仅提取充分预热后的测量序列。"""
        return data.with_columns(build_polars_expr(node).alias("value"))["value"].tail(100).to_numpy()

    base = values(panel())
    rebased = values(panel(scale=10))
    valid = np.isfinite(base) & np.isfinite(rebased)
    if condition.share_unit_invariant:
        passed = bool(valid.sum() >= 50 and np.allclose(base[valid], rebased[valid], rtol=1e-7, atol=1e-12))
        checks.append({"test": "股份单位重标", "passed": passed, "description": "价格乘10、股数成交量除10后，目标经济状态不变，测量应不变", "valid_points": int(valid.sum())})
    if condition.response_test != "none":
        altered = panel(trend=.003 if condition.response_test in {"trend", "reversal"} else .0007,
            amplitude=.04 if condition.response_test in {"volatility", "relative_range"} else .01)
        test = condition.response_test
        financial_response = {"annual_gross_profit": "annual_gross_profit",
            "annual_profit_improvement": "annual_gross_profit",
            "annual_cash_flow": "annual_cash_flow_from_operating_activities",
            "annual_leverage": "annual_total_liabilities"}
        if test in financial_response:
            field = financial_response[test]
            altered = altered.with_columns((pl.col(field) * 2).alias(field))
        elif test == 'trade_frequency_growth':
            altered=altered.with_columns(pl.Series('trade_count',np.round(1000*np.exp(.02*index))))
        elif test == 'trade_return_coupling':
            close=altered['close'].to_numpy();returns=np.r_[0.,close[1:]/close[:-1]-1]
            altered=altered.with_columns(pl.Series('trade_count',np.round(1000*np.exp(60*returns))))
        elif test == "capitalization_size":
            altered=altered.with_columns((pl.col('market_cap_usd')*3).alias('market_cap_usd'))
        elif test == "leader_catchup":
            leader=altered['leader_return'].to_numpy()
            lagged=np.r_[0.,leader[:-1]]
            altered=altered.with_columns(pl.Series('stock_price_return',.7*leader+.8*lagged+.001*np.cos(index*.37)))
        elif test == "industry_leader_strength":
            altered = altered.with_columns((pl.col("industry_leader_return") + .01).alias("industry_leader_return"))
        elif test == "industry_lag_beta":
            leader = altered["industry_leader_return"].to_numpy()
            peer = altered["industry_peer_return"].to_numpy()
            altered = altered.with_columns(pl.Series("stock_price_return", .7*peer + .8*np.r_[0., leader[:-1]] + .001*np.cos(index*.37)))
        elif test == "turnover_level":
            altered = altered.with_columns((pl.col('capitalization_volume_raw')*3).alias('capitalization_volume_raw'))
        elif test == "turnover_return_coupling":
            # 同一价格、股本路径，只提高正收益日的成交股数，检验量价耦合方向。
            close = altered['close'].to_numpy()
            returns = np.r_[0., close[1:]/close[:-1]-1]
            altered = altered.with_columns(pl.Series('capitalization_volume_raw',1e6*np.exp(40*returns)))
        elif test == "quoted_spread_width":
            # 保持同日中点和成交价，仅将报价半价差扩大三倍。
            middle = (pl.col('quote_bid_raw')+pl.col('quote_ask_raw'))/2
            half = (pl.col('quote_ask_raw')-pl.col('quote_bid_raw'))/2
            altered = altered.with_columns((middle-3*half).alias('quote_bid_raw'), (middle+3*half).alias('quote_ask_raw'))
        elif test == "quote_trade_location":
            # 固定报价，提高最后成交价；不由此声称真实订单买卖方向。
            altered = altered.with_columns((pl.col('quote_close_raw')*1.005).alias('quote_close_raw'))
        elif test == "closing_quote_displacement":
            # 固定两日区间中点，仅扩大收盘价偏离；不把合成输入当真实报价。
            altered = altered.with_columns(pl.Series("close", 50 * np.exp(.008 * (-1.) ** index)))
        elif test == "salience_context":
            # 股票收益集合保持不变，只将正收益市场环境镜像为负收益环境。
            altered = altered.with_columns((-pl.col("market_return")).alias("market_return"))
        elif test == "joint_tail_events":
            # 保持每20观察的股票收益集合，将严重损失日移到市场严重损失日。
            original = .001 * np.cos(index * .37) - .08 * (index % 20 == 0)
            returns = np.concatenate([np.roll(chunk, 10) for chunk in np.split(original, 15)])
            returns[0] = 0.
            close = altered['close'].to_numpy()
            ratio = close[0] * np.cumprod(1 + returns) / close
            altered = altered.with_columns([(pl.col(field) * pl.Series(ratio)).alias(field) for field in ["open", "high", "low", "close"]])
        elif test == "drawdown_recovery":
            ratio = np.where(index % 40 >= 6, 80. / 70., 1.)
            altered = altered.with_columns([(pl.col(field) * pl.Series(ratio)).alias(field) for field in ["open", "high", "low", "close"]])
        elif test == "intraday_return":
            # 同一收盘路径下开盘价降低，使每日开至收盘毛收益乘1.01。
            altered = altered.with_columns((pl.col("open") / 1.01).alias("open")).with_columns(
                pl.min_horizontal("low", "open").alias("low"))
        elif test == "overnight_up_day_down":
            # 保持收盘路径，在隔夜上行后出现日内下跌，不模拟未来价格或投资者身份。
            close = altered["close"].to_numpy()
            opening = np.maximum(close, np.r_[close[0], close[:-1]]) * 1.01
            altered = altered.with_columns(pl.Series("open", opening)).with_columns(
                pl.max_horizontal("high", "open").alias("high"))
        elif test == "bid_ask_bounce":
            # 固定基础路径，增加交替报价回跳；不把这一合成检验当真实价差证据。
            old_close = altered["close"].to_numpy()
            ratio = (old_close + .5 * (-1.)**index) / old_close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        elif test == "recent_return_acceleration":
            # 同一市场及原始冲击之上，仅加入越来越高的历史日收益。
            close = altered["close"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1] + .0003 * index
            changed = close[0] * np.cumprod(1 + returns)
            ratio = changed / close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "market_correlation":
            # 市场路径与股票原有冲击保持，仅增加股票对同期市场信息的响应。
            old_close = altered["close"].to_numpy()
            changed_close = old_close * np.exp(np.cumsum(2 * altered["market_return"].to_numpy()))
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field,altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        elif test == "distributed_market_delay":
            # 在固定市场路径中加入滞后两日和四日的非同期响应。
            close = altered["close"].to_numpy()
            market = altered["market_return"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1]
            returns += 2 * np.r_[np.zeros(2), market[:-2]] + 2 * np.r_[np.zeros(4), market[:-4]]
            returns[0] = 0.
            ratio = close[0] * np.cumprod(1 + returns) / close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "market_residual_variation":
            # 固定市场路径，把全部股票收益的市场外部分放大三倍。
            close = altered["close"].to_numpy()
            market = altered["market_return"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1]
            changed_returns = market + 3 * (returns - market)
            changed_returns[0] = 0.
            ratio = close[0] * np.cumprod(1 + changed_returns) / close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "dispersion_hedge":
            # 固定市场与分散程度，仅增加股票对分散冲击的已知收益响应。
            close = altered["close"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1] + 2 * altered["dispersion_change"].to_numpy()
            returns[0] = 0.
            ratio = close[0] * np.cumprod(1 + returns) / close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "market_cubic_response":
            # 加入已知市场三次项响应，普通市场路径和原始个股冲击保持。
            close = altered["close"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1]
            returns += 1000 * altered["market_return"].to_numpy()**3
            returns[0] = 0.
            changed = close[0] * np.cumprod(1 + returns)
            ratio = changed / close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "market_curvature":
            # 固定市场路径，只添加与市场收益平方相关的股票简单收益，保留原有冲击。
            old_close = altered["close"].to_numpy()
            market_returns = altered["market_return"].to_numpy()
            stock_returns = np.r_[0., old_close[1:] / old_close[:-1] - 1]
            stock_returns += 100 * (market_returns**2 - .00005)
            stock_returns[0] = 0.
            changed_close = old_close[0] * np.cumprod(1 + stock_returns)
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field,altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        elif test == "market_downside_asymmetry":
            # 只放大市场下跌时的股票负收益响应，上涨市场路径与原冲击保持。
            old_close = altered["close"].to_numpy()
            stock_returns = np.r_[0., old_close[1:] / old_close[:-1] - 1]
            market_returns = altered["market_return"].to_numpy()
            stock_returns += np.minimum(market_returns, 0.)
            stock_returns[0] = 0.
            changed_close = old_close[0] * np.cumprod(1 + stock_returns)
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field, altered[field].to_numpy()*ratio) for field in ["open", "high", "low", "close"]))
        elif test == "market_delay":
            # 增加前一市场日信息的同向响应，市场本身及原股票冲击保持不变。
            old_close = altered["close"].to_numpy()
            market_returns = altered["market_return"].to_numpy()
            stock_returns = np.r_[0., old_close[1:] / old_close[:-1] - 1]
            stock_returns += 2 * np.r_[0., market_returns[:-1]]
            changed_close = old_close[0] * np.cumprod(1 + stock_returns)
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field,altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        elif test == "price_updates":
            # 固定成交量，将收盘报价每五行更新一次；不据此推断没有真实成交。
            old_close = altered["close"].to_numpy()
            changed_close = old_close[(index // 5) * 5]
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field,altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        elif test in {"volatility_hedge", "volatility_uncertainty_hedge", "oil_uncertainty_hedge"}:
            # 固定市场与VIX路径，在股票原简单收益中加入正向波动冲击响应。
            old_close = altered["close"].to_numpy()
            stock_returns = np.r_[0., old_close[1:] / old_close[:-1] - 1]
            field = {"volatility_hedge": "vix_change", "volatility_uncertainty_hedge": "vvix_change", "oil_uncertainty_hedge": "ovx_change"}[test]
            stock_returns += .01 * altered[field].to_numpy()
            stock_returns[0] = 0.
            changed_close = old_close[0] * np.cumprod(1 + stock_returns)
            ratio = changed_close / old_close
            altered = altered.with_columns(*(pl.Series(field,altered[field].to_numpy()*ratio) for field in ["open","high","low","close"]))
        if test == "volume_growth":
            altered = altered.with_columns(pl.Series("volume", altered["volume"].to_numpy() * np.exp(.02 * index)))
        elif test == "volume_instability":
            altered = altered.with_columns(pl.Series("volume", 1e6 * (1 + .8 * np.sin(index * .41))))
        elif test == "illiquidity":
            altered = altered.with_columns(pl.col("volume") / 10)
        elif test == "overnight_gap":
            # 保持昨日收盘不变，提高今日开盘，同时扩展高价保证 OHLC 合法。
            altered = altered.with_columns((pl.col("open") * 1.03).alias("open")).with_columns(pl.max_horizontal("high", "open").alias("high"))
        elif test == "close_location":
            altered = altered.with_columns((pl.col("low") + .9 * (pl.col("high") - pl.col("low"))).alias("close"))
        elif test == "upside_tail":
            altered = altered.with_columns((pl.col("high") * 1.05).alias("high"))
        elif test == "downside_tail":
            altered = altered.with_columns((pl.col("low") * .95).alias("low"))
        elif test == "range_instability":
            spread = .01 * (1 + .9 * np.sin(index * .41))
            altered = altered.with_columns(
                pl.Series("high", np.maximum(altered["close"].to_numpy(), altered["open"].to_numpy()) * (1 + spread)),
                pl.Series("low", np.minimum(altered["close"].to_numpy(), altered["open"].to_numpy()) * (1 - spread)))
        elif test == "return_volume_coupling":
            # 收益路径不变，仅让成交活跃程度与当期收益正向联动。
            close = altered["close"].to_numpy()
            returns = np.r_[0.0, close[1:] / close[:-1] - 1]
            altered = altered.with_columns(pl.Series("volume", 1e6 * np.exp(30 * returns)))
        elif test == "liquidity_bad_market":
            # 股票价格固定，只令成交在市场下跌时收缩；冲击代理的市场斜率应下降。
            volume = altered["volume"].to_numpy() * np.exp(40 * altered["market_return"].to_numpy())
            altered = altered.with_columns(pl.Series("volume", volume))
        elif test == "stress_volume_support":
            # 固定股票和市场价格，在每日成交量增长比中加入已知绝对市场变动响应。
            volume = altered["volume"].to_numpy()
            gross = np.r_[1., volume[1:] / volume[:-1]]
            gross[1:] += 10 * np.abs(altered["market_return"].to_numpy()[1:])
            altered = altered.with_columns(pl.Series("volume", volume[0] * np.cumprod(gross)))
        elif test == "absolute_return_volume_coupling":
            # 价格路径完全不变，只把成交活跃程度与绝对收益绑定。
            close = altered["close"].to_numpy()
            absolute_returns = np.abs(np.r_[0.0, close[1:] / close[:-1] - 1])
            altered = altered.with_columns(pl.Series("volume", 1e6 * (1 + 50 * absolute_returns)))
        elif test == "range_persistence":
            # 重排合成振幅的时间顺序，保持每100日的振幅分布及开收盘路径。
            ratios = altered["high"].to_numpy() / altered["low"].to_numpy()
            persistent = np.concatenate([np.sort(chunk) for chunk in np.split(ratios, 3)])
            center = np.sqrt(altered["open"].to_numpy() * altered["close"].to_numpy())
            altered = altered.with_columns(pl.Series("high", center * np.sqrt(persistent)),
                                           pl.Series("low", center / np.sqrt(persistent)))
        elif test == "intraday_strength":
            # 收盘路径不变，降低开盘价；单纯收盘动量不能响应日内需求检验。
            altered = altered.with_columns((pl.col("open") * .97).alias("open")).with_columns(pl.min_horizontal("low", "open").alias("low"))
        elif test == "residual_pressure":
            close = altered["close"].to_numpy()
            returns = np.r_[0., close[1:] / close[:-1] - 1]
            returns += index * .00001
            changed = close[0] * np.cumprod(1 + returns)
            ratio = changed / close
            altered = altered.with_columns([(pl.col(field) * pl.Series(ratio)).alias(field) for field in ["open", "high", "low", "close"]])
        elif test == "anchor_reversal":
            # 抬高旧参考峰值，评估尾窗中绝大部分日期的近期端点保持不变。
            ratio = np.ones(len(index)); ratio[180] = 2.
            altered = altered.with_columns([(pl.col(field) * pl.Series(ratio)).alias(field) for field in ["open", "high", "low", "close"]])
        elif test == "near_low":
            # 持续下跌每天更新低点，低点/现价趋于1。
            altered = panel(trend=-.03)
        elif test == "near_high":
            # 持续上涨路径每天创出新高，近高测量应高于含回撤的对照路径。
            altered = panel(trend=.03)
        elif test in {"positive_close_jump", "negative_close_jump"}:
            close = altered["close"].to_numpy()
            log_returns = np.r_[0.0, np.diff(np.log(close))]
            jump = .08 if test == "positive_close_jump" else -.08
            log_returns = log_returns + np.where((index > 0) & (index % 25 == 0), jump, 0.0)
            changed_close = 50 * np.exp(np.cumsum(log_returns))
            opening = np.r_[changed_close[0], changed_close[:-1]]
            altered = altered.with_columns(pl.Series("close", changed_close), pl.Series("open", opening),
                pl.Series("high", np.maximum(changed_close, opening) * 1.01),
                pl.Series("low", np.minimum(changed_close, opening) * .99))
        elif test in {"positive_intraday_jump", "positive_gap_jump"}:
            multiplier = np.where(index % 25 == 0, .92 if test == "positive_intraday_jump" else 1.08, 1.0)
            altered = altered.with_columns(pl.Series("open", altered["open"].to_numpy() * multiplier)).with_columns(
                pl.max_horizontal("high", "open").alias("high"), pl.min_horizontal("low", "open").alias("low"))
        changed = values(altered)
        usable = np.isfinite(base) & np.isfinite(changed)
        delta = float(np.median(changed[usable] - base[usable])) if usable.any() else None
        passed = delta is not None and (delta > 1e-12 if condition.expected_response == "increase" else delta < -1e-12)
        if response_aggregation == 'directional_changes':
            # 稀疏事件只改变少量观察，不要求超过半数合成日期改变；反向响应仍否决。
            differences = changed[usable] - base[usable]
            signed = differences * (1 if condition.expected_response == 'increase' else -1)
            passed = bool((signed > 1e-12).sum() >= 2 and not (signed < -1e-12).any())
        elif response_aggregation != 'median':
            raise ValueError('未知合成响应汇总规则')
        checks.append({"test": condition.response_test, "passed": passed, "median_response": delta, "expected": condition.expected_response})
        if response_aggregation == 'directional_changes':
            checks[-1].update(response_aggregation=response_aggregation,
                directional_changes=int((signed > 1e-12).sum()), opposite_changes=int((signed < -1e-12).sum()))
    status = "偏离" if any(not item["passed"] for item in checks) else ("基础检验符合" if len(checks) > 1 else "证据不足")
    return {"version": "construct-lite-v1", "status": status, "condition": condition.model_dump(), "checks": checks,
        "mechanism_status": "mechanism_unverified", "return_labels_used": False,
        "limitations": "通过合成反例不证明真实经济机制。只有尺度检验时仍为证据不足；条件来源由 origin 区分事前登记与事后审计。"}
