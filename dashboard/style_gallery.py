"""Dashboard 六套候选视觉风格的隔离预览。"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True, slots=True)
class ThemePreview:
    """一套只用于选型、不进入正式研究页面的视觉主题。"""

    theme_id: str
    label: str
    tagline: str
    description: str
    background: str
    surface: str
    ink: str
    muted: str
    accent: str
    positive: str
    border: str
    radius: str
    display_font: str
    body_font: str


THEMES: tuple[ThemePreview, ...] = (
    ThemePreview(
        "apple",
        "Apple",
        "克制、通透、产品化",
        "大留白、柔和卡片与系统字体，最接近当前公共主题。",
        "#f5f5f7", "#ffffff", "#1d1d1f", "#6e6e73", "#0066cc", "#248a3d",
        "#d2d2d7", "22px", "-apple-system,BlinkMacSystemFont,sans-serif", "-apple-system,BlinkMacSystemFont,sans-serif",
    ),
    ThemePreview(
        "ferrari",
        "Ferrari",
        "速度、机械、强对比",
        "黑白红、直角分区与大写标题，视觉张力最强。",
        "#181818", "#232323", "#f5f5f2", "#a8a8a4", "#da291c", "#ffffff",
        "#424242", "0px", "Arial Narrow,Arial,sans-serif", "Arial,Helvetica,sans-serif",
    ),
    ThemePreview(
        "claude",
        "Claude",
        "温和、编辑感、研究气质",
        "奶油底、珊瑚色和衬线标题，适合假设与解释文本较多的页面。",
        "#faf9f5", "#f1efe8", "#141413", "#6b6963", "#cc785c", "#2f6f5e",
        "#d8d4ca", "14px", "Georgia,Times New Roman,serif", "Arial,Helvetica,sans-serif",
    ),
    ThemePreview(
        "spacex",
        "SpaceX",
        "冷峻、任务控制、极简",
        "纯黑白、细线与大写字，像任务状态终端。",
        "#000000", "#080808", "#ffffff", "#9a9a9a", "#ffffff", "#ffffff",
        "#333333", "0px", "Arial Narrow,Arial,sans-serif", "Arial,Helvetica,sans-serif",
    ),
    ThemePreview(
        "mastercard",
        "MasterCard",
        "圆润、数据叙事、品牌感",
        "暖灰底、黑色胶囊与橙色轨道，亲和而有识别度。",
        "#f3f0ee", "#ffffff", "#141413", "#696661", "#f37338", "#2d715e",
        "#d9d5d1", "32px", "Arial Black,Arial,sans-serif", "Arial,Helvetica,sans-serif",
    ),
    ThemePreview(
        "binance",
        "Binance",
        "高密度、交易终端、效率",
        "深色金融终端、黄色操作色与紧凑数字，信息密度最高。",
        "#0b0e11", "#181a20", "#eaecef", "#848e9c", "#fcd535", "#0ecb81",
        "#2b3139", "8px", "Arial,Helvetica,sans-serif", "Arial,Helvetica,sans-serif",
    ),
)


def theme_by_id(theme_id: str) -> ThemePreview:
    """按稳定 ID 返回主题，未知值显式失败。"""

    for theme in THEMES:
        if theme.theme_id == theme_id:
            return theme
    raise ValueError(f"未知 Dashboard 主题：{theme_id}")


def preview_html(theme_id: str) -> str:
    """生成隔离 iframe 中的完整样例页，不加载外部字体或脚本。"""

    theme = theme_by_id(theme_id)
    css_values = {
        "background": theme.background,
        "surface": theme.surface,
        "ink": theme.ink,
        "muted": theme.muted,
        "accent": theme.accent,
        "positive": theme.positive,
        "border": theme.border,
        "radius": theme.radius,
        "display_font": theme.display_font,
        "body_font": theme.body_font,
    }
    variables = ";".join(f"--{key}:{value}" for key, value in css_values.items())
    label = escape(theme.label)
    tagline = escape(theme.tagline)
    description = escape(theme.description)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}} body{{margin:0;background:var(--background);color:var(--ink);font-family:var(--body_font)}}
.shell{{min-height:820px;padding:28px 34px;background:var(--background)}}
.top{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:18px}}
.brand{{font-family:var(--display_font);font-size:18px;font-weight:800;letter-spacing:-.02em}} .nav{{display:flex;gap:22px;color:var(--muted);font-size:12px}}
.hero{{display:grid;grid-template-columns:1.7fr .8fr;gap:28px;align-items:end;padding:44px 0 28px}}
.eyebrow{{color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.15em;text-transform:uppercase}}
h1{{font-family:var(--display_font);font-size:50px;line-height:1.02;letter-spacing:-.055em;margin:12px 0 13px;max-width:760px}}
.lead{{color:var(--muted);font-size:14px;line-height:1.6;max-width:680px}} .status{{justify-self:end;border:1px solid var(--border);border-radius:999px;padding:10px 15px;font-size:12px}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--positive);margin-right:7px}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}} .card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;min-height:110px}}
.label{{color:var(--muted);font-size:11px;font-weight:700}} .value{{font-family:var(--display_font);font-size:29px;font-weight:800;margin-top:14px;letter-spacing:-.035em}} .note{{color:var(--muted);font-size:10px;margin-top:5px}}
.main{{display:grid;grid-template-columns:1.65fr .75fr;gap:12px;margin-top:12px}} .panel{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}}
.panel-title{{display:flex;justify-content:space-between;align-items:center;font-weight:800;font-size:13px;margin-bottom:18px}} .chip{{color:var(--accent);border:1px solid var(--border);border-radius:999px;padding:6px 9px;font-size:10px}}
svg{{width:100%;height:210px}} .grid{{stroke:var(--border);stroke-width:1}} .line{{fill:none;stroke:var(--accent);stroke-width:3}} .area{{fill:var(--accent);opacity:.08}}
.row{{display:grid;grid-template-columns:1fr auto;gap:12px;padding:13px 0;border-bottom:1px solid var(--border);font-size:12px}} .row:last-child{{border:0}} .factor{{font-weight:750}} .small{{color:var(--muted);font-size:10px;margin-top:4px}} .rank{{color:var(--positive);font-family:monospace;font-weight:800}}
.controls{{display:flex;gap:8px;margin-top:12px}} .control{{flex:1;border:1px solid var(--border);border-radius:var(--radius);padding:12px;color:var(--muted);font-size:11px}} .control b{{display:block;color:var(--ink);margin-top:5px;font-size:13px}}
.ferrari .shell,.spacex .shell{{text-transform:uppercase}} .ferrari h1,.spacex h1{{letter-spacing:.01em}} .ferrari .card{{border-top:3px solid var(--accent)}}
.claude h1{{font-weight:500}} .claude .hero{{border-bottom:1px solid var(--border)}}
.mastercard .shell{{position:relative;overflow:hidden}} .mastercard .shell:after{{content:'';position:absolute;width:360px;height:360px;border:34px solid var(--accent);border-radius:50%;right:-190px;top:-160px;opacity:.16;pointer-events:none}}
.binance .shell{{padding:20px 24px}} .binance .hero{{padding:26px 0 20px}} .binance h1{{font-size:38px}} .binance .card,.binance .panel{{border-radius:8px}} .binance .value{{font-family:monospace;color:var(--positive)}}
@media(max-width:760px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.main,.hero{{grid-template-columns:1fr}}.status{{justify-self:start}}}}
</style></head><body style="{variables}"><div class="{theme.theme_id}"><main class="shell">
<header class="top"><div class="brand">FACTOR MINER</div><nav class="nav"><span>因子探索</span><span>研究运行</span><span>回测</span><span>审计</span></nav></header>
<section class="hero"><div><div class="eyebrow">{label} / STYLE STUDY</div><h1>{tagline}</h1><div class="lead">{description} 同一组研究内容、指标和控件，仅改变视觉语言，方便直接比较。</div></div><div class="status"><span class="dot"></span>本地演示运行正常</div></section>
<section class="metrics">
<article class="card"><div class="label">IC 均值</div><div class="value">0.0218</div><div class="note">原始小数</div></article>
<article class="card"><div class="label">RankIC 均值</div><div class="value">0.0264</div><div class="note">原始小数</div></article>
<article class="card"><div class="label">RankIC HAC t</div><div class="value">3.84</div><div class="note">冻结 maxlags=5</div></article>
<article class="card"><div class="label">Calmar</div><div class="value">1.62</div><div class="note">年化收益 / 最大回撤</div></article>
</section>
<section class="main"><article class="panel"><div class="panel-title"><span>单因子净值路径</span><span class="chip">演示因子 / 5 日</span></div>
<svg viewBox="0 0 720 210" preserveAspectRatio="none"><path class="grid" d="M0 42H720M0 84H720M0 126H720M0 168H720"/><path class="area" d="M0 176 L45 170 L90 158 L135 163 L180 139 L225 146 L270 119 L315 126 L360 91 L405 106 L450 75 L495 82 L540 52 L585 65 L630 32 L675 41 L720 17 L720 210 L0 210Z"/><path class="line" d="M0 176 L45 170 L90 158 L135 163 L180 139 L225 146 L270 119 L315 126 L360 91 L405 106 L450 75 L495 82 L540 52 L585 65 L630 32 L675 41 L720 17"/></svg>
<div class="controls"><div class="control">交易成本<b>14 bps</b></div><div class="control">额外滑点<b>0 bps</b></div><div class="control">回测区间<b>2023—2024</b></div></div></article>
<aside class="panel"><div class="panel-title"><span>候选复核</span><span class="chip">3 个</span></div>
<div class="row"><div><div class="factor">huan020</div><div class="small">成交量冲击后的短期反转</div></div><div class="rank">0.0566</div></div>
<div class="row"><div><div class="factor">huan286</div><div class="small">信息扩散与趋势延续</div></div><div class="rank">0.0564</div></div>
<div class="row"><div><div class="factor">demo_momentum_20d</div><div class="small">单因子回测演示</div></div><div class="rank">0.0264</div></div>
</aside></section>
</main></div></body></html>"""
