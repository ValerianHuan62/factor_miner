"""美股本地数据模块。"""

import streamlit as st

from dashboard.market_page import render_market_page


render_market_page(st, "us_equity")
