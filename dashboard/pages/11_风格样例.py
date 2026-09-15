"""六套 Dashboard 视觉风格的交互样例页。"""

import streamlit as st
import streamlit.components.v1 as components

from dashboard.style_gallery import THEMES, preview_html
from dashboard.ui import apply_theme, hero


apply_theme(st, page_title="Factor Miner｜风格样例")
hero(
    st,
    kicker="DESIGN SYSTEM / ISOLATED PREVIEW",
    title="先看同一张页面，再决定整套系统的气质。",
    copy="六套样例使用完全相同的研究内容与指标，只替换颜色、字体、密度、圆角和层级。样例运行在隔离 iframe 中，不会污染 Dashboard 当前公共主题。",
    meta="Apple · Ferrari · Claude · SpaceX · MasterCard · Binance",
)

labels = [theme.label for theme in THEMES]
selected_label = st.segmented_control("选择风格", labels, default=labels[0], selection_mode="single")
selected = next(theme for theme in THEMES if theme.label == selected_label)
st.caption(f"{selected.label}：{selected.description}")
components.html(preview_html(selected.theme_id), height=860, scrolling=True)
st.info("这里只做视觉选型，不改变研究指标、统计协议或数据合同。确定主题后，再把选中的 tokens 收敛进唯一公共主题。")
