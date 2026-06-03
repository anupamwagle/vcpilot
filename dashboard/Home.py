"""
VCPilot Dashboard — Home Page
"""
import streamlit as st
from datetime import date, datetime

st.set_page_config(
    page_title="VCPilot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Simple password gate
# ---------------------------------------------------------------------------
import os
_pwd = os.getenv("DASHBOARD_PASSWORD", "changeme")
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 VCPilot")
    pwd = st.text_input("Password", type="password")
    if st.button("Login"):
        if pwd == _pwd:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()

# ---------------------------------------------------------------------------
# Authenticated — Main Dashboard
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, "/app")

from app.database import get_db
from app.models.trade import Position, Trade, TradeStatus
from app.models.signal import Signal, SignalStatus
from app.models.config import SystemConfig
from app.models.audit import AuditLog

st.title("📈 VCPilot Dashboard")
st.caption(f"Last updated: {datetime.now().strftime('%d %b %Y %H:%M:%S AEST')}")

# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------
with get_db() as db:
    trading_paused = False
    paused_cfg = db.query(SystemConfig).filter(SystemConfig.key == "trading_paused").first()
    if paused_cfg:
        trading_paused = paused_cfg.value.lower() == "true"

    regime_cfg = db.query(SystemConfig).filter(SystemConfig.key == "last_market_regime").first()
    regime = regime_cfg.value if regime_cfg else "UNKNOWN"

    heartbeat_cfg = db.query(SystemConfig).filter(SystemConfig.key == "last_heartbeat").first()
    heartbeat = heartbeat_cfg.value[:19] if heartbeat_cfg else "Never"

    open_positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).count()
    today_signals  = db.query(Signal).filter(Signal.signal_date == date.today()).count()

    all_trades = db.query(Trade).all()
    pnl_total  = sum(float(t.net_pnl_aud or 0) for t in all_trades)
    today_trades = [t for t in all_trades if t.exit_date == date.today()]
    pnl_today  = sum(float(t.net_pnl_aud or 0) for t in today_trades)

regime_emoji = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime, "⚪")
status_emoji = "⏸" if trading_paused else "▶️"

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Trading", f"{status_emoji} {'PAUSED' if trading_paused else 'ACTIVE'}")
c2.metric("Market Regime", f"{regime_emoji} {regime}")
c3.metric("Open Positions", open_positions)
c4.metric("Today's Signals", today_signals)
c5.metric("Today P&L", f"${pnl_today:+,.0f}")
c6.metric("Total P&L", f"${pnl_total:+,.0f}")

st.divider()

# ---------------------------------------------------------------------------
# Quick actions
# ---------------------------------------------------------------------------
st.subheader("Quick Actions")
col1, col2, col3 = st.columns(3)

with col1:
    if trading_paused:
        if st.button("▶️ Resume Trading", type="primary", use_container_width=True):
            with get_db() as db:
                cfg = db.query(SystemConfig).filter(SystemConfig.key == "trading_paused").first()
                if cfg:
                    cfg.value = "false"
                    cfg.updated_by = "dashboard"
            st.success("Trading resumed!")
            st.rerun()
    else:
        if st.button("⏸ Pause Trading", type="secondary", use_container_width=True):
            with get_db() as db:
                cfg = db.query(SystemConfig).filter(SystemConfig.key == "trading_paused").first()
                if cfg:
                    cfg.value = "true"
                    cfg.updated_by = "dashboard"
            st.warning("Trading paused!")
            st.rerun()

with col2:
    if st.button("🔍 Run Screener Now", use_container_width=True):
        from app.tasks.screening import run_daily_screen
        run_daily_screen.delay()
        st.info("Screener task queued!")

with col3:
    if st.button("📊 Send Report", use_container_width=True):
        from app.tasks.reporting import send_daily_report
        send_daily_report.delay()
        st.info("Report sending via WhatsApp!")

st.divider()

# ---------------------------------------------------------------------------
# Recent signals
# ---------------------------------------------------------------------------
st.subheader(f"Today's Signals ({date.today()})")
with get_db() as db:
    signals = db.query(Signal).filter(Signal.signal_date == date.today()).all()

if signals:
    import pandas as pd
    rows = []
    for s in signals:
        rows.append({
            "Ticker": s.ticker,
            "Status": s.status,
            "Close": f"${s.close_price:.3f}" if s.close_price else "—",
            "Pivot": f"${s.pivot_price:.3f}" if s.pivot_price else "—",
            "Stop": f"${s.stop_price:.3f}" if s.stop_price else "—",
            "RS": f"{s.rs_rating:.0f}" if s.rs_rating else "—",
            "Size": f"{s.suggested_size_shares} shares" if s.suggested_size_shares else "—",
            "Risk": f"${s.risk_per_trade_aud:.0f}" if s.risk_per_trade_aud else "—",
            "VCP": f"{s.vcp_contractions}c/{s.vcp_weeks}w" if s.vcp_contractions else "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.info("No signals generated today. Screener runs at 5:30pm AEST on trading days.")

st.divider()

# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------
st.subheader("System Health")
h1, h2, h3 = st.columns(3)
h1.metric("Last Heartbeat", heartbeat)
h2.metric("Paper Mode", "✅ YES" if os.getenv("IBKR_PAPER_MODE", "true").lower() == "true" else "⚠️ LIVE")
h3.metric("DB", "✅ Connected")
