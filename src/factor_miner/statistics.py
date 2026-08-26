"""冻结 HAC 均值检验和 Bonferroni family 校正。"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import statsmodels.api as sm
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.schema import CampaignSpec


class HacInference(BaseModel):
    """RankIC 序列的双侧 HAC 均值推断结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nobs: int = Field(ge=1)
    mean: FiniteFloat
    standard_error: FiniteFloat = Field(ge=0)
    t_value: FiniteFloat
    raw_p_value: FiniteFloat = Field(ge=0, le=1)
    bonferroni_p_value: FiniteFloat = Field(ge=0, le=1)
    conf_low: FiniteFloat
    conf_high: FiniteFloat
    maxlags: int = Field(ge=0)


class FrozenStatisticalPolicy(BaseModel):
    """正式研究族在结果揭晓前冻结的统计参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(min_length=1)
    alpha: FiniteFloat = Field(gt=0, le=1)
    hac_max_lags: int = Field(ge=0)
    min_valid_dates: int = Field(ge=1)
    bonferroni_denominator: int = Field(ge=1)
    multiplicity_policy: str = Field(min_length=1)

    @classmethod
    def formal_120(
        cls,
        *,
        policy_id: str,
        alpha: float = 0.05,
        hac_max_lags: int = 5,
        min_valid_dates: int = 60,
    ) -> FrozenStatisticalPolicy:
        """构造阶段 C 固定 120 槽的统计预算，不接受候选覆盖分母。"""

        return cls(
            policy_id=policy_id,
            alpha=alpha,
            hac_max_lags=hac_max_lags,
            min_valid_dates=min_valid_dates,
            bonferroni_denominator=120,
            multiplicity_policy="bonferroni_over_frozen_family_budget",
        )


def bonferroni_adjust(p_value: float, family_size: int) -> float:
    """按冻结 family size 计算 Bonferroni p 值。"""

    if not math.isfinite(p_value) or not 0 <= p_value <= 1:
        raise ValueError("p_value 必须是 [0, 1] 内的有限数字")
    if isinstance(family_size, bool) or family_size <= 0:
        raise ValueError("family_size 必须是正整数")
    if not isinstance(family_size, int):
        raise ValueError("family_size 必须是正整数")
    return min(1.0, p_value * family_size)


def hac_mean_test(
    rank_ic_values: Sequence[float], campaign: CampaignSpec
) -> HacInference:
    """使用 campaign 冻结 lags 和完整 hypothesis budget 执行 HAC 均值检验。

    参数：
        rank_ic_values: 评价层返回的有效 RankIC 序列。
        campaign: 冻结 min_valid_dates、alpha、HAC lags 和 family size 的协议。

    返回：
        包含样本数、均值、标准误、t 值、双侧 raw p、Bonferroni p 和置信区间的结果。

    异常：
        有效日期不足、family budget 未冻结、输入非有限或 statsmodels 推断失败时抛出领域错误。
    """

    if campaign.max_hypotheses < 1 or campaign.max_hypotheses < len(campaign.candidate_ids):
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "campaign hypothesis family size 未冻结或小于已登记候选数",
        )
    policy = FrozenStatisticalPolicy(
        policy_id="legacy_campaign_policy",
        alpha=campaign.alpha,
        hac_max_lags=campaign.hac_max_lags,
        min_valid_dates=campaign.min_valid_dates,
        bonferroni_denominator=campaign.max_hypotheses,
        multiplicity_policy="bonferroni_over_frozen_family_budget",
    )
    return hac_mean_test_frozen(rank_ic_values, policy)


def hac_mean_test_frozen(
    rank_ic_values: Sequence[float],
    policy: FrozenStatisticalPolicy,
) -> HacInference:
    """使用独立冻结的 HAC 参数和正式 120 槽分母推断 RankIC 均值。"""

    if policy.bonferroni_denominator < 1:
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "Bonferroni 分母未冻结",
        )
    if len(rank_ic_values) < policy.min_valid_dates:
        raise FactorMinerError(
            FailureCode.INSUFFICIENT_VALID_DATES,
            f"有效 RankIC 日期不足：{len(rank_ic_values)} < {policy.min_valid_dates}",
        )
    try:
        values = np.asarray(tuple(rank_ic_values), dtype=float)
    except (TypeError, ValueError) as error:
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "RankIC 序列无法转换为有限数字",
        ) from error
    if values.ndim != 1 or not np.isfinite(values).all():
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "RankIC 序列包含非有限数字",
        )

    try:
        design = np.ones((values.size, 1), dtype=float)
        fitted = sm.OLS(values, design, missing="raise").fit(
            cov_type="HAC",
            cov_kwds={"maxlags": policy.hac_max_lags},
        )
        mean = float(fitted.params[0])
        standard_error = float(fitted.bse[0])
        t_value = float(fitted.tvalues[0])
        raw_p_value = float(fitted.pvalues[0])
        confidence = fitted.conf_int(alpha=policy.alpha)
        conf_low = float(confidence[0, 0])
        conf_high = float(confidence[0, 1])
    except (IndexError, TypeError, ValueError, np.linalg.LinAlgError) as error:
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            f"HAC 均值检验失败：{error}",
        ) from error
    values_to_check = (mean, standard_error, t_value, raw_p_value, conf_low, conf_high)
    if not all(math.isfinite(value) for value in values_to_check):
        raise FactorMinerError(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "HAC 结果包含非有限统计量",
        )
    return HacInference(
        nobs=int(values.size),
        mean=mean,
        standard_error=standard_error,
        t_value=t_value,
        raw_p_value=raw_p_value,
        bonferroni_p_value=bonferroni_adjust(
            raw_p_value, policy.bonferroni_denominator
        ),
        conf_low=conf_low,
        conf_high=conf_high,
        maxlags=policy.hac_max_lags,
    )
