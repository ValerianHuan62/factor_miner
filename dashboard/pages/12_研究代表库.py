"""按全局市场展示已采纳研究库。"""
import streamlit as st
from dashboard.market_profiles import current_market_id, profile_by_id
from dashboard.favor_cost_page import render_cost_candidates
from dashboard.ui import apply_theme

apply_theme(st, page_title='Factor Miner｜研究代表库')
st.title('研究代表库')
profile=profile_by_id(current_market_id())
if profile is None or not profile.joint_library_path:
    st.info('当前市场尚未采纳研究代表库。可切换市场，或在研究完成后采纳代表。')
else:
    render_cost_candidates(st, profile)
