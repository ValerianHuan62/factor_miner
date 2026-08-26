"""分钟聚合数据的唯一来源和可得时点合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


FROZEN_MINUTE_ROOT = Path(
    "/data/quantlake/raw/market/minbar_h5/equities_final_2005_20260630"
)


class IntradaySourceSpec(BaseModel):
    """服务器已聚合 HDF5 的冻结读取合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path = FROZEN_MINUTE_ROOT
    dataset_key: Literal["data"] = "data"
    datetime_format: Literal["%Y%m%d%H%M%S"] = "%Y%m%d%H%M%S"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    expected_minutes_per_complete_day: Literal[240] = 240
    earliest_use: Literal["next_trading_day_open"] = "next_trading_day_open"
    required_fields: tuple[str, ...] = Field(
        default=(
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "total_turnover",
            "num_trades",
        ),
        min_length=8,
        max_length=8,
    )

    @field_validator("root")
    @classmethod
    def validate_frozen_root(cls, value: Path) -> Path:
        """禁止扫描回填、补丁或旧分钟目录。"""

        if value != FROZEN_MINUTE_ROOT:
            raise ValueError("分钟数据只能读取冻结的 equities_final_2005_20260630")
        return value
