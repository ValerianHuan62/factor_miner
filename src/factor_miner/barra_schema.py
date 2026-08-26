"""Barra 行业、Size、风格暴露与收益归因的数据合同。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factor_miner.canonical import sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


class BarraEvaluationPolicy(BaseModel):
    """结果前冻结的 Barra 归因和完整风险分解要求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Literal["1"] = "1"
    model_family: Literal["Barra"] = "Barra"
    model_version: str = Field(min_length=1)
    exposure_version: str = Field(min_length=1)
    factor_return_version: str = Field(min_length=1)
    industry_dimension: Literal["industry"] = "industry"
    industry_schema_version: str = Field(min_length=1)
    required_style_factors: tuple[str, ...] = ("Size",)
    exposure_asof_rule: Literal["signal_date_or_latest_prior"] = (
        "signal_date_or_latest_prior"
    )
    attribution_mode: Literal[
        "realized_attribution_only",
        "full_risk_decomposition",
    ] = "realized_attribution_only"
    covariance_version: str | None = None
    specific_risk_version: str | None = None

    @field_validator(
        "model_version",
        "exposure_version",
        "factor_return_version",
        "industry_schema_version",
    )
    @classmethod
    def validate_versions(cls, value: str) -> str:
        """模型和数据版本不能是空白。"""

        if not value.strip():
            raise ValueError("Barra 版本不能为空")
        return value

    @field_validator("required_style_factors")
    @classmethod
    def validate_style_factors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """样式因子列表必须有 Size 且保持稳定排序。"""

        if "Size" not in values:
            raise ValueError("Barra 归因必须包含 Size 暴露")
        if tuple(sorted(set(values))) != values:
            raise ValueError("Barra 样式因子必须唯一并按字典序排列")
        return values

    @model_validator(mode="after")
    def validate_full_risk_sources(self) -> BarraEvaluationPolicy:
        """完整风险分解必须同时声明协方差和特异风险版本。"""

        if self.attribution_mode == "full_risk_decomposition":
            if not self.covariance_version or not self.specific_risk_version:
                raise ValueError("完整风险分解缺少协方差或特异风险版本")
        return self


class BarraInputIdentity(BaseModel):
    """单次运行使用的 Barra 输入版本和可得日身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exposure_version: str = Field(min_length=1)
    factor_return_version: str = Field(min_length=1)
    covariance_version: str | None = None
    specific_risk_version: str | None = None
    exposure_asof_date: date
    signal_date: date
    industry_columns: tuple[str, ...] = Field(min_length=1)
    style_columns: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_asof_and_columns(self) -> BarraInputIdentity:
        """暴露不得晚于信号日，并且必须包含行业和 Size。"""

        if self.exposure_asof_date > self.signal_date:
            raise ValueError("Barra 暴露不能晚于信号日")
        if "Size" not in self.style_columns:
            raise ValueError("Barra 输入缺少 Size 暴露列")
        if len(set(self.industry_columns)) != len(self.industry_columns):
            raise ValueError("Barra 行业列不能重复")
        if len(set(self.style_columns)) != len(self.style_columns):
            raise ValueError("Barra 风格列不能重复")
        return self


def validate_barra_input_identity(
    policy: BarraEvaluationPolicy,
    identity: BarraInputIdentity,
) -> None:
    """校验版本、时点和必需暴露字段的硬合同。"""

    if (
        identity.exposure_version != policy.exposure_version
        or identity.factor_return_version != policy.factor_return_version
    ):
        raise FactorMinerError(
            FailureCode.BARRA_DATA_CONTRACT_INVALID,
            "Barra 暴露或因子收益版本与政策不一致",
        )
    if policy.attribution_mode == "full_risk_decomposition" and (
        identity.covariance_version != policy.covariance_version
        or identity.specific_risk_version != policy.specific_risk_version
    ):
        raise FactorMinerError(
            FailureCode.BARRA_DATA_CONTRACT_INVALID,
            "完整 Barra 风险分解的风险源版本不一致",
        )
    if any(
        required not in identity.style_columns
        for required in policy.required_style_factors
    ):
        raise FactorMinerError(
            FailureCode.BARRA_DATA_CONTRACT_INVALID,
            "Barra 输入缺少冻结政策要求的字段",
        )


def barra_policy_id(policy: BarraEvaluationPolicy) -> str:
    """根据完整 Barra 政策生成内容寻址身份。"""

    return f"barrapolicy_{sha256_json(policy.model_dump(mode='json'))[:24]}"
