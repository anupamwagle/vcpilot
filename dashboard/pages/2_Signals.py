"""Signals — today's and historical screener output."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from datetime import date
from app.database import get_db
from app.models.signal import Signal

st.set_page_config(page_title="Signals — AstraTrade", layout="wide")
st.title("📈 Signals")
screen_date = st.date_input("Date", value=date.today())
with get_db() as db:
    signals = db.query(Signal).filter(Signal.signal_date == screen_date).all()
if not signals:
    st.info(f"No signals for {screen_date}.")
else:
    for s in signals:
        with st.expander(f"**{s.ticker}** — {s.status} | RS {s.rs_rating:.0f} | Pivot ${s.pivot_price:.3f}"):
            c1, c2 = st.columns(2)
            c1.metric("Pivot", f"${s.pivot_price:.3f}")
            c1.metric("Stop", f"${s.stop_price:.3f}")
            c2.metric("Target 1", f"${s.target_price_1:.3f}" if s.target_price_1 else "—")
            c2.metric("Target 2", f"${s.target_price_2:.3f}" if s.target_price_2 else "—")
            st.json(s.rule_results or {})
