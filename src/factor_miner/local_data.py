"""把用户本地 CSV 或 Parquet 转换为可审计的跨市场标准发布。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess

import polars as pl

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.data_source import IDENTITY_COLUMNS, MASK_COLUMNS
from factor_miner.errors import FactorMinerError, FailureCode


_REQUIRED_MARKET_COLUMNS = ("date", "asset", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class LocalDataRelease:
    """一次本地标准数据发布的入口文件。"""

    release_id: str
    release_root: Path
    manifest_path: Path
    config_path: Path


def _error(message: str) -> FactorMinerError:
    return FactorMinerError(FailureCode.DATA_RELEASE_MISMATCH, message)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_input(path: Path) -> pl.DataFrame:
    if not path.is_file():
        raise _error(f"本地数据文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pl.read_csv(path, try_parse_dates=True)
    elif suffix in {".parquet", ".pq"}:
        frame = pl.read_parquet(path)
    else:
        raise _error("本地数据只支持 CSV 或 Parquet")
    missing = set(_REQUIRED_MARKET_COLUMNS).difference(frame.columns)
    if missing:
        raise _error(f"本地行情缺少标准列：{sorted(missing)}")
    if frame.schema.get("date") == pl.Datetime:
        frame = frame.with_columns(pl.col("date").cast(pl.Date))
    elif frame.schema.get("date") != pl.Date:
        try:
            frame = frame.with_columns(pl.col("date").cast(pl.String).str.to_date(strict=True))
        except Exception as error:
            raise _error(f"date 无法解析为交易日期：{error}") from error
    return frame


def _validate_market(frame: pl.DataFrame) -> pl.DataFrame:
    selected = [*_REQUIRED_MARKET_COLUMNS]
    if "amount" in frame.columns:
        selected.append("amount")
    selected.extend(column for column in MASK_COLUMNS if column in frame.columns)
    frame = frame.select(selected).sort(list(IDENTITY_COLUMNS))
    if frame.select(pl.any_horizontal(pl.col("date").is_null(), pl.col("asset").is_null())).to_series().any():
        raise _error("date/asset 主键不能包含空值")
    duplicates = frame.group_by(list(IDENTITY_COLUMNS)).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise _error(f"本地行情存在重复 date/asset 主键：{duplicates.head(1).to_dicts()}")
    numeric = ("open", "high", "low", "close", "volume")
    invalid = frame.select(
        pl.any_horizontal(
            *[pl.col(column).cast(pl.Float64, strict=False).is_null() for column in numeric]
        ).any()
    ).item()
    if invalid:
        raise _error("OHLCV 必须全部是可转换为数值的非空值")
    frame = frame.with_columns(pl.col(column).cast(pl.Float64) for column in numeric)
    bad_price = frame.filter(
        (pl.col("open") <= 0)
        | (pl.col("high") <= 0)
        | (pl.col("low") <= 0)
        | (pl.col("close") <= 0)
        | (pl.col("volume") < 0)
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
    )
    if bad_price.height:
        raise _error(f"OHLCV 数值关系非法：{bad_price.head(1).to_dicts()}")
    return frame


def prepare_local_release(
    input_path: Path,
    output_root: Path,
    *,
    artifact_root: Path,
    adjustment_convention: str,
    calendar_version: str,
    data_origin: str = "local_files",
    assume_tradable: bool = False,
    repository_root: Path | None = None,
) -> LocalDataRelease:
    """生成 market/state/label、manifest 和可直接传给 ``doctor`` 的配置。"""

    source = input_path.expanduser().resolve(strict=False)
    release_root = output_root.expanduser().resolve(strict=False)
    artifacts = artifact_root.expanduser().resolve(strict=False)
    if release_root == artifacts or release_root in artifacts.parents or artifacts in release_root.parents:
        raise _error("数据发布目录与运行产物目录必须分离")
    release_root.mkdir(parents=True, exist_ok=True)
    targets = tuple(release_root / name for name in ("market.parquet", "state.parquet", "label.parquet", "manifest.json", "runtime.env"))
    if any(path.exists() for path in targets):
        raise _error("输出目录已包含正式发布文件；请使用新的空目录")

    frame = _validate_market(_read_input(source))
    supplied_masks = set(MASK_COLUMNS).intersection(frame.columns)
    if supplied_masks and supplied_masks != set(MASK_COLUMNS):
        raise _error("三类有效性 mask 必须全部提供或全部省略")
    if supplied_masks:
        state = frame.select([*IDENTITY_COLUMNS, *MASK_COLUMNS])
        if any(state.schema.get(column) != pl.Boolean for column in MASK_COLUMNS):
            raise _error("三类有效性 mask 必须是 Boolean 类型")
        state_version = "user-supplied-masks-v1"
    else:
        if not assume_tradable:
            raise _error("数据缺少有效性 mask；确认数据已处理停牌和可交易状态后显式使用 --assume-tradable")
        state = frame.select(list(IDENTITY_COLUMNS)).with_columns(
            pl.lit(True).alias(column) for column in MASK_COLUMNS
        )
        state_version = "explicit-assume-tradable-v1"

    market_columns = [*_REQUIRED_MARKET_COLUMNS]
    if "amount" in frame.columns:
        market_columns.append("amount")
    market = frame.select(market_columns)
    label = market.select(
        *IDENTITY_COLUMNS,
        (
            pl.col("open").shift(-6).over("asset")
            / pl.col("open").shift(-1).over("asset")
            - 1.0
        ).alias("label_o2o_5d"),
    )
    market_path, state_path, label_path = targets[:3]
    market.write_parquet(market_path)
    state.write_parquet(state_path)
    label.write_parquet(label_path)
    file_hashes = {
        "market_sha256": _sha256_file(market_path),
        "state_sha256": _sha256_file(state_path),
        "label_sha256": _sha256_file(label_path),
    }
    release_id = f"local_{sha256_json(file_hashes)[:24]}"
    cutoff = market.get_column("date").max()
    if cutoff is None:
        raise _error("本地行情不能为空")
    manifest = {
        "release_id": release_id,
        "release_kind": "standard_panel_v1",
        "data_origin": data_origin,
        "release_root": str(release_root),
        "label_release_root": str(release_root),
        "market_uri": str(market_path),
        "state_uri": str(state_path),
        "label_uri": str(label_path),
        **file_hashes,
        "schema_version": "standard-panel-v1",
        "market_cutoff": cutoff.isoformat(),
        "adjustment_convention": adjustment_convention,
        "calendar_version": calendar_version,
        "state_table_version": state_version,
        "state_table_cutoff": cutoff.isoformat(),
        "source_file_sha256": _sha256_file(source),
    }
    manifest_path = targets[3]
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        code_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise _error("无法读取当前 Git 提交；正式配置必须绑定代码身份") from error
    config_path = targets[4]
    values = {
        "FM_MODE": "visible",
        "FM_QUANTLAKE_ROOT": "",
        "FM_ARTIFACT_ROOT": str(artifacts),
        "FM_SMOKE_INPUT_ROOT": "",
        "FM_DERIVED_RELEASE_ROOT": "",
        "FM_MARKET_URI": str(market_path),
        "FM_STATE_URI": str(state_path),
        "FM_LABEL_URI": str(label_path),
        "FM_DATA_ORIGIN": data_origin,
        "FM_RESOLVED_RELEASE_ID": release_id,
        "FM_RELEASE_MANIFEST_PATH": str(manifest_path),
        "FM_RELEASE_MANIFEST_SHA256": _sha256_file(manifest_path),
        "FM_SCHEMA_VERSION": "standard-panel-v1",
        "FM_MARKET_CUTOFF": cutoff.isoformat(),
        "FM_ADJUSTMENT_CONVENTION": adjustment_convention,
        "FM_CALENDAR_VERSION": calendar_version,
        "FM_STATE_TABLE_VERSION": state_version,
        "FM_STATE_TABLE_CUTOFF": cutoff.isoformat(),
        "FM_CODE_COMMIT": code_commit,
        "FM_CONFIG_PATH": str(config_path),
    }
    values["FM_CONFIG_HASH"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    config_path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return LocalDataRelease(release_id, release_root, manifest_path, config_path)
