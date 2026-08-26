"""因子计算使用的数据源端口和标准数据合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import polars as pl


IDENTITY_COLUMNS: tuple[str, ...] = ("date", "asset")
MARKET_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
STATE_BASE_COLUMNS: tuple[str, ...] = (
    "is_st",
    "is_newly_listed",
    "is_suspended",
    "can_buy",
    "can_sell",
)
MASK_COLUMNS: tuple[str, ...] = (
    "valid_for_factor_compute",
    "valid_for_factor_rank",
    "valid_for_trading",
)
LABEL_COLUMNS: tuple[str, ...] = ("label_o2o_5d",)
CANONICAL_COLUMNS: tuple[str, ...] = (
    *IDENTITY_COLUMNS,
    *MARKET_COLUMNS,
    *STATE_BASE_COLUMNS,
    *MASK_COLUMNS,
    *LABEL_COLUMNS,
)


@dataclass(frozen=True, slots=True)
class DataRequest:
    """描述一次数据读取的日期范围、字段和 warmup 要求。"""

    start: date
    end: date
    required_fields: tuple[str, ...]
    warmup_days: int = 0

    def __post_init__(self) -> None:
        """校验请求范围和字段集合。"""

        if self.end < self.start:
            raise ValueError("数据请求的 end 不能早于 start")
        if self.warmup_days < 0:
            raise ValueError("warmup_days 不能为负数")
        if not self.required_fields:
            raise ValueError("required_fields 不能为空")
        if any(not field.strip() for field in self.required_fields):
            raise ValueError("required_fields 不能包含空字段")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields 不能重复")


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """一次数据合同验证所绑定的完整 provenance。"""

    data_origin: str
    resolved_release_id: str
    release_manifest_sha256: str
    schema_version: str
    market_cutoff: str
    adjustment_convention: str
    calendar_version: str
    state_table_version: str
    state_table_cutoff: str
    code_commit: str
    config_hash: str

    def __post_init__(self) -> None:
        """拒绝任何空 provenance 字段。"""

        fields = (
            self.data_origin,
            self.resolved_release_id,
            self.release_manifest_sha256,
            self.schema_version,
            self.market_cutoff,
            self.adjustment_convention,
            self.calendar_version,
            self.state_table_version,
            self.state_table_cutoff,
            self.code_commit,
            self.config_hash,
        )
        if any(not value.strip() for value in fields):
            raise ValueError("DataProvenance 的所有字段都必须非空")


class DataSource(Protocol):
    """向因子计算提供合同化 LazyFrame 的数据源端口。"""

    def inspect(self) -> DataProvenance:
        """先验证数据合同并返回 provenance。"""

    def scan(self, request: DataRequest) -> pl.LazyFrame:
        """在 inspect 通过后按请求返回 LazyFrame。"""

    def validate_contract(self) -> None:
        """验证 schema、主键、cutoff、状态 mask 和 provenance。"""


@dataclass(frozen=True, slots=True)
class FactorInputRequest:
    """只请求因子输入和 mask，不包含任何 outcome。"""

    start: date
    end: date
    required_fields: tuple[str, ...]
    warmup_observations: int = 0

    def __post_init__(self) -> None:
        """校验 input-only 请求的范围、字段和 warmup。"""

        if self.end < self.start:
            raise ValueError("FactorInputRequest end 不能早于 start")
        if self.warmup_observations < 0:
            raise ValueError("warmup_observations 不能为负数")
        if not self.required_fields or any(
            not field.strip() for field in self.required_fields
        ):
            raise ValueError("required_fields 不能为空或包含空字段")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields 不能重复")


@dataclass(frozen=True, slots=True)
class OutcomeRequest:
    """只请求冻结 visible 区间的 outcome。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        """拒绝反向日期区间。"""

        if self.end < self.start:
            raise ValueError("OutcomeRequest end 不能早于 start")


@dataclass(frozen=True, slots=True)
class InputProvenance:
    """market/state input 合同绑定的 provenance。"""

    data_origin: str
    resolved_release_id: str
    release_manifest_sha256: str
    schema_version: str
    market_cutoff: str
    adjustment_convention: str
    calendar_version: str
    state_table_version: str
    state_table_cutoff: str
    code_commit: str
    config_hash: str

    def __post_init__(self) -> None:
        """拒绝任何空 input provenance 字段。"""

        if any(
            not value.strip()
            for value in (
                self.data_origin,
                self.resolved_release_id,
                self.release_manifest_sha256,
                self.schema_version,
                self.market_cutoff,
                self.adjustment_convention,
                self.calendar_version,
                self.state_table_version,
                self.state_table_cutoff,
                self.code_commit,
                self.config_hash,
            )
        ):
            raise ValueError("InputProvenance 的所有字段都必须非空")


@dataclass(frozen=True, slots=True)
class OutcomeProvenance:
    """label release 与冻结 policy 的独立 provenance。"""

    label_id: str
    label_manifest_sha256: str
    label_schema_version: str
    label_cutoff: str
    label_formula_version: str
    source_release_id: str

    def __post_init__(self) -> None:
        """拒绝任何空 outcome provenance 字段。"""

        if any(
            not value.strip()
            for value in (
                self.label_id,
                self.label_manifest_sha256,
                self.label_schema_version,
                self.label_cutoff,
                self.label_formula_version,
                self.source_release_id,
            )
        ):
            raise ValueError("OutcomeProvenance 的所有字段都必须非空")


@dataclass(frozen=True, slots=True)
class ReferenceProvenance:
    """冻结 reference factor manifest 的 provenance。"""

    manifest_id: str
    manifest_sha256: str
    factor_ids: tuple[str, ...]
    data_cutoff: str

    def __post_init__(self) -> None:
        """拒绝空 manifest、空 factor set 和重复 factor ID。"""

        if not self.manifest_id.strip() or not self.manifest_sha256.strip():
            raise ValueError("reference manifest ID/hash 不能为空")
        if not self.factor_ids or any(not value.strip() for value in self.factor_ids):
            raise ValueError("reference factor_ids 不能为空")
        if len(set(self.factor_ids)) != len(self.factor_ids):
            raise ValueError("reference factor_ids 不能重复")
        if not self.data_cutoff.strip():
            raise ValueError("reference data_cutoff 不能为空")


class FactorInputSource(Protocol):
    """只允许检查和扫描 market/state input 的端口。"""

    def inspect_inputs(self) -> InputProvenance:
        """验证 input 合同，不得访问 label。"""

    def scan_inputs(self, request: FactorInputRequest) -> pl.LazyFrame:
        """返回行情字段与 mask，不得返回 label。"""


class OutcomeSource(Protocol):
    """只允许 evaluator 打开的 outcome 端口。"""

    def inspect_outcomes(self) -> OutcomeProvenance:
        """验证 label release 与冻结 policy。"""

    def scan_outcomes(self, request: OutcomeRequest) -> pl.LazyFrame:
        """返回 date/asset/label，不得返回行情。"""


class ReferenceFactorSource(Protocol):
    """读取冻结 reference manifest 的端口。"""

    def inspect_references(self, manifest_id: str) -> ReferenceProvenance:
        """验证调用方请求的 manifest 与配置一致。"""

    def scan_reference(
        self,
        factor_id: str,
        start: date,
        end: date,
    ) -> pl.LazyFrame:
        """返回单个 reference 的 date/asset/raw_factor。"""
