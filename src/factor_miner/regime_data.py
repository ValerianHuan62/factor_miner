"""市场状态研究使用的 input-only 公司 A 股数据端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import polars as pl

from factor_miner.company_a_share import CompanyAShareFactorInputSource
from factor_miner.data_source import IDENTITY_COLUMNS, InputProvenance, STATE_BASE_COLUMNS
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.runtime import RuntimeProfile


@dataclass(frozen=True, slots=True)
class RegimeDataRequest:
    """一次市场状态 input-only 日期请求。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        """日期区间必须正向。"""

        if self.end < self.start:
            raise ValueError("RegimeDataRequest end 不能早于 start")


class RegimeDataSource(Protocol):
    """只提供市场和状态、不接触 outcome 的数据源端口。"""

    def inspect_inputs(self) -> InputProvenance:
        """验证输入合同并返回完整 provenance。"""

    def scan_inputs(self, request: RegimeDataRequest) -> pl.LazyFrame:
        """读取指定日期区间的市场状态输入。"""


class CompanyAShareRegimeDataSource:
    """复用公司 input 合同并投影 HMM 所需行情和基础状态。"""

    def __init__(
        self,
        profile: RuntimeProfile,
        *,
        code_commit: str,
        config_hash: str,
    ) -> None:
        """保存显式路径并构造不读取标签的合同验证器。"""

        self._profile = profile
        self._input_source = CompanyAShareFactorInputSource(
            profile,
            code_commit=code_commit,
            config_hash=config_hash,
        )
        self._provenance: InputProvenance | None = None

    @property
    def quantlake_root(self) -> Path | None:
        """暴露只读 QuantLake 根目录供产物边界检查。"""

        return self._profile.quantlake_root

    def inspect_inputs(self) -> InputProvenance:
        """验证 market/state、状态 mask、cutoff 和 provenance。"""

        self._provenance = self._input_source.inspect_inputs()
        return self._provenance

    def scan_inputs(self, request: RegimeDataRequest) -> pl.LazyFrame:
        """连接市场和状态表，绝不读取 label URI。"""

        if self._provenance is None:
            raise FactorMinerError(
                FailureCode.STATE_COVERAGE_INCOMPLETE,
                "必须先通过市场状态 input 合同检查",
            )
        market_path = self._required_path(self._profile.market_uri, "market_uri")
        state_path = self._required_path(self._profile.state_uri, "state_uri")
        market = (
            pl.scan_parquet(market_path)
            .filter(pl.col("date").is_between(request.start, request.end, closed="both"))
            .select([*IDENTITY_COLUMNS, "close", "amount"])
        )
        state = (
            pl.scan_parquet(state_path)
            .filter(pl.col("date").is_between(request.start, request.end, closed="both"))
            .select(
                [
                    *IDENTITY_COLUMNS,
                    *STATE_BASE_COLUMNS,
                    "valid_for_factor_rank",
                ]
            )
        )
        return market.join(
            state,
            on=list(IDENTITY_COLUMNS),
            how="inner",
            validate="1:1",
        ).sort(list(IDENTITY_COLUMNS))

    @staticmethod
    def _required_path(path: Path | None, name: str) -> Path:
        """返回已配置路径；合同通过后缺失仍按稳定错误失败。"""

        if path is None:
            raise FactorMinerError(
                FailureCode.FIELD_MISSING,
                f"市场状态数据源缺少 {name}",
            )
        return path
