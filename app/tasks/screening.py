"""
Screening tasks — run after ASX close to generate signals for next day.
"""
from __future__ import annotations
from datetime import date, datetime
from loguru import logger
from app.utils.time_helper import get_current_date, get_current_time
from celery import shared_task

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.models.market import Stock, PriceBar
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.data.fetcher import (
    get_asx200_tickers, get_asx200_metadata, get_price_history, get_batch_prices,
    get_fundamentals, compute_rs_ratings, get_top_crypto_tickers, normalize_ticker,
)
from app.data.calendar import today_is_trading_day
from app.screener.rules import RuleEngine
from app.screener.trend_template import evaluate_trend_template
from app.screener.fundamentals import evaluate_fundamentals
from app.screener.vcp import detect_vcp
from app.screener.market_regime import evaluate_market_regime, MarketRegime
from app.screener.crypto_rules import evaluate_crypto_rules, get_crypto_fundamental_data
from app.risk.manager import calculate_position_size
from app.notifications import get_notifier

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


def _safe_float(val):
    if val is None:
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    if val is None:
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return int(f)
    except (ValueError, TypeError):
        return None




@app.task(name="app.tasks.screening.refresh_crypto_universe", bind=True, max_retries=2)
def refresh_crypto_universe(self, exchange_key: str = "CRYPTO_INDEPENDENTRESERVE"):
    """
    Bootstrap (or refresh) the crypto stock universe for a given exchange.
    Seeds the top-100 crypto tokens as Stock records so that refresh_price_data
    and the screener have something to work with.
    Called automatically by refresh_price_data when the crypto universe is empty.
    """
    logger.info(f"Refreshing crypto universe for {exchange_key}...")
    try:
        from app.data.fetcher import CRYPTO_AUD_EXCHANGES
        currency = "AUD" if exchange_key in CRYPTO_AUD_EXCHANGES else "USD"
        tickers = get_top_crypto_tickers(exchange_key)
        seeded = 0
        with get_db() as db:
            for yf_ticker in tickers:
                norm = normalize_ticker(yf_ticker, exchange_key)
                stock = db.query(Stock).filter(Stock.ticker == norm["yfinance_ticker"]).first()
                if not stock:
                    db.add(Stock(
                        ticker=norm["yfinance_ticker"],
                        exchange_code=norm["display_code"],
                        exchange_key=exchange_key,
                        asset_type="CRYPTO",
                        currency=norm["currency"],
                        in_asx200=False,
                        is_active=True,
                        name=norm["display_code"],
                        sector="Crypto",
                        industry="Digital Asset",
                    ))
                    seeded += 1
                else:
                    # Ensure exchange metadata is set
                    if not stock.exchange_key:
                        stock.exchange_key = exchange_key
                    if not stock.asset_type:
                        stock.asset_type = "CRYPTO"
                    if not stock.currency:
                        stock.currency = currency

            db.add(AuditLog(
                action=AuditAction.SYSTEM_STARTED,
                message=f"[{exchange_key}] Crypto universe refreshed: {seeded} new / {len(tickers)} total tokens seeded",
            ))
        logger.info(f"[{exchange_key}] Crypto universe: {seeded} new stocks seeded ({len(tickers)} total)")
    except Exception as exc:
        logger.error(f"Crypto universe refresh failed for {exchange_key}: {exc}")
        raise self.retry(exc=exc, countdown=120)


@app.task(name="app.tasks.screening.refresh_universe", bind=True, max_retries=3)
def refresh_universe(self):
    """Update ASX200 stock universe in the database, including names and sectors."""
    logger.info("Refreshing ASX universe...")
    try:
        metadata = get_asx200_metadata()
        tickers = get_asx200_tickers()
        with get_db() as db:
            for ticker in tickers:
                asx_code = ticker.replace(".AX", "")
                stock = db.query(Stock).filter(Stock.ticker == ticker).first()
                meta = metadata.get(ticker, {})
                name = meta.get("name", "")
                sector = meta.get("sector", "")

                if not stock:
                    stock = Stock(
                        ticker=ticker,
                        asx_code=asx_code,
                        in_asx200=True,
                        name=name,
                        sector=sector
                    )
                    db.add(stock)
                else:
                    stock.in_asx200 = True
                    if name and not stock.name:
                        stock.name = name
                    if sector and not stock.sector:
                        stock.sector = sector

            db.add(AuditLog(
                action=AuditAction.SYSTEM_STARTED,
                message=f"Universe refreshed: {len(tickers)} stocks",
            ))
        logger.info(f"Universe updated: {len(tickers)} ASX200 tickers")
    except Exception as exc:
        logger.error(f"Universe refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@app.task(name="app.tasks.screening.refresh_price_data", bind=True, max_retries=2)
def refresh_price_data(self, exchange_key: str = None):
    """
    Fetch and store EOD price data for all active stocks.

    Args:
        exchange_key: If specified, only refresh stocks for this exchange.
                      If None, refresh all active stocks (original behaviour).
                      "CRYPTO" refreshes all CRYPTO_* exchange stocks.
    """
    from app.data.calendar import today_is_trading_day as _is_trading

    # Crypto trades 24/7 — never skip on calendar grounds
    _is_crypto = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
    if not _is_crypto:
        check_exchange = exchange_key if exchange_key in ("ASX", "NYSE", "NASDAQ") else "ASX"
        if exchange_key not in (None,) and not _is_trading(check_exchange):
            logger.info(f"Not a trading day for {exchange_key} — skipping price refresh")
            return

    logger.info(f"Refreshing price data{f' for {exchange_key}' if exchange_key else ' (all exchanges)'}...")
    try:
        with get_db() as db:
            query = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False)
            if exchange_key:
                if exchange_key == "CRYPTO":
                    # All crypto exchanges
                    query = query.filter(Stock.asset_type == "CRYPTO")
                elif exchange_key == "US":
                    query = query.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
                else:
                    query = query.filter(Stock.exchange_key == exchange_key)
            tickers = [s.ticker for s in query.all()]

        if not tickers:
            # Auto-bootstrap crypto universe if this is a crypto exchange refresh
            is_crypto_key = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
            if is_crypto_key:
                logger.warning(f"No crypto stocks found for {exchange_key} — auto-bootstrapping universe...")
                effective_key = exchange_key if exchange_key != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
                refresh_crypto_universe(exchange_key=effective_key)
                # Re-query after bootstrap
                with get_db() as db:
                    query = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False, Stock.asset_type == "CRYPTO")
                    tickers = [s.ticker for s in query.all()]
            if not tickers:
                with get_db() as db:
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        message=f"Price refresh aborted: no stocks found for exchange_key={exchange_key!r}. Run universe setup first.",
                    ))
                logger.warning(f"No tickers found for {exchange_key!r} after bootstrap attempt — aborting refresh")
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
        today = get_current_date()
        with get_db() as db:
            for ticker, df in all_prices.items():
                if df is None or df.empty:
                    continue
                latest = df.iloc[-1]
                bar_date_str = str(latest["date"])
                # For crypto (24/7), accept yesterday's bar too (yfinance lag).
                # For equities, require today's date to avoid storing stale data.
                if not _is_crypto and bar_date_str != str(today):
                    continue

                bar_date = latest["date"] if hasattr(latest["date"], "year") else today

                # Upsert price bar
                existing = db.query(PriceBar).filter(
                    PriceBar.ticker == ticker,
                    PriceBar.date == bar_date,
                ).first()
                if not existing:
                    bar = PriceBar(ticker=ticker, date=bar_date)
                    db.add(bar)
                else:
                    bar = existing

                bar.open      = _safe_float(latest.get("open"))
                bar.high      = _safe_float(latest.get("high"))
                bar.low       = _safe_float(latest.get("low"))
                bar.close     = _safe_float(latest.get("close"))
                bar.adj_close = _safe_float(latest.get("adj_close"))
                bar.volume    = _safe_int(latest.get("volume"))
                bar.ma_10     = _safe_float(latest.get("ma_10"))
                bar.ma_21     = _safe_float(latest.get("ma_21"))
                bar.ma_50     = _safe_float(latest.get("ma_50"))
                bar.ma_150    = _safe_float(latest.get("ma_150"))
                bar.ma_200    = _safe_float(latest.get("ma_200"))
                bar.ma_200_prev = _safe_float(latest.get("ma_200_prev"))
                bar.avg_vol_50 = _safe_float(latest.get("avg_vol_50"))
                bar.vol_ratio = _safe_float(latest.get("vol_ratio"))
                bar.high_52w  = _safe_float(latest.get("high_52w"))
                bar.low_52w   = _safe_float(latest.get("low_52w"))
                bar.pct_from_52w_high = _safe_float(latest.get("pct_from_52w_high"))
                bar.pct_from_52w_low  = _safe_float(latest.get("pct_from_52w_low"))
                bar.atr_14    = _safe_float(latest.get("atr_14"))
                bar.rs_rating = _safe_float(rs_ratings.get(ticker))

            
            db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                message=f"[{exchange_key or 'ALL'}] Price data refreshed for {len(all_prices)} stocks",
            ))

        logger.info(f"Price data refreshed for {len(all_prices)} stocks")

    except Exception as exc:
        logger.error(f"Price refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=600)


@app.task(name="app.tasks.screening.evaluate_market_regime_task", bind=True)
def evaluate_market_regime_task(self, exchange_key: str = "ASX"):
    """
    Evaluate market regime for a specific exchange and store the result.

    Writes to:
      - MarketRegimeRecord table (structured history, one row per evaluation)
      - SystemConfig key 'last_market_regime_{exchange_key}' per org (for dashboard display)
      - Global 'last_market_regime' key for backward compat (ASX only)

    Args:
        exchange_key: "ASX", "NYSE", "NASDAQ", or "CRYPTO_*"
    """
    import pandas as pd
    from app.data.fetcher import EXCHANGE_BENCHMARKS
    from app.models.exchange import MarketRegimeRecord

    logger.info(f"Evaluating market regime for {exchange_key}...")
    try:
        benchmark = EXCHANGE_BENCHMARKS.get(exchange_key, "^GSPC")
        index_df = get_price_history(benchmark, period="1y")
        if index_df is None:
            logger.warning(f"Could not fetch benchmark {benchmark} for regime check ({exchange_key})")
            return

        today = get_current_date()
        with get_db() as db:
            if exchange_key == "CRYPTO":
                bars = db.query(PriceBar).filter(
                    PriceBar.date == today,
                    PriceBar.exchange_key.like("CRYPTO_%")
                ).all()
            elif exchange_key == "US":
                bars = db.query(PriceBar).filter(
                    PriceBar.date == today,
                    PriceBar.exchange_key.in_(["NYSE", "NASDAQ"])
                ).all()
            else:
                bars = db.query(PriceBar).filter(
                    PriceBar.date == today,
                    PriceBar.exchange_key == exchange_key
                ).all()
            universe_data = [
                {"ticker": b.ticker, "close": float(b.close or 0), "ma_200": float(b.ma_200 or 0)}
                for b in bars if b.close and b.ma_200
            ]
        universe_df = pd.DataFrame(universe_data) if universe_data else pd.DataFrame(columns=["ticker","close","ma_200"])

        engine = RuleEngine()
        regime, rule_results = evaluate_market_regime(index_df, universe_df, engine, exchange_key=exchange_key)

        latest_idx = index_df.iloc[-1]
        index_close = float(latest_idx.get("close", 0))
        index_ma200 = float(index_df["close"].tail(200).mean()) if len(index_df) >= 200 else 0
        breadth_pct = None
        if not universe_df.empty:
            valid = universe_df.dropna(subset=["close","ma_200"])
            if len(valid) > 0:
                breadth_pct = float((valid["close"] > valid["ma_200"]).sum() / len(valid) * 100)

        serialized_rules = {
            r: {"passed": bool(v.passed), "value": v.value.item() if hasattr(v.value, "item") else v.value}
            for r, v in rule_results.items()
        }

        # Write MarketRegimeRecord (global — no org scope for shared regime data)
        with get_db() as db:
            db.add(MarketRegimeRecord(
                exchange_key=exchange_key,
                organization_id=None,
                regime=regime.value,
                evaluated_at=get_current_time() if callable(get_current_time) else __import__("datetime").datetime.utcnow(),
                index_close=index_close,
                index_ma200=index_ma200,
                breadth_pct=breadth_pct,
                rule_results=serialized_rules,
            ))

            # Backward-compat: global 'last_market_regime' key (ASX only)
            if exchange_key == "ASX":
                for key, value in [
                    ("last_market_regime", regime.value),
                    ("last_regime_check", str(today)),
                ]:
                    cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == key, SystemConfig.organization_id == None
                    ).first()
                    if cfg:
                        cfg.value = value
                    else:
                        db.add(SystemConfig(key=key, value=value, label=key))

            db.add(AuditLog(
                action=AuditAction.MARKET_REGIME_CHANGE,
                message=f"[{exchange_key}] Market regime: {regime.value}",
                detail=serialized_rules,
            ))

        # Update per-org SystemConfig key for dashboard display
        from app.models.account import Organization
        with get_db() as db:
            orgs = db.query(Organization).filter(Organization.is_active == True).all()

        for org in orgs:
            # Update per-exchange regime key for this org
            with get_db() as db:
                cfg_key = f"last_market_regime_{exchange_key}"
                cfg = db.query(SystemConfig).filter(
                    SystemConfig.key == cfg_key,
                    SystemConfig.organization_id == org.id
                ).first()
                if cfg:
                    cfg.value = regime.value
                else:
                    db.add(SystemConfig(
                        key=cfg_key, value=regime.value,
                        label=f"{exchange_key} Market Regime",
                        organization_id=org.id, group="system"
                    ))

            # Notification
            try:
                notifier = get_notifier(organization_id=org.id)
                notifier.send(f"📊 [{exchange_key}] Market regime: *{regime.value}*")
            except Exception as org_err:
                logger.error(f"Failed to notify Org {org.name} of {exchange_key} regime: {org_err}")

        logger.info(f"[{exchange_key}] Market regime evaluated: {regime}")

    except Exception as exc:
        logger.error(f"[{exchange_key}] Regime evaluation failed: {exc}")


@app.task(name="app.tasks.screening.run_daily_screen", bind=True, max_retries=2)
def run_daily_screen(self, exchange_key: str = "ASX"):
    """
    Main Minervini screening task. Runs all enabled rules against the full universe.
    Auto-bootstraps the universe if the stocks table is empty (first run).
    """
    if exchange_key != "CRYPTO" and not today_is_trading_day(exchange_key):
        logger.info(f"Not a trading day for {exchange_key} — skipping screener")
        return

    logger.info(f"Starting Minervini daily screen for {exchange_key}...")
    today    = get_current_date()

    try:
        with get_db() as db:
            from app.models.account import Organization
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            
            stock_query = db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            )
            if exchange_key:
                if exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"):
                    stock_query = stock_query.filter(Stock.asset_type == "CRYPTO")
                else:
                    stock_query = stock_query.filter(Stock.exchange_key == exchange_key)
            tickers = [s.ticker for s in stock_query.all()]

            # Pre-load stocks map
            stocks_map = {s.ticker: s for s in db.query(Stock).filter(Stock.is_active == True).all()}

        # ── Auto-bootstrap: if universe is empty, fetch it now ────────────
        if not tickers and exchange_key == "ASX":
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
            engine        = RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type="EQUITY")
            engine_crypto = RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type="CRYPTO")
            notifier = get_notifier(organization_id=org.id)

            signals_generated = 0
            watchlist_added   = 0

            for ticker in tickers:
                try:
                    # Fetch price history
                    df = get_price_history(ticker, period="2y")
                    if df is None or len(df) < 200:
                        continue

                    # Select engine based on asset type
                    stock_obj = stocks_map.get(ticker)
                    ticker_engine = engine_crypto if (stock_obj and stock_obj.asset_type == "CRYPTO") else engine

                    # --- Trend Template ---
                    trend_results = evaluate_trend_template(ticker, df, ticker_engine)
                    trend_passed = sum(1 for r in trend_results.values() if r.passed)
                    trend_total  = len(trend_results)

                    # Must pass ALL trend template rules to proceed
                    if trend_passed < trend_total:
                        # Add to watchlist if ≥ 6/8 criteria met
                        if trend_passed >= 6:
                            with get_db() as db:
                                _upsert_watchlist(ticker, trend_results, db, organization_id=org.id)
                            watchlist_added += 1
                        else:
                            with get_db() as db:
                                _update_watchlist_if_exists(ticker, trend_results, db, organization_id=org.id)
                        continue


                    # --- Fundamentals ---
                    # (stock_obj and ticker_engine already set above)
                    asset_type = stock_obj.asset_type if stock_obj else "EQUITY"
                    if asset_type == "CRYPTO":
                        fundamentals = {
                            "company_name": "", "sector": "Crypto", "industry": "Digital Asset",
                            "eps_quarterly": [], "revenue_quarterly": [], "roe": None,
                            "net_margin": None, "inst_ownership_pct": None, "next_earnings_date": None,
                        }
                    else:
                        fundamentals = get_fundamentals(ticker)

                    # Persist company name/sector to Stock row (free — info already fetched)
                    if fundamentals.get("company_name"):
                        with get_db() as _db:
                            _stk = _db.query(Stock).filter(Stock.ticker == ticker).first()
                            if _stk:
                                _stk.name     = fundamentals["company_name"]
                                _stk.sector   = fundamentals.get("sector") or _stk.sector
                                _stk.industry = fundamentals.get("industry") or _stk.industry
                    fund_results = evaluate_fundamentals(ticker, fundamentals, ticker_engine)
                    fund_passed  = sum(1 for r in fund_results.values() if r.passed)
                    fund_total   = len(fund_results)

                    # Must pass ≥ 75% of fundamental rules
                    if fund_total > 0 and (fund_passed / fund_total) < 0.75:
                        with get_db() as db:
                            _upsert_watchlist(ticker, {**trend_results, **fund_results}, db, organization_id=org.id)
                        watchlist_added += 1
                        continue


                    # --- VCP Detection ---
                    avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
                    vcp_result, vcp_rules = detect_vcp(ticker, df, ticker_engine, avg_vol)
                    if not vcp_result.detected:
                        with get_db() as db:
                            _upsert_watchlist(ticker, {**trend_results, **fund_results, **vcp_rules}, db, organization_id=org.id)
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

                    # Retrieve working capital currency from SystemConfig
                    from app.models.config import SystemConfig
                    currency = stock_obj.currency if (stock_obj and stock_obj.currency) else ("USD" if asset_type == "CRYPTO" or exchange_key in ("NYSE", "NASDAQ") else "AUD")
                    with get_db() as db_cfg:
                        currency_cfg = db_cfg.query(SystemConfig).filter(
                            SystemConfig.key == "working_capital_currency",
                            SystemConfig.organization_id == org.id
                        ).first()
                        base_currency = currency_cfg.value if currency_cfg else "AUD"

                    sizing = calculate_position_size(
                        capital_aud=capital,
                        entry_price=vcp_result.pivot_price,
                        stop_price=vcp_result.stop_price,
                        engine=ticker_engine,
                        currency=currency,
                        base_currency=base_currency,
                        is_crypto=(asset_type == "CRYPTO"),
                    )

                    all_rule_results = {**trend_results, **fund_results, **vcp_rules}

                    signal = Signal(
                        ticker=ticker,
                        exchange_key=exchange_key,
                        asset_type=asset_type,
                        currency=currency,
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
                        notes="[VCP Screener]",
                    )

                    with get_db() as db3:
                        from app.models.trade import Position, TradeStatus
                        # Dedup: skip if active signal or open position already exists
                        existing = db3.query(Signal).filter(
                            Signal.ticker == ticker,
                            Signal.organization_id == org.id,
                            Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
                        ).first()
                        open_pos = db3.query(Position).filter(
                            Position.ticker == ticker,
                            Position.organization_id == org.id,
                            Position.status == TradeStatus.OPEN,
                        ).first()
                        if not existing and not open_pos:
                            db3.add(signal)
                            # Promote watchlist entry to SIGNALLED so it doesn't show as duplicate
                            wl = db3.query(Watchlist).filter(
                                Watchlist.ticker == ticker,
                                Watchlist.organization_id == org.id,
                                Watchlist.status == WatchlistStatus.WATCHING,
                            ).first()
                            if wl:
                                wl.status = WatchlistStatus.SIGNALLED
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
                    message=f"[{exchange_key or 'ASX'}] Screen complete: {signals_generated} signals, {watchlist_added} watchlist",
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
            notifier = get_notifier(organization_id=org.id)
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
def _run_screen_force(self, organization_id: int = None, exchange_key: str = "ASX"):
    """
    Full Minervini screen bypassing the trading-day gate. For manual triggers.
    When organization_id is provided, runs ONLY for that org (manual dashboard trigger).
    When None (scheduled or superadmin), runs for all active orgs.
    Writes a SCREENER_TICKER audit row per stock so the Task Log shows live progress.
    """
    logger.info(f"Running forced screen [{exchange_key}] (bypassing trading-day check)...")
    today    = get_current_date()

    try:
        with get_db() as db:
            from app.models.account import Organization
            org_query = db.query(Organization).filter(Organization.is_active == True)
            if organization_id:
                org_query = org_query.filter(Organization.id == organization_id)
            orgs = org_query.all()

            stock_query = db.query(Stock).filter(
                Stock.is_active == True, Stock.blacklisted == False
            )
            # Normalise: any CRYPTO_* key (e.g. CRYPTO_BINANCE) → filter by asset_type
            is_crypto_key = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
            if exchange_key:
                if is_crypto_key:
                    stock_query = stock_query.filter(Stock.asset_type == "CRYPTO")
                elif exchange_key == "US":
                    stock_query = stock_query.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
                else:
                    stock_query = stock_query.filter(Stock.exchange_key == exchange_key)
            tickers = [s.ticker for s in stock_query.all()]

            # Pre-load stocks map
            stocks_map = {s.ticker: s for s in db.query(Stock).filter(Stock.is_active == True).all()}

        # Auto-bootstrap ASX universe only — never bootstrap on crypto/US keys
        if not tickers and exchange_key in (None, "ASX"):
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
        elif not tickers:
            logger.warning(f"No stocks found for exchange_key={exchange_key!r} — aborting screen (nothing to do)")

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
            # Create separate engines per asset type so crypto-only rules are applied correctly
            engine        = RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type="EQUITY")
            engine_crypto = RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type="CRYPTO")
            notifier = get_notifier(organization_id=org.id)
            
            signals_generated = 0
            watchlist_added   = 0
            skipped_no_data   = 0

            # Log start per organization
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_RUN,
                    organization_id=org.id,
                    message=f"[{exchange_key or 'ASX'}] Force screen started: {len(tickers)} stocks to check",
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

                    # Pick the right rule engine based on the asset's type
                    stock_obj  = stocks_map.get(ticker)
                    asset_type = stock_obj.asset_type if stock_obj else "EQUITY"
                    ticker_engine = engine_crypto if asset_type == "CRYPTO" else engine

                    # ── Trend Template ──────────────────────────────────────────
                    trend_results = evaluate_trend_template(ticker, df, ticker_engine)
                    trend_passed  = sum(1 for r in trend_results.values() if r.passed)
                    trend_total   = len(trend_results)

                    # Build human-readable rule breakdown for the audit log
                    rule_summary = []
                    for rid, r in trend_results.items():
                        icon = "✓" if r.passed else "✗"
                        rule_summary.append(f"{icon} {rid.replace('trend_','')}: {r.message or ''}")

                    if trend_passed == trend_total:
                        # ── All trend rules pass → run fundamentals ─────────────
                        # (asset_type and stock_obj already set above for engine selection)
                        if asset_type == "CRYPTO":
                            fundamentals = {
                                "company_name": "", "sector": "Crypto", "industry": "Digital Asset",
                                "eps_quarterly": [], "revenue_quarterly": [], "roe": None,
                                "net_margin": None, "inst_ownership_pct": None, "next_earnings_date": None,
                            }
                        else:
                            fundamentals = get_fundamentals(ticker)

                        # Persist company name/sector to Stock row (free — info already fetched)
                        if fundamentals.get("company_name"):
                            with get_db() as _db:
                                _stk = _db.query(Stock).filter(Stock.ticker == ticker).first()
                                if _stk:
                                    _stk.name     = fundamentals["company_name"]
                                    _stk.sector   = fundamentals.get("sector") or _stk.sector
                                    _stk.industry = fundamentals.get("industry") or _stk.industry
                        fund_results = evaluate_fundamentals(ticker, fundamentals, ticker_engine)
                        fund_passed  = sum(1 for r in fund_results.values() if r.passed)
                        fund_total   = len(fund_results)

                        for rid, r in fund_results.items():
                            icon = "✓" if r.passed else "✗"
                            rule_summary.append(f"{icon} {rid.replace('fundamental_','')}: {r.message or ''}")

                        if fund_total == 0 or (fund_passed / fund_total) >= 0.75:
                            # ── Fundamentals pass → VCP check ───────────────────
                            avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
                            vcp_result, vcp_rules = detect_vcp(ticker, df, ticker_engine, avg_vol)

                            for rid, r in vcp_rules.items():
                                icon = "✓" if r.passed else "✗"
                                rule_summary.append(f"{icon} {rid.replace('vcp_','')}: {r.message or ''}")

                            # ── Crypto-specific rules ─────────────────────────────
                            crypto_rule_results: dict = {}
                            if asset_type == "CRYPTO":
                                from app.screener.crypto_rules import evaluate_crypto_rules, get_crypto_fundamental_data
                                crypto_data = get_crypto_fundamental_data(ticker)
                                crypto_rule_results = evaluate_crypto_rules(
                                    ticker=ticker, df=df, engine=ticker_engine,
                                    market_cap_usd=crypto_data.get("market_cap_usd"),
                                    volume_24h_usd=crypto_data.get("volume_24h_usd"),
                                    btc_df=None,
                                )
                                for rid, r in crypto_rule_results.items():
                                    icon = "✓" if r.passed else "✗"
                                    rule_summary.append(f"{icon} {rid.replace('crypto_','')}: {r.message or ''}")
                            crypto_passed = sum(1 for r in crypto_rule_results.values() if r.passed)
                            crypto_total  = len(crypto_rule_results)
                            crypto_ok = (crypto_total == 0) or (crypto_passed == crypto_total)

                            if vcp_result.detected and crypto_ok:
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

                                # Retrieve working capital currency from SystemConfig
                                from app.models.config import SystemConfig
                                currency = stock_obj.currency if (stock_obj and stock_obj.currency) else ("USD" if asset_type == "CRYPTO" or exchange_key in ("NYSE", "NASDAQ") else "AUD")
                                with get_db() as db_cfg:
                                    currency_cfg = db_cfg.query(SystemConfig).filter(
                                        SystemConfig.key == "working_capital_currency",
                                        SystemConfig.organization_id == org.id
                                    ).first()
                                    base_currency = currency_cfg.value if currency_cfg else "AUD"

                                sizing = calculate_position_size(
                                    capital_aud=capital,
                                    entry_price=vcp_result.pivot_price,
                                    stop_price=vcp_result.stop_price,
                                    engine=ticker_engine,
                                    currency=currency,
                                    base_currency=base_currency,
                                    is_crypto=(asset_type == "CRYPTO"),
                                )

                                # Use stock's actual exchange_key for the signal (not the generic "CRYPTO" sweep key)
                                signal_exchange_key = (stock_obj.exchange_key if stock_obj and stock_obj.exchange_key else exchange_key) or exchange_key

                                all_rule_results = {**trend_results, **fund_results, **vcp_rules, **crypto_rule_results}
                                signal = Signal(
                                    ticker=ticker,
                                    exchange_key=signal_exchange_key,
                                    asset_type=asset_type,
                                    currency=currency,
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
                                    notes="[VCP Screener]",
                                )

                                with get_db() as db3:
                                    from app.models.trade import Position, TradeStatus
                                    # Dedup: skip if active signal or open position already exists
                                    existing = db3.query(Signal).filter(
                                        Signal.ticker == ticker,
                                        Signal.organization_id == org.id,
                                        Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
                                    ).first()
                                    open_pos = db3.query(Position).filter(
                                        Position.ticker == ticker,
                                        Position.organization_id == org.id,
                                        Position.status == TradeStatus.OPEN,
                                    ).first()
                                    if not existing and not open_pos:
                                        db3.add(signal)
                                        # Promote watchlist entry to SIGNALLED
                                        wl = db3.query(Watchlist).filter(
                                            Watchlist.ticker == ticker,
                                            Watchlist.organization_id == org.id,
                                            Watchlist.status == WatchlistStatus.WATCHING,
                                        ).first()
                                        if wl:
                                            wl.status = WatchlistStatus.SIGNALLED
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
                                # Trend + fundamentals pass but no VCP / crypto gate → watchlist
                                reason = f"VCP not detected ({vcp_result.contraction_count or 0} contractions)" if not vcp_result.detected else f"Crypto rules failed ({crypto_passed}/{crypto_total})"
                                with get_db() as db:
                                    _upsert_watchlist(ticker, {**trend_results, **fund_results, **vcp_rules, **crypto_rule_results}, db, organization_id=org.id)
                                    db.add(AuditLog(
                                        action=AuditAction.SCREENER_TICKER,
                                        organization_id=org.id,
                                        ticker=ticker,
                                        message=f"🔵 WATCHLIST — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total}" + (f" crypto {crypto_passed}/{crypto_total}" if asset_type == "CRYPTO" else "") + f" | {reason}",
                                        detail={"result": "watchlist", "reason": reason, "rules": rule_summary},
                                    ))
                                watchlist_added += 1
                        else:
                            # Trend passes but fundamentals fail
                            fund_fails = [rid.replace("fundamental_","") for rid, r in fund_results.items() if not r.passed]
                            with get_db() as db:
                                _upsert_watchlist(ticker, {**trend_results, **fund_results}, db, organization_id=org.id)
                                db.add(AuditLog(
                                    action=AuditAction.SCREENER_TICKER,
                                    organization_id=org.id,
                                    ticker=ticker,
                                    message=f"🟡 FAIL fundamentals — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total} | failed: {', '.join(fund_fails[:4])}",
                                    detail={"result": "fail_fundamentals", "rules": rule_summary},
                                ))
                            watchlist_added += 1

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
                            _update_watchlist_if_exists(ticker, trend_results, db, organization_id=org.id)
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
                    message=f"[{exchange_key or 'ASX'}] Force screen done: {signals_generated} signals, {watchlist_added} watchlist, {skipped_no_data} skipped (no data)",
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
    from app.models.market import Stock

    # Query stock metadata to avoid default ASX/EQUITY/AUD values for non-ASX assets
    stock = db.query(Stock).filter(Stock.ticker == ticker).first()
    exchange_key = stock.exchange_key if stock else "ASX"
    asset_type = stock.asset_type if stock else "EQUITY"
    currency = stock.currency if stock else "AUD"

    existing = db.query(Watchlist).filter(
        Watchlist.ticker == ticker,
        Watchlist.organization_id == organization_id,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()
    if not existing:
        db.add(Watchlist(
            ticker=ticker,
            exchange_key=exchange_key,
            asset_type=asset_type,
            currency=currency,
            organization_id=organization_id,
            rule_results=serialize_rule_results(rule_results),
            added_by="screener",
        ))
    else:
        existing.rule_results = serialize_rule_results(rule_results)
        # Ensure metadata is updated if it was previously default
        existing.exchange_key = exchange_key
        existing.asset_type = asset_type
        existing.currency = currency


def _update_watchlist_if_exists(ticker: str, rule_results: dict, db, organization_id: int):
    """Update a stock on the watchlist only if it already exists."""
    from app.models.signal import Watchlist, WatchlistStatus
    existing = db.query(Watchlist).filter(
        Watchlist.ticker == ticker,
        Watchlist.organization_id == organization_id,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()
    if existing:
        existing.rule_results = serialize_rule_results(rule_results)



@app.task(name="app.tasks.screening.screen_single_ticker", bind=True)
def screen_single_ticker(
    self,
    ticker: str,                  # yfinance canonical format: "BHP.AX", "AAPL", "BTC-USD"
    notes: str = "",
    organization_id: int = None,
    label_id: int = None,
    exchange_key: str = "ASX",    # "ASX", "NYSE", "NASDAQ", "CRYPTO_BINANCE", etc.
    asset_type: str = "EQUITY",   # "EQUITY" | "CRYPTO"
    currency: str = "AUD",        # Native price currency
):
    """
    Screen a single ticker on-demand and add to watchlist or signals.
    Called when a user manually adds a stock/crypto from the UI.

    For non-ASX200 tickers this is the ONLY way data gets fetched — there is
    no scheduled batch fetch for custom watchlist items until they appear here.

    The function:
      1. Creates/updates the Stock record with exchange metadata
      2. Fetches 2 years of price history via yfinance
      3. Stores price bars in the central price_bars table
      4. Runs Minervini rules (trend template + fundamentals + VCP)
         - Crypto assets skip the fundamentals check
      5. Creates a Signal (full pass) or Watchlist entry (partial pass)
    """
    logger.info(f"Screening single ticker manually: {ticker} [{exchange_key}] (Org: {organization_id})")

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

        engine = RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type=asset_type)
        notifier = get_notifier(organization_id=organization_id)
        today = get_current_date()

        # 1. Ensure stock exists in central Stock table (shared across orgs)
        with get_db() as db:
            from app.data.fetcher import normalize_ticker
            norm = normalize_ticker(ticker, exchange_key)
            yf_ticker    = norm["yfinance_ticker"]
            display_code = norm["display_code"]
            currency     = norm.get("currency", currency)
            asset_type   = norm.get("asset_type", asset_type)

            # Use yf_ticker as canonical key
            ticker = yf_ticker

            stock = db.query(Stock).filter(Stock.ticker == ticker).first()
            if not stock:
                stock = Stock(
                    ticker=ticker,
                    exchange_code=display_code,
                    asx_code=display_code if exchange_key == "ASX" else None,
                    exchange_key=exchange_key,
                    asset_type=asset_type,
                    currency=currency,
                    in_asx200=False,
                    is_active=True,
                )
                db.add(stock)
                db.commit()
            else:
                # Update exchange metadata if missing
                if not stock.exchange_key or stock.exchange_key == "ASX":
                    stock.exchange_key = exchange_key
                if not stock.exchange_code:
                    stock.exchange_code = display_code
                if not stock.currency:
                    stock.currency = currency
                if not stock.asset_type:
                    stock.asset_type = asset_type
                db.commit()

        # 2. Fetch price history (2y) — same yfinance call works for all exchanges
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

        # 3. Populate latest price bar in central DB (shared across orgs)
        latest_row = df.iloc[-1]
        with get_db() as db:
            bar = db.query(PriceBar).filter(PriceBar.ticker == ticker, PriceBar.date == today).first()
            if not bar:
                bar = PriceBar(ticker=ticker, date=today, exchange_key=exchange_key)
                db.add(bar)

            bar.open      = _safe_float(latest_row.get("open"))
            bar.high      = _safe_float(latest_row.get("high"))
            bar.low       = _safe_float(latest_row.get("low"))
            bar.close     = _safe_float(latest_row.get("close"))
            bar.adj_close = _safe_float(latest_row.get("adj_close"))
            bar.volume    = _safe_int(latest_row.get("volume"))
            bar.ma_10     = _safe_float(latest_row.get("ma_10"))
            bar.ma_21     = _safe_float(latest_row.get("ma_21"))
            bar.ma_50     = _safe_float(latest_row.get("ma_50"))
            bar.ma_150    = _safe_float(latest_row.get("ma_150"))
            bar.ma_200    = _safe_float(latest_row.get("ma_200"))
            bar.ma_200_prev = _safe_float(latest_row.get("ma_200_prev"))
            bar.avg_vol_50 = _safe_float(latest_row.get("avg_vol_50"))
            bar.vol_ratio = _safe_float(latest_row.get("vol_ratio"))
            bar.high_52w  = _safe_float(latest_row.get("high_52w"))
            bar.low_52w   = _safe_float(latest_row.get("low_52w"))
            bar.pct_from_52w_high = _safe_float(latest_row.get("pct_from_52w_high"))
            bar.pct_from_52w_low  = _safe_float(latest_row.get("pct_from_52w_low"))
            bar.atr_14    = _safe_float(latest_row.get("atr_14"))
            
            rs_val = _safe_float(latest_row.get("rs_rating"))
            if rs_val is not None:
                bar.rs_rating = rs_val

            db.commit()

        # 4. Fetch fundamentals (skip for crypto assets — no earnings data)
        fundamentals = get_fundamentals(ticker) if asset_type == "EQUITY" else {
            "company_name": "", "sector": "Crypto", "industry": "Digital Asset",
            "eps_quarterly": [], "revenue_quarterly": [], "roe": None,
            "net_margin": None, "inst_ownership_pct": None, "next_earnings_date": None,
        }
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

        # --- Fundamentals (EQUITY only — empty dict for CRYPTO) ---
        fund_results = evaluate_fundamentals(ticker, fundamentals, engine)
        fund_passed  = sum(1 for r in fund_results.values() if r.passed)
        fund_total   = len(fund_results)

        # --- VCP Detection ---
        avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)
        vcp_result, vcp_rules = detect_vcp(ticker, df, engine, avg_vol)

        # --- Crypto-specific rules (CRYPTO only) ---
        crypto_rule_results: dict = {}
        if asset_type == "CRYPTO":
            crypto_data = get_crypto_fundamental_data(ticker)
            crypto_rule_results = evaluate_crypto_rules(
                ticker=ticker,
                df=df,
                engine=engine,
                market_cap_usd=crypto_data.get("market_cap_usd"),
                volume_24h_usd=crypto_data.get("volume_24h_usd"),
                btc_df=None,  # BTC regime self-checks; non-BTC assets skip if no BTC data
            )

        all_rule_results = {**trend_results, **fund_results, **vcp_rules, **crypto_rule_results}

        # Crypto rule gate: all enabled crypto rules must pass (or no crypto rules evaluated)
        crypto_passed = sum(1 for r in crypto_rule_results.values() if r.passed)
        crypto_total  = len(crypto_rule_results)
        crypto_ok = (crypto_total == 0) or (crypto_passed == crypto_total)

        # If all rules pass + VCP detected, create a Signal!
        if trend_passed == trend_total and (fund_total == 0 or (fund_passed / fund_total) >= 0.75) and vcp_result.detected and crypto_ok:
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
                exchange_key=exchange_key,
                asset_type=asset_type,
                currency=currency,
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
                from app.models.trade import Position, TradeStatus
                existing_sig = db.query(Signal).filter(
                    Signal.ticker == ticker,
                    Signal.organization_id == organization_id,
                    Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
                ).first()
                open_pos = db.query(Position).filter(
                    Position.ticker == ticker,
                    Position.organization_id == organization_id,
                    Position.status == TradeStatus.OPEN,
                ).first()
                if not existing_sig and not open_pos:
                    db.add(signal)
                    # Promote watchlist entry to SIGNALLED
                    wl = db.query(Watchlist).filter(
                        Watchlist.ticker == ticker,
                        Watchlist.organization_id == organization_id,
                        Watchlist.status == WatchlistStatus.WATCHING,
                    ).first()
                    if wl:
                        wl.status = WatchlistStatus.SIGNALLED
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
                        exchange_key=exchange_key,
                        asset_type=asset_type,
                        currency=currency,
                        organization_id=organization_id,
                        added_date=today,
                        status=WatchlistStatus.WATCHING,
                        added_by="admin_manual",
                        notes=notes,
                        label_id=label_id,
                        rule_results=serialize_rule_results(all_rule_results)
                    ))
                    db.add(AuditLog(
                        action=AuditAction.SCREENER_TICKER,
                        organization_id=organization_id,
                        ticker=ticker,
                        message=f"🔵 WATCHLIST (Manual) — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total}" + (f" crypto {crypto_passed}/{crypto_total}" if asset_type == "CRYPTO" else ""),
                        detail={"result": "watchlist"}
                    ))
                    db.commit()
                    logger.info(f"Added {ticker} to watchlist (Manually screened)")
                else:
                    existing_wl.rule_results = serialize_rule_results(all_rule_results)
                    existing_wl.exchange_key = exchange_key
                    existing_wl.asset_type = asset_type
                    existing_wl.currency = currency
                    if notes:
                        existing_wl.notes = notes
                    if label_id is not None:
                        existing_wl.label_id = label_id
                    db.add(AuditLog(
                        action=AuditAction.SCREENER_TICKER,
                        organization_id=organization_id,
                        ticker=ticker,
                        message=f"🔵 WATCHLIST (Manual Update) — trend {trend_passed}/{trend_total} fund {fund_passed}/{fund_total}" + (f" crypto {crypto_passed}/{crypto_total}" if asset_type == "CRYPTO" else ""),
                        detail={"result": "watchlist"}
                    ))
                    db.commit()
                    logger.info(f"Updated {ticker} on watchlist (Manually screened)")

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

