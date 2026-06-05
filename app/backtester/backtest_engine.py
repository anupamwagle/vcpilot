"""
VCPilot Backtesting Engine
Runs historical replay simulation of VCP screening, entry breakouts, and exit rule evaluations
using price bars stored in the database.
"""
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
from loguru import logger
from sqlalchemy import desc

from app.database import get_db
from app.models.market import PriceBar, Stock
from app.models.config import RuleConfig
from app.screener.rules import RuleEngine
from app.screener.vcp import detect_vcp, check_breakout
from app.screener.exit_rules import evaluate_exit_rules
from app.models.trade import ExitReason

def run_backtest_for_ticker(ticker: str, start_date: date, end_date: date, org_id: int = None, tier: str = "GOLD") -> dict:
    """
    Simulates VCP screener, breakouts, and exit logic for a single ticker over a date range.
    """
    logger.info(f"Starting historical backtest for {ticker} from {start_date} to {end_date}...")
    
    # 1. Load historical data from DB
    with get_db() as db:
        bars = db.query(PriceBar).filter(
            PriceBar.ticker == ticker,
            PriceBar.date >= start_date - timedelta(days=365),  # Load 1 year of prep data for indicators/MAs
            PriceBar.date <= end_date
        ).order_by(PriceBar.date).all()
    
    if not bars:
        logger.warning(f"No price bars found in database for {ticker} in range.")
        return {"error": "No data"}
    
    # Convert DB rows to DataFrame
    data_list = []
    for b in bars:
        data_list.append({
            "date": b.date,
            "open": float(b.open or 0),
            "high": float(b.high or 0),
            "low": float(b.low or 0),
            "close": float(b.close or 0),
            "volume": int(b.volume or 0),
            "ma_50": float(b.ma_50 or 0) if b.ma_50 else None,
            "ma_150": float(b.ma_150 or 0) if b.ma_150 else None,
            "ma_200": float(b.ma_200 or 0) if b.ma_200 else None,
            "avg_vol_50": float(b.avg_vol_50 or 0) if b.avg_vol_50 else None,
            "pct_from_52w_high": float(b.pct_from_52w_high or 0) if b.pct_from_52w_high else None,
            "pct_from_52w_low": float(b.pct_from_52w_low or 0) if b.pct_from_52w_low else None,
            "rs_rating": float(b.rs_rating or 0) if b.rs_rating else None,
            "atr_14": float(b.atr_14 or 0) if b.atr_14 else None,
        })
        
    full_df = pd.DataFrame(data_list)
    full_df["date"] = pd.to_datetime(full_df["date"]).dt.date
    
    # Initialize Rule Engine
    engine = RuleEngine(organization_id=org_id, tier=tier)
    
    # Backtest state
    pending_signal = None  # Stores VCPResult if VCP detected and waiting for breakout
    position = None        # Stores open trade dictionary
    trades_log = []        # Completed trades
    
    # Get trading days inside the target backtest window
    test_df = full_df[full_df["date"] >= start_date].copy()
    test_dates = test_df["date"].tolist()
    
    for today in test_dates:
        # Get historical data up to today (inclusive for tracking, exclusive for screening)
        hist_incl_today = full_df[full_df["date"] <= today].copy()
        hist_excl_today = full_df[full_df["date"] < today].copy()
        
        if hist_excl_today.empty or len(hist_excl_today) < 60:
            continue
            
        today_bar = hist_incl_today.iloc[-1]
        today_close = float(today_bar["close"])
        today_high = float(today_bar["high"])
        today_low = float(today_bar["low"])
        today_vol = float(today_bar["volume"])
        avg_vol_50 = float(today_bar["avg_vol_50"] or today_vol)
        
        # --- PHASE 1: MANAGE OPEN POSITION ---
        if position is not None:
            # Check if stop loss was hit today
            if today_low <= position["current_stop"]:
                # Stop loss triggered
                exit_price = min(position["current_stop"], float(today_bar["open"]))
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                
                trades_log.append({
                    "ticker": ticker,
                    "entry_date": position["entry_date"],
                    "entry_price": position["entry_price"],
                    "exit_date": today,
                    "exit_price": exit_price,
                    "qty": position["qty"],
                    "pnl_aud": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": "STOP_LOSS",
                })
                logger.info(f"[{today}] EXIT STOP LOSS: closed {ticker} @ {exit_price:.3f} (P&L: {pnl_pct:+.2f}%)")
                position = None
                pending_signal = None  # Clear signal
                continue
            
            # Retrieve weekly closes for ExitRules (Fri resample, latest first)
            hist_weekly = hist_incl_today.copy()
            hist_weekly["date"] = pd.to_datetime(hist_weekly["date"])
            weekly_df = hist_weekly.set_index("date").resample("W-FRI")["close"].last().dropna()
            weekly_closes = weekly_df.tail(5).tolist()[::-1]  # Latest first
            
            # Evaluate Exit Rules
            exit_signals = evaluate_exit_rules(
                ticker=ticker,
                entry_price=position["entry_price"],
                current_price=today_close,
                current_stop=position["current_stop"],
                entry_date=position["entry_date"],
                today=today,
                weekly_closes=weekly_closes,
                df_daily=hist_incl_today,
                avg_vol_50=avg_vol_50,
                next_earnings_date=None,  # Not tracked in basic backtest
                engine=engine
            )
            
            # Check if exits triggered
            triggered_exit = next((s for s in exit_signals if s.should_exit), None)
            if triggered_exit:
                exit_price = today_close
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                
                trades_log.append({
                    "ticker": ticker,
                    "entry_date": position["entry_date"],
                    "entry_price": position["entry_price"],
                    "exit_date": today,
                    "exit_price": exit_price,
                    "qty": position["qty"],
                    "pnl_aud": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": str(triggered_exit.reason.name if triggered_exit.reason else triggered_exit.rule_id),
                })
                logger.info(f"[{today}] EXIT RULE ({triggered_exit.reason or triggered_exit.rule_id}): closed {ticker} @ {exit_price:.3f} (P&L: {pnl_pct:+.2f}%)")
                position = None
                pending_signal = None  # Clear signal
                continue
                
        # --- PHASE 2: CHECK BREAKOUT TRIGGER ON PENDING SIGNAL ---
        elif pending_signal is not None:
            # Check breakout price and volume conditions
            breakout_rules = check_breakout(
                ticker=ticker,
                df=hist_incl_today,
                pivot_price=pending_signal["pivot_price"],
                avg_vol_50=avg_vol_50,
                engine=engine
            )
            all_passed = all(r.passed for r in breakout_rules.values())
            
            if all_passed:
                # Triggered entry breakout!
                entry_price = max(pending_signal["pivot_price"], float(today_bar["open"]))
                stop_price = pending_signal["stop_price"]
                
                # Mock Sizing: assume $10,000 starting capital, 2% risk rule
                capital = 10000.0
                risk_pct = 2.0  # 2% max risk
                risk_amt = capital * (risk_pct / 100.0)
                risk_per_share = entry_price - stop_price
                
                if risk_per_share > 0:
                    qty = int(risk_amt // risk_per_share)
                else:
                    qty = 100  # Default fallback
                
                if qty < 1:
                    qty = 1
                
                position = {
                    "entry_date": today,
                    "entry_price": entry_price,
                    "current_stop": stop_price,
                    "qty": qty,
                }
                logger.info(f"[{today}] ENTRY TRIGGERED: bought {qty}x {ticker} @ {entry_price:.3f} (Stop: {stop_price:.3f})")
                pending_signal = None  # Promoted to position
                continue
                
            # If breakout window expires (e.g. 5 trading days after screener detection), clear signal
            signal_age = (today - pending_signal["date"]).days
            if signal_age > 7:  # 7 calendar days (~5 trading days)
                logger.debug(f"[{today}] Signal expired for {ticker} (age {signal_age} days)")
                pending_signal = None
                
        # --- PHASE 3: SCREEN FOR NEW VCP SIGNALS ---
        else:
            # Detect VCP based on historical bars up to yesterday close
            vcp_res, rule_results = detect_vcp(ticker, hist_excl_today, engine, avg_vol_50=avg_vol_50)
            if vcp_res.detected:
                pending_signal = {
                    "date": today,
                    "pivot_price": vcp_res.pivot_price,
                    "stop_price": vcp_res.stop_price,
                }
                logger.info(f"[{today}] VCP DETECTED (Signal Created): {ticker} | Pivot: {vcp_res.pivot_price:.3f} | Stop: {vcp_res.stop_price:.3f}")
                
    # If a position remains open at the end of backtest, close it at last price
    if position is not None:
        last_bar = test_df.iloc[-1]
        exit_price = float(last_bar["close"])
        exit_date = last_bar["date"]
        pnl = (exit_price - position["entry_price"]) * position["qty"]
        pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
        trades_log.append({
            "ticker": ticker,
            "entry_date": position["entry_date"],
            "entry_price": position["entry_price"],
            "exit_date": exit_date,
            "exit_price": exit_price,
            "qty": position["qty"],
            "pnl_aud": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": "END_OF_BACKTEST",
        })
        logger.info(f"[{exit_date}] END OF BACKTEST: closed {ticker} @ {exit_price:.3f} (P&L: {pnl_pct:+.2f}%)")

    # 4. Generate Backtest Statistics
    total_trades = len(trades_log)
    if total_trades > 0:
        wins = [t for t in trades_log if t["pnl_aud"] > 0]
        losses = [t for t in trades_log if t["pnl_aud"] <= 0]
        win_rate = (len(wins) / total_trades) * 100
        
        gross_profits = sum(t["pnl_aud"] for t in wins)
        gross_losses = abs(sum(t["pnl_aud"] for t in losses))
        profit_factor = round(gross_profits / gross_losses, 2) if gross_losses > 0 else float("inf")
        net_profit = sum(t["pnl_aud"] for t in trades_log)
        
        # Calculate hold times
        hold_days = []
        for t in trades_log:
            days = (t["exit_date"] - t["entry_date"]).days
            hold_days.append(days)
        avg_hold_days = round(sum(hold_days) / len(hold_days), 1)
    else:
        win_rate = 0.0
        profit_factor = 0.0
        net_profit = 0.0
        avg_hold_days = 0.0
        
    return {
        "ticker": ticker,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "profit_factor": profit_factor,
        "net_profit_aud": round(net_profit, 2),
        "avg_hold_days": avg_hold_days,
        "trades": trades_log
    }
