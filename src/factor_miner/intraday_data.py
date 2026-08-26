"""正式分钟 HDF5 的只读合同探针和有界读取器。"""

from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path

import h5py
import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.intraday_schema import IntradaySourceSpec


class IntradayFileContract(BaseModel):
    """单个 HDF5 文件的非原始结构摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_key: str
    row_count: int = Field(ge=0)
    columns: tuple[str, ...]
    dtype_description: str
    datetime_min: int
    datetime_max: int
    file_size_bytes: int = Field(ge=0)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    """流式计算文件身份，不把内容载入日志或模型上下文。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_hdf5_contract(path: Path) -> IntradayFileContract:
    """读取 HDF5 metadata 和时间范围，不返回任何分钟记录。"""

    if not path.is_file():
        raise ValueError("分钟 HDF5 文件不存在")
    with h5py.File(path, "r") as handle:
        if tuple(handle.keys()) != ("data",):
            raise ValueError("分钟 HDF5 根节点必须精确为 data")
        dataset = handle["data"]
        columns = tuple(dataset.dtype.names or ())
        required = IntradaySourceSpec().required_fields
        if columns != required:
            raise ValueError("分钟 HDF5 compound 字段与冻结合同不一致")
        row_count = int(dataset.shape[0])
        if row_count < 1:
            raise ValueError("分钟 HDF5 data 为空")
        datetime_min = int(dataset[0]["datetime"])
        datetime_max = int(dataset[row_count - 1]["datetime"])
        dtype_description = str(dataset.dtype.descr)
    return IntradayFileContract(
        dataset_key="data",
        row_count=row_count,
        columns=columns,
        dtype_description=dtype_description,
        datetime_min=datetime_min,
        datetime_max=datetime_max,
        file_size_bytes=path.stat().st_size,
        file_sha256=_file_sha256(path),
    )


def _date_boundary(value: date, *, end: bool) -> int:
    """把日期转换为冻结的整数时间边界。"""

    suffix = "150000" if end else "093100"
    return int(value.strftime("%Y%m%d") + suffix)


def read_intraday_file(
    path: Path,
    *,
    asset: str,
    start_date: date,
    end_date: date,
    spec: IntradaySourceSpec,
) -> pl.DataFrame:
    """从唯一正式目录读取一个资产的有界分钟切片。"""

    if path.parent != spec.root or path.suffix != ".h5":
        raise ValueError("分钟文件不在冻结正式目录")
    if start_date > end_date:
        raise ValueError("分钟读取日期区间倒置")
    with h5py.File(path, "r") as handle:
        dataset = handle[spec.dataset_key]
        columns = tuple(dataset.dtype.names or ())
        if columns != spec.required_fields:
            raise ValueError("分钟文件字段与冻结合同不一致")
        datetimes = dataset.fields("datetime")[:]
        left = int(np.searchsorted(datetimes, _date_boundary(start_date, end=False)))
        right = int(np.searchsorted(datetimes, _date_boundary(end_date, end=True), side="right"))
        values = dataset[left:right]
    if len(values) == 0:
        raise ValueError("分钟读取区间没有数据")
    payload = {name: values[name] for name in spec.required_fields}
    frame = pl.DataFrame(payload).with_columns(
        pl.col("datetime")
        .cast(pl.String)
        .str.to_datetime(spec.datetime_format, strict=True)
        .alias("datetime"),
        pl.lit(asset).alias("asset"),
    )
    return frame.select("asset", *spec.required_fields)
