"""A 股与美股数据模块页面。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dashboard.market_profiles import MarketProfile, preflight_market, profile_by_id
from dashboard.ui import apply_theme, hero, metric_card, section


def _size(value: int | None) -> str:
    """把文件大小格式化为易读文本。"""

    if value is None:
        return "—"
    return f"{value / 1024 ** 3:.2f} GB"


def _manifest_notes(profile: MarketProfile) -> tuple[str, ...]:
    """提取少量上游状态，不执行或修改清单内容。"""

    path = profile.manifest_path
    if path is None or not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ("上游清单存在，但不是可解析 JSON。",)
    notes: list[str] = []
    if isinstance(payload, dict):
        if payload.get("canonical_promoted") is True:
            notes.append("上游标记：canonical_promoted=true")
        downstream = payload.get("downstream_status")
        if downstream not in (None, "ready", "complete", "completed"):
            notes.append(f"上游下游状态：{downstream}；正式运行前需要重新确认。")
    return tuple(notes)


def _mapping_rows(profile: MarketProfile) -> list[dict[str, str]]:
    """展示统一面板字段如何从本地上游显式映射。"""

    rows = [
        {"标准字段": "date", "上游字段或规则": profile.date_column},
        {"标准字段": "asset", "上游字段或规则": profile.asset_column},
    ]
    rows.extend(
        {"标准字段": key, "上游字段或规则": value}
        for key, value in profile.market_columns.items()
    )
    if profile.market_id == "a_share":
        rows.extend(
            [
                {"标准字段": "valid_for_factor_compute", "上游字段或规则": "valid_for_factor"},
                {"标准字段": "valid_for_factor_rank", "上游字段或规则": "valid_for_factor AND NOT is_suspended"},
                {"标准字段": "valid_for_trading", "上游字段或规则": "valid_for_factor AND can_buy AND can_sell"},
            ]
        )
    else:
        rows.extend(
            {"标准字段": value, "上游字段或规则": value}
            for value in profile.state_columns
        )
    return rows


def render_market_page(st: Any, market_id: str) -> None:
    """渲染一个市场的输入合同、边界与独立运行空间。"""

    profile = profile_by_id(market_id)
    apply_theme(st, page_title=f"Factor Miner｜{profile.display_name if profile else market_id}")
    if profile is None:
        hero(
            st,
            kicker="MARKET ADAPTER / NOT CONFIGURED",
            title="这个市场还没有本地配置",
            copy="复制 configs/market-profiles.example.json 为 configs/market_profiles.local.json，再填写你自己的只读数据路径。配置文件被 Git 忽略。",
        )
        return

    preflight = preflight_market(profile)
    hero(
        st,
        kicker=f"MARKET MODULE / {profile.market_id.upper()}",
        title=f"{profile.display_name}研究空间",
        copy=(
            "这个模块只读取行情、状态表和上游清单，并把字段显式映射到 Factor Miner 标准面板。"
            "能够读取数据不等于因子有效；正式结论仍要求冻结假设、标签、掩码、评价协议和独立产物。"
        ),
        meta=f"适配器 {profile.adapter} · 产物与其他市场隔离",
    )

    cards = st.columns(4)
    with cards[0]:
        metric_card(st, "适配预检", "通过" if preflight.ready else "未通过", "只验证路径、Schema 与清单")
    with cards[1]:
        metric_card(st, "行情行数", f"{preflight.rows:,}" if preflight.rows is not None else "—", "不加载全表到内存")
    with cards[2]:
        metric_card(st, "覆盖日期", f"{preflight.start_date or '—'}\n至 {preflight.end_date or '—'}", "上游行情边界")
    with cards[3]:
        metric_card(st, "已发布运行", str(preflight.run_count), "仅限当前市场产物根")

    if preflight.failures:
        for failure in preflight.failures:
            st.error(failure)
    else:
        st.success("路径和字段预检通过；尚未执行标准化发布、因子计算或回测。")
    for note in _manifest_notes(profile):
        st.warning(note) if "需要" in note else st.caption(note)

    section(st, "数据入口", "本地文件保持只读")
    st.dataframe(
        [
            {"对象": "数据根", "路径": str(profile.data_root), "大小": "—"},
            {"对象": "行情表", "路径": str(profile.market_path), "大小": _size(preflight.market_size_bytes)},
            {"对象": "状态表", "路径": str(profile.state_path), "大小": _size(preflight.state_size_bytes)},
            {"对象": "独立产物根", "路径": str(profile.artifact_root), "大小": "—"},
        ],
        width="stretch",
        hide_index=True,
    )

    section(st, "标准面板映射", "任何市场都通过显式字段与状态合同接入")
    st.dataframe(_mapping_rows(profile), width="stretch", hide_index=True)
    st.caption(f"复权口径：{profile.adjustment_convention}；交易日历：{profile.calendar_version}。系统不会猜测停牌、退市、涨跌停或交易时段语义。")

    section(st, "研究链路", "一条市场一套产物，结果不串用")
    st.code(
        "只读上游 → 标准面板发布 → 冻结研究合同 → 因子计算与评价 → 不可变产物 → PostgreSQL 投影 → Dashboard",
        language=None,
    )
    if preflight.run_count == 0:
        st.info("当前还没有该市场的正式运行。IC、回测、Barra 与审计页面会保持可访问，并明确显示等待状态。")
