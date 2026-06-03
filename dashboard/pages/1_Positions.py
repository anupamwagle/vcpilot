"""Open & Closed Positions"""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from datetime import date
from app.database import get_db
from app.models.trade import Position, Trade, TradeStatus

st.set_page_config(page_title="Positions — VCPilot", layout="wide")
st.title("📋 Positions")

tab1, tab2 = st.tabs(["Open", "Closed"])

with tab1:
    with get_db() as db:
        positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).all()
    if not positions:
        st.info("No open positions.")
    else:
        rows = []
        for p in positions:
            curr = float(p.current_price or p.entry_price)
            pnl_pct = (curr - float(p.entry_price)) / float(p.entry_price) * 100
            pnl_aud = (curr - float(p.entry_price)) * p.qty
            rows.append({
                "Ticker": p.ticker, "Qty": p.qty,
                "Entry": f"${float(p.entry_price):.3f}",
                "Current": f"${curr:.3f}",
                "Stop": f"${float(p.current_stop):.3f}",
                "P&L %": f"{pnl_pct:+.1f}%",
                "P&L $": f"${pnl_aud:+.0f}",
                "Days": (date.today() - p.entry_date).days,
                "Mode": "📄" if p.is_paper else "💰",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

with tab2:
    with get_db() as db:
        trades = db.query(Trade).order_by(Trade.exit_date.desc()).limit(100).all()
    if not trades:
        st.info("No closed trades yet.")
    else:
        rows = [{"Ticker": t.ticker, "Entry": str(t.entry_date), "Exit": str(t.exit_date),
                 "Days": t.hold_days, "Entry $": f"${float(t.entry_price):.3f}",
                 "Exit $": f"${float(t.exit_price):.3f}",
                 "P&L %": f"{float(t.pnl_pct)*100:+.1f}%" if t.pnl_pct else "—",
                 "P&L $": f"${float(t.net_pnl_aud):+.0f}" if t.net_pnl_aud else "—",
                 "Reason": str(t.exit_reason).replace("ExitReason.", ""),
                 "CGT": "✅" if t.cgt_eligible_discount else "—"}
                for t in trades]
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
        total_pnl = sum(float(t.net_pnl_aud or 0) for t in trades)
        wins = sum(1 for t in trades if (t.net_pnl_aud or 0) > 0)
        st.metric("Total P&L", f"${total_pnl:+,.0f}")
        st.metric("Win Rate", f"{wins/len(trades)*100:.0f}%" if trades else "—")
