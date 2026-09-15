"""从不可变候选 Spec 构建便携的日频与周频筛选特征面板。"""

from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path

import polars as pl

from factor_miner.compiler import build_polars_expr
from factor_miner.schema import FactorNode


def build_factor_screening_panel(
    *,
    candidates_path: Path,
    artifact_root: Path,
    schedule_panel: Path,
    market_paths: tuple[Path, ...],
    state_path: Path,
    output_root: Path,
    batch_size: int = 50,
    selection_path: Path | None = None,
    development_start: date | None = None,
    development_end: date | None = None,
) -> dict[str, object]:
    """分批计算冻结方向的 mean5/last 周频特征，限制峰值内存。"""

    candidates_path = candidates_path.expanduser().resolve(strict=True)
    artifact_root = artifact_root.expanduser().resolve(strict=True)
    schedule_panel = schedule_panel.expanduser().resolve(strict=True)
    state_path = state_path.expanduser().resolve(strict=True)
    market_paths = tuple(path.expanduser().resolve(strict=True) for path in market_paths)
    output_root = output_root.expanduser().resolve(strict=False)
    if output_root.exists():
        raise ValueError("因子面板输出目录已存在，请使用新的路径")
    if batch_size < 1:
        raise ValueError("batch_size 必须为正整数")
    with candidates_path.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    if selection_path is not None:
        with selection_path.expanduser().resolve(strict=True).open(encoding="utf-8-sig", newline="") as handle:
            selected_ids = {(row.get("business_id") or "").strip() for row in csv.DictReader(handle)}
        source_rows = [row for row in source_rows if row.get("business_id") in selected_ids]
        if len(source_rows) != len(selected_ids):
            raise ValueError("选择清单不能完整映射到候选来源")
    if (development_start is None) != (development_end is None):
        raise ValueError("development_start 和 development_end 必须同时提供")
    required = {"candidate_id", "business_id", "source_run_id", "selected_direction"}
    if not source_rows or any(required.difference(row) for row in source_rows):
        raise ValueError("候选清单缺少 candidate_id/business_id/source_run_id/selected_direction")
    if len({row["business_id"] for row in source_rows}) != len(source_rows):
        raise ValueError("候选清单 business_id 重复")

    expressions: list[pl.Expr] = []
    catalog: list[dict[str, object]] = []
    for row in source_rows:
        spec_path = artifact_root / "artifacts" / "runs" / row["source_run_id"] / "candidates" / row["candidate_id"] / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        business_id = row["business_id"]
        raw = f"_raw_{business_id}"
        daily = f"fm_{business_id}"
        sign = -1.0 if row["selected_direction"] == "negative" else 1.0
        expressions.append(build_polars_expr(FactorNode.model_validate(spec["expression"])).alias(raw))
        catalog.append(
            {
                "candidate_id": row["candidate_id"],
                "business_id": business_id,
                "source_run_id": row["source_run_id"],
                "selected_direction": row["selected_direction"],
                "raw_column": raw,
                "daily_column": daily,
                "direction_multiplier": sign,
                "max_lookback": int(spec["max_lookback"]),
            }
        )
    if not market_paths:
        raise ValueError("至少需要一个行情 Parquet")
    bars = pl.concat(
        [
            pl.scan_parquet(path).select(
                pl.col("date"), pl.col("code").alias("asset"),
                pl.col("adj_open").alias("open"), pl.col("adj_high").alias("high"),
                pl.col("adj_low").alias("low"), pl.col("adj_close").alias("close"),
                pl.col("volume"), pl.col("money").alias("amount"),
            )
            for path in market_paths
        ]
    )
    state = pl.scan_parquet(state_path).select(
        pl.col("trade_date").alias("date"), pl.col("code").alias("asset"),
        pl.col("valid_for_factor_compute"),
    )
    output_root.mkdir(parents=True)
    signal_dates = pl.scan_parquet(schedule_panel).select("date").unique().collect().get_column("date").to_list()
    batch_rows: list[dict[str, object]] = []
    for start in range(0, len(catalog), batch_size):
        batch_catalog = catalog[start:start + batch_size]
        batch_expressions = expressions[start:start + batch_size]
        raw_columns = [str(item["raw_column"]) for item in batch_catalog]
        clipped_columns = [f"_clip_{item['business_id']}" for item in batch_catalog]
        daily_columns = [str(item["daily_column"]) for item in batch_catalog]
        daily = (
            bars.join(state, on=["date", "asset"], how="left", validate="1:1")
            .sort(["asset", "date"])
            .with_columns(batch_expressions)
            .with_columns(
                pl.when(pl.col("valid_for_factor_compute").fill_null(False) & pl.col(name).is_finite())
                .then(pl.col(name)).otherwise(None).alias(name)
                for name in raw_columns
            )
            .with_columns(
                pl.col(raw).clip(pl.col(raw).quantile(0.01).over("date"), pl.col(raw).quantile(0.99).over("date")).alias(clipped)
                for raw, clipped in zip(raw_columns, clipped_columns, strict=True)
            )
            .with_columns(
                (((pl.col(clipped) - pl.col(clipped).mean().over("date")) / (pl.col(clipped).std(ddof=0).over("date") + 1e-8)) * float(item["direction_multiplier"]))
                .cast(pl.Float32).alias(str(item["daily_column"]))
                for item, clipped in zip(batch_catalog, clipped_columns, strict=True)
            )
            .select("date", "asset", *daily_columns)
        )
        weekly = (
            daily.with_columns(pl.col("date").dt.year().alias("_year"), pl.col("date").dt.week().alias("_week"))
            .sort(["asset", "date"])
            .group_by(["_year", "_week", "asset"])
            .agg(
                pl.col("date").max().alias("date"),
                *[
                    expression
                    for column in daily_columns
                    for expression in (
                        pl.col(column).tail(5).mean().cast(pl.Float32).alias(f"{column}__mean5"),
                        pl.col(column).last().cast(pl.Float32).alias(f"{column}__last"),
                    )
                ],
            )
            .filter(pl.col("date").is_in(signal_dates))
            .select(pl.col("date"), pl.col("asset").alias("code"), pl.exclude("_year", "_week", "asset", "date"))
            .sort(["date", "code"])
        )
        number = start // batch_size + 1
        if development_start is not None and development_end is not None:
            daily.filter(pl.col("date").is_between(development_start, development_end)).sort(["date", "asset"]).sink_parquet(
                output_root / f"development_daily_{number:03d}.parquet",
                compression="zstd",
                statistics=True,
            )
        weekly_path = output_root / f"weekly_factor_features_{number:03d}.parquet"
        weekly.sink_parquet(weekly_path, compression="zstd", statistics=True)
        candidate_path = output_root / f"candidates_{number:03d}.csv"
        with candidate_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["business_id"])
            writer.writeheader()
            writer.writerows({"business_id": item["business_id"]} for item in batch_catalog)
        batch_rows.append({"batch": number, "candidate_count": len(batch_catalog), "panel": str(weekly_path), "candidates": str(candidate_path)})
    (output_root / "候选目录.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_root / "批次清单.json").write_text(json.dumps(batch_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"version": "screening-factor-panel-v2", "candidate_count": len(catalog), "batch_count": len(batch_rows), "batch_size": batch_size}
