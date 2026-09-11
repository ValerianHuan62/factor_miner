"""Dashboard 本地市场配置与只读数据预检。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class MarketProfile:
    """一个市场的显式上游、字段合同和独立产物根。"""

    market_id: str
    display_name: str
    adapter: str
    data_root: Path
    market_path: Path
    state_path: Path
    manifest_path: Path | None
    artifact_root: Path
    backtest_path: Path | None
    backtest_roots: tuple[Path, ...]
    date_column: str
    asset_column: str
    market_columns: dict[str, str]
    state_columns: tuple[str, ...]
    adjustment_convention: str
    calendar_version: str
    research_root: Path | None = None
    worker_config_path: Path | None = None
    library_review_path: Path | None = None
    research_pool_path: Path | None = None
    regime_manifest_path: Path | None = None
    regime_context_id: str | None = None
    cost_review_path: Path | None = None
    joint_library_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MarketPreflight:
    """只读预检结果；不把可访问误报成可研究。"""

    ready: bool
    rows: int | None
    start_date: str | None
    end_date: str | None
    market_size_bytes: int | None
    state_size_bytes: int | None
    run_count: int
    failures: tuple[str, ...]


def _resolve(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path.resolve(strict=False) if path.is_absolute() else (base / path).resolve(strict=False)


def _resolve_many(base: Path, values: object) -> tuple[Path, ...]:
    """解析显式路径数组；不扫描未配置的研究目录。"""

    if values is None:
        return ()
    if not isinstance(values, list):
        raise ValueError("backtest_roots 必须是路径数组")
    result: list[Path] = []
    for value in values:
        path = _resolve(base, str(value))
        if path is not None:
            result.append(path)
    return tuple(result)


def market_profiles_path() -> Path:
    """返回显式配置或仓库内被 Git 忽略的本地配置。"""

    configured = os.environ.get("FM_MARKET_PROFILES_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1] / "configs/market_profiles.local.json"


def load_market_profiles(path: Path | None = None) -> tuple[MarketProfile, ...]:
    """加载本地市场配置；不存在时返回空集合，不回退个人路径。"""

    source = (path or market_profiles_path()).expanduser().resolve(strict=False)
    if not source.is_file():
        return ()
    payload = json.loads(source.read_text(encoding="utf-8"))
    values = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError("市场配置必须包含 markets 数组")
    result: list[MarketProfile] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("每个市场配置必须是对象")
        market_id = str(item.get("market_id", "")).strip()
        if not market_id or market_id in seen:
            raise ValueError("market_id 必须非空且不能重复")
        seen.add(market_id)
        data_root = _resolve(source.parent, str(item.get("data_root", "")))
        artifact_root = _resolve(source.parent, str(item.get("artifact_root", "")))
        if data_root is None or artifact_root is None:
            raise ValueError(f"市场 {market_id} 缺少 data_root 或 artifact_root")
        market_path = _resolve(data_root, str(item.get("market_path", "")))
        state_path = _resolve(data_root, str(item.get("state_path", "")))
        if market_path is None or state_path is None:
            raise ValueError(f"市场 {market_id} 缺少行情或状态表路径")
        columns = item.get("market_columns")
        if not isinstance(columns, dict):
            raise ValueError(f"市场 {market_id} 缺少 market_columns")
        result.append(
            MarketProfile(
                market_id=market_id,
                display_name=str(item.get("display_name", market_id)),
                adapter=str(item.get("adapter", "standard_panel_v1")),
                data_root=data_root,
                market_path=market_path,
                state_path=state_path,
                manifest_path=_resolve(data_root, item.get("manifest_path")),
                artifact_root=artifact_root,
                backtest_path=_resolve(source.parent, item.get("backtest_path")),
                backtest_roots=_resolve_many(source.parent, item.get("backtest_roots")),
                date_column=str(item.get("date_column", "date")),
                asset_column=str(item.get("asset_column", "asset")),
                market_columns={str(key): str(value) for key, value in columns.items()},
                state_columns=tuple(str(value) for value in item.get("state_columns", ())),
                adjustment_convention=str(item.get("adjustment_convention", "unknown")),
                calendar_version=str(item.get("calendar_version", "unknown")),
                research_root=_resolve(source.parent, item.get("research_root")),
                worker_config_path=_resolve(source.parent, item.get("worker_config_path")),
                library_review_path=_resolve(source.parent, item.get("library_review_path")),
                research_pool_path=_resolve(source.parent, item.get("research_pool_path")),
                regime_manifest_path=_resolve(source.parent, item.get("regime_manifest_path")),
                regime_context_id=item.get("regime_context_id"),
                cost_review_path=_resolve(source.parent, item.get("cost_review_path")),
                joint_library_path=_resolve(source.parent, item.get("joint_library_path")),
            )
        )
    return tuple(result)


def profile_by_id(market_id: str) -> MarketProfile | None:
    """按稳定市场 ID 查找配置。"""

    return next((item for item in load_market_profiles() if item.market_id == market_id), None)


def current_market_id() -> str:
    """返回当前 Dashboard 市场；无会话时使用第一个显式 profile。"""

    profiles = load_market_profiles()
    fallback = default_market_id(profiles)
    try:
        import streamlit as st  # type: ignore[import-not-found]

        value = str(st.session_state.get("fm_market_id", fallback))
    except (ImportError, RuntimeError):
        return fallback
    return value if any(profile.market_id == value for profile in profiles) else fallback


def default_market_id(profiles: tuple[MarketProfile, ...]) -> str:
    """优先进入美股；没有美股配置时使用第一个可用市场。"""

    if any(profile.market_id == "us_equity" for profile in profiles):
        return "us_equity"
    return profiles[0].market_id if profiles else "a_share"


def dashboard_dsn(market_id: str | None = None) -> str:
    """返回当前市场的独立 PostgreSQL DSN；美股不回退到 A 股连接。"""

    selected = (market_id or current_market_id()).strip()
    configured = os.environ.get(f"FM_DASHBOARD_DSN_{selected.upper()}", "").strip()
    if configured:
        return configured
    if selected == "a_share":
        return os.environ.get("FM_DASHBOARD_DSN", "").strip()
    return ""


def _date_text(value: object) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value) if value is not None else None


def _scan_table(path: Path) -> pl.LazyFrame:
    """按扩展名只读扫描 CSV 或 Parquet，不引入市场专属运行时。"""

    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pl.scan_parquet(path)
    if suffix == ".csv":
        return pl.scan_csv(path, try_parse_dates=True)
    raise ValueError(f"仅支持 CSV 或 Parquet：{path}")


def preflight_market(profile: MarketProfile) -> MarketPreflight:
    """只读取 Parquet Schema 和统计元数据，验证字段映射与数据边界。"""

    failures: list[str] = []
    if not profile.market_path.is_file():
        failures.append("行情表不存在")
    if not profile.state_path.is_file():
        failures.append("状态表不存在")
    if profile.manifest_path is not None and not profile.manifest_path.is_file():
        failures.append("上游清单不存在")
    rows: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    if profile.market_path.is_file():
        market_scan = _scan_table(profile.market_path)
        schema = market_scan.collect_schema()
        required_market = {
            profile.date_column,
            profile.asset_column,
            *profile.market_columns.values(),
        }
        missing = sorted(required_market.difference(schema.names()))
        if missing:
            failures.append(f"行情缺列：{missing}")
        elif schema[profile.date_column] not in (pl.Date, pl.Datetime):
            failures.append("行情日期列不是 Date/Datetime")
        else:
            summary = (
                market_scan
                .select(
                    pl.len().alias("rows"),
                    pl.col(profile.date_column).min().alias("start_date"),
                    pl.col(profile.date_column).max().alias("end_date"),
                )
                .collect()
                .row(0, named=True)
            )
            rows = int(summary["rows"])
            start_date = _date_text(summary["start_date"])
            end_date = _date_text(summary["end_date"])
    if profile.state_path.is_file():
        state_schema = _scan_table(profile.state_path).collect_schema()
        required_state = {
            profile.date_column,
            profile.asset_column,
            *profile.state_columns,
        }
        missing_state = sorted(required_state.difference(state_schema.names()))
        if missing_state:
            failures.append(f"状态表缺列：{missing_state}")
    runs_root = profile.artifact_root / "artifacts/runs"
    run_count = sum(1 for path in runs_root.glob("run_*") if path.is_dir()) if runs_root.is_dir() else 0
    return MarketPreflight(
        ready=not failures,
        rows=rows,
        start_date=start_date,
        end_date=end_date,
        market_size_bytes=profile.market_path.stat().st_size if profile.market_path.is_file() else None,
        state_size_bytes=profile.state_path.stat().st_size if profile.state_path.is_file() else None,
        run_count=run_count,
        failures=tuple(failures),
    )
