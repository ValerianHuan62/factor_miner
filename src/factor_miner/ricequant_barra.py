"""从米筐下载并审计发布 Barra 风险模型派生数据。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode


STYLE_NAMES_CN = {
    "beta": "贝塔",
    "book_to_price": "账面市值比",
    "comovement": "市场联动",
    "dividend_yield": "股息率",
    "earnings_quality": "盈利质量",
    "earnings_variability": "盈利波动",
    "earnings_yield": "盈利收益率",
    "growth": "成长",
    "industry_momentum": "行业动量",
    "investment_quality": "投资质量",
    "leverage": "杠杆",
    "liquidity": "流动性",
    "longterm_reversal": "长期反转",
    "mid_cap": "中盘规模",
    "momentum": "动量",
    "profitability": "盈利能力",
    "residual_volatility": "残差波动",
    "seasonality": "季节性",
    "sentiment": "情绪",
    "shortterm_reversal": "短期反转",
    "size": "规模",
}


class RiceQuantBarraProvider(Protocol):
    """米筐外部数据端口；实现必须返回结构化表，不得记录凭据。"""

    def trading_dates(self, start: date, end: date) -> tuple[date, ...]: ...

    def factor_exposure(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame: ...

    def factor_return(self, start: date, end: date) -> pl.DataFrame: ...

    def specific_return(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame: ...

    def factor_covariance(self, dates: tuple[date, ...]) -> pl.DataFrame: ...

    def specific_risk(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame: ...

    def csi300_weights(self, start: date, end: date) -> pl.DataFrame: ...


@dataclass(frozen=True)
class RiceQuantBarraDownloadRequest:
    """不含任何凭据的米筐 Barra 下载请求。"""

    derived_root: Path
    universe_uri: Path
    start_date: date
    end_date: date
    batch_size: int = 500
    model: str = "v2trd"
    industry_mapping: str = "sws_2021"
    benchmark: str = "000300.XSHG"

    def __post_init__(self) -> None:
        """冻结唯一获批模型、行业口径和基本日期合同。"""

        if self.start_date > self.end_date:
            raise _error("下载开始日不能晚于截止日")
        if self.batch_size <= 0:
            raise _error("下载批大小必须为正整数")
        if self.model != "v2trd" or self.industry_mapping != "sws_2021":
            raise _error("当前只允许 v2trd 与申万 2021 的冻结组合")
        if self.benchmark != "000300.XSHG":
            raise _error("当前只允许沪深 300 基准")


@dataclass(frozen=True)
class RiceQuantBarraDownloadResult:
    """一次可恢复下载的发布摘要。"""

    manifest_path: Path
    completed_years: tuple[int, ...]
    reused_years: tuple[int, ...]


def _error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.BARRA_DATA_CONTRACT_INVALID, message)


def _atomic_write(path: Path, payload: bytes) -> None:
    """在同一文件系统内原子替换小型清单文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _file_identity(path: Path, *, row_count: int | None = None) -> dict[str, object]:
    identity: dict[str, object] = {
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
    if row_count is not None:
        identity["row_count"] = row_count
    return identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_universe(path: Path) -> tuple[tuple[str, ...], str]:
    if not path.is_file():
        raise _error("股票代码全集文件不存在")
    if path.suffix.lower() == ".parquet":
        source = pl.scan_parquet(path)
    elif path.suffix.lower() == ".csv":
        source = pl.scan_csv(path)
    else:
        raise _error("股票代码全集只支持 Parquet 或 CSV")
    columns = source.collect_schema().names()
    column = next(
        (
            candidate
            for candidate in ("security_id", "order_book_id", "code")
            if candidate in columns
        ),
        None,
    )
    if column is None:
        raise _error("股票代码全集缺少 security_id、order_book_id 或 code")
    values = (
        source.select(pl.col(column).drop_nulls().cast(pl.String).unique().sort())
        .collect()
        .get_column(column)
        .to_list()
    )
    normalized = tuple(
        sorted(
            identifier
            for identifier in {_to_ricequant_id(value) for value in values}
            if identifier.endswith((".XSHG", ".XSHE"))
        )
    )
    if not normalized:
        raise _error("股票代码全集为空")
    return normalized, _sha256_file(path)


def _to_ricequant_id(value: str) -> str:
    suffixes = {".SH": ".XSHG", ".SZ": ".XSHE", ".BJ": ".XBSE"}
    for source, target in suffixes.items():
        if value.endswith(source):
            return f"{value[:-len(source)]}{target}"
    return value


def _style_code(source_name: str) -> str:
    if source_name == "size":
        return "Size"
    return "Style_" + "_".join(part.capitalize() for part in source_name.split("_"))


def _factor_catalog(source_names: list[str]) -> tuple[dict[str, str], dict[str, object]]:
    industries = sorted(name for name in source_names if name not in STYLE_NAMES_CN)
    mapping = {name: f"industry_{index:03d}" for index, name in enumerate(industries, 1)}
    mapping.update({name: _style_code(name) for name in source_names if name in STYLE_NAMES_CN})
    factors: list[dict[str, str]] = []
    for name in industries:
        factors.append(
            {
                "factor_code": mapping[name],
                "factor_type": "industry",
                "source_name": name,
                "name_cn": name,
                "description_cn": "申万 2021 行业哑变量暴露",
            }
        )
    for name in sorted(
        (item for item in source_names if item in STYLE_NAMES_CN and item != "size")
    ):
        factors.append(
            {
                "factor_code": mapping[name],
                "factor_type": "style",
                "source_name": name,
                "name_cn": STYLE_NAMES_CN[name],
                "description_cn": "米筐 v2trd 风格因子暴露",
            }
        )
    if "size" in source_names:
        factors.append(
            {
                "factor_code": "Size",
                "factor_type": "style",
                "source_name": "size",
                "name_cn": "规模",
                "description_cn": "米筐 v2trd 规模风格因子暴露",
            }
        )
    catalog = {
        "version": "ricequant-barra-factor-catalog-v1",
        "model": "v2trd",
        "industry_mapping": "sws_2021",
        "description_cn": "米筐 Barra 风格与申万 2021 行业因子中文名称目录",
        "factors": factors,
    }
    catalog["catalog_sha256"] = sha256_json(catalog)
    return mapping, catalog


def _normalize_exposure(frame: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
    required = {"date", "order_book_id", *mapping}
    if not required.issubset(frame.columns):
        raise _error("Barra 暴露字段与冻结因子目录不一致")
    factor_columns = ["size"] if "size" in mapping else []
    factor_columns.extend(
        sorted(name for name in mapping if name in STYLE_NAMES_CN and name != "size")
    )
    factor_columns.extend(sorted(name for name in mapping if name not in STYLE_NAMES_CN))
    return (
        frame.select(["date", "order_book_id", *factor_columns])
        .rename(
            {
                "date": "date",
                "order_book_id": "security_id",
                **{name: mapping[name] for name in factor_columns},
            }
        )
        .with_columns(pl.col("date").cast(pl.Date))
        .sort(["date", "security_id"])
    )


def _normalize_factor_return(
    frame: pl.DataFrame, mapping: dict[str, str]
) -> pl.DataFrame:
    required = {"date", *mapping}
    if not required.issubset(frame.columns):
        raise _error("Barra 因子收益字段与暴露字段不一致")
    return (
        frame.select(["date", *mapping])
        .unpivot(index="date", variable_name="factor", value_name="factor_return")
        .with_columns(
            pl.col("date").cast(pl.Date),
            pl.col("factor").replace_strict(mapping),
        )
        .select(["date", "factor", "factor_return"])
        .sort(["date", "factor"])
    )


def _normalize_security_panel(
    frame: pl.DataFrame, value_column: str
) -> pl.DataFrame:
    required = {"date", "order_book_id", value_column}
    if not required.issubset(frame.columns):
        raise _error(f"{value_column} 数据字段不完整")
    normalized = (
        frame.select(["date", "order_book_id", value_column])
        .rename({"order_book_id": "security_id"})
        .with_columns(pl.col("date").cast(pl.Date))
        .filter(pl.col(value_column).is_finite())
    )
    if value_column == "specific_risk":
        normalized = normalized.filter(pl.col(value_column) > 0)
    if normalized.is_empty():
        raise _error(f"{value_column} 没有有效证券日记录")
    return (
        normalized
        .sort(["date", "security_id"])
    )


def _normalize_covariance(
    frame: pl.DataFrame, mapping: dict[str, str]
) -> pl.DataFrame:
    required = {"date", "factor_1", "factor_2", "covariance"}
    if not required.issubset(frame.columns):
        raise _error("Barra 协方差数据字段不完整")
    unknown = set(frame.get_column("factor_1").drop_nulls().to_list()) | set(
        frame.get_column("factor_2").drop_nulls().to_list()
    )
    if unknown.difference(mapping):
        raise _error("Barra 协方差包含目录外因子")
    return (
        frame.select(["date", "factor_1", "factor_2", "covariance"])
        .with_columns(
            pl.col("date").cast(pl.Date),
            pl.col("factor_1").replace_strict(mapping),
            pl.col("factor_2").replace_strict(mapping),
        )
        .rename({"factor_1": "factor_a", "factor_2": "factor_b"})
        .sort(["date", "factor_a", "factor_b"])
    )


def _normalize_weights(frame: pl.DataFrame) -> pl.DataFrame:
    required = {"date", "order_book_id", "weight"}
    if not required.issubset(frame.columns):
        raise _error("沪深 300 权重数据字段不完整")
    normalized = (
        frame.select(["date", "order_book_id", "weight"])
        .rename({"order_book_id": "security_id"})
        .with_columns(pl.col("date").cast(pl.Date))
    )
    if normalized.filter(
        ~pl.col("weight").is_finite() | (pl.col("weight") < 0)
    ).height:
        raise _error("沪深 300 权重包含非有限值或负值")
    sums = normalized.group_by("date").agg(pl.col("weight").sum().alias("weight_sum"))
    if sums.filter(~pl.col("weight_sum").is_finite() | (pl.col("weight_sum") <= 0)).height:
        raise _error("沪深 300 单日权重和必须为有限正数")
    return (
        normalized.with_columns(
            (pl.col("weight") / pl.col("weight").sum().over("date")).alias("weight")
        )
        .sort(["date", "security_id"])
    )


def _partition_identity(
    request: RiceQuantBarraDownloadRequest,
    *,
    year: int,
    start: date,
    end: date,
    universe_sha256: str,
) -> dict[str, object]:
    return {
        "version": "ricequant-barra-partition-v1",
        "year": year,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "model": request.model,
        "industry_mapping": request.industry_mapping,
        "factor_return_method": "implicit",
        "risk_horizon": "daily",
        "benchmark": request.benchmark,
        "universe_sha256": universe_sha256,
    }


def _verify_existing_partition(
    root: Path, year: int, expected_identity: dict[str, object]
) -> bool:
    manifest_path = root / "partitions" / f"year={year}.json"
    if not manifest_path.exists():
        return False
    if not manifest_path.is_file():
        raise _error(f"已存在的 year={year} 不是完整 Barra 分片")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise _error(f"已存在的 year={year} 清单损坏") from error
    if manifest.get("identity") != expected_identity:
        raise _error(f"已存在的 year={year} 与本次下载身份不一致")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise _error(f"已存在的 year={year} 缺少文件身份")
    for name, identity in files.items():
        file_path = root / name
        if (
            not file_path.is_file()
            or not isinstance(identity, dict)
            or _sha256_file(file_path) != identity.get("sha256")
            or file_path.stat().st_size != identity.get("size_bytes")
        ):
            raise _error(f"已存在的 year={year}/{name} 缺失或哈希不一致")
    return True


def _write_partition(
    root: Path,
    *,
    year: int,
    identity: dict[str, object],
    tables: dict[str, pl.DataFrame],
) -> Path:
    staging = root / f".year={year}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        files: dict[str, dict[str, object]] = {}
        for dataset, frame in tables.items():
            path = staging / f"{dataset}.parquet"
            frame.write_parquet(path, compression="zstd", statistics=True)
            relative_path = f"{dataset}/year={year}.parquet"
            files[relative_path] = _file_identity(path, row_count=frame.height)
        manifest = {
            "identity": identity,
            "description_cn": "米筐 Barra 年度原子分片清单",
            "files": files,
        }
        manifest["partition_sha256"] = sha256_json(manifest)
        for dataset in tables:
            target = root / dataset / f"year={year}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / f"{dataset}.parquet", target)
        manifest_path = root / "partitions" / f"year={year}.json"
        _atomic_write(manifest_path, canonical_json_bytes(manifest))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest_path


def _root_partition_record(manifest_path: Path, year: int) -> dict[str, object]:
    """把年度提交标记中的六个文件哈希直接提升到根清单。"""

    payload = json.loads(manifest_path.read_text())
    files = payload.get("files")
    if not isinstance(files, dict) or len(files) != 6:
        raise _error(f"year={year} 年度清单没有完整引用六类数据")
    return {
        "year": year,
        "partition_manifest_sha256": _sha256_file(manifest_path),
        "files": files,
    }


def fetch_ricequant_barra(
    request: RiceQuantBarraDownloadRequest,
    provider: RiceQuantBarraProvider,
) -> RiceQuantBarraDownloadResult:
    """逐年下载六类 Barra 数据，完整分片复用、损坏分片硬失败。"""

    security_ids, universe_sha256 = _read_universe(request.universe_uri)
    request.derived_root.mkdir(parents=True, exist_ok=True)
    completed: list[int] = []
    reused: list[int] = []
    catalog_mapping: dict[str, str] | None = None
    partition_records: list[dict[str, object]] = []

    for year in range(request.start_date.year, request.end_date.year + 1):
        start = max(request.start_date, date(year, 1, 1))
        end = min(request.end_date, date(year, 12, 31))
        identity = _partition_identity(
            request,
            year=year,
            start=start,
            end=end,
            universe_sha256=universe_sha256,
        )
        if _verify_existing_partition(request.derived_root, year, identity):
            reused.append(year)
            completed.append(year)
            partition_records.append(
                _root_partition_record(
                    request.derived_root / "partitions" / f"year={year}.json",
                    year,
                )
            )
            continue

        dates = provider.trading_dates(start, end)
        if not dates:
            raise _error(f"{year} 年没有可用交易日")
        exposure_raw = provider.factor_exposure(
            security_ids, start, end, batch_size=request.batch_size
        )
        source_names = [
            name for name in exposure_raw.columns if name not in {"date", "order_book_id"}
        ]
        current_mapping, current_catalog = _factor_catalog(source_names)
        if catalog_mapping is None:
            catalog_mapping = current_mapping
            catalog_path = request.derived_root / "factor_catalog.json"
            if catalog_path.exists():
                try:
                    existing = json.loads(catalog_path.read_text())
                except (OSError, ValueError) as error:
                    raise _error("既有 Barra 因子目录损坏") from error
                if existing != current_catalog:
                    raise _error("既有 Barra 因子目录与本次米筐字段不一致")
            else:
                _atomic_write(catalog_path, canonical_json_bytes(current_catalog))
        elif current_mapping != catalog_mapping:
            raise _error(f"{year} 年 Barra 因子集合发生变化")

        assert catalog_mapping is not None
        tables = {
            "exposure": _normalize_exposure(exposure_raw, catalog_mapping),
            "factor_return": _normalize_factor_return(
                provider.factor_return(start, end), catalog_mapping
            ),
            "specific_return": _normalize_security_panel(
                provider.specific_return(
                    security_ids, start, end, batch_size=request.batch_size
                ),
                "specific_return",
            ),
            "factor_covariance": _normalize_covariance(
                provider.factor_covariance(dates), catalog_mapping
            ),
            "specific_risk": _normalize_security_panel(
                provider.specific_risk(
                    security_ids, start, end, batch_size=request.batch_size
                ),
                "specific_risk",
            ),
            "csi300_weight": _normalize_weights(
                provider.csi300_weights(start, end)
            ),
        }
        partition_manifest_path = _write_partition(
            request.derived_root,
            year=year,
            identity=identity,
            tables=tables,
        )
        completed.append(year)
        partition_records.append(_root_partition_record(partition_manifest_path, year))

    catalog_path = request.derived_root / "factor_catalog.json"
    if not catalog_path.is_file():
        raise _error("Barra 因子目录尚未发布")
    manifest = {
        "version": "ricequant-barra-manifest-v1",
        "source": "RiceQuant RQData",
        "description_cn": "米筐 v2trd 风格、申万行业、收益与日频风险模型派生数据",
        "model": request.model,
        "industry_mapping": request.industry_mapping,
        "factor_return_method": "implicit",
        "risk_horizon": "daily",
        "benchmark": request.benchmark,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "universe_sha256": universe_sha256,
        "factor_catalog_sha256": _sha256_file(catalog_path),
        "partitions": partition_records,
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    manifest_path = request.derived_root / "manifest.json"
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    return RiceQuantBarraDownloadResult(
        manifest_path=manifest_path,
        completed_years=tuple(completed),
        reused_years=tuple(reused),
    )


def load_ricequant_credentials(path: Path) -> tuple[str, str]:
    """从私有 env 文件读取凭据；错误和返回摘要绝不包含凭据值。"""

    if not path.is_file():
        raise _error("米筐私有凭据文件不存在")
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            if key.strip() in {"RQ_USERNAME", "RQ_TOKEN"}:
                values[key.strip()] = value.strip().strip("'\"")
    except OSError as error:
        raise _error("无法读取米筐私有凭据文件") from error
    if not values.get("RQ_USERNAME") or not values.get("RQ_TOKEN"):
        raise _error("米筐私有凭据文件缺少 RQ_USERNAME 或 RQ_TOKEN")
    return values["RQ_USERNAME"], values["RQ_TOKEN"]


def _as_polars(frame: Any) -> pl.DataFrame:
    """把 rqdatac 的 pandas 返回值转换为显式 Polars 表。"""

    reset = frame.reset_index()
    return pl.DataFrame(reset.to_dict(orient="list"))


class RqdatacBarraProvider:
    """rqdatac 3.5 系列的 v2trd/sws_2021 只读适配器。"""

    def __init__(self, username: str, token: str) -> None:
        try:
            import rqdatac
        except ImportError as error:
            raise _error("当前 Python 环境未安装 rqdatac") from error
        self._rqdatac = rqdatac
        rqdatac.init(username, token)

    def close(self) -> None:
        self._rqdatac.reset()

    def trading_dates(self, start: date, end: date) -> tuple[date, ...]:
        values = self._rqdatac.get_trading_dates(start, end)
        return tuple(value.date() if hasattr(value, "date") else value for value in values)

    def _batched_panel(
        self,
        function: Any,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
        value_name: str | None = None,
    ) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for offset in range(0, len(security_ids), batch_size):
            pandas_frame = function(list(security_ids[offset : offset + batch_size]), start, end)
            if pandas_frame is None or bool(getattr(pandas_frame, "empty", False)):
                continue
            frame = _as_polars(pandas_frame)
            if value_name is not None and value_name not in frame.columns:
                index_columns = [name for name in ("date",) if name in frame.columns]
                frame = frame.unpivot(
                    index=index_columns,
                    variable_name="order_book_id",
                    value_name=value_name,
                )
            frames.append(frame)
        if not frames:
            raise _error(f"{start} 至 {end} 的证券批次均无米筐数据")
        return pl.concat(frames, how="diagonal_relaxed")

    def factor_exposure(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        return self._batched_panel(
            lambda ids, first, last: self._rqdatac.get_factor_exposure(
                ids,
                first,
                last,
                factors=None,
                industry_mapping="sws_2021",
                model="v2trd",
            ),
            security_ids,
            start,
            end,
            batch_size=batch_size,
        )

    def factor_return(self, start: date, end: date) -> pl.DataFrame:
        return _as_polars(
            self._rqdatac.get_factor_return(
                start,
                end,
                factors=None,
                universe="whole_market",
                method="implicit",
                industry_mapping="sws_2021",
                model="v2trd",
            )
        )

    def specific_return(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        return self._batched_panel(
            lambda ids, first, last: self._rqdatac.get_specific_return(
                ids, first, last, model="v2trd", industry_mapping="sws_2021"
            ),
            security_ids,
            start,
            end,
            batch_size=batch_size,
            value_name="specific_return",
        )

    def factor_covariance(self, dates: tuple[date, ...]) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for current in dates:
            frame = _as_polars(
                self._rqdatac.get_factor_covariance(
                    current,
                    horizon="daily",
                    model="v2trd",
                    industry_mapping="sws_2021",
                )
            )
            index_column = next(
                (name for name in ("factor", "index") if name in frame.columns),
                frame.columns[0],
            )
            frames.append(
                frame.unpivot(
                    index=index_column,
                    variable_name="factor_2",
                    value_name="covariance",
                )
                .rename({index_column: "factor_1"})
                .with_columns(pl.lit(current).cast(pl.Date).alias("date"))
                .select(["date", "factor_1", "factor_2", "covariance"])
            )
        return pl.concat(frames, how="vertical")

    def specific_risk(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        return self._batched_panel(
            lambda ids, first, last: self._rqdatac.get_specific_risk(
                ids,
                first,
                last,
                horizon="daily",
                model="v2trd",
                industry_mapping="sws_2021",
            ),
            security_ids,
            start,
            end,
            batch_size=batch_size,
            value_name="specific_risk",
        )

    def csi300_weights(self, start: date, end: date) -> pl.DataFrame:
        frame = _as_polars(
            self._rqdatac.index_weights(
                "000300.XSHG", start_date=start, end_date=end
            )
        )
        if "weight" not in frame.columns:
            value_columns = [
                name for name in frame.columns if name not in {"date", "order_book_id"}
            ]
            if len(value_columns) != 1:
                raise _error("沪深 300 权重返回结构无法识别")
            frame = frame.rename({value_columns[0]: "weight"})
        return frame
