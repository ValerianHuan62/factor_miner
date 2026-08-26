"""使用 point-in-time A 股面板构造 HMM 市场特征。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.regime_schema import RegimeFeaturePolicy, ReturnAggregation


FEATURE_COLUMNS: tuple[str, ...] = (
    "market_return",
    "realized_volatility_20d",
    "log_amount_relative_20d",
    "advancers_ratio",
)

_REQUIRED_PANEL_COLUMNS: tuple[str, ...] = (
    "date",
    "asset",
    "close",
    "amount",
    "is_st",
    "is_newly_listed",
    "is_suspended",
    "can_buy",
    "can_sell",
    "valid_for_factor_rank",
)


@dataclass(frozen=True, slots=True)
class MarketFeatureFrame:
    """一种收益聚合方式对应的逐日原始市场特征。"""

    frame: pl.DataFrame
    return_aggregation: ReturnAggregation
    min_daily_assets: int


@dataclass(frozen=True, slots=True)
class TrainingStandardizer:
    """只由一个训练窗口估计的四维均值和样本标准差。"""

    feature_columns: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]


def _regime_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造市场状态领域错误。"""

    return FactorMinerError(code, message)


def _validate_panel(panel: pl.DataFrame) -> None:
    """验证构造特征所需的输入列与主键。"""

    missing = set(_REQUIRED_PANEL_COLUMNS) - set(panel.columns)
    if missing:
        raise _regime_error(
            FailureCode.FIELD_MISSING,
            f"市场状态面板缺少字段：{sorted(missing)}",
        )
    if panel.schema.get("date") != pl.Date:
        raise _regime_error(FailureCode.FIELD_MISSING, "市场状态面板 date 必须是 Date")
    duplicate = panel.group_by(["date", "asset"]).len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise _regime_error(
            FailureCode.DATA_RELEASE_MISMATCH,
            f"市场状态面板存在重复 date/asset：{duplicate.head(1).to_dicts()}",
        )


def build_market_features(
    panel: pl.DataFrame,
    policy: RegimeFeaturePolicy,
    aggregation: ReturnAggregation,
    *,
    min_daily_assets: int,
) -> MarketFeatureFrame:
    """构造收益、实现波动、相对成交额和市场宽度。

    该函数只读取当日及过去观察。ST、新股和停牌通过已经验证的
    ``valid_for_factor_rank`` 排除；交易受限股票仍保留在市场状态中。
    """

    _validate_panel(panel)
    if min_daily_assets < 2:
        raise ValueError("min_daily_assets 必须至少为 2")
    aggregation = ReturnAggregation(aggregation)
    eligible = (
        pl.col(policy.universe_mask_column)
        & pl.col("close").is_finite()
        & (pl.col("close") > 0)
        & pl.col("amount").is_finite()
        & (pl.col("amount") >= 0)
    )
    prepared = (
        panel.sort(["asset", "date"])
        .with_columns(
            eligible.alias("_eligible"),
            pl.when(eligible).then(pl.col("close")).otherwise(None).alias("_eligible_close"),
        )
        .with_columns(
            pl.col("_eligible_close")
            .forward_fill()
            .shift(1)
            .over("asset")
            .alias("_previous_eligible_close")
        )
        .with_columns(
            pl.when(pl.col("_eligible") & pl.col("_previous_eligible_close").is_not_null())
            .then(pl.col("close") / pl.col("_previous_eligible_close") - 1.0)
            .otherwise(None)
            .alias("_asset_return"),
            (
                pl.col("_eligible")
                & (~pl.col("can_buy") | ~pl.col("can_sell"))
            ).alias("_restricted"),
        )
    )
    return_expression = (
        pl.col("_asset_return").median()
        if aggregation is ReturnAggregation.MEDIAN
        else pl.col("_asset_return").mean()
    )
    daily = (
        prepared.group_by("date")
        .agg(
            return_expression.alias("market_return"),
            pl.when(pl.col("_eligible"))
            .then(pl.col("amount"))
            .otherwise(None)
            .sum()
            .alias("_total_amount"),
            pl.col("_eligible").sum().cast(pl.Int64).alias("eligible_assets"),
            pl.col("_asset_return").is_not_null().sum().alias("_return_count"),
            (pl.col("_asset_return") > 0).sum().alias("_advancers"),
            pl.col("_restricted").sum().alias("_restricted_count"),
        )
        .sort("date")
        .with_columns(
            pl.col("market_return")
            .rolling_std(
                window_size=policy.realized_volatility_window,
                min_samples=policy.realized_volatility_window,
                ddof=policy.realized_volatility_ddof,
            )
            .alias("realized_volatility_20d"),
            pl.col("_total_amount")
            .rolling_mean(
                window_size=policy.amount_average_window,
                min_samples=policy.amount_average_window,
            )
            .alias("_amount_average_20d"),
            (pl.col("_advancers") / pl.col("_return_count")).alias("advancers_ratio"),
            (pl.col("_restricted_count") / pl.col("eligible_assets")).alias(
                "restricted_trading_ratio"
            ),
        )
        .with_columns(
            pl.when(
                (pl.col("_total_amount") > 0)
                & (pl.col("_amount_average_20d") > 0)
            )
            .then((pl.col("_total_amount") / pl.col("_amount_average_20d")).log())
            .otherwise(None)
            .alias("log_amount_relative_20d")
        )
    )
    all_finite = pl.all_horizontal(
        [pl.col(column).is_not_null() & pl.col(column).is_finite() for column in FEATURE_COLUMNS]
    )
    daily = daily.with_columns(
        (
            (pl.col("eligible_assets") >= min_daily_assets)
            & (pl.col("_return_count") >= min_daily_assets)
            & all_finite
        ).alias("feature_valid")
    ).with_columns(
        pl.when(pl.col("feature_valid"))
        .then(None)
        .otherwise(pl.lit(FailureCode.REGIME_FEATURE_INCOMPLETE.value))
        .alias("failure_code")
    )
    return MarketFeatureFrame(
        frame=daily.select(
            [
                "date",
                *FEATURE_COLUMNS,
                "eligible_assets",
                "restricted_trading_ratio",
                "feature_valid",
                "failure_code",
            ]
        ),
        return_aggregation=aggregation,
        min_daily_assets=min_daily_assets,
    )


def fit_training_standardizer(features: pl.DataFrame) -> TrainingStandardizer:
    """只使用传入训练窗口估计四维样本均值与标准差。"""

    missing = set(FEATURE_COLUMNS) - set(features.columns)
    if missing:
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            f"训练特征缺少列：{sorted(missing)}",
        )
    values = features.select(FEATURE_COLUMNS).to_numpy().astype(float, copy=False)
    if values.shape[0] < 2 or not np.isfinite(values).all():
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "训练特征必须至少包含两行完整有限值",
        )
    means = values.mean(axis=0)
    scales = values.std(axis=0, ddof=1)
    if (
        not np.isfinite(means).all()
        or not np.isfinite(scales).all()
        or np.any(scales < 1e-12)
    ):
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "训练特征均值或样本标准差无效",
        )
    return TrainingStandardizer(
        feature_columns=FEATURE_COLUMNS,
        means=tuple(float(value) for value in means),
        scales=tuple(float(value) for value in scales),
    )


def transform_market_features(
    features: pl.DataFrame,
    standardizer: TrainingStandardizer,
) -> np.ndarray:
    """使用冻结训练 scaler 转换特征，不重新估计统计量。"""

    if standardizer.feature_columns != FEATURE_COLUMNS:
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "训练 scaler 的特征顺序与正式四维顺序不一致",
        )
    missing = set(FEATURE_COLUMNS) - set(features.columns)
    if missing:
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            f"待转换特征缺少列：{sorted(missing)}",
        )
    values = features.select(FEATURE_COLUMNS).to_numpy().astype(float, copy=False)
    if not np.isfinite(values).all():
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "待转换特征包含非有限值",
        )
    means = np.asarray(standardizer.means, dtype=float)
    scales = np.asarray(standardizer.scales, dtype=float)
    transformed = (values - means) / scales
    if not np.isfinite(transformed).all():
        raise _regime_error(
            FailureCode.REGIME_STANDARDIZATION_INVALID,
            "标准化结果包含非有限值",
        )
    return transformed
