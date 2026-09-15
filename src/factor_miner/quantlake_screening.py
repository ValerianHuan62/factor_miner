"""把 QuantLake 截面因子宽表转换为固定协议的周频筛选面板。"""

from __future__ import annotations

import csv
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Sequence

import polars as pl


KEY_COLUMNS = {"date", "code"}


def _factor_columns(paths: Sequence[Path]) -> tuple[str, ...]:
    if not paths:
        raise ValueError("至少需要一个 QuantLake 截面因子分区")
    schemas = [pl.scan_parquet(path).collect_schema().names() for path in paths]
    first = schemas[0]
    if any(schema != first for schema in schemas[1:]):
        raise ValueError("QuantLake 截面因子分区字段不一致")
    missing = KEY_COLUMNS.difference(first)
    if missing:
        raise ValueError(f"QuantLake 截面因子缺少主键：{sorted(missing)}")
    factors = tuple(name for name in first if name not in KEY_COLUMNS)
    if not factors or len(set(factors)) != len(factors):
        raise ValueError("QuantLake 因子字段为空或重复")
    return factors


def _raw_weekly_panel(
    paths: Sequence[Path],
    factors: Sequence[str],
    label_panel: Path,
    *,
    end: date,
) -> pl.LazyFrame:
    labels = pl.scan_parquet(label_panel).select("date", "code", "label_raw")
    signal_dates = labels.select("date").unique().collect().get_column("date").to_list()
    daily = (
        pl.concat([pl.scan_parquet(path).select("date", "code", *factors) for path in paths])
        .filter(pl.col("date") <= end)
        .sort(["code", "date"])
    )
    return (
        daily.with_columns(pl.col("date").dt.year().alias("_year"), pl.col("date").dt.week().alias("_week"))
        .group_by(["_year", "_week", "code"])
        .agg(
            pl.col("date").max().alias("date"),
            *[
                expression
                for factor in factors
                for expression in (
                    pl.col(factor).tail(5).mean().cast(pl.Float32).alias(f"{factor}__mean5"),
                    pl.col(factor).last().cast(pl.Float32).alias(f"{factor}__last"),
                )
            ],
        )
        .filter(pl.col("date").is_in(signal_dates))
        .select("date", "code", *[f"{factor}__{suffix}" for factor in factors for suffix in ("mean5", "last")])
        .join(labels, on=["date", "code"], how="inner", validate="1:1")
        .sort(["date", "code"])
    )


def _discover_direction(panel: pl.LazyFrame, factor: str, *, start: date, end: date) -> tuple[str, float | None]:
    signal = ((pl.col(f"{factor}__mean5") + pl.col(f"{factor}__last")) / 2.0).alias("signal")
    correlations = (
        panel.filter(pl.col("date").is_between(start, end))
        .select("date", "label_raw", signal)
        .drop_nulls(["signal", "label_raw"])
        .group_by("date")
        .agg(pl.corr("signal", "label_raw", method="spearman").alias("rank_ic"))
        .select(pl.col("rank_ic").mean())
        .collect()
        .item()
    )
    value = float(correlations) if correlations is not None and math.isfinite(float(correlations)) else None
    return ("negative" if value is not None and value < 0 else "positive", value)


def build_quantlake_screening_batches(
    *,
    factor_paths: Sequence[Path],
    label_panel: Path,
    output_root: Path,
    batch_size: int = 30,
    discovery_start: date = date(2021, 1, 1),
    discovery_end: date = date(2023, 12, 31),
    confirmation_end: date = date(2026, 6, 30),
) -> dict[str, object]:
    """分批构建周频面板，并只在发现期确定一次方向。"""

    paths = tuple(path.expanduser().resolve(strict=True) for path in factor_paths)
    label_panel = label_panel.expanduser().resolve(strict=True)
    output_root = output_root.expanduser().resolve(strict=False)
    if output_root.exists():
        raise ValueError("QuantLake 筛选输出目录已存在，请使用新的路径")
    if batch_size < 1:
        raise ValueError("batch_size 必须为正整数")
    factors = _factor_columns(paths)
    output_root.mkdir(parents=True)
    catalog: list[dict[str, object]] = []
    batches: list[dict[str, object]] = []
    for offset in range(0, len(factors), batch_size):
        batch = factors[offset:offset + batch_size]
        number = offset // batch_size + 1
        raw_path = output_root / f"weekly_raw_{number:03d}.parquet"
        _raw_weekly_panel(paths, batch, label_panel, end=confirmation_end).sink_parquet(
            raw_path, compression="zstd", statistics=True
        )
        raw = pl.scan_parquet(raw_path)
        directions: dict[str, float] = {}
        for factor in batch:
            direction, raw_rank_ic = _discover_direction(raw, factor, start=discovery_start, end=discovery_end)
            multiplier = -1.0 if direction == "negative" else 1.0
            directions[factor] = multiplier
            catalog.append(
                {
                    "business_id": factor,
                    "selected_direction": direction,
                    "direction_source": "data_discovered_2021_2023_rank_ic",
                    "discovery_raw_rank_ic": raw_rank_ic,
                }
            )
        output_path = output_root / f"weekly_factor_features_{number:03d}_open.parquet"
        raw.select(
            "date",
            "code",
            "label_raw",
            *[
                (pl.col(f"{factor}__{suffix}") * directions[factor])
                .cast(pl.Float32)
                .alias(f"fm_{factor}__{suffix}")
                for factor in batch
                for suffix in ("mean5", "last")
            ],
        ).sink_parquet(output_path, compression="zstd", statistics=True)
        candidate_path = output_root / f"candidates_{number:03d}.csv"
        with candidate_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["business_id"])
            writer.writeheader()
            writer.writerows({"business_id": factor} for factor in batch)
        raw_path.unlink()
        batches.append(
            {
                "batch": number,
                "candidate_count": len(batch),
                "panel": output_path.name,
                "panel_sha256": sha256(output_path.read_bytes()).hexdigest(),
                "candidates": candidate_path.name,
            }
        )
    catalog_path = output_root / "因子方向目录.csv"
    pl.DataFrame(catalog).write_csv(catalog_path, include_bom=True)
    summary = {
        "version": "quantlake-fixed-screening-panel-v2",
        "label_panel_sha256": sha256(label_panel.read_bytes()).hexdigest(),
        "factor_partition_sha256s": [sha256(path.read_bytes()).hexdigest() for path in paths],
        "factor_count": len(factors),
        "family_size": len(factors),
        "batch_size": batch_size,
        "batch_count": len(batches),
        "direction_rule": "发现期逐周截面 RankIC 均值定向一次，确认期禁止翻转",
        "catalog_sha256": sha256(catalog_path.read_bytes()).hexdigest(),
        "batches": batches,
    }
    (output_root / "批次清单.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_quantlake_selected_daily_panels(
    *,
    factor_paths: Sequence[Path],
    selected_path: Path,
    direction_catalog: Path,
    output_root: Path,
    batch_size: int = 30,
    start: date = date(2021, 1, 1),
    end: date = date(2023, 12, 31),
) -> dict[str, object]:
    """只为已通过门槛的因子输出发现期日频聚类面板。"""

    paths = tuple(path.expanduser().resolve(strict=True) for path in factor_paths)
    output_root = output_root.expanduser().resolve(strict=False)
    if output_root.exists():
        raise ValueError("日频聚类输出目录已存在，请使用新的路径")
    selected = pl.read_csv(selected_path, encoding="utf8-lossy").get_column("business_id").to_list()
    directions = {
        row["business_id"]: (-1.0 if row["selected_direction"] == "negative" else 1.0)
        for row in pl.read_csv(direction_catalog, encoding="utf8-lossy").iter_rows(named=True)
    }
    missing = sorted(set(selected).difference(directions))
    if missing:
        raise ValueError(f"方向目录缺少已选因子：{missing}")
    output_root.mkdir(parents=True)
    outputs: list[dict[str, object]] = []
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset:offset + batch_size]
        number = offset // batch_size + 1
        path = output_root / f"development_daily_{number:03d}.parquet"
        (
            pl.concat([pl.scan_parquet(source).select("date", "code", *batch) for source in paths])
            .filter(pl.col("date").is_between(start, end))
            .select(
                "date",
                pl.col("code").alias("asset"),
                *[(pl.col(factor) * directions[factor]).cast(pl.Float32).alias(f"fm_{factor}") for factor in batch],
            )
            .sort(["date", "asset"])
            .sink_parquet(path, compression="zstd", statistics=True)
        )
        outputs.append({"path": path.name, "factor_count": len(batch), "sha256": sha256(path.read_bytes()).hexdigest()})
    return {"version": "quantlake-selected-daily-v1", "factor_count": len(selected), "outputs": outputs}
