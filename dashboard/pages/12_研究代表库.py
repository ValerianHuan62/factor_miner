"""已采纳研究代表库的独立查询入口。"""
import streamlit as st
from dashboard.market_profiles import load_market_profiles
from dashboard.favor_cost_page import render_cost_candidates

st.set_page_config(page_title='研究代表库', layout='wide')
profiles = [p for p in load_market_profiles() if p.joint_library_path]
if not profiles:
    st.info('尚未配置已采纳的研究代表库。')
else:
    selected = st.selectbox('代表库市场', profiles, format_func=lambda p:p.display_name)
    render_cost_candidates(st, selected)
