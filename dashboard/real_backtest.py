"""为 Dashboard 生成和读取聚合后的真实单因子回测产物。"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import polars as pl

from factor_miner.canonical import canonical_json_bytes
from factor_miner.causal_backtest import simulate_causal_extreme_portfolio
from factor_miner.trading_schedule import RebalanceWindow


REAL_BACKTEST_SCHEMA = "dashboard-real-backtest-v2-causal"
ACCOUNTING_BACKTEST_SCHEMA = "dashboard-real-backtest-v3-accounting"


def _require_columns(frame: pl.LazyFrame, required: set[str], label: str) -> None:
    """要求真实输入显式提供全部字段。"""

    missing = required.difference(frame.collect_schema().names())
    if missing:
        raise ValueError(f"{label}缺少字段：{sorted(missing)}")


def _require_unique(frame: pl.LazyFrame, label: str) -> None:
    """拒绝重复日期和证券主键。"""

    duplicate = (
        frame.group_by("date", "asset")
        .len()
        .filter(pl.col("len") > 1)
        .limit(1)
        .collect()
    )
    if duplicate.height:
        raise ValueError(f"{label}存在重复 date/asset 主键")


def _jsonable(value: object) -> object:
    """把回测审计中的日期递归转换为稳定 JSON 值。"""

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def build_real_us_backtest(
    *,
    factor_path: Path,
    market_path: Path,
    state_path: Path,
    manifest_path: Path,
    output_path: Path,
    source_candidate_id: str,
    factor_id: str,
    factor_name: str,
    start_date: date,
    end_date: date,
    holding_sessions: int = 5,
    group_count: int = 10,
    base_cost_bps: float = 10.0,
    rank_ic_mean: float,
    rank_ic_hac_t: float,
) -> dict[str, Any]:
    """使用真实 split-adjusted open 和显式状态表构建开发期目标多头回测。"""

    if holding_sessions <= 0 or group_count < 2 or base_cost_bps < 0:
        raise ValueError("持有期、分组数或成本模型非法")
    factor = pl.scan_parquet(factor_path)
    market = pl.scan_parquet(market_path)
    state = pl.scan_parquet(state_path)
    _require_columns(
        factor,
        {"date", "asset", "raw_factor", "valid_for_factor_compute"},
        "因子面板",
    )
    _require_columns(market, {"date", "asset", "open"}, "行情面板")
    _require_columns(
        state,
        {
            "date",
            "asset",
            "valid_for_factor_rank",
            "can_open_long",
            "can_close_long",
        },
        "状态面板",
    )
    _require_unique(factor, "因子面板")
    _require_unique(market, "行情面板")
    _require_unique(state, "状态面板")

    sessions = (
        market.select("date")
        .unique()
        .sort("date")
        .collect()
        .get_column("date")
        .to_list()
    )
    session_index = {value: index for index, value in enumerate(sessions)}
    visible_sessions = [value for value in sessions if start_date <= value <= end_date]
    signal_dates = visible_sessions[::holding_sessions]
    schedule_rows = []
    for signal_date in signal_dates:
        index = session_index[signal_date]
        exit_index = index + holding_sessions + 1
        if exit_index >= len(sessions):
            continue
        schedule_rows.append(
            {
                "signal_date": signal_date,
                "entry_date": sessions[index + 1],
                "exit_date": sessions[exit_index],
            }
        )
    if len(schedule_rows) < 2:
        raise ValueError("真实回测不足两个非重叠持有窗口")
    windows = tuple(RebalanceWindow(**row) for row in schedule_rows)
    factor_input = (
        factor.filter(pl.col("date").is_between(start_date, end_date, closed="both"))
        .filter(pl.col("valid_for_factor_compute"))
        .select(
            pl.col("date").alias("signal_date"),
            pl.col("asset").cast(pl.Utf8).alias("security_id"),
            pl.col("raw_factor").alias("factor_value"),
        )
        .collect()
    )
    market_input = market.select(
        pl.col("date").alias("trade_date"),
        pl.col("asset").cast(pl.Utf8).alias("security_id"),
        "open",
    ).collect()
    state_input = state.select(
        pl.col("date").alias("trade_date"),
        pl.col("asset").cast(pl.Utf8).alias("security_id"),
        "valid_for_factor_rank",
        "can_open_long",
        "can_close_long",
    ).collect()
    target_result = simulate_causal_extreme_portfolio(
        factor_input,
        market_input,
        state_input,
        windows,
        direction="positive",
        group_count=group_count,
        round_trip_cost_bps=base_cost_bps,
    )
    benchmark_result = simulate_causal_extreme_portfolio(
        factor_input,
        market_input,
        state_input,
        windows,
        direction="positive",
        group_count=1,
        round_trip_cost_bps=0.0,
    )
    benchmark_by_period = {
        (row["entry_date"], row["exit_date"]): row["target_long_net_return"]
        for row in benchmark_result.daily_returns
    }
    holding_count_by_signal: dict[date, int] = {}
    eligible_count_by_signal: dict[date, int] = {}
    for selection in target_result.selections:
        signal_date = selection["signal_date"]
        holding_count_by_signal[signal_date] = holding_count_by_signal.get(signal_date, 0) + 1  # type: ignore[arg-type]
        eligible_count_by_signal[signal_date] = int(selection["eligible_count"])  # type: ignore[index]
    rows: list[dict[str, object]] = []
    benchmark_terminal_date = max(row["exit_date"] for row in benchmark_result.daily_returns)
    for row in target_result.daily_returns:
        key = (row["entry_date"], row["exit_date"])
        if key in benchmark_by_period:
            benchmark_return = float(benchmark_by_period[key])
        elif row["entry_date"] >= benchmark_terminal_date:
            benchmark_return = 0.0
        else:
            raise ValueError(f"基准缺少目标组合对应逐日区间：{key}")
        signal_date = max(
            (item.signal_date for item in windows if item.signal_date <= row["entry_date"]),
            default=windows[0].signal_date,
        )
        rows.append(
            {
                **row,
                "cost_turnover_sides": 2,
                "entry_date": row["entry_date"].isoformat(),
                "exit_date": row["exit_date"].isoformat(),
                "benchmark_return": benchmark_return,
                "eligible_count": eligible_count_by_signal.get(signal_date, 0),
                "holding_count": holding_count_by_signal.get(signal_date, 0),
            }
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload: dict[str, Any] = {
        "schema_version": REAL_BACKTEST_SCHEMA,
        "mode": "Smoke",
        "market_id": "us_equity",
        "factor_id": factor_id,
        "source_candidate_id": source_candidate_id,
        "factor_name": factor_name,
        "scope": "开发期真实美股数据 Smoke；不是密封 OOS 或 Formal evidence",
        "protocol": {
            "signal_timestamp": "after_close_t",
            "information_cutoff": "close_t",
            "execution_timestamp": "open_t_plus_1",
            "price_source": "TradFi canonical split_adjusted_open",
            "price_basis": "split_adjusted_ohlcv",
            "dividend_treatment": "本 Smoke 仅计算 open-to-open 价格收益，不含现金分红",
            "holding_period": f"{holding_sessions} 个交易日",
            "rebalance_step": f"{holding_sessions} 个交易日，非重叠",
            "universe": "CRSP/EODHD PIT eligible universe；只用 T 日 valid_for_factor_rank 冻结选择",
            "benchmark": "同一 T 日候选池等权，并按相同因果成交状态机执行",
            "benchmark_terminal_treatment": "最后计划退出后基准转为现金；目标延迟退出尾部对应收益为 0",
            "unfilled_entry_treatment": "T+1 无法买入则该权重留作现金，不替补、不追单",
            "delayed_exit_treatment": "计划退出日无法卖出则逐交易日重试，冻结资金不复用",
            "terminal_treatment": "数据终点仍有未合法退出持仓则 fail closed",
            "cost_model_version": "us_equity_dashboard_smoke_v2_causal",
            "base_cost_bps": base_cost_bps,
            "cost_components_bps": {
                "commission": 0.0,
                "spread_and_slippage": base_cost_bps,
                "impact": 0.0,
                "borrow_and_financing": 0.0,
            },
        },
        "data_identity": {
            "source_release_id": "20260828_unified_v3",
            "standard_panel_release_id": manifest.get("release_id"),
            "market_cutoff": manifest.get("market_cutoff"),
            "adjustment_convention": manifest.get("adjustment_convention"),
            "calendar_version": manifest.get("calendar_version"),
            "asset_key": "security_id",
        },
        "evaluation": {
            "rank_ic_mean": rank_ic_mean,
            "rank_ic_hac_t": rank_ic_hac_t,
            "hac_max_lags": 5,
        },
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "daily_rows": rows,
        "execution_audit": {
            "target": target_result.execution_summary,
            "benchmark": benchmark_result.execution_summary,
            "orders": _jsonable(target_result.orders),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json_bytes(payload))
    return payload


def load_real_backtest(path: Path) -> dict[str, Any]:
    """读取聚合真实回测，拒绝缺失字段和非有限收益。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") not in {REAL_BACKTEST_SCHEMA, ACCOUNTING_BACKTEST_SCHEMA}:
        raise ValueError("真实回测产物 Schema 不匹配")
    rows = payload.get("daily_rows")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("真实回测产物缺少逐期聚合收益")
    required = {
        "entry_date", "exit_date", "target_long_gross_return",
        "target_long_turnover", "target_long_cost",
        "target_long_net_return", "benchmark_return",
    }
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError("真实回测逐期字段不完整")
        for field in required.difference({"entry_date", "exit_date"}):
            value = row[field]
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"真实回测逐期字段 {field} 非有限数值")
    quality_path = path.parent.parent / "data_quality_audit.json"
    if payload.get("schema_version") == ACCOUNTING_BACKTEST_SCHEMA and quality_path.is_file():
        payload["data_quality_audit"] = json.loads(quality_path.read_text(encoding="utf-8"))
    return payload


def load_real_backtest_catalog(
    *,
    backtest_path: Path | None = None,
    backtest_roots: tuple[Path, ...] = (),
) -> tuple[dict[str, Any], ...]:
    """加载显式配置的真实回测目录，并按稳定 factor_id 返回。"""

    paths: list[Path] = []
    if backtest_path is not None:
        paths.append(backtest_path)
    for root in backtest_roots:
        if not root.is_dir():
            raise ValueError(f"真实回测目录不存在：{root}")
        paths.extend(sorted(root.glob("*.json")))
    if not paths:
        raise ValueError("未配置真实回测产物")

    required = {
        "factor_id", "factor_name", "market_id", "mode", "scope",
        "evaluation", "protocol", "data_identity", "window",
    }
    catalog: dict[str, dict[str, Any]] = {}
    for path in dict.fromkeys(item.resolve(strict=False) for item in paths):
        header = json.loads(path.read_text(encoding="utf-8"))
        if header.get("schema_version") not in {REAL_BACKTEST_SCHEMA, ACCOUNTING_BACKTEST_SCHEMA}:
            continue
        payload = load_real_backtest(path)
        payload["report_batch"] = str(path.parent.resolve())
        screening_path = path.parent.parent / "screening_summary.json"
        screening = {}
        if screening_path.is_file():
            screening = json.loads(screening_path.read_text(encoding="utf-8"))
            payload["screening"] = screening.get("by_factor", {}).get(str(payload.get("factor_id")), {})
            payload["batch_summary"] = screening.get("message")
        structure_path = path.parent.parent / "legacy_structure_audit.json"
        if structure_path.is_file() and payload.get("screening"):
            structure = json.loads(structure_path.read_text(encoding="utf-8"))
            fid = str(payload["factor_id"])
            repeated = [item for item in structure.get("comparisons", []) if item["factor_id"] == fid and item.get("parameter_or_sign_family_matches")]
            semantic = [item for item in structure.get("semantic", []) if item["factor_id"] == fid and item["status"] == "已有机制与公式家族"]
            if repeated or semantic:
                details = dict(payload["screening"])
                details["novelty_pass"] = False
                details["label"] = "统计通过 · 已知机制" if details.get("statistical_pass") else "未入选 · 已知机制"
                reasons = ["跨市场已有相似公式只影响新颖性，不等于没有研究价值；保留与否另看统计、机制和增量。"]
                for match in repeated:
                    reasons.append("同类来源：A 股 " + "、".join(match["parameter_or_sign_family_matches"]) + "；编号与美股独立。")
                for match in semantic:
                    reasons.append("同类来源：QuantLake " + "、".join(match["reference_ids"]) + "。" + match.get("reason", ""))
                details["reasons"] = list(details.get("reasons", [])) + reasons
                payload["screening"] = details
            stats = {key: value for key, value in screening.get("by_factor", {}).items() if value.get("statistical_pass")}
            duplicates = {item["factor_id"] for item in structure.get("comparisons", []) if item.get("parameter_or_sign_family_matches")}
            duplicates.update(item["factor_id"] for item in structure.get("semantic", []) if item["status"] == "已有机制与公式家族")
            incremental_failed = sum(not item.get("incremental_pass") for item in stats.values())
            payload["batch_summary"] = f"最新批次：统计备选 {len(stats)} 个 · 跨市场已知机制 {len(set(stats) & duplicates)} 个（不据此淘汰） · 增量未通过 {incremental_failed} 个 · 完整达标的新因子 {screening.get('final_count', '尚未判定')} 个"
        missing = sorted(field for field in required if payload.get(field) is None)
        if missing:
            raise ValueError(f"真实回测产物缺少目录字段 {missing}：{path}")
        factor_id = str(payload["factor_id"])
        if factor_id in catalog:
            raise ValueError(f"真实回测 factor_id 重复：{factor_id}")
        catalog[factor_id] = payload
    if not catalog:
        raise ValueError("未找到 dashboard-real-backtest-v2-causal 产物；旧回测已失效")

    def order(item: dict[str, Any]) -> tuple[int, str]:
        factor_id = str(item["factor_id"])
        suffix = factor_id.removeprefix("huan")
        return (int(suffix), factor_id) if suffix.isdigit() else (10**9, factor_id)

    return tuple(sorted(catalog.values(), key=order))
