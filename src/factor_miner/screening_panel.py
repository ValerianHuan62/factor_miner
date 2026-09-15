"""候选筛选周频面板的 open-to-open 标签重建。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import polars as pl


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebuild_open_label_panel(
    *,
    feature_panel: Path,
    market_paths: tuple[Path, ...],
    state_path: Path,
    output_path: Path,
    market_date_column: str = "date",
    market_asset_column: str = "code",
    market_open_column: str = "adj_open",
    state_date_column: str = "trade_date",
    state_asset_column: str = "code",
    tradable_column: str = "valid_for_trading",
) -> dict[str, object]:
    """用每只股票未来第 1 与第 6 个可交易日开盘价替换旧标签。"""

    feature_panel = feature_panel.expanduser().resolve(strict=True)
    state_path = state_path.expanduser().resolve(strict=True)
    paths = tuple(path.expanduser().resolve(strict=True) for path in market_paths)
    if not paths:
        raise ValueError("至少需要一个行情 Parquet")
    output_path = output_path.expanduser().resolve(strict=False)
    metadata_path = output_path.with_suffix(output_path.suffix + ".metadata.json")
    if output_path.exists() or metadata_path.exists():
        raise ValueError("输出或元数据已存在，请使用新的路径")

    features = pl.scan_parquet(feature_panel)
    schema = features.collect_schema()
    required_features = {"date", "code"}
    if missing := required_features.difference(schema.names()):
        raise ValueError(f"周频特征面板缺少字段：{sorted(missing)}")

    bars = pl.concat(
        [
            pl.scan_parquet(path).select(
                pl.col(market_date_column).alias("date"),
                pl.col(market_asset_column).alias("code"),
                pl.col(market_open_column).cast(pl.Float64).alias("open"),
            )
            for path in paths
        ]
    )
    state = pl.scan_parquet(state_path).select(
        pl.col(state_date_column).alias("date"),
        pl.col(state_asset_column).alias("code"),
        pl.col(tradable_column).cast(pl.Boolean).alias("tradable"),
    )
    calendar = (
        bars.select("date").unique().sort("date")
        .with_columns(
            pl.col("date").shift(-1).alias("entry_date_open"),
            pl.col("date").shift(-6).alias("exit_date_open"),
        )
    )
    executable_open = bars.join(state, on=["date", "code"], how="left", validate="1:1").select(
        "date",
        "code",
        pl.when(
            pl.col("tradable").fill_null(False)
            & pl.col("open").is_finite()
            & (pl.col("open") > 0)
        ).then(pl.col("open")).otherwise(None).alias("executable_open"),
    )
    entry = executable_open.rename({"date": "entry_date_open", "executable_open": "entry_open"})
    exit_ = executable_open.rename({"date": "exit_date_open", "executable_open": "exit_open"})
    fixed_horizon = (
        features.select("date", "code")
        .join(calendar, on="date", how="left", validate="m:1")
        .join(entry, on=["entry_date_open", "code"], how="left", validate="1:1")
        .join(exit_, on=["exit_date_open", "code"], how="left", validate="1:1")
        .with_columns((pl.col("exit_open") / pl.col("entry_open") - 1.0).alias("label_o2o_5d"))
        .select("date", "code", "entry_date_open", "exit_date_open", "label_o2o_5d")
    )
    old_contract = {
        name for name in ("label_raw", "target_z", "entry_date", "exit_date")
        if name in schema.names()
    }
    panel = (
        features.drop(*sorted(old_contract))
        .join(fixed_horizon, on=["date", "code"], how="left", validate="1:1")
        .with_columns(
            pl.col("label_o2o_5d").alias("label_raw"),
            (
                (pl.col("label_o2o_5d") - pl.col("label_o2o_5d").mean().over("date"))
                / pl.col("label_o2o_5d").std(ddof=0).over("date")
            ).alias("target_z"),
            pl.col("entry_date_open").alias("entry_date"),
            pl.col("exit_date_open").alias("exit_date"),
        )
        .drop("entry_date_open", "exit_date_open", "label_o2o_5d")
        .sort(["date", "code"])
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    panel.sink_parquet(temporary, compression="zstd", statistics=True)
    temporary.replace(output_path)

    audit = (
        pl.scan_parquet(output_path)
        .select(
            pl.len().alias("rows"),
            pl.col("date").n_unique().alias("weeks"),
            pl.col("date").min().alias("date_min"),
            pl.col("date").max().alias("date_max"),
            pl.col("label_raw").is_not_null().mean().alias("label_coverage"),
        )
        .collect()
        .to_dicts()[0]
    )
    metadata = {
        "version": "screening-open-panel-v2",
        "label": "label_o2o_5d = adjusted_open(global_market_t+6) / adjusted_open(global_market_t+1) - 1; fixed entry/exit non-tradable => null",
        "feature_panel_sha256": _sha256_file(feature_panel),
        "market_sha256s": [_sha256_file(path) for path in paths],
        "state_sha256": _sha256_file(state_path),
        "output_sha256": _sha256_file(output_path),
        **{key: str(value) if key.startswith("date_") else value for key, value in audit.items()},
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def merge_feature_columns(
    *,
    panel_path: Path,
    source_path: Path,
    prefixes: tuple[str, ...],
    output_path: Path,
) -> dict[str, object]:
    """按 date/code 把显式前缀的特征合并到 open 标签面板。"""

    panel_path = panel_path.expanduser().resolve(strict=True)
    source_path = source_path.expanduser().resolve(strict=True)
    output_path = output_path.expanduser().resolve(strict=False)
    metadata_path = output_path.with_suffix(output_path.suffix + ".metadata.json")
    if output_path.exists() or metadata_path.exists():
        raise ValueError("输出或元数据已存在，请使用新的路径")
    source = pl.scan_parquet(source_path)
    names = source.collect_schema().names()
    selected = sorted(name for name in names if any(name.startswith(prefix) for prefix in prefixes))
    if not selected:
        raise ValueError("来源面板没有匹配前缀的特征")
    panel_names = pl.scan_parquet(panel_path).collect_schema().names()
    overlap = sorted(set(selected).intersection(panel_names))
    if overlap:
        raise ValueError(f"待合并特征与目标面板重名：{overlap}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    (
        pl.scan_parquet(panel_path)
        .join(source.select("date", "code", *selected), on=["date", "code"], how="left", validate="1:1")
        .sort(["date", "code"])
        .sink_parquet(temporary, compression="zstd", statistics=True)
    )
    temporary.replace(output_path)
    metadata = {
        "version": "screening-feature-merge-v1",
        "feature_count": len(selected),
        "prefixes": list(prefixes),
        "panel_sha256": _sha256_file(panel_path),
        "source_sha256": _sha256_file(source_path),
        "output_sha256": _sha256_file(output_path),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata
