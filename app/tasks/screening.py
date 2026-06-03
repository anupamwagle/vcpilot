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

def serialize_rule_results(rule_results: dict) -> dict:
    """Helper to convert rule results to a JSON-serializable dict, avoiding numpy serialization errors."""
    serialized = {}
    for k, v in rule_results.items():
        if hasattr(v, "passed"):
            passed = bool(v.passed)
            val = getattr(v, "value", None)
            msg = getattr(v, "message", None)
        elif isinstance(v, dict):
            passed = bool(v.get("passed", False))
            val = v.get("value", None)
            msg = v.get("message", None)
        else:
            passed = bool(v)
            val = None
            msg = None
        
        # Handle numpy float/int/bool
        if hasattr(val, "item"):
            val = val.item()
            
        serialized[k] = {
            "passed": passed,
            "value": val,
            "message": msg
        }
    return serialized



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
                detail={r: {"passed": bool(v.passed), "value": v.value.item() if hasattr(v.value, "item") else v.value} for r, v in rule_results.items()},
            ))

        # Notify via WhatsApp
        from app.models.account import Organization
        with get_db() as db:
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
        for org in orgs:
            try:
                notifier = WhatsAppNotifier(organization_id=org.id)
                if notifier.whatsapp_enabled and notifier.admin_jid:
                    notifier.send(f"📊 Market regime check: *{regime.value}*")
            except Exception as org_err:
                logger.error(f"Failed to notify Org {org.name} (ID: {org.id}) of regime: {org_err}")

        logger.info(f"Market regime evaluated: {regime}")

    except Exception as exc:
        logger.error(f"Regime evaluation failed: {exc}")


@app.task(name="app.tasks.screening.run_daily_screen", bind=True, max_retries=2)
def run_daily_screen(self):
    """
    Main Minervini screening task. Runs all enabled rules against the full universe.
    Auto-bootstraps the universe if the stocks table is empty (first run).
    """
    if not today_is_trading_day():
        logger.info("Not a trading day — skipping screener")
        return

    logger.info("Starting Minervini daily screen...")
    today    = date.today()

    try:
        with get_db() as db:
            from app.models.account import Organization
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            
            tickers = [s.ticker for s in db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            ).all()]

        # ── Auto-bootstrap: if universe is empty, fetch it now ────────────
        if not tickers:
            logger.warning("Stock universe is empty — auto-fetching ASX200 from Wikipedia...")
            tickers = get_asx200_tickers()
            with get_db() as db:
                for ticker in tickers:
                    asx_code = ticker.replace(".AX", "")
                    if not db.query(Stock).filter(Stock.ticker == ticker).first():
                        db.add(Stock(ticker=ticker, asx_code=asx_code, in_asx200=True, is_active=True))
                db.add(AuditLog(
                    action=AuditAction.SYSTEM_STARTED,
                    message=f"Universe auto-bootstrapped: {len(tickers)} ASX200 tickers",
                ))
            logger.info(f"Universe bootstrapped with {len(tickers)} tickers")

        if not tickers:
            logger.error("Could not load any tickers — screener aborted")
            return

        for org in orgs:
            logger.info(f"Running daily screen for organization '{org.name}' ({org.tier.value})...")
            engine = RuleEngine(organization_id=org.id, tier=org.tier.value)
            notifier = WhatsAppNotifier(organization_id=org.id)
            
            signals_generated = 0
            watchlist_added   = 0

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
                            with get_db() as db:
                                _upsert_watchlist(ticker, trend_results, db, organization_id=org.id)
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
                        with get_db() as db:
                            _upsert_watchlist(ticker, {**trend_results, **fund_results}, db, organization_id=org.id)
                        watchlist_added += 1
                        continue

                    # --- All criteria met — generate signal ---
                    latest    = df.iloc[-1]
                    rs_rating = float(latest.get("rs_rating") or 0)

                    # Risk sizing
                    with get_db() as db2:
                        from app.models.account import Account
                        account = db2.query(Account).filter(
                            Account.organization_id == org.id, 
                            Account.is_active == True
                        ).first()
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
                        organization_id=org.id,
                        status=SignalStatus.PENDING,
                        close_price=float(latest["close"]),
                        pivot_price=vcp_result.pivot_price,
                        stop_price=vcp_result.stop_price,
                        target_price_1=vcp_result.pivot_price * 1.20,
                        target_price_2=vcp_result.pivot_price * 1.40,
                        rs_rating=rs_rating,
                        trend_score=trend_passed,
                        fundamental_score=fund_passed,
                        rule_results=serialize_rule_results(all_rule_results),
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
                            Signal.organization_id == org.id,
                        ).first()
                        if not existing:
                            db3.add(signal)
                            signals_generated += 1
                            notifier.send_signal_alert(signal.__dict__)
                            logger.info(f"Signal for {org.name}: {ticker} pivot=${vcp_result.pivot_price:.3f}")

                except Exception as e:
                    logger.warning(f"Screener error for {ticker} (Org: {org.name}): {e}")
                    continue

            # Audit organization-specific run
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_RUN,
                    organization_id=org.id,
                    message=f"Screen complete: {signals_generated} signals, {watchlist_added} watchlist",
                    detail={"date": str(today), "universe_size": len(tickers)},
                ))

            logger.info(f"Screen complete for {org.name}: {signals_generated} signals | {watchlist_added} watchlist")
            notifier.send(f"🔍 Screen done for *{org.name}*: *{signals_generated} signals*, {watchlist_added} on watchlist")

    except Exception as exc:
        logger.error(f"Daily screen failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@app.task(name="app.tasks.screening.run_full_setup", bind=True)
def run_full_setup(self):
    """
    First-time setup chain:
    1. Refresh universe (fetch ASX200 tickers)
    2. Refresh price data (2yr OHLCV for all tickers)
    3. Evaluate market regime
    4. Run Minervini screener
    Designed to be triggered manually from the dashboard on first run.
    """
    from celery import chain as celery_chain
    logger.info("Starting full VCPilot setup sequence...")
    from app.models.account import Organization
    with get_db() as db:
        orgs = db.query(Organization).filter(Organization.is_active == True).all()
    for org in orgs:
        try:
            notifier = WhatsAppNotifier(organization_id=org.id)
            if notifier.whatsapp_enabled and notifier.admin_jid:
                notifier.send("⚙️ VCPilot full setup starting: universe → data → regime → screener")
        except Exception as org_err:
            logger.error(f"Failed to notify Org {org.name} (ID: {org.id}) of full setup: {org_err}")

    # Run sequentially as a chain
    celery_chain(
        refresh_universe.si(),
        refresh_price_data.si(),
        evaluate_market_regime_task.si(),
        _run_screen_force.si(),
    ).delay()


@app.task(name="app.tasks.screening._run_screen_force", bind=True, max_retries=1)
def _run_screen_force(self):
    """
    Full Minervini screen bypassing the trading-day gate. For manual triggers.
    Writes a SCREENER_TICKER audit row per stock so the Task Log shows live progress.
    """
    logger.info("Running forced screen (bypassing trading-day check)...")
    today    = date.today()

    try:
        with get_db() as db:
            from app.models.account import Organization
            orgs = db.query(Organization).filter(Organization.is_active == True).all()

            tickers = [s.ticker for s in db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            ).all()]

        # Auto-bootstrap universe if empty
        if not tickers:
            logger.warning("Universe empty — fetching ASX200...")
            tickers = get_asx200_tickers()
            with get_db() as db:
                for ticker in tickers:
                    asx_code = ticker.replace(".AX", "")
                    if not db.query(Stock).filter(Stock.ticker == ticker).first():
                        db.add(Stock(ticker=ticker, asx_code=asx_code, in_asx200=True, is_active=True))
                db.add(AuditLog(
                    action=AuditAction.SYSTEM_STARTED,
                    message=f"Universe bootstrapped: {len(tickers)} ASX200 tickers",
                ))
            logger.info(f"Universe bootstrapped: {len(tickers)} tickers")

        if not tickers:
            return

        # Pre-compute RS ratings for the whole universe in one batch for accuracy
        logger.info(f"Pre-fetching RS ratings for {len(tickers)} tickers...")
        all_prices = {}
        batch_size = 50
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            prices = get_batch_prices(batch, period="2y")
            all_prices.update(prices)
            logger.debug(f"Batch {i//batch_size+1}: fetched {len(prices)} dataframes")

        rs_ratings = compute_rs_ratings(all_prices) if all_prices else {}
        logger.info(f"RS ratings computed for {len(rs_ratings)} stocks")

        for org in orgs:
            logger.info(f"Running forced screen for organization '{org.name}' ({org.tier.value})...")
            engine = RuleEngine(organization_id=org.id, tier=org.tier.value)
            notifier = WhatsAppNotifier(organization_id=org.id)
            
            signals_generated = 0
            watchlist_added   = 0
            skipped_no_data   = 0

            # Log start per organization
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_RUN,
                    organization_id=org.id,
                    message=f"Force screen started: {len(tickers)} stocks to check",
                    detail={"mode": "force_start", "universe_size": len(tickers)},
                ))

            # ── Per-ticker screening loop ─────────────────────────────────────────
            for ticker in tickers:
                try:
                    df = all_prices.get(ticker)
                    if df is None:
                        df = get_price_history(ticker, period="2y")
                    if df is None or len(df) < 50:
                        skipped_no_data += 1
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.SCREENER_TICKER,
                                organization_id=org.id,
                                ticker=ticker,
                                message="⚪ SKIP — insufficient price data",
                                detail={"reason": "no_data", "bars": len(df) if df is not None else 0},
                            ))
                        continue

                    # Inject pre-computed RS rating into the dataframe
                    if ticker in rs_ratings:
                        df["rs_rating"] = rs_ratings[ticker]

                    # ── Trend Template ──────────────────────────────────────────
                    trend_results = evaluate_trend_template(ticker, df, engine)
                    trend_passed  = sum(1 for r in trend_results.values() if r.passed)
                    trend_total   = len(trend_results)

                    # Build human-readable rule breakdown for the audit log
                    rule_summary = []
                    for rid, r in trend_results.items():
                        icon = "✓" if r.passed else "✗"
                        rule_summary.append(f"{icon} {rid.replace('trend_','')}: {r.message or ''}")

                    if trend_passed == trend_total:
                        # ── All trend rules pass → run fundamentals ─────────────
                        fundamentals = get_fundamentals(ticker)
                        fund_results = evaluate_fundamentals(ticker, fundamentals, engine)
                        fund_passed  = sum(1 for r in fund_results.values() if r.passed)
                        fund_total   = len(fund_results)

                        for rid, r in fund_results.items():
                            icon = "✓" if r.passed else "✗"
                            rule_summary.append(f"{icon} {rid.replace('fundamental_','')}: {r.message or ''}")

                        if fund_total == 0 or (fund_passed / fund_total) >= 0.75:
                            # ── Fundamentals pass → VCP check ───────────────────
                            avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
                            vcp_result, vcp_rules = detect_vcp(ticker, df, engine, avg_vol)

                            for rid, r in vcp_rules.items():
                                icon = "✓" if r.passed else "✗"
                                rule_summary.append(f"{icon} {rid.replace('vcp_','')}: {r.message or ''}")

                            if vcp_result.detected:
                                # ── FULL SIGNAL ──────────────────────────────────
                                latest    = df.iloc[-1]
                                rs_rating = float(rs_ratings.get(ticker, 0))

                                with get_db() as db2:
                                    from app.models.account import Account
                                    account = db2.query(Account).filter(
                                        Account.organization_id == org.id, 
                                        Account.is_active == True
                                    ).first()
                                    capital = float(account.capital_aud) if account else 1000.0

                                sizing = calculate_position_size(
                                    capital_aud=capital,
                                    entry_price=vcp_result.pivot_price,
                                    stop_price=vcp_result.stop_price,
                                    engine=engine,
                                )

                                all_rule_results = {**trend_results, **fund_results, **vcp_rules}
                                signal = Signal(
                                    ticker=ticker, signal_date=today,
                                    organization_id=org.id,
                                    status=SignalStatus.PENDING,
                                    close_price=float(latest["close"]),
                                    pivot_price=vcp_result.pivot_price,
                                    stop_price=vcp_result.stop_price,
                                    target_price_1=vcp_result.pivot_price * 1.20,
                                    target_price_2=vcp_result.pivot_price * 1.40,
                                    rs_rating=rs_rating,
                                    trend_score=trend_passed,
                                    fundamental_score=fund_passed,
                                    rule_results=serialize_rule_results(all_rule_results),
                                    suggested_size_shares=sizing.shares,
                                    suggested_size_aud=sizing.capital_aud,
                                    risk_per_trade_aud=sizing.risk_aud,
                                    vcp_contractions=vcp_result.contraction_count,
                                    vcp_weeks=vcp_result.base_weeks,
                                )

                                with get_db() as db3:
                                    existing = db3.query(Signal).filter(
                                        Signal.ticker == ticker, 
                                        Signal.signal_date == today,
                                        Signal.organization_id == org.id,
                                    ).first()
                                    if not existing:
                                        db3.add(signal)
                                        db3.add(AuditLog(
                                            action=AuditAction.SCREENER_TICKER,
                                            organization_id=org.id,
                                            ticker=ticker,
                                            message=f"🟢 SIGNAL — pivot ${vcp_result.pivot_price:.3f} stop ${vcp_result.stop_price:.3f} RS={rs_rating:.0f} trend={trend_passed}/{trend_total} fund={fund_passed}/{fund_total} vcp={vcp_result.contraction_count}c/{vcp_result.base_weeks}w",
                                            detail={"result": "signal", "rules": rule_summary},
                                        ))
                                        signals_generated += 1
                                        notifier.send_signal_alert(signal.__dict__)
                                        logger.info(f"SIGNAL: {ticker} pivot=${vcp_result.pivot_price:.3f}")
                            else:
                                # Trend + fundamentals pass but no VCP yet → watchlist
                                reason = f"VCP not detected ({vcp_result.contraction_count or 0} contractions)"
                                with get_db() as db:
                                    _upsert_watchlist(ticker, {**trend_results, **fund_results}, db, organization_id=org.id)
                                    db.add(AuditLog(
                                        action=AuditAction.SCREENER_TICKER,
                                        organization_id=org.id,
                                        ticker=ticker,
                                        message=f"🔵 WATCHLIST — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total} | {reason}",
                                        detail={"result": "watchlist", "reason": reason, "rules": rule_summary},
                                    ))
                                watchlist_added += 1
                        else:
                            # Trend passes but fundamentals fail
                            fund_fails = [rid.replace("fundamental_","") for rid, r in fund_results.items() if not r.passed]
                            with get_db() as db:
                                db.add(AuditLog(
                                    action=AuditAction.SCREENER_TICKER,
                                    organization_id=org.id,
                                    ticker=ticker,
                                    message=f"🟡 FAIL fundamentals — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total} | failed: {', '.join(fund_fails[:4])}",
                                    detail={"result": "fail_fundamentals", "rules": rule_summary},
                                ))
                    elif trend_passed >= 6:
                        # Partial trend pass (≥6) → watchlist
                        trend_fails = [rid.replace("trend_","") for rid, r in trend_results.items() if not r.passed]
                        with get_db() as db:
                            _upsert_watchlist(ticker, trend_results, db, organization_id=org.id)
                            db.add(AuditLog(
                                action=AuditAction.SCREENER_TICKER,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"🔵 WATCHLIST — trend {trend_passed}/{trend_total} (partial) | failed: {', '.join(trend_fails)}",
                                detail={"result": "watchlist_partial_trend", "rules": rule_summary},
                            ))
                        watchlist_added += 1
                    else:
                        # Trend fails badly → log which rules failed
                        trend_fails = [rid.replace("trend_","") for rid, r in trend_results.items() if not r.passed]
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.SCREENER_TICKER,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"🔴 FAIL trend {trend_passed}/{trend_total} | failed: {', '.join(trend_fails)}",
                                detail={"result": "fail_trend", "rules": rule_summary},
                            ))

                except Exception as e:
                    logger.warning(f"Force screen error for {ticker}: {e}")
                    with get_db() as db:
                        db.add(AuditLog(
                            action=AuditAction.SCREENER_TICKER,
                            organization_id=org.id,
                            ticker=ticker,
                            message=f"⚠ ERROR — {str(e)[:80]}",
                            detail={"result": "error"},
                        ))
                    continue

            # ── Final organization summary ─────────────────────────────────────────────────────
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_RUN,
                    organization_id=org.id,
                    message=f"Force screen done: {signals_generated} signals, {watchlist_added} watchlist, {skipped_no_data} skipped (no data)",
                    detail={"mode": "force_complete", "tickers_checked": len(tickers),
                            "signals": signals_generated, "watchlist": watchlist_added},
                ))

            notifier.send(f"🔍 Force screen done for *{org.name}*: *{signals_generated} signals*, {watchlist_added} watchlist, {skipped_no_data} skipped")
            logger.info(f"Force screen complete for {org.name}: {signals_generated} signals | {watchlist_added} watchlist")

    except Exception as exc:
        logger.error(f"Force screen failed: {exc}")


def _upsert_watchlist(ticker: str, rule_results: dict, db, organization_id: int):
    """Add or update a stock on the watchlist."""
    from app.models.signal import Watchlist, WatchlistStatus
    existing = db.query(Watchlist).filter(
        Watchlist.ticker == ticker,
        Watchlist.organization_id == organization_id,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()
    if not existing:
        db.add(Watchlist(
            ticker=ticker,
            organization_id=organization_id,
            rule_results=serialize_rule_results(rule_results),
            added_by="screener",
        ))


@app.task(name="app.tasks.screening.screen_single_ticker", bind=True)
def screen_single_ticker(self, ticker: str, notes: str = "", organization_id: int = None):
    """Screen a single ticker immediately and add to watchlist or signals."""
    logger.info(f"Screening single ticker manually: {ticker} (Org: {organization_id})")

    try:
        with get_db() as db:
            from app.models.account import Organization
            if not organization_id:
                # Fallback to first active organization
                org = db.query(Organization).filter(Organization.is_active == True).first()
                organization_id = org.id if org else 1
            else:
                org = db.query(Organization).get(organization_id)

        if not org:
            logger.error(f"Organization ID {organization_id} not found for screen_single_ticker")
            return

        engine = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = WhatsAppNotifier(organization_id=organization_id)
        today = date.today()

        # 1. Ensure stock exists in Stock table
        with get_db() as db:
            asx_code = ticker.replace(".AX", "")
            stock = db.query(Stock).filter(Stock.ticker == ticker).first()
            if not stock:
                stock = Stock(ticker=ticker, asx_code=asx_code, in_asx200=False, is_active=True)
                db.add(stock)
                db.commit()

        # 2. Fetch price history (2y)
        df = get_price_history(ticker, period="2y")
        if df is None or len(df) < 50:
            logger.warning(f"Insufficient price history for {ticker}")
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_TICKER,
                    organization_id=organization_id,
                    ticker=ticker,
                    message=f"⚪ SKIP manual add — insufficient price data",
                    detail={"result": "skip_no_data"}
                ))
                db.commit()
            return

        # 3. Populate latest price bar in DB so stats are available
        latest_row = df.iloc[-1]
        with get_db() as db:
            existing_bar = db.query(PriceBar).filter(PriceBar.ticker == ticker, PriceBar.date == today).first()
            if not existing_bar:
                bar = PriceBar(
                    ticker=ticker,
                    date=today,
                    open=float(latest_row.get("open", 0) or 0),
                    high=float(latest_row.get("high", 0) or 0),
                    low=float(latest_row.get("low", 0) or 0),
                    close=float(latest_row.get("close", 0) or 0),
                    adj_close=float(latest_row.get("adj_close", 0) or 0),
                    volume=int(latest_row.get("volume", 0) or 0),
                    ma_50=float(latest_row.get("ma_50") or 0) or None,
                    ma_150=float(latest_row.get("ma_150") or 0) or None,
                    ma_200=float(latest_row.get("ma_200") or 0) or None,
                    high_52w=float(latest_row.get("high_52w") or 0) or None,
                    low_52w=float(latest_row.get("low_52w") or 0) or None,
                    rs_rating=float(latest_row.get("rs_rating") or 0) or None,
                )
                db.add(bar)
                db.commit()

        # 4. Fetch fundamentals (and update stock name)
        fundamentals = get_fundamentals(ticker)
        if fundamentals.get("company_name"):
            with get_db() as db:
                stock_db = db.query(Stock).filter(Stock.ticker == ticker).first()
                if stock_db:
                    stock_db.name = fundamentals["company_name"]
                    db.add(stock_db)
                    db.commit()

        # 5. Run Minervini Screener check
        # --- Trend Template ---
        trend_results = evaluate_trend_template(ticker, df, engine)
        trend_passed = sum(1 for r in trend_results.values() if r.passed)
        trend_total  = len(trend_results)

        # --- Fundamentals ---
        fund_results = evaluate_fundamentals(ticker, fundamentals, engine)
        fund_passed  = sum(1 for r in fund_results.values() if r.passed)
        fund_total   = len(fund_results)

        # --- VCP Detection ---
        avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
        vcp_result, vcp_rules = detect_vcp(ticker, df, engine, avg_vol)

        all_rule_results = {**trend_results, **fund_results, **vcp_rules}

        # If all rules pass + VCP detected, create a Signal!
        if trend_passed == trend_total and (fund_total == 0 or (fund_passed / fund_total) >= 0.75) and vcp_result.detected:
            # Full Signal!
            with get_db() as db:
                from app.models.account import Account
                account = db.query(Account).filter(
                    Account.organization_id == organization_id, 
                    Account.is_active == True
                ).first()
                capital = float(account.capital_aud) if account else 1000.0

            sizing = calculate_position_size(
                capital_aud=capital,
                entry_price=vcp_result.pivot_price,
                stop_price=vcp_result.stop_price,
                engine=engine,
            )

            signal = Signal(
                ticker=ticker,
                signal_date=today,
                organization_id=organization_id,
                status=SignalStatus.PENDING,
                close_price=float(latest_row["close"]),
                pivot_price=vcp_result.pivot_price,
                stop_price=vcp_result.stop_price,
                target_price_1=vcp_result.pivot_price * 1.20,
                target_price_2=vcp_result.pivot_price * 1.40,
                rs_rating=float(latest_row.get("rs_rating") or 0),
                trend_score=trend_passed,
                fundamental_score=fund_passed,
                rule_results=serialize_rule_results(all_rule_results),
                suggested_size_shares=sizing.shares,
                suggested_size_aud=sizing.capital_aud,
                risk_per_trade_aud=sizing.risk_aud,
                vcp_contractions=vcp_result.contraction_count,
                vcp_weeks=vcp_result.base_weeks,
                notes=notes,
            )

            with get_db() as db:
                existing_sig = db.query(Signal).filter(
                    Signal.ticker == ticker, 
                    Signal.signal_date == today,
                    Signal.organization_id == organization_id
                ).first()
                if not existing_sig:
                    db.add(signal)
                    db.add(AuditLog(
                        action=AuditAction.SCREENER_TICKER,
                        organization_id=organization_id,
                        ticker=ticker,
                        message=f"🟢 SIGNAL (Manual) — pivot ${vcp_result.pivot_price:.3f} stop ${vcp_result.stop_price:.3f}",
                        detail={"result": "signal"}
                    ))
                    db.commit()
                    notifier.send_signal_alert(signal.__dict__)
                    logger.info(f"Signal: {ticker} pivot=${vcp_result.pivot_price:.3f} (Manually screened)")
        else:
            # Put on watchlist
            with get_db() as db:
                existing_wl = db.query(Watchlist).filter(
                    Watchlist.ticker == ticker,
                    Watchlist.organization_id == organization_id,
                    Watchlist.status == WatchlistStatus.WATCHING
                ).first()

                if not existing_wl:
                    db.add(Watchlist(
                        ticker=ticker,
                        organization_id=organization_id,
                        added_date=today,
                        status=WatchlistStatus.WATCHING,
                        added_by="admin_manual",
                        notes=notes,
                        rule_results=serialize_rule_results(all_rule_results)
                    ))
                    db.add(AuditLog(
                        action=AuditAction.SCREENER_TICKER,
                        organization_id=organization_id,
                        ticker=ticker,
                        message=f"🔵 WATCHLIST (Manual) — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total}",
                        detail={"result": "watchlist"}
                    ))
                    db.commit()
                    logger.info(f"Added {ticker} to watchlist (Manually screened)")

    except Exception as e:
        logger.error(f"Manual screening failed for {ticker}: {e}")
        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.SCREENER_TICKER,
                organization_id=organization_id,
                ticker=ticker,
                message=f"⚠ ERROR manual screen — {str(e)[:80]}",
                detail={"result": "error"}
            ))
            db.commit()

