"""Dashboard 可视化组件与研究指标格式化辅助。"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from factor_miner.dashboard_projection import candidate_reference_id


PRIMARY = "#1d1d1f"
ACCENT = "#0071e3"
POSITIVE = "#34c759"
MUTED = "#6e6e73"
GRID = "#d2d2d7"


def apply_theme(st: Any, *, page_title: str) -> None:
    """应用统一的研究驾驶舱视觉主题。"""

    st.set_page_config(page_title=page_title, page_icon="◈", layout="wide")
    st.markdown(
        """
        <style>
        :root { --ink:#1d1d1f; --muted:#6e6e73; --paper:#f5f5f7; --line:#d2d2d7;
                --blue:#0071e3; --green:#34c759; --orange:#ff9f0a; }
        .stApp { background: var(--paper); color: var(--ink); }
        [data-testid="stHeader"] { background: rgba(245,245,247,.78); backdrop-filter: blur(18px); }
        [data-testid="stSidebar"] { background:rgba(245,245,247,.76); border-right:1px solid rgba(210,210,215,.65); }
        [data-testid="stSidebar"] * { color:#1d1d1f !important; }
        [data-testid="stSidebarNav"] span { font-weight:600; letter-spacing:-.01em; }
        h1, h2, h3 { color:var(--ink); letter-spacing:-.035em; font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',sans-serif; }
        h1 { font-size:2.55rem; font-weight:700; }
        h2 { font-size:1.35rem; margin-top:1.2rem; }
        h3 { font-size:1.05rem; }
        .hero { background:linear-gradient(145deg,#ffffff 0%,#f7f7fa 60%,#e9f2ff 100%);
                border:1px solid rgba(210,210,215,.7); border-radius:28px; padding:38px 42px 34px; color:var(--ink); margin:4px 0 24px;
                box-shadow:0 18px 45px rgba(0,0,0,.055); position:relative; overflow:hidden; }
        .hero:after { content:''; position:absolute; width:330px; height:330px; right:-120px; top:-175px;
                      border:1px solid rgba(0,113,227,.18); border-radius:50%; box-shadow:0 0 0 34px rgba(0,113,227,.045),0 0 0 70px rgba(0,113,227,.028); }
        .hero-kicker { color:var(--blue); font-size:.72rem; text-transform:uppercase; letter-spacing:.14em; font-weight:700; }
        .hero-title { font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',sans-serif; font-size:3rem; font-weight:700; letter-spacing:-.055em; line-height:1.05; margin:13px 0 10px; }
        .hero-copy { color:#424245; max-width:780px; font-size:1.02rem; line-height:1.55; }
        .hero-meta { color:var(--muted); font-size:.82rem; margin-top:20px; }
        .section-kicker { color:var(--blue); font-size:.7rem; text-transform:uppercase; letter-spacing:.13em; font-weight:700; margin:26px 0 6px; }
        .metric-card { background:rgba(255,255,255,.84); border:1px solid rgba(210,210,215,.82); border-radius:20px; padding:18px 20px; min-height:105px; box-shadow:0 8px 22px rgba(0,0,0,.038); }
        .metric-label { color:var(--muted); font-size:.78rem; font-weight:700; letter-spacing:.03em; }
        .metric-value { color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',sans-serif; font-size:1.7rem; margin-top:8px; font-weight:700; letter-spacing:-.035em; }
        .metric-note { color:var(--muted); font-size:.75rem; margin-top:4px; }
        .callout { border-left:4px solid var(--orange); background:#fff4df; color:#7a4d00; padding:13px 16px; border-radius:0 14px 14px 0; margin:12px 0; }
        .callout-ok { border-left-color:var(--green); background:#eaf8ee; color:#17652d; }
        .small-note { color:var(--muted); font-size:.8rem; }
        [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px; overflow:hidden; }
        button[kind="primary"] { background:var(--blue); border-color:var(--blue); border-radius:999px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_research_cockpit_theme(st: Any) -> None:
    """为自主研究运行台叠加深蓝黑审批主题。"""

    st.markdown(
        """
        <style>
        :root { --cockpit:#07101d; --cockpit-card:#0d1929; --cockpit-line:#1d3048;
                --cockpit-text:#e8f1fb; --cockpit-muted:#8ca2ba; --cyan:#43d7c5;
                --reject:#cf6f78; --info:#67a7ff; }
        .stApp { background:radial-gradient(circle at 82% -10%,#12304b 0,#07101d 38%,#050b14 100%);
                 color:var(--cockpit-text); }
        [data-testid="stHeader"] { background:rgba(7,16,29,.82); }
        [data-testid="stSidebar"] { background:#07101d; border-right:1px solid var(--cockpit-line); }
        [data-testid="stSidebar"] * { color:var(--cockpit-text)!important; }
        h1,h2,h3,h4,p,label,[data-testid="stMarkdownContainer"] { color:var(--cockpit-text); }
        .research-hero { border:1px solid var(--cockpit-line); border-radius:24px; padding:28px 32px;
                         background:linear-gradient(135deg,rgba(13,25,41,.96),rgba(12,39,54,.82));
                         box-shadow:0 20px 55px rgba(0,0,0,.26); margin-bottom:20px; }
        .research-kicker { color:var(--cyan); font-size:.72rem; font-weight:750;
                           letter-spacing:.15em; text-transform:uppercase; }
        .research-title { font-size:2.35rem; font-weight:720; letter-spacing:-.045em; margin:10px 0; }
        .research-copy { color:var(--cockpit-muted); max-width:880px; line-height:1.65; }
        .status-pill { display:inline-flex; gap:7px; align-items:center; padding:7px 11px;
                       border-radius:999px; border:1px solid var(--cockpit-line);
                       background:#0b1726; color:var(--cockpit-text); margin:4px 6px 4px 0; font-size:.78rem; }
        .status-dot { width:7px; height:7px; border-radius:50%; background:var(--cockpit-muted); }
        .status-dot.online { background:var(--cyan); box-shadow:0 0 12px rgba(67,215,197,.8); }
        .hypothesis-detail { background:rgba(13,25,41,.88); border:1px solid var(--cockpit-line);
                             border-radius:18px; padding:20px 22px; margin:8px 0 14px; }
        .hypothesis-detail h4 { color:var(--cyan); margin:.2rem 0 .55rem; font-size:.82rem;
                                text-transform:uppercase; letter-spacing:.08em; }
        .hypothesis-detail p { line-height:1.68; color:#dbe8f5; }
        [data-testid="stMetric"], [data-testid="stRadio"] { background:rgba(13,25,41,.68);
            border:1px solid var(--cockpit-line); border-radius:16px; padding:12px; }
        button[kind="primary"] { background:#147d75; border-color:#43d7c5; }
        button[kind="secondary"] { background:#17263a; border-color:#314761; color:#e8f1fb; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def research_status_header(
    st: Any,
    *,
    run_id: str,
    stage_label: str,
    decision_count: int,
) -> None:
    """渲染运行台顶部状态，避免把阶段状态误作研究结论。"""

    st.markdown(
        f"""
        <section class="research-hero">
          <div class="research-kicker">AUTONOMOUS RESEARCH / CONTROL DESK</div>
          <div class="research-title">自主因子研究运行台</div>
          <div class="research-copy">先完整审批十条事前假设，再冻结候选族并进入统一的可见评价。所有候选共用同一套回测协议；机制验证方案尚未执行，也不参与候选通过判定。开发区间通过不代表有效 Alpha 或可实盘。</div>
          <div style="margin-top:16px">
            <span class="status-pill">批次 {run_id}</span>
            <span class="status-pill">阶段 {stage_label}</span>
            <span class="status-pill">审批 {decision_count}/10</span>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def hero(st: Any, *, kicker: str, title: str, copy: str, meta: str = "") -> None:
    """渲染页面顶部的研究语境区。"""

    st.markdown(
        f"""
        <section class="hero">
          <div class="hero-kicker">{kicker}</div>
          <div class="hero-title">{title}</div>
          <div class="hero-copy">{copy}</div>
          {f'<div class="hero-meta">{meta}</div>' if meta else ''}
        </section>
        """,
        unsafe_allow_html=True,
    )


def section(st: Any, label: str, title: str | None = None) -> None:
    """渲染统一的小节标题。"""

    st.markdown(f'<div class="section-kicker">{label}</div>', unsafe_allow_html=True)
    if title:
        st.subheader(title)


def metric_card(st: Any, label: str, value: str, note: str = "") -> None:
    """渲染可读指标卡。"""

    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div>
          {f'<div class="metric-note">{note}</div>' if note else ''}
        </div>
        """,
        unsafe_allow_html=True,
    )


def pct(value: Any, digits: int = 2) -> str:
    """将小数收益或相关系数格式化为百分比。"""

    if not isinstance(value, (int, float)):
        return "—"
    return f"{value * 100:.{digits}f}%"


def number(value: Any, digits: int = 2) -> str:
    """将数值格式化为短数字。"""

    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.{digits}f}"


def candidate_payload(value: Any) -> dict[str, Any]:
    """读取候选维度；兼容正式产物的 version/candidates 包装。"""

    payload = value if isinstance(value, dict) else {}
    candidates = payload.get("candidates")
    return candidates if isinstance(candidates, dict) else payload


def candidate_ids(snapshot: Any) -> list[str]:
    """按稳定顺序提取本次运行中的候选。"""

    definitions = candidate_payload(getattr(snapshot, "candidate_definitions", None))
    if definitions:
        aliases = getattr(snapshot, "candidate_aliases", {})
        return sorted(
            str(aliases.get(item, candidate_reference_id(str(item))))
            for item in definitions
        )

    pools: list[Iterable[str]] = []
    for value in (
        snapshot.candidate_metrics,
        snapshot.ic_diagnostics,
        snapshot.portfolio_metrics,
        snapshot.portfolio_daily,
        snapshot.barra_attribution,
    ):
        if isinstance(value, dict):
            pools.append(
                candidate_reference_id(str(item))
                for item in candidate_payload(value).keys()
            )
    return sorted({item for pool in pools for item in pool})


def candidate_source_id(snapshot: Any, candidate_id: str) -> str:
    """将 Dashboard 业务引用编号还原为快照中的原始候选键。"""

    aliases = getattr(snapshot, "candidate_aliases", {})
    if isinstance(aliases, dict):
        for source_id, reference_id in aliases.items():
            if reference_id == candidate_id:
                return str(source_id)
    if candidate_id.startswith("huan") and candidate_id[4:].isdigit():
        return f"pilot_fixed_{int(candidate_id[4:]):03d}"
    return candidate_id


def candidate_name(candidate_id: str) -> str:
    """把内部候选编号转换为更适合界面的名称。"""

    if candidate_id.startswith("huan") and candidate_id[4:].isdigit():
        return candidate_id
    suffix = candidate_id.rsplit("_", 1)[-1]
    return f"候选 {suffix}" if suffix.isdigit() else candidate_id


def date_text(value: Any) -> str:
    """格式化 JSON 日期。"""

    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else "—"


def ic_values(snapshot: Any, candidate_id: str) -> dict[str, Any]:
    """读取一个候选的 IC 诊断字典。"""

    values = candidate_payload(snapshot.ic_diagnostics)
    source_id = candidate_source_id(snapshot, candidate_id)
    value = values.get(source_id, {}) if isinstance(values, dict) else {}
    return value if isinstance(value, dict) else {}


def portfolio_values(snapshot: Any, candidate_id: str) -> dict[str, Any]:
    """读取一个候选的组合指标字典。"""

    values = candidate_payload(snapshot.portfolio_metrics)
    source_id = candidate_source_id(snapshot, candidate_id)
    value = values.get(source_id, {}) if isinstance(values, dict) else {}
    return value if isinstance(value, dict) else {}


def recent_portfolio_values(snapshot: Any, candidate_id: str) -> dict[str, Any]:
    """读取 2025 年以来的近期展示指标。"""

    values = candidate_payload(getattr(snapshot, "recent_portfolio_metrics", None))
    source_id = candidate_source_id(snapshot, candidate_id)
    value = values.get(source_id, {}) if isinstance(values, dict) else {}
    return value if isinstance(value, dict) else {}


def direction_values(snapshot: Any, candidate_id: str) -> dict[str, Any]:
    """读取发现期冻结方向及其与事前假设的关系。"""

    values = candidate_payload(getattr(snapshot, "direction_decisions", None))
    source_id = candidate_source_id(snapshot, candidate_id)
    value = values.get(source_id, {}) if isinstance(values, dict) else {}
    decision = value.get("decision", value) if isinstance(value, dict) else {}
    return decision if isinstance(decision, dict) else {}


def daily_rows(snapshot: Any, candidate_id: str) -> list[dict[str, Any]]:
    """读取并按退出日期排序的组合日收益。"""

    values = candidate_payload(snapshot.portfolio_daily)
    source_id = candidate_source_id(snapshot, candidate_id)
    rows = values.get(source_id, []) if isinstance(values, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("daily", [])
    if not isinstance(rows, list):
        return []
    clean = [row for row in rows if isinstance(row, dict)]
    return sorted(clean, key=lambda row: date_text(row.get("exit_date")))


def make_ic_decay_figure(snapshot: Any, candidate_id: str) -> Any:
    """生成多期限 IC 衰减图。"""

    import plotly.graph_objects as go  # type: ignore[import-not-found]

    decay = ic_values(snapshot, candidate_id).get("decay", [])
    rows = [row for row in decay if isinstance(row, dict)] if isinstance(decay, list) else []
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=[row.get("horizon") for row in rows],
        y=[row.get("ic_mean") for row in rows],
        mode="lines+markers", name="IC", line={"color": ACCENT, "width": 3},
        marker={"size": 8},
    ))
    figure.add_trace(go.Scatter(
        x=[row.get("horizon") for row in rows],
        y=[row.get("rank_ic_mean") for row in rows],
        mode="lines+markers", name="RankIC", line={"color": POSITIVE, "width": 3},
        marker={"size": 8},
    ))
    return _style_figure(figure, "标签期限（交易日）", "平均相关系数")


def make_ic_sequence_figure(snapshot: Any, candidate_id: str) -> Any:
    """生成主期限每日 IC 序列图。"""

    import plotly.graph_objects as go  # type: ignore[import-not-found]

    diagnostics = ic_values(snapshot, candidate_id)
    daily = diagnostics.get("daily", [])
    rows = [row for row in daily if isinstance(row, dict)] if isinstance(daily, list) else []
    figure = go.Figure()
    figure.add_hline(y=0, line_color=GRID, line_width=1)
    figure.add_trace(go.Scatter(
        x=[date_text(row.get("date")) for row in rows],
        y=[row.get("ic") for row in rows],
        mode="lines", name="IC", line={"color": ACCENT, "width": 2},
    ))
    figure.add_trace(go.Scatter(
        x=[date_text(row.get("date")) for row in rows],
        y=[row.get("rank_ic") for row in rows],
        mode="lines", name="RankIC", line={"color": POSITIVE, "width": 2},
    ))
    return _style_figure(figure, "日期", "相关系数")


def make_wealth_figure(snapshot: Any, candidate_id: str) -> Any:
    """生成目标多头与基准的累计收益图。"""

    import plotly.graph_objects as go  # type: ignore[import-not-found]

    rows = daily_rows(snapshot, candidate_id)
    columns = [
        ("target_long_net_return", "目标多头（净）", ACCENT),
        ("target_long_gross_return", "目标多头（毛）", POSITIVE),
        ("benchmark_return", "沪深300", MUTED),
    ]
    figure = go.Figure()
    for column, label, color in columns:
        values = [row.get(column) for row in rows]
        if not values or not all(isinstance(value, (int, float)) for value in values):
            continue
        wealth = 1.0
        path: list[float] = []
        for value in values:
            wealth *= 1.0 + value
            path.append((wealth - 1.0) * 100)
        figure.add_trace(go.Scatter(
            x=[date_text(row.get("exit_date")) for row in rows], y=path,
            mode="lines", name=label, line={"color": color, "width": 3 if "净" in label else 1.5},
        ))
    return _style_figure(figure, "日期", "累计收益（%）")


def make_drawdown_figure(snapshot: Any, candidate_id: str) -> Any:
    """生成目标多头净值相对历史高点的回撤曲线。"""

    import plotly.graph_objects as go  # type: ignore[import-not-found]

    rows = daily_rows(snapshot, candidate_id)
    column = "target_long_net_return"
    values = [row.get(column) for row in rows]
    figure = go.Figure()
    if values and all(isinstance(value, (int, float)) for value in values):
        wealth = 1.0
        peak = 1.0
        drawdowns: list[float] = []
        for value in values:
            wealth *= 1.0 + value
            peak = max(peak, wealth)
            drawdowns.append((wealth / peak - 1.0) * 100)
        figure.add_trace(go.Scatter(
            x=[date_text(row.get("exit_date")) for row in rows],
            y=drawdowns,
            mode="lines",
            name="目标多头回撤",
            line={"color": "#b6463c", "width": 2.5},
            fill="tozeroy",
        ))
    return _style_figure(figure, "日期", "回撤（%）")


def _style_figure(figure: Any, x_title: str, y_title: str) -> Any:
    """统一 Plotly 图表的研究报告样式。"""

    figure.update_layout(
        template="simple_white",
        height=350,
        margin={"l": 12, "r": 12, "t": 24, "b": 12},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#fffdfa",
        font={"family": "Avenir Next, Noto Sans SC, sans-serif", "color": PRIMARY, "size": 12},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        hovermode="x unified",
    )
    figure.update_xaxes(title=x_title, showgrid=False, linecolor=GRID, tickangle=-30)
    figure.update_yaxes(title=y_title, gridcolor="#e7ecec", zerolinecolor=GRID)
    return figure
