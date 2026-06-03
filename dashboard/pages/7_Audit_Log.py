"""Audit Log — immutable record of all system events."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from app.database import get_db
from app.models.audit import AuditLog, AuditAction

st.set_page_config(page_title="Audit Log — VCPilot", layout="wide")
st.title("📜 Audit Log")

col1, col2, col3 = st.columns(3)
with col1:
    actions = ["ALL"] + [a.value for a in AuditAction]
    filter_action = st.selectbox("Action", actions)
with col2:
    filter_ticker = st.text_input("Ticker (optional)")
with col3:
    limit = st.slider("Show last N records", 25, 500, 100)

with get_db() as db:
    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if filter_action != "ALL":
        query = query.filter(AuditLog.action == filter_action)
    if filter_ticker:
        query = query.filter(AuditLog.ticker == filter_ticker.upper() + ".AX")
    logs = query.limit(limit).all()

rows = [{"Time": str(l.created_at)[:19], "Action": str(l.action).replace("AuditAction.", ""),
         "Actor": l.actor, "Ticker": l.ticker or "—",
         "Message": (l.message or "")[:80], "Before": l.before_value or "—",
         "After": l.after_value or "—"} for l in logs]
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
st.caption(f"Showing {len(rows)} records (append-only table — no rows ever modified or deleted)")
