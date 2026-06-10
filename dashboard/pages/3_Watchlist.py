"""Watchlist — stocks in formation."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from app.database import get_db
from app.models.signal import Watchlist, WatchlistStatus

st.set_page_config(page_title="Watchlist — AstraTrade", layout="wide")
st.title("👀 Watchlist")
with get_db() as db:
    items = db.query(Watchlist).filter(Watchlist.status == WatchlistStatus.WATCHING).all()
if not items:
    st.info("Watchlist is empty.")
else:
    rows = [{"Ticker": w.ticker, "Added": str(w.added_date), "By": w.added_by, "Notes": w.notes or "—"} for w in items]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Add to Watchlist")
with st.form("add_watchlist"):
    ticker = st.text_input("ASX Code (e.g. BHP)").upper()
    notes  = st.text_area("Notes")
    if st.form_submit_button("Add"):
        if ticker:
            with get_db() as db:
                db.add(Watchlist(ticker=f"{ticker}.AX", notes=notes, added_by="dashboard"))
            st.success(f"{ticker}.AX added to watchlist!")
            st.rerun()
