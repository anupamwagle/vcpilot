"""Backtest — run Minervini strategy against historical data."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
st.set_page_config(page_title="Backtest — VCPilot", layout="wide")
st.title("🧪 Backtest")
st.info("Backtest engine (Vectorbt) — implementation coming in Phase 2.")
st.markdown("""
**Planned capabilities:**
- Run full Minervini screener + VCP detection on 3yr historical data
- Per-ticker and portfolio-level performance metrics
- Sharpe ratio, max drawdown, win rate, average R-multiple
- Visual equity curve and drawdown chart
""")
