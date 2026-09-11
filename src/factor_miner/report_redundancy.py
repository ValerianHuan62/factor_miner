"""候选冻结后的同市场历史库查重，仅拒绝重复，不能回传生成新变体。"""
from pathlib import Path
from datetime import date
import json
import polars as pl

from factor_miner.research_report import write_json


def check_legacy(reports: list[dict], legacy_root: Path, state: pl.LazyFrame,
                 start: date, end: date, threshold: float, output_root: Path) -> set[int]:
    """逐候选比较完整发现期每日截面 Spearman，缺少重叠不能宣称低相关。"""
    manifest = json.loads((legacy_root / "run_manifest.json").read_text())
    old = manifest["reports"]
    mask = state.filter(pl.col("date").is_between(start, end) & pl.col("valid_for_factor_rank")).select("date", "asset")
    rejected = set()
    comparisons = []
    for index, current in enumerate(reports):
        print(f"冻结后历史库去重 {index+1}/{len(reports)}", flush=True)
        raw = pl.scan_parquet(Path(current["folder"]) / "raw_factor.parquet").filter(
            pl.col("valid_for_factor_compute") & pl.col("raw_factor").is_finite()).select("date", "asset", pl.col("raw_factor").alias("new"))
        base = mask.join(raw, on=["date", "asset"]).collect().lazy()
        for reference in old:
            prior = pl.scan_parquet(Path(reference["folder"]) / "raw_factor.parquet").filter(
                pl.col("valid_for_factor_compute") & pl.col("raw_factor").is_finite()).select("date", "asset", pl.col("raw_factor").alias("old"))
            daily = base.join(prior, on=["date", "asset"]).group_by("date").agg(
                pl.len().alias("names"), pl.corr("new", "old", method="spearman").abs().alias("corr")
            ).filter((pl.col("names") >= 20) & pl.col("corr").is_finite()).collect()
            p95 = daily["corr"].quantile(.95) if daily.height >= 60 else None
            same_ast = current["ast_hash"] == reference.get("ast_hash")
            failed = same_ast or p95 is None or p95 >= threshold
            if failed:
                rejected.add(index)
            comparisons.append(dict(candidate_id=current["candidate_id"], reference_id=reference["factor_id"],
                canonical_duplicate=same_ast, valid_dates=daily.height, p95_abs_spearman=p95,
                status="duplicate" if same_ast or (p95 is not None and p95 >= threshold) else ("insufficient_overlap" if p95 is None else "distinct")))
    write_json(output_root / "legacy_redundancy.json", dict(threshold=threshold, comparisons=comparisons,
        rejected_slots=[reports[i]["slot"] for i in sorted(rejected)], source_run=manifest["run_id"],
        scope="只比较当前美股标准面板的历史因子；A股输出不可与美股截面直接相关；不读取确认期"))
    return rejected
