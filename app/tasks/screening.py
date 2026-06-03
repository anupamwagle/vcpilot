"""
Screening tasks — run after ASX close to generate signals for next day.
"""
from __future__ import annotations
from datetime import date
from loguru import logger
from celery import shared_task

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.models.market import Stock, PriceBar
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.data.fetcher import (
    get_asx200_tickers, get_price_history, get_batch_prices,
    get_fundamentals, compute_rs_ratings
)
from app.data.calendar import today_is_trading_day
from app.screener.rules import RuleEngine
from app.screener.trend_template import evaluate_trend_template
from app.screener.fundamentals import evaluate_fundamentals
from app.screener.vcp import detect_vcp
from app.screener.market_regime import evaluate_market_regime, MarketRegime
from app.risk.manager import calculate_position_size
from app.notifications.whatsapp import WhatsAppNotifier


@app.task(name="app.tasks.screening.refresh_universe", bind=True, max_retries=3)
def refresh_universe(self):
    """Update ASX200 stock universe in the database."""
    logger.info("Refreshing ASX universe...")
    try:
        tickers = get_asx200_tickers()
        with get_db() as db:
            for ticker in tickers:
                asx_code = ticker.replace(".AX", "")
                stock = db.query(Stock).filter(Stock.ticker == ticker).first()
                if not stock:
                    stock = Stock(ticker=ticker, asx_code=asx_code, in_asx200=True)
                    db.add(stock)
                else:
                    stock.in_asx200 = True
            db.add(AuditLog(
                action=AuditAction.SYSTEM_STARTED,
                message=f"Universe refreshed: {len(tickers)} stocks",
            ))
        logger.info(f"Universe updated: {len(tickers)} ASX200 tickers")
    except Exception as exc:
        logger.error(f"Universe refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@app.task(name="app.tasks.screening.refresh_price_data", bind=True, max_retries=2)
def refresh_price_data(self):
    """Fetch and store EOD price data for all active stocks."""
    if not today_is_trading_day():
        logger.info("Not a trading day — skipping price refresh")
        return

    logger.info("Refreshing price data...")
    try:
        with get_db() as db:
            tickers = [s.ticker for s in db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            ).all()]

        if not tickers:
            logger.warning("No tickers in universe. Run refresh_universe first.")
            return

        # Fetch in batches of 100
        batch_size = 100
        all_prices = {}
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            prices = get_batch_prices(batch, period="2y")
            all_prices.update(prices)
            logger.debug(f"Fetched {len(prices)}/{len(batch)} in batch {i//batch_size+1}")

        # Compute RS ratings across all stocks
        rs_ratings = compute_rs_ratings(all_prices)

        # Store latest bar for each stock
        today = date.today()
        with get_db() as db:
            for ticker, df in all_prices.items():
                if df is None or df.empty:
                    continue
                latest = df.iloc[-1]
                if str(latest["date"]) != str(today):
                    continue  # Skip if not today's data

                # Upsert price bar
                existing = db.query(PriceBar).filter(
                    PriceBar.ticker == ticker,
                    PriceBar.date == today,
                ).first()
                if not existing:
                    bar = PriceBar(ticker=ticker, date=today)
                    db.add(bar)
                else:
                    bar = existing

                bar.open      = float(latest.get("open", 0) or 0)
                bar.high      = float(latest.get("high", 0) or 0)
                bar.low       = float(latest.get("low", 0) or 0)
                bar.close     = float(latest.get("close", 0) or 0)
                bar.adj_close = float(latest.get("adj_close", 0) or 0)
                bar.volume    = int(latest.get("volume", 0) or 0)
                bar.ma_50     = float(latest.get("ma_50") or 0) or None
                bar.ma_150    = float(latest.get("ma_150") or 0) or None
                bar.ma_200    = float(latest.get("ma_200") or 0) or None
                bar.high_52w  = float(latest.get("high_52w") or 0) or None
                bar.low_52w   = float(latest.get("low_52w") or 0) or None
                bar.rs_rating = rs_ratings.get(ticker)

        logger.info(f"Price data refreshed for {len(all_prices)} stocks")

    except Exception as exc:
        logger.error(f"Price refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=600)


@app.task(name="app.tasks.screening.evaluate_market_regime_task", bind=True)
def evaluate_market_regime_task(self):
    """Evaluate ASX200 market regime and store result."""
    import pandas as pd
    logger.info("Evaluating market regime...")
    try:
        from app.data.fetcher import get_price_history
        index_df = get_price_history("^AXJO", period="1y")
        if index_df is None:
            logger.warning("Could not fetch ASX200 data for regime check")
            return

        with get_db() as db:
            bars = db.query(PriceBar).filter(PriceBar.date == date.today()).all()
            universe_data = [
                {"ticker": b.ticker, "close": float(b.close or 0), "ma_200": float(b.ma_200 or 0)}
                for b in bars if b.close and b.ma_200
            ]
        universe_df = pd.DataFrame(universe_data)

        engine = RuleEngine()
        regime, rule_results = evaluate_market_regime(index_df, universe_df, engine)

        # Store regime in SystemConfig
        with get_db() as db:
            for key, value in [
                ("last_market_regime", regime.value),
                ("last_regime_check", str(date.today())),
            ]:
                cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
                if cfg:
                    cfg.value = value
                else:
                    db.add(SystemConfig(key=key, value=value, label=key))

            db.add(AuditLog(
                action=AuditAction.MARKET_REGIME_CHANGE,
                message=f"Market regime: {regime.value}",
                detail={r: {"passed": v.passed, "value": v.value} for r, v in rule_results.items()},
            ))

        # Notify via WhatsApp
        notifier = WhatsAppNotifier()
        notifier.send(f"📊 Market regime check: *{regime.value}*")

        logger.info(f"Market regime evaluated: {regime}")

    except Exception as exc:
        logger.error(f"Regime evaluation failed: {exc}")


@app.task(name="app.tasks.screening.run_daily_screen", bind=True, max_retries=2)
def run_daily_screen(self):
    """
    Main Minervini screening task. Runs all enabled rules against the full universe.
    Generates Signal records for stocks that pass all criteria.
    Adds partial-pass stocks to Watchlist.
    """
    if not today_is_trading_day():
        logger.info("Not a trading day — skipping screener")
        return

    logger.info("Starting Minervini daily screen...")
    engine   = RuleEngine()
    notifier = WhatsAppNotifier()
    today    = date.today()

    signals_generated = 0
    watchlist_added   = 0

    try:
        with get_db() as db:
            tickers = [s.ticker for s in db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            ).all()]

        for ticker in tickers:
            try:
                # Fetch price history
                df = get_price_history(ticker, period="2y")
                if df is None or len(df) < 200:
                    continue

                # --- Trend Template ---
                trend_results = evaluate_trend_template(ticker, df, engine)
                trend_passed = sum(1 for r in trend_results.values() if r.passed)
                trend_total  = len(trend_results)

                # Must pass ALL trend template rules to proceed
                if trend_passed < trend_total:
                    # Add to watchlist if ≥ 6/8 criteria met
                    if trend_passed >= 6:
                        _upsert_watchlist(ticker, trend_results, db)
                        watchlist_added += 1
                    continue

                # --- Fundamentals ---
                fundamentals = get_fundamentals(ticker)
                fund_results = evaluate_fundamentals(ticker, fundamentals, engine)
                fund_passed  = sum(1 for r in fund_results.values() if r.passed)
                fund_total   = len(fund_results)

                # Must pass ≥ 75% of fundamental rules
                if fund_total > 0 and (fund_passed / fund_total) < 0.75:
                    continue

                # --- VCP Detection ---
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
                vcp_result, vcp_rules = detect_vcp(ticker, df, engine, avg_vol)
                if not vcp_result.detected:
                    _upsert_watchlist(ticker, {**trend_results, **fund_results}, db)
                    watchlist_added += 1
                    continue

                # --- All criteria met — generate signal ---
                latest    = df.iloc[-1]
                rs_rating = float(latest.get("rs_rating") or 0)

                # Risk sizing
                with get_db() as db2:
                    from app.models.account import Account
                    account = db2.query(Account).filter(Account.is_active == True).first()
                    capital = float(account.capital_aud) if account else 1000.0

                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=vcp_result.pivot_price,
                    stop_price=vcp_result.stop_price,
                    engine=engine,
                )

                all_rule_results = {**trend_results, **fund_results, **vcp_rules}

                signal = Signal(
                    ticker=ticker,
                    signal_date=today,
                    status=SignalStatus.PENDING,
                    close_price=float(latest["close"]),
                    pivot_price=vcp_result.pivot_price,
                    stop_price=vcp_result.stop_price,
                    target_price_1=vcp_result.pivot_price * 1.20,
                    target_price_2=vcp_result.pivot_price * 1.40,
                    rs_rating=rs_rating,
                    trend_score=trend_passed,
                    fundamental_score=fund_passed,
                    rule_results={k: {"passed": v.passed, "value": v.value}
                                  for k, v in all_rule_results.items()},
                    suggested_size_shares=sizing.shares,
                    suggested_size_aud=sizing.capital_aud,
                    risk_per_trade_aud=sizing.risk_aud,
                    vcp_contractions=vcp_result.contraction_count,
                    vcp_weeks=vcp_result.base_weeks,
                )

                with get_db() as db3:
                    # Avoid duplicate signals for same ticker/date
                    existing = db3.query(Signal).filter(
                        Signal.ticker == ticker,
                        Signal.signal_date == today,
                    ).first()
                    if not existing:
                        db3.add(signal)
                        signals_generated += 1
                        notifier.send_signal_alert(signal.__dict__)
                        logger.info(f"Signal: {ticker} pivot=${vcp_result.pivot_price:.3f}")

            except Exception as e:
                logger.warning(f"Screener error for {ticker}: {e}")
                continue

        # Audit
        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.SCREENER_RUN,
                message=f"Screen complete: {signals_generated} signals, {watchlist_added} watchlist",
                detail={"date": str(today), "universe_size": len(tickers)},
            ))

        logger.info(f"Screen complete: {signals_generated} signals | {watchlist_added} watchlist additions")
        notifier.send(f"🔍 Screen done: *{signals_generated} signals*, {watchlist_added} on watchlist")

    except Exception as exc:
        logger.error(f"Daily screen failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


def _upsert_watchlist(ticker: str, rule_results: dict, db):
    """Add or update a stock on the watchlist."""
    from app.models.signal import Watchlist, WatchlistStatus
    existing = db.query(Watchlist).filter(
        Watchlist.ticker == ticker,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()
    if not existing:
        db.add(Watchlist(
            ticker=ticker,
            rule_results={k: {"passed": v.passed} for k, v in rule_results.items()},
            added_by="screener",
        ))
