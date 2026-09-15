"""Dashboard 可视化组件与研究指标格式化辅助。"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from factor_miner.dashboard_projection import candidate_reference_id


PRIMARY = "#e8f0e9"
ACCENT = "#c7f36b"
POSITIVE = "#70d7c4"
MUTED = "#91a096"
GRID = "#2b3931"


def apply_theme(st: Any, *, page_title: str) -> None:
    """应用统一的研究驾驶舱视觉主题。"""

    st.set_page_config(page_title=page_title, page_icon="◈", layout="wide")
    st.markdown(
        """
        <style>
        :root { --ink:#e8f0e9; --muted:#91a096; --paper:#0d1210; --panel:#151c18;
                --panel-2:#111814; --line:#2b3931; --lime:#c7f36b; --cyan:#70d7c4;
                --orange:#ff9f68; --danger:#ff8e72; }
        .stApp { color:var(--ink); background-color:var(--paper);
          background-image:linear-gradient(rgba(199,243,107,.025) 1px,transparent 1px),
                           linear-gradient(90deg,rgba(199,243,107,.025) 1px,transparent 1px),
                           radial-gradient(circle at 82% -10%,rgba(112,215,196,.10),transparent 34%);
          background-size:32px 32px,32px 32px,100% 100%; }
        [data-testid="stHeader"] { background:rgba(13,18,16,.86); backdrop-filter:blur(16px); }
        [data-testid="stSidebar"] { background:#101613; border-right:1px solid var(--line); }
        [data-testid="stSidebar"] * { color:var(--ink)!important; }
        [data-testid="stSidebarNav"] span { font-weight:600; letter-spacing:-.01em; }
        h1,h2,h3,h4 { color:var(--ink); letter-spacing:-.035em;
          font-family:'Iowan Old Style','Songti SC','STSong',serif; }
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li { color:var(--ink); }
        h1 { font-size:2.55rem; font-weight:700; }
        h2 { font-size:1.35rem; margin-top:1.2rem; }
        h3 { font-size:1.05rem; }
        .hero { background:linear-gradient(135deg,#172019,#0f1713 72%);
                border:1px solid var(--line); border-radius:3px; padding:38px 42px 34px;
                color:var(--ink); margin:4px 0 24px; box-shadow:12px 12px 0 rgba(199,243,107,.055);
                position:relative; overflow:hidden; }
        .hero:after { content:''; position:absolute; width:330px; height:330px; right:-120px; top:-175px;
                      border:1px solid rgba(199,243,107,.18); border-radius:50%;
                      box-shadow:0 0 0 34px rgba(199,243,107,.035),0 0 0 70px rgba(112,215,196,.022); }
        .hero-kicker { color:var(--lime); font:700 .72rem 'SFMono-Regular','Menlo',monospace;
                       text-transform:uppercase; letter-spacing:.14em; }
        .hero-title { color:var(--ink); font:700 3rem/1.05 'Iowan Old Style','Songti SC','STSong',serif;
                      letter-spacing:-.055em; margin:13px 0 10px; }
        .hero-copy { color:var(--muted); max-width:780px; font-size:1.02rem; line-height:1.55; }
        .hero-meta { color:var(--muted); font-size:.82rem; margin-top:20px; }
        .section-kicker { color:var(--lime); font:700 .7rem 'SFMono-Regular','Menlo',monospace;
                          text-transform:uppercase; letter-spacing:.13em; margin:26px 0 6px; }
        .metric-card { background:rgba(21,28,24,.94); border:1px solid var(--line); border-radius:3px;
                       padding:18px 20px; min-height:105px; box-shadow:none; }
        .metric-label { color:var(--muted); font-size:.78rem; font-weight:700; letter-spacing:.03em; }
        .metric-value { color:var(--lime); font:700 1.7rem 'SFMono-Regular','Menlo',monospace;
                        margin-top:8px; letter-spacing:-.035em; }
        .metric-note { color:var(--muted); font-size:.75rem; margin-top:4px; }
        .callout { border-left:4px solid var(--orange); background:#211b16; color:#ffd7b8;
                   padding:13px 16px; border-radius:0 3px 3px 0; margin:12px 0; }
        .callout-ok { border-left-color:var(--cyan); background:#10221d; color:#b9f3e7; }
        .small-note { color:var(--muted); font-size:.8rem; }
        [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:2px; overflow:hidden; }
        [data-testid="stMetric"] { background:var(--panel); border:1px solid var(--line); border-radius:3px; padding:12px; container-type:inline-size; }
        [data-testid="stMetric"] label { color:var(--muted)!important; }
        [data-testid="stMetricValue"] { color:var(--lime)!important; font-family:'SFMono-Regular','Menlo',monospace;
            font-size:clamp(.85rem,14cqw,1.5rem)!important; font-variant-numeric:tabular-nums; }
        [data-testid="stAlert"] { background:var(--panel)!important; border:1px solid var(--line); color:var(--ink)!important; }
        [data-testid="stAlert"] * { color:var(--ink)!important; }
        [data-baseweb="select"] > div,[data-baseweb="input"] > div,
        [data-testid="stDateInput"] > div > div { background:var(--panel)!important; border-color:var(--line)!important; color:var(--ink)!important; }
        input,textarea { color:var(--ink)!important; caret-color:var(--lime)!important; }
        [data-baseweb="popover"],[role="listbox"] { background:var(--panel)!important; color:var(--ink)!important; }
        [data-baseweb="tab-list"] { border-bottom:1px solid var(--line); }
        [data-baseweb="tab"] { color:var(--muted)!important; }
        [aria-selected="true"][data-baseweb="tab"] { color:var(--lime)!important; }
        button[kind="primary"] { background:var(--lime); color:#111710; border-color:var(--lime); border-radius:3px; }
        button[kind="secondary"] { background:var(--panel); color:var(--ink); border-color:var(--line); }
        .research-hero { background:linear-gradient(135deg,#172019,#0f1713 72%); border:1px solid var(--line);
                         border-radius:3px; padding:30px 34px; box-shadow:12px 12px 0 rgba(199,243,107,.055); margin-bottom:20px; }
        .research-kicker { color:var(--lime); font-size:.72rem; font-weight:700;
                           letter-spacing:.14em; text-transform:uppercase; }
        .research-title { color:var(--ink); font-size:2.35rem; font-weight:700;
                          letter-spacing:-.045em; margin:10px 0; }
        .research-copy { color:var(--muted); max-width:880px; line-height:1.65; }
        .status-pill { display:inline-flex; gap:7px; align-items:center; padding:7px 11px;
                       border-radius:999px; border:1px solid var(--line); background:var(--panel);
                       color:var(--ink); margin:4px 6px 4px 0; font-size:.78rem; }
        .status-dot { width:7px; height:7px; border-radius:50%; background:var(--muted); }
        .status-dot.online { background:var(--cyan); }
        .hypothesis-detail { background:var(--panel); border:1px solid var(--line);
                             border-radius:3px; padding:20px 22px; margin:8px 0 14px; }
        .hypothesis-detail h4 { color:var(--lime); margin:.2rem 0 .55rem; font-size:.82rem;
                                text-transform:uppercase; letter-spacing:.08em; }
        .stApp { background-image:none; }
        .block-container { max-width:1400px; padding-top:2rem; }
        .hero { padding:22px 26px; box-shadow:none; }
        .hero:after { display:none; }
        .hero-title { font-size:2rem; }
        .research-hero { box-shadow:none; padding:20px; }
        [data-testid="stTextArea"] textarea { background:var(--panel)!important; }
        [data-testid="stWidgetLabel"] p, [data-testid="stSelectbox"] { color:var(--ink); }
        button[kind="primary"] p { color:#111710!important; }
        @media (max-width:700px) { .block-container { padding:1rem; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_factor_atlas_theme(st: Any) -> None:
    """为因子探索页叠加纸质研究图鉴风格。"""

    st.markdown(
        """
        <style>
        :root { --atlas-paper:#eee9dd; --atlas-card:#fffdf7; --atlas-ink:#17211d;
                --atlas-muted:#687169; --atlas-line:#cfc8b8; --atlas-red:#b63b2e;
                --atlas-green:#1e6753; }
        .stApp {
          background-color:var(--atlas-paper);
          background-image:linear-gradient(rgba(23,33,29,.035) 1px,transparent 1px),
                           linear-gradient(90deg,rgba(23,33,29,.035) 1px,transparent 1px);
          background-size:24px 24px;
          color:var(--atlas-ink);
        }
        [data-testid="stHeader"] { background:rgba(238,233,221,.84); backdrop-filter:blur(16px); }
        [data-testid="stSidebar"] { background:#e4ded0; border-right:1px solid var(--atlas-line); }
        [data-testid="stSidebar"] * { color:var(--atlas-ink)!important; }
        h1,h2,h3 { font-family:'Iowan Old Style','Songti SC','STSong',serif; color:var(--atlas-ink); }
        .atlas-hero { background:var(--atlas-card); border:1px solid var(--atlas-line);
                      border-radius:4px 24px 4px 24px; padding:34px 38px; margin:4px 0 22px;
                      box-shadow:9px 9px 0 rgba(23,33,29,.08); position:relative; overflow:hidden; }
        .atlas-hero:before { content:'FACTOR / ATLAS'; position:absolute; right:-20px; top:24px;
                             transform:rotate(8deg); color:rgba(182,59,46,.12); font:700 3.8rem 'Iowan Old Style',serif; }
        .atlas-kicker { color:var(--atlas-red); font-size:.72rem; letter-spacing:.18em; font-weight:800; }
        .atlas-title { font:700 3.15rem/1.05 'Iowan Old Style','Songti SC','STSong',serif;
                       letter-spacing:-.045em; margin:12px 0 10px; max-width:760px; }
        .atlas-copy { color:#465049; max-width:820px; line-height:1.68; }
        .atlas-rule { width:64px; height:4px; background:var(--atlas-red); margin-top:20px; }
        .metric-card { border-radius:4px 16px 4px 16px; background:rgba(255,253,247,.92);
                       border-color:var(--atlas-line); box-shadow:5px 5px 0 rgba(23,33,29,.06); }
        .section-kicker { color:var(--atlas-red); }
        [data-testid="stDataFrame"] { border-color:var(--atlas-line); background:var(--atlas-card); }
        code { color:var(--atlas-green)!important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def factor_atlas_hero(st: Any, *, factor_count: int) -> None:
    """渲染因子图鉴入口，明确聚合指标的研究边界。"""

    st.markdown(
        f"""
        <section class="atlas-hero">
          <div class="atlas-kicker">RESEARCH CATALOG / READ-ONLY</div>
          <div class="atlas-title">因子不是奖杯，是待复核的研究标本。</div>
          <div class="atlas-copy">搜索公式与金融假设，按方向和统计证据筛选，再把候选并排比较。这里展示 {factor_count} 个已投影候选的聚合指标；筛选、排序和下载都不会重算结果，也不会改变候选状态。</div>
          <div class="atlas-rule"></div>
        </section>
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


def apply_backtest_lab_theme(st: Any) -> None:
    """为交互回测页叠加工业化行情终端风格。"""

    st.markdown(
        """
        <style>
        :root { --lab-bg:#0d1210; --lab-panel:#151c18; --lab-line:#2b3931;
                --lab-text:#e8f0e9; --lab-muted:#91a096; --lab-lime:#c7f36b;
                --lab-cyan:#70d7c4; }
        .stApp { background-color:var(--lab-bg); color:var(--lab-text);
          background-image:linear-gradient(rgba(199,243,107,.025) 1px,transparent 1px),
                           linear-gradient(90deg,rgba(199,243,107,.025) 1px,transparent 1px);
          background-size:32px 32px; }
        [data-testid="stHeader"] { background:rgba(13,18,16,.86); backdrop-filter:blur(14px); }
        [data-testid="stSidebar"] { background:#101613; border-right:1px solid var(--lab-line); }
        [data-testid="stSidebar"] * { color:var(--lab-text)!important; }
        h1,h2,h3,h4,p,label,[data-testid="stMarkdownContainer"] { color:var(--lab-text); }
        .hero { background:linear-gradient(135deg,#172019,#0f1713 72%); color:var(--lab-text);
          border:1px solid var(--lab-line); border-radius:3px;
          box-shadow:12px 12px 0 rgba(199,243,107,.055); }
        .hero:after { border-color:rgba(199,243,107,.18);
          box-shadow:0 0 0 34px rgba(199,243,107,.035),0 0 0 70px rgba(112,215,196,.022); }
        .hero-kicker,.section-kicker { color:var(--lab-lime); font-family:'SFMono-Regular','Menlo',monospace; }
        .hero-title { color:var(--lab-text); font-family:'Iowan Old Style','Songti SC',serif; }
        .hero-copy,.hero-meta { color:var(--lab-muted); }
        .metric-card,[data-testid="stMetric"] { background:rgba(21,28,24,.94);
          border:1px solid var(--lab-line); border-radius:3px; box-shadow:none; }
        .metric-label,.metric-note { color:var(--lab-muted); }
        .metric-value { color:var(--lab-lime); font-family:'SFMono-Regular','Menlo',monospace; }
        [data-testid="stDataFrame"] { border:1px solid var(--lab-line); border-radius:2px; }
        [data-baseweb="tab-list"] { border-bottom:1px solid var(--lab-line); }
        button[kind="primary"] { background:var(--lab-lime); color:#111710; border-color:var(--lab-lime); }
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

    st.subheader(stage_label)
    if decision_count:
        st.caption(f"已审阅 {decision_count}/10 条假设")


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


def render_no_published_run(st: Any, *, lens: str) -> None:
    """在市场尚无正式运行时保持页面可访问，并明确证据边界。"""

    hero(
        st,
        kicker=f"{lens} / WAITING FOR LOCAL RUN",
        title="当前市场还没有可展示的研究运行",
        copy=(
            "行情与状态表可以先在市场模块完成只读预检；IC、回测、归因和审计必须等待该市场完成标准面板发布与一次冻结研究。"
            "这里不会用另一市场结果代替，也不会把缺失指标填成零。"
        ),
        meta="数据配置是可选入口 · 研究产物按市场隔离 · PostgreSQL 仍是可重建读模型",
    )
    st.info("请先打开侧栏的 A 股或美股模块检查数据合同；生成正式运行后，本页会自动读取最新发布产物。")


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
        template="plotly_dark",
        height=350,
        margin={"l": 12, "r": 12, "t": 24, "b": 12},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#111814",
        font={"family": "SFMono-Regular, Menlo, monospace", "color": PRIMARY, "size": 12},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        hovermode="x unified",
    )
    figure.update_xaxes(title=x_title, showgrid=False, linecolor=GRID, tickangle=-30)
    figure.update_yaxes(title=y_title, gridcolor="#26342c", zerolinecolor=GRID)
    return figure
