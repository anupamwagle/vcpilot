"""
VCPilot Backtest Runner CLI
Example: python scripts/run_backtest.py --ticker BHP.AX --start 2024-01-01 --end 2026-06-01
"""
import sys
import os
import argparse
from datetime import datetime, date

# Add project root to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_db
from app.models.market import PriceBar
from app.backtester.backtest_engine import run_backtest_for_ticker

def main():
    parser = argparse.ArgumentParser(description="VCPilot Rule Backtest Runner")
    parser.add_argument("--ticker", type=str, help="Ticker to backtest (e.g. BHP.AX). If not specified, runs on the first available ticker with data.")
    parser.add_argument("--start", type=str, default="2025-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD), defaults to today")
    
    args = parser.parse_args()
    
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
    
    ticker = args.ticker
    if not ticker:
        # Find first ticker with data in DB
        with get_db() as db:
            first_bar = db.query(PriceBar.ticker).first()
            if first_bar:
                ticker = first_bar[0]
            else:
                print("Error: No price bars found in the database. Please run the 'Refresh Price Data' task first.")
                sys.exit(1)
                
    results = run_backtest_for_ticker(ticker, start_dt, end_dt)
    
    if "error" in results:
        print(f"Backtest failed: {results['error']}")
        sys.exit(1)
        
    print("\n" + "="*50)
    print(f" BACKTEST SUMMARY: {results['ticker']} ")
    print("="*50)
    print(f"Period:        {start_dt} to {end_dt}")
    print(f"Total Trades:  {results['total_trades']}")
    print(f"Win Rate:      {results['win_rate']}%")
    print(f"Profit Factor: {results['profit_factor']}")
    print(f"Net Profit:    ${results['net_profit_aud']:.2f}")
    print(f"Avg Hold Time: {results['avg_hold_days']} days")
    print("="*50)
    
    if results["trades"]:
        print("\nTRADES LOG:")
        print(f"{'Entry Date':<12} | {'Entry px':<10} | {'Exit Date':<12} | {'Exit px':<10} | {'P&L ($)':<9} | {'P&L (%)':<9} | {'Exit Reason':<20}")
        print("-"*95)
        for t in results["trades"]:
            pnl_sign = "+" if t["pnl_aud"] > 0 else ""
            pct_sign = "+" if t["pnl_pct"] > 0 else ""
            print(f"{str(t['entry_date']):<12} | {t['entry_price']:<10.3f} | {str(t['exit_date']):<12} | {t['exit_price']:<10.3f} | {pnl_sign}{t['pnl_aud']:<8.2f} | {pct_sign}{t['pnl_pct']:<8.2f} | {t['exit_reason']:<20}")
        print("="*50)
    else:
        print("\nNo trades were triggered during this period.")

if __name__ == "__main__":
    main()
