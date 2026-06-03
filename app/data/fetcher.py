"""
VCPilot Data Fetcher — yfinance wrapper for price, volume, and fundamental data.

Strategy:
  - yfinance for ALL price/volume/MA data (free, unlimited EOD)
  - yfinance quarterly_financials for EPS, revenue, ROE (free, covers ASX)
  - FMP free tier (250 calls/day) used ONLY for supplemental data on shortlisted stocks
  - ASX index components fetched from Wikipedia / hardcoded lists (updated weekly)
"""
from __future__ import annotations
import time
from datetime import datetime, date, timedelta
from typing import Optional
import pandas as pd
import numpy as np
import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------

ASX200_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_200"


def get_asx200_tickers() -> list[str]:
    """
    Fetch current ASX200 constituents from Wikipedia.
    Returns list in yfinance format: ["BHP.AX", "CBA.AX", ...]
    Falls back to a cached list if Wikipedia is unavailable.
    """
    import io, requests as _req
    try:
        # pd.read_html blocks Wikipedia without a browser UA — use requests first
        headers = {"User-Agent": "Mozilla/5.0 (compatible; VCPilot/1.0; +https://github.com/anupamwagle/vcpilot)"}
        resp = _req.get(ASX200_WIKIPEDIA_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if "code" in cols_lower:
                col = next(c for c in tbl.columns if str(c).lower() == "code")
                codes = tbl[col].dropna().tolist()
                tickers = [f"{str(c).strip().upper()}.AX" for c in codes if isinstance(c, str) and len(c.strip()) >= 2]
                if len(tickers) > 100:
                    logger.info(f"Fetched {len(tickers)} ASX200 tickers from Wikipedia")
                    return tickers
    except Exception as e:
        logger.warning(f"Wikipedia ASX200 fetch failed: {e}. Using fallback list.")

    # Minimal fallback — top 20 by market cap (extend this in production)
    logger.warning("Using 20-stock fallback universe — Wikipedia unavailable")
    return [
        "BHP.AX", "CBA.AX", "NAB.AX", "WBC.AX", "ANZ.AX", "WES.AX",
        "MQG.AX", "CSL.AX", "RIO.AX", "WOW.AX", "FMG.AX", "TLS.AX",
        "GMG.AX", "TCL.AX", "WDS.AX", "STO.AX", "QBE.AX", "IAG.AX",
        "AMP.AX", "SUN.AX",
    ]


# ---------------------------------------------------------------------------
# Price & OHLCV
# ---------------------------------------------------------------------------

def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute moving averages, volume metrics, 52-week range, and ATR."""
    if df is None or df.empty:
        return df

    # Ensure standard names
    if "adj close" in df.columns and "adj_close" not in df.columns:
        df = df.rename(columns={"adj close": "adj_close"})

    # Compute moving averages
    for period_ma, col in [(10, "ma_10"), (21, "ma_21"), (50, "ma_50"),
                           (150, "ma_150"), (200, "ma_200")]:
        df[col] = df["close"].rolling(period_ma, min_periods=period_ma).mean()

    df["ma_200_prev"] = df["ma_200"].shift(1)

    # Volume metrics
    df["avg_vol_50"] = df["volume"].rolling(50, min_periods=20).mean()
    df["vol_ratio"] = df["volume"] / df["avg_vol_50"].replace(0, np.nan)

    # 52-week range
    df["high_52w"] = df["high"].rolling(252, min_periods=50).max()
    df["low_52w"]  = df["low"].rolling(252, min_periods=50).min()
    df["pct_from_52w_high"] = (df["close"] - df["high_52w"]) / df["high_52w"] * 100
    df["pct_from_52w_low"]  = (df["close"] - df["low_52w"]) / df["low_52w"] * 100

    # ATR (14-day)
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df[["high", "low", "prev_close"]].apply(
        lambda r: max(r["high"] - r["low"],
                      abs(r["high"] - r["prev_close"]),
                      abs(r["low"] - r["prev_close"])), axis=1
    )
    df["atr_14"] = df["tr"].rolling(14, min_periods=14).mean()

    return df


def get_price_history(
    ticker: str,
    period: str = "2y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV for a single ticker.
    Returns DataFrame with: date, open, high, low, close, adj_close, volume
    or None on failure.
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period, interval=interval, auto_adjust=False)
        if df.empty:
            logger.debug(f"No price data for {ticker}")
            return None

        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"adj close": "adj_close"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)

        df = _add_indicators(df)

        return df

    except Exception as e:
        logger.error(f"Price fetch failed for {ticker}: {e}")
        return None


def get_batch_prices(
    tickers: list[str],
    period: str = "2y",
) -> dict[str, pd.DataFrame]:
    """
    Fetch price history for multiple tickers efficiently.
    Returns dict of ticker → DataFrame.
    """
    results = {}
    # yfinance batch download
    try:
        raw = yf.download(
            tickers, period=period, interval="1d",
            auto_adjust=False, progress=False, group_by="ticker"
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        raw = None

    if raw is None or raw.empty:
        # Fallback: individual fetches
        for ticker in tickers:
            df = get_price_history(ticker, period=period)
            if df is not None:
                results[ticker] = df
            time.sleep(0.1)  # Be polite to yfinance
        return results

    # Parse batch response
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy()
            df = df.dropna(how="all")
            if df.empty:
                continue
            df = df.reset_index()
            df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.sort_values("date").reset_index(drop=True)
            df = _add_indicators(df)
            results[ticker] = df
        except Exception as e:
            logger.debug(f"Batch parse failed for {ticker}: {e}")

    return results


# ---------------------------------------------------------------------------
# Relative Strength
# ---------------------------------------------------------------------------

def compute_rs_ratings(
    stock_prices: dict[str, pd.DataFrame],
    benchmark_ticker: str = "^AXJO",
    lookback_days: int = 252,
) -> dict[str, float]:
    """
    Compute Relative Strength rating (0–100 percentile) for each stock
    vs the ASX200 index over the last N trading days.

    RS formula: (stock 12m performance) ranked against all stocks.
    Minervini uses IBD RS Rating; we implement a compatible percentile rank.
    """
    try:
        benchmark_df = get_price_history(benchmark_ticker, period="2y")
        if benchmark_df is None:
            logger.warning("Could not fetch ASX200 benchmark for RS calculation")
            return {}

        benchmark_perf = _compute_performance(benchmark_df, lookback_days)

        stock_perfs: dict[str, float] = {}
        for ticker, df in stock_prices.items():
            perf = _compute_performance(df, lookback_days)
            if perf is not None:
                stock_perfs[ticker] = perf

        if not stock_perfs:
            return {}

        perfs_series = pd.Series(stock_perfs)
        rs_ratings = perfs_series.rank(pct=True) * 100
        return rs_ratings.round(1).to_dict()

    except Exception as e:
        logger.error(f"RS rating computation failed: {e}")
        return {}


def _compute_performance(df: pd.DataFrame, days: int) -> Optional[float]:
    """Weighted RS performance: 40% last 3m + 20% prior 3m + 20% prior 3m + 20% first 3m."""
    if len(df) < days:
        return None
    closes = df["close"].tail(days).values
    if len(closes) < days:
        return None
    q4 = closes[-1] / closes[max(-63, -len(closes))] - 1   # Last 3 months
    q3 = closes[max(-63, -len(closes))] / closes[max(-126, -len(closes))] - 1
    q2 = closes[max(-126, -len(closes))] / closes[max(-189, -len(closes))] - 1
    q1 = closes[max(-189, -len(closes))] / closes[0] - 1
    return (q4 * 0.40) + (q3 * 0.20) + (q2 * 0.20) + (q1 * 0.20)


# ---------------------------------------------------------------------------
# Fundamentals (yfinance quarterly financials)
# ---------------------------------------------------------------------------

def get_fundamentals(ticker: str) -> dict:
    """
    Fetch fundamental data from yfinance.
    Returns dict with eps_quarterly, revenue_quarterly, roe, net_margin, etc.
    """
    result = {
        "company_name": "",
        "sector": "",
        "industry": "",
        "eps_quarterly": [],
        "revenue_quarterly": [],
        "roe": None,
        "net_margin": None,
        "net_margin_prev": None,
        "inst_ownership_pct": None,
        "next_earnings_date": None,
    }

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        # EPS quarterly (trailing 8 quarters)
        try:
            q_earnings = stock.quarterly_earnings
            if q_earnings is not None and not q_earnings.empty:
                result["eps_quarterly"] = q_earnings["EPS"].tolist()[:8]
        except Exception:
            pass

        # Revenue quarterly
        try:
            q_financials = stock.quarterly_financials
            if q_financials is not None and not q_financials.empty:
                if "Total Revenue" in q_financials.index:
                    result["revenue_quarterly"] = (
                        q_financials.loc["Total Revenue"].dropna().tolist()[:8]
                    )
        except Exception:
            pass

        # Company name — prefer longName, fall back to shortName
        long_name = info.get("longName") or info.get("shortName") or ""
        if long_name:
            result["company_name"] = long_name.strip()

        # Sector / industry
        result["sector"]   = info.get("sector") or ""
        result["industry"] = info.get("industry") or ""

        # ROE, margins from info
        result["roe"] = info.get("returnOnEquity")
        result["net_margin"] = info.get("profitMargins")
        result["inst_ownership_pct"] = info.get("heldPercentInstitutions")

        # Next earnings date
        try:
            cal = stock.calendar
            if cal is not None and "Earnings Date" in cal:
                earnings_dates = cal["Earnings Date"]
                if isinstance(earnings_dates, list) and earnings_dates:
                    result["next_earnings_date"] = earnings_dates[0].date() \
                        if hasattr(earnings_dates[0], "date") else earnings_dates[0]
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"Fundamentals fetch failed for {ticker}: {e}")

    return result
