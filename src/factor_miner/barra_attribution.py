"""Barra 行业、Size/风格主动暴露与已实现收益归因。"""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.barra_schema import (
    BarraEvaluationPolicy,
    BarraInputIdentity,
    validate_barra_input_identity,
    barra_policy_id,
)
from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


class BarraAttributionResult(BaseModel):
    """Barra 归因长表、暴露摘要和对账状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    attribution_mode: Literal["realized_attribution_only", "full_risk_decomposition"]
    exposure_summary: tuple[dict[str, object], ...] = Field(min_length=1)
    attribution: tuple[dict[str, object], ...] = Field(min_length=1)
    risk_decomposition: tuple[dict[str, object], ...] = ()
    max_abs_reconciliation_error: float = Field(ge=0)
    risk_decomposition_status: Literal["realized_attribution_only", "complete"]
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        policy: BarraEvaluationPolicy,
        exposure_summary: tuple[dict[str, object], ...],
        attribution: tuple[dict[str, object], ...],
        risk_decomposition: tuple[dict[str, object], ...] = (),
        max_abs_reconciliation_error: float,
        risk_decomposition_status: Literal["realized_attribution_only", "complete"],
    ) -> BarraAttributionResult:
        """构造内容寻址归因结果。"""

        def jsonable(value: object) -> object:
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, dict):
                return {str(key): jsonable(item) for key, item in value.items()}
            if isinstance(value, tuple | list):
                return [jsonable(item) for item in value]
            return value

        payload = {
            "policy_id": barra_policy_id(policy),
            "attribution_mode": policy.attribution_mode,
            "exposure_summary": jsonable(exposure_summary),
            "attribution": jsonable(attribution),
            "risk_decomposition": jsonable(risk_decomposition),
            "max_abs_reconciliation_error": max_abs_reconciliation_error,
            "risk_decomposition_status": risk_decomposition_status,
        }
        return cls(
            policy_id=barra_policy_id(policy),
            attribution_mode=policy.attribution_mode,
            exposure_summary=exposure_summary,
            attribution=attribution,
            risk_decomposition=risk_decomposition,
            max_abs_reconciliation_error=max_abs_reconciliation_error,
            risk_decomposition_status=risk_decomposition_status,
            result_sha256=sha256_json(payload),
        )


def _barra_error(message: str) -> FactorMinerError:
    """构造 Barra 输入或对账错误。"""

    return FactorMinerError(FailureCode.BARRA_DATA_CONTRACT_INVALID, message)


def _require_columns(frame: pl.DataFrame, required: set[str], name: str) -> None:
    """检查 Barra 输入列。"""

    missing = required.difference(frame.columns)
    if missing:
        raise _barra_error(f"{name} 缺少字段：{sorted(missing)}")


def _reject_duplicates(frame: pl.DataFrame, keys: list[str], name: str) -> None:
    """拒绝 Barra 主键重复。"""

    if frame.group_by(keys).len().filter(pl.col("len") > 1).height:
        raise _barra_error(f"{name} 主键重复：{keys}")


def _calculate_risk_decomposition(
    *,
    joined: pl.DataFrame,
    exposure_summary: pl.DataFrame,
    factors: tuple[str, ...],
    covariance: pl.DataFrame,
    specific_risk: pl.DataFrame,
) -> tuple[dict[str, object], ...]:
    """按信号日和组合计算主动因子、特异及总方差。"""

    _require_columns(
        covariance,
        {"signal_date", "factor_a", "factor_b", "covariance"},
        "covariance",
    )
    _require_columns(
        specific_risk,
        {"signal_date", "security_id", "specific_risk"},
        "specific_risk",
    )
    _reject_duplicates(
        covariance,
        ["signal_date", "factor_a", "factor_b"],
        "covariance",
    )
    _reject_duplicates(
        specific_risk,
        ["signal_date", "security_id"],
        "specific_risk",
    )
    if covariance.filter(~pl.col("covariance").is_finite()).height:
        raise _barra_error("Barra 协方差包含非有限值")
    if specific_risk.filter(
        ~pl.col("specific_risk").is_finite() | (pl.col("specific_risk") <= 0)
    ).height:
        raise _barra_error("Barra 特异风险必须为有限正数")

    expected_pairs = {(left, right) for left in factors for right in factors}
    covariance_by_date: dict[date, dict[tuple[str, str], float]] = {}
    for signal_date, rows in covariance.partition_by(
        "signal_date", as_dict=True
    ).items():
        date_key = signal_date[0] if isinstance(signal_date, tuple) else signal_date
        matrix = {
            (str(row["factor_a"]), str(row["factor_b"])): float(row["covariance"])
            for row in rows.to_dicts()
        }
        if set(matrix) != expected_pairs:
            raise _barra_error("Barra 暴露因子与协方差因子不一致")
        for factor in factors:
            if matrix[(factor, factor)] < 0:
                raise _barra_error("Barra 协方差对角线不能为负")
        for left in factors:
            for right in factors:
                if not math.isclose(
                    matrix[(left, right)],
                    matrix[(right, left)],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise _barra_error("Barra 协方差矩阵不对称")
        covariance_by_date[date_key] = matrix

    risk_weights = joined.join(
        specific_risk,
        on=["signal_date", "security_id"],
        how="left",
    )
    if risk_weights.filter(pl.col("specific_risk").is_null()).height:
        raise _barra_error("组合权重缺少对应 Barra 特异风险")
    specific_variance = risk_weights.group_by(
        ["signal_date", "entry_date", "portfolio"]
    ).agg(
        (
            pl.col("active_weight").pow(2)
            * pl.col("specific_risk").pow(2)
        ).sum().alias("specific_variance")
    )
    specific_by_period = {
        (row["signal_date"], row["entry_date"], row["portfolio"]): float(
            row["specific_variance"]
        )
        for row in specific_variance.to_dicts()
    }

    result: list[dict[str, object]] = []
    for row in exposure_summary.to_dicts():
        signal_date = row["signal_date"]
        matrix = covariance_by_date.get(signal_date)
        if matrix is None:
            raise _barra_error("主动暴露缺少对应信号日 Barra 协方差")
        factor_variance = sum(
            float(row[left]) * matrix[(left, right)] * float(row[right])
            for left in factors
            for right in factors
        )
        period_key = (signal_date, row["entry_date"], row["portfolio"])
        period_specific_variance = specific_by_period[period_key]
        total_variance = factor_variance + period_specific_variance
        if factor_variance < -1e-12 or total_variance < -1e-12:
            raise _barra_error("Barra 协方差不能产生负的主动风险方差")
        factor_variance = max(factor_variance, 0.0)
        total_variance = max(total_variance, 0.0)
        result.append(
            {
                "signal_date": signal_date,
                "entry_date": row["entry_date"],
                "portfolio": row["portfolio"],
                "factor_variance": factor_variance,
                "specific_variance": period_specific_variance,
                "total_variance": total_variance,
                "volatility": math.sqrt(total_variance),
            }
        )
    return tuple(result)


def calculate_barra_attribution(
    portfolio_weights: pl.LazyFrame,
    benchmark_weights: pl.LazyFrame,
    exposures: pl.LazyFrame,
    factor_returns: pl.LazyFrame,
    policy: BarraEvaluationPolicy,
    *,
    identity: BarraInputIdentity | None = None,
    covariance: pl.LazyFrame | None = None,
    specific_risk: pl.LazyFrame | None = None,
) -> BarraAttributionResult:
    """计算主动行业/风格暴露和已实现因子收益贡献。

    权重面板必须包含 `signal_date`、`entry_date`、`portfolio`、`security_id`、
    `weight`、`realized_return`、`benchmark_return`；若提供
    `specific_return`，它会参与收益对账。暴露按信号日或更早最近可得日绑定。
    """

    if identity is None:
        raise _barra_error("Barra 运行必须提供已登记的输入版本身份")
    validate_barra_input_identity(policy, identity)
    full_risk = policy.attribution_mode == "full_risk_decomposition"
    if full_risk and (
        covariance is None or specific_risk is None
    ):
        raise _barra_error("完整 Barra 风险分解缺少协方差或特异风险输入")

    weights = portfolio_weights.collect()
    benchmark = benchmark_weights.collect()
    exposure = exposures.collect()
    returns = factor_returns.collect()
    covariance_frame = covariance.collect() if covariance is not None else None
    specific_risk_frame = specific_risk.collect() if specific_risk is not None else None
    _require_columns(
        weights,
        {
            "signal_date",
            "entry_date",
            "portfolio",
            "security_id",
            "weight",
            "realized_return",
            "benchmark_return",
        },
        "portfolio_weights",
    )
    _require_columns(
        benchmark,
        {"signal_date", "entry_date", "security_id", "weight"},
        "benchmark_weights",
    )
    _require_columns(exposure, {"signal_date", "security_id"}, "exposures")
    _require_columns(returns, {"entry_date", "factor", "factor_return"}, "factor_returns")
    _reject_duplicates(
        weights,
        ["signal_date", "entry_date", "portfolio", "security_id"],
        "portfolio_weights",
    )
    _reject_duplicates(
        benchmark,
        ["signal_date", "entry_date", "security_id"],
        "benchmark_weights",
    )
    _reject_duplicates(exposure, ["signal_date", "security_id"], "exposures")
    _reject_duplicates(returns, ["entry_date", "factor"], "factor_returns")

    key_columns = {
        "signal_date",
        "security_id",
        "entry_date",
        "portfolio",
        "weight",
        "realized_return",
        "benchmark_return",
        "specific_return",
    }
    factor_columns = [column for column in exposure.columns if column not in key_columns]
    industry_columns = [
        column for column in factor_columns if column not in policy.required_style_factors
    ]
    if not industry_columns:
        raise _barra_error("Barra 暴露缺少行业字段")
    for required in policy.required_style_factors:
        if required not in exposure.columns:
            raise _barra_error(f"Barra 暴露缺少样式字段：{required}")
    factors = tuple(industry_columns) + policy.required_style_factors
    unknown_returns = set(returns["factor"].to_list()).difference(factors)
    if unknown_returns or set(factors).difference(returns["factor"].to_list()):
        raise _barra_error("Barra 暴露字段与因子收益字段不一致")

    exposure_join = exposure.select(
        ["signal_date", "security_id", *factors]
    )
    benchmark_join = benchmark.rename({"weight": "benchmark_weight"})
    portfolio_identity_columns = [
        "signal_date",
        "entry_date",
        "portfolio",
        "realized_return",
        "benchmark_return",
    ]
    position_key_columns = [
        "signal_date",
        "entry_date",
        "portfolio",
        "security_id",
    ]
    portfolio_identities = weights.select(portfolio_identity_columns).unique()
    portfolio_positions = weights.select([*position_key_columns, "weight"])
    benchmark_positions = (
        portfolio_identities.join(
            benchmark_join,
            on=["signal_date", "entry_date"],
            how="inner",
        )
        .select([*position_key_columns, "benchmark_weight"])
    )
    joined = (
        portfolio_positions.join(
            benchmark_positions,
            on=position_key_columns,
            how="full",
            coalesce=True,
        )
        .with_columns(
            pl.col("weight").fill_null(0.0),
            pl.col("benchmark_weight").fill_null(0.0),
        )
        .join(
            portfolio_identities,
            on=["signal_date", "entry_date", "portfolio"],
            how="left",
            validate="m:1",
        )
        .join(exposure_join, on=["signal_date", "security_id"], how="left")
    )
    if joined.filter(pl.any_horizontal([pl.col(column).is_null() for column in factors])).height:
        raise _barra_error("组合权重缺少对应 Barra 暴露")
    joined = joined.with_columns(
        (pl.col("weight") - pl.col("benchmark_weight")).alias("active_weight")
    )
    exposure_summary = (
        joined.group_by(["signal_date", "entry_date", "portfolio"])
        .agg(
            [
                (pl.col("active_weight") * pl.col(column)).sum().alias(column)
                for column in factors
            ]
        )
        .sort(["entry_date", "portfolio"])
    )
    long_exposure = exposure_summary.unpivot(
        index=["signal_date", "entry_date", "portfolio"],
        on=list(factors),
        variable_name="factor",
        value_name="active_exposure",
    )
    contribution = long_exposure.join(
        returns,
        on=["entry_date", "factor"],
        how="left",
    )
    if contribution.filter(pl.col("factor_return").is_null()).height:
        raise _barra_error("主动暴露缺少对应 Barra 因子收益")
    contribution = contribution.with_columns(
        (pl.col("active_exposure") * pl.col("factor_return")).alias("contribution")
    )
    explained = contribution.group_by(["signal_date", "entry_date", "portfolio"]).agg(
        pl.col("contribution").sum().alias("explained_return")
    )
    realized = weights.group_by(["signal_date", "entry_date", "portfolio"]).agg(
        [
            pl.col("realized_return").first(),
            pl.col("benchmark_return").first(),
            pl.col("specific_return").first() if "specific_return" in weights.columns else pl.lit(None).alias("specific_return"),
        ]
    )
    reconciliation = (
        explained.join(realized, on=["signal_date", "entry_date", "portfolio"], how="inner")
        .with_columns(
            (pl.col("realized_return") - pl.col("benchmark_return")).alias("active_return"),
            (
                pl.col("realized_return")
                - pl.col("benchmark_return")
                - pl.col("explained_return")
            ).alias("calculated_specific_return"),
        )
    )
    if reconciliation.filter(
        pl.col("specific_return").is_not_null()
        & (
            (pl.col("specific_return") - pl.col("calculated_specific_return")).abs()
            > 1e-10
        )
    ).height:
        raise _barra_error("Barra 已实现贡献与特异收益对账不成立")
    max_error = float(
        reconciliation.with_columns(
            (
                pl.col("active_return")
                - pl.col("explained_return")
                - pl.col("calculated_specific_return")
            )
            .abs()
            .alias("_reconciliation_error")
        )
        .select(pl.col("_reconciliation_error").max())
        .item()
        or 0.0
    )
    attribution_rows = contribution.select(
        ["signal_date", "entry_date", "portfolio", "factor", "active_exposure", "factor_return", "contribution"]
    ).to_dicts()
    attribution_rows.extend(
        reconciliation.select(
            [
                "signal_date",
                "entry_date",
                "portfolio",
                pl.lit("specific_residual").alias("factor"),
                pl.lit(None).cast(pl.Float64).alias("active_exposure"),
                pl.lit(None).cast(pl.Float64).alias("factor_return"),
                pl.col("calculated_specific_return").alias("contribution"),
            ]
        ).to_dicts()
    )
    risk_decomposition = (
        _calculate_risk_decomposition(
            joined=joined,
            exposure_summary=exposure_summary,
            factors=factors,
            covariance=covariance_frame,
            specific_risk=specific_risk_frame,
        )
        if full_risk
        and covariance_frame is not None
        and specific_risk_frame is not None
        else ()
    )
    status: Literal["realized_attribution_only", "complete"] = (
        "complete" if full_risk else "realized_attribution_only"
    )
    return BarraAttributionResult.build(
        policy=policy,
        exposure_summary=tuple(exposure_summary.to_dicts()),
        attribution=tuple(attribution_rows),
        risk_decomposition=risk_decomposition,
        max_abs_reconciliation_error=max_error,
        risk_decomposition_status=status,
    )
