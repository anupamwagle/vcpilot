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
from app.models.market import Stock, PriceBar, StockFundamentals
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.data.fetcher import (
    get_asx200_tickers, get_asx200_metadata,
    get_asx300_tickers, get_asx300_metadata,
    get_asx_all_listed,
    get_sp500_tickers, get_sp500_metadata,
    get_nasdaq100_tickers, get_nasdaq100_metadata,
    get_price_history, get_batch_prices,
    get_fundamentals, compute_rs_ratings, get_top_crypto_tickers, normalize_ticker,
    get_stock_story,
)
from app.data.calendar import today_is_trading_day
from app.screener.rules import RuleEngine
from app.screener.trend_template import evaluate_trend_template
from app.screener.fundamentals import evaluate_fundamentals
from app.screener.vcp import detect_vcp
from app.screener.price_filter import price_in_range
from app.screener.liquidity_filter import liquidity_ok
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
def refresh_crypto_universe(self, exchange_key: str = "CRYPTO_INDEPENDENTRESERVE", organization_id: int = None):
    """
    Bootstrap (or refresh) the crypto stock universe for a given exchange.

    Seeds Stock records for all coins supported by the exchange, then deactivates
    and purges watchlist/signal entries for any coin that is no longer listed on the
    exchange — ensuring we never try to trade a coin that can't be executed.

    For IR  → exactly the ~41 AUD pairs from IR's live public API
    For MEXC → top-300 USD pairs, filtered by 'mexc_trading_pairs' SystemConfig if set
    For others → generic top-300 USD list
    """
    logger.info(f"Refreshing crypto universe for {exchange_key}...")
    try:
        from app.data.fetcher import CRYPTO_AUD_EXCHANGES
        from app.models.account import Organization
        currency = "AUD" if exchange_key in CRYPTO_AUD_EXCHANGES else "USD"
        tickers = get_top_crypto_tickers(exchange_key)

        # ── MEXC: apply user-configured pair whitelist if set ────────────────
        if exchange_key == "CRYPTO_MEXC":
            with get_db() as _db:
                pair_cfg = _db.query(SystemConfig).filter(
                    SystemConfig.key == "mexc_trading_pairs",
                    SystemConfig.organization_id == organization_id if organization_id else True,
                ).first()
                if pair_cfg and pair_cfg.value and pair_cfg.value.strip():
                    # e.g. "BTC-USD,ETH-USD,SOL-USD"
                    allowed = {p.strip().upper() for p in pair_cfg.value.split(",") if p.strip()}
                    tickers = [t for t in tickers if t.upper() in allowed]
                    logger.info(f"MEXC: filtered to {len(tickers)} configured trading pairs")

        supported_set = set(tickers)
        seeded = 0
        deactivated = 0
        purged_wl = 0

        with get_db() as db:
            # ── Seed / update supported coins ────────────────────────────────
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
                    # Ensure metadata correct and mark active
                    stock.is_active = True
                    if not stock.exchange_key:
                        stock.exchange_key = exchange_key
                    if not stock.asset_type:
                        stock.asset_type = "CRYPTO"
                    if not stock.currency:
                        stock.currency = currency

            # ── Deactivate orphaned coins (exchange_key matches but not in new list) ─
            orphaned_stocks = (
                db.query(Stock)
                .filter(
                    Stock.exchange_key == exchange_key,
                    Stock.asset_type == "CRYPTO",
                    ~Stock.ticker.in_(supported_set),
                    Stock.is_active == True,
                )
                .all()
            )
            orphaned_tickers = {s.ticker for s in orphaned_stocks}
            for s in orphaned_stocks:
                s.is_active = False
                deactivated += 1

            # ── Purge watchlist entries for orphaned tickers (per-org or all orgs) ─
            if orphaned_tickers:
                wl_query = db.query(Watchlist).filter(
                    Watchlist.ticker.in_(orphaned_tickers),
                    Watchlist.status.in_([WatchlistStatus.WATCHING, WatchlistStatus.ALERTED]),
                )
                if organization_id:
                    wl_query = wl_query.filter(Watchlist.organization_id == organization_id)
                orphaned_wl = wl_query.all()
                for wl in orphaned_wl:
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=wl.organization_id,
                        message=(
                            f"Watchlist: removed {wl.ticker} — no longer listed on {exchange_key}. "
                            f"Re-add via watchlist if the exchange re-lists this coin."
                        ),
                    ))
                    db.delete(wl)
                    purged_wl += 1

                # Also cancel any pending signals for orphaned tickers
                from app.models.signal import SignalStatus
                orphaned_signals = db.query(Signal).filter(
                    Signal.ticker.in_(orphaned_tickers),
                    Signal.status == SignalStatus.PENDING,
                )
                if organization_id:
                    orphaned_signals = orphaned_signals.filter(Signal.organization_id == organization_id)
                for sig in orphaned_signals.all():
                    sig.status = SignalStatus.EXPIRED
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=sig.organization_id,
                        message=f"Signal: expired {sig.ticker} — coin no longer listed on {exchange_key}",
                    ))

            summary = (
                f"[{exchange_key}] Universe refreshed: {seeded} new / {len(tickers)} supported "
                f"| {deactivated} deactivated | {purged_wl} watchlist entries purged"
            )
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            for org in orgs:
                db.add(AuditLog(
                    action=AuditAction.TASK_RUN,
                    organization_id=org.id,
                    message=summary,
                ))
        logger.info(summary)
    except Exception as exc:
        logger.error(f"Crypto universe refresh failed for {exchange_key}: {exc}")
        raise self.retry(exc=exc, countdown=120)


@app.task(name="app.tasks.screening.refresh_universe", bind=True, max_retries=3)
def refresh_universe(self, scope: str = None, organization_id: int = None):
    """
    Update ASX stock universe in the database, including names and sectors.

    Args:
        scope: Universe scope — "ASX200" (default), "ASX300", "ALL_LISTED".
               If None, reads 'asx_universe_scope' from the org's SystemConfig.
        organization_id: Org to read config from (uses first active org if None).
    """
    # Resolve scope from config if not passed
    if scope is None:
        try:
            with get_db() as db:
                cfg_query = db.query(SystemConfig).filter(SystemConfig.key == "asx_universe_scope")
                if organization_id:
                    cfg_query = cfg_query.filter(SystemConfig.organization_id == organization_id)
                cfg = cfg_query.first()
                scope = (cfg.value or "ASX200") if cfg else "ASX200"
        except Exception:
            scope = "ASX200"

    scope = (scope or "ASX200").upper().strip()
    logger.info(f"Refreshing ASX universe (scope={scope})...")

    try:
        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                message=f"[ASX] Universe refresh started — scope={scope}",
                organization_id=organization_id,
            ))

        # ── Fetch data based on scope ──────────────────────────────────────
        if scope == "ASX300":
            metadata = get_asx300_metadata()   # includes in_asx200/in_asx300 flags
            tickers  = get_asx300_tickers()
        elif scope == "ALL_LISTED":
            # Full ASX listing — includes small caps
            all_rows = get_asx_all_listed()
            if not all_rows:
                logger.warning("ASX all-listed fetch returned nothing — falling back to ASX300")
                metadata = get_asx300_metadata()
                tickers  = get_asx300_tickers()
            else:
                # Build metadata dict from all-listed response
                asx300_meta = get_asx300_metadata()  # for in_asx200/in_asx300 flags
                metadata = {}
                for row in all_rows:
                    t = row["ticker"]
                    asx300_info = asx300_meta.get(t, {})
                    metadata[t] = {
                        "name":      row.get("name", ""),
                        "sector":    row.get("sector", ""),
                        "industry":  row.get("industry", ""),
                        "market_cap": row.get("market_cap"),
                        "in_asx200": asx300_info.get("in_asx200", False),
                        "in_asx300": asx300_info.get("in_asx300", False),
                    }
                tickers = list(metadata.keys())
        else:
            # Default: ASX200
            metadata = get_asx200_metadata()
            tickers  = get_asx200_tickers()

        seeded = 0
        updated = 0
        with get_db() as db:
            for ticker in tickers:
                asx_code = ticker.replace(".AX", "")
                stock = db.query(Stock).filter(Stock.ticker == ticker).first()
                meta = metadata.get(ticker, {})
                name = meta.get("name", "")
                sector = meta.get("sector", "")
                industry = meta.get("industry", "")
                market_cap = meta.get("market_cap")
                in_asx200 = meta.get("in_asx200", scope == "ASX200")
                in_asx300 = meta.get("in_asx300", scope in ("ASX200", "ASX300"))

                if not stock:
                    stock = Stock(
                        ticker=ticker,
                        exchange_code=asx_code,
                        asx_code=asx_code,
                        exchange_key="ASX",
                        asset_type="EQUITY",
                        currency="AUD",
                        in_asx200=in_asx200,
                        in_asx300=in_asx300,
                        in_index=in_asx200,
                        index_name="ASX200" if in_asx200 else ("ASX300" if in_asx300 else ""),
                        name=name,
                        sector=sector,
                        industry=industry,
                        market_cap=market_cap,
                        is_active=True,
                    )
                    db.add(stock)
                    seeded += 1
                else:
                    if in_asx200:
                        stock.in_asx200 = True
                        stock.in_index  = True
                        stock.index_name = "ASX200"
                    if in_asx300:
                        stock.in_asx300 = True
                    if not stock.exchange_code:
                        stock.exchange_code = asx_code
                    if not stock.exchange_key:
                        stock.exchange_key = "ASX"
                    if name and (not stock.name or stock.name == asx_code):
                        stock.name = name
                    if sector and not stock.sector:
                        stock.sector = sector
                    if industry and not stock.industry:
                        stock.industry = industry
                    if market_cap and not stock.market_cap:
                        stock.market_cap = market_cap
                    updated += 1

            db.add(AuditLog(
                action=AuditAction.SYSTEM_STARTED,
                organization_id=organization_id,
                message=f"[ASX] Universe refreshed ({scope}): {seeded} new + {updated} updated = {len(tickers)} total stocks",
            ))

        logger.info(f"Universe updated: {len(tickers)} tickers (scope={scope}, {seeded} new, {updated} updated)")

    except Exception as exc:
        logger.error(f"Universe refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@app.task(name="app.tasks.screening.refresh_us_universe", bind=True, max_retries=3)
def refresh_us_universe(self, scope: str = None, organization_id: int = None):
    """
    Seed / update the US equity universe (S&P 500 and/or NASDAQ-100) in the DB.

    Args:
        scope: Universe scope — "SP500", "NASDAQ100", "SP500+NASDAQ100" (default).
               If None, reads 'us_universe_scope' from the org's SystemConfig.
        organization_id: Org to read config from (uses first active org if None).

    Stock rows are tagged:
        - exchange_key = "NYSE"   for S&P 500 stocks
        - exchange_key = "NASDAQ" for NASDAQ-100 stocks
        - in_index = True, index_name = "SP500" | "NASDAQ100"
    Stocks in BOTH indices get the index they are *primarily* listed on
    (NASDAQ for NASDAQ-listed, NYSE for NYSE-listed; resolved by checking
    whether the ticker also appears in the NASDAQ-100 list).
    """
    from app.models.account import Organization

    # ── Resolve scope from org config if not explicitly passed ──────────────
    if scope is None:
        try:
            with get_db() as db:
                cfg_q = db.query(SystemConfig).filter(SystemConfig.key == "us_universe_scope")
                if organization_id:
                    cfg_q = cfg_q.filter(SystemConfig.organization_id == organization_id)
                cfg = cfg_q.first()
                scope = (cfg.value or "SP500+NASDAQ100") if cfg else "SP500+NASDAQ100"
        except Exception:
            scope = "SP500+NASDAQ100"

    scope = (scope or "SP500+NASDAQ100").strip().upper()
    logger.info(f"Refreshing US equity universe (scope={scope})...")

    try:
        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                message=f"[US] Universe refresh started — scope={scope}",
                organization_id=organization_id,
            ))

        # ── Fetch ticker lists + metadata ────────────────────────────────────
        sp500_tickers:    list[str]       = []
        sp500_meta:       dict[str, dict] = {}
        nasdaq100_tickers: list[str]      = []
        nasdaq100_meta:   dict[str, dict] = {}

        include_sp500    = scope in ("SP500",    "SP500+NASDAQ100", "ALL_US")
        include_nasdaq100 = scope in ("NASDAQ100", "SP500+NASDAQ100", "ALL_US")

        if include_sp500:
            sp500_tickers = get_sp500_tickers()
            sp500_meta    = get_sp500_metadata()
            logger.info(f"S&P 500: {len(sp500_tickers)} tickers fetched")

        if include_nasdaq100:
            nasdaq100_tickers = get_nasdaq100_tickers()
            nasdaq100_meta    = get_nasdaq100_metadata()
            logger.info(f"NASDAQ-100: {len(nasdaq100_tickers)} tickers fetched")

        # Build combined set with per-ticker metadata + index membership
        nasdaq100_set = set(nasdaq100_tickers)
        sp500_set     = set(sp500_tickers)

        # All unique tickers across both lists
        all_tickers: set[str] = set()
        if include_sp500:
            all_tickers |= sp500_set
        if include_nasdaq100:
            all_tickers |= nasdaq100_set

        seeded  = 0
        updated = 0

        with get_db() as db:
            for ticker in all_tickers:
                # Determine exchange_key: NASDAQ-listed stocks go to NASDAQ,
                # the rest go to NYSE. Use NASDAQ membership as the indicator.
                in_sp500    = ticker in sp500_set
                in_nasdaq100 = ticker in nasdaq100_set
                exchange_key = "NASDAQ" if in_nasdaq100 else "NYSE"

                # Metadata: prefer the index the stock belongs to
                if in_nasdaq100:
                    meta = nasdaq100_meta.get(ticker, sp500_meta.get(ticker, {}))
                else:
                    meta = sp500_meta.get(ticker, {})

                name     = meta.get("name", "") or ticker
                sector   = meta.get("sector", "") or ""
                industry = meta.get("industry", "") or ""

                # index_name: prefer the more prestigious / stricter index
                if in_sp500 and in_nasdaq100:
                    index_name = "NASDAQ100"
                elif in_nasdaq100:
                    index_name = "NASDAQ100"
                else:
                    index_name = "SP500"

                stock = db.query(Stock).filter(Stock.ticker == ticker).first()
                if not stock:
                    stock = Stock(
                        ticker=ticker,
                        exchange_code=ticker,   # NOT NULL — use ticker as code for US stocks
                        asx_code=None,
                        exchange_key=exchange_key,
                        asset_type="EQUITY",
                        currency="USD",
                        in_index=True,
                        index_name=index_name,
                        in_asx200=False,
                        in_asx300=False,
                        name=name,
                        sector=sector,
                        industry=industry,
                        is_active=True,
                    )
                    db.add(stock)
                    seeded += 1
                else:
                    # Update index membership flags
                    stock.in_index  = True
                    stock.index_name = index_name
                    if not stock.exchange_key or stock.exchange_key == "ASX":
                        stock.exchange_key = exchange_key
                    stock.asset_type = "EQUITY"
                    stock.currency   = "USD"
                    stock.is_active  = True
                    if name and name != ticker and (not stock.name or stock.name == ticker):
                        stock.name = name
                    if sector and not stock.sector:
                        stock.sector = sector
                    if industry and not stock.industry:
                        stock.industry = industry
                    updated += 1

            summary = (
                f"[US] Universe refreshed (scope={scope}): "
                f"{seeded} new + {updated} updated = {len(all_tickers)} total "
                f"| SP500={len(sp500_tickers)} NASDAQ100={len(nasdaq100_tickers)}"
            )
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            for org in orgs:
                db.add(AuditLog(
                    action=AuditAction.TASK_RUN,
                    organization_id=org.id,
                    message=summary,
                ))

        logger.info(summary)

    except Exception as exc:
        logger.error(f"US universe refresh failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


def _write_task_heartbeat(progress_msg: str = ""):
    """
    Write a heartbeat + optional audit entry during a long-running task.
    Prevents the health page from showing false 'offline' while the worker
    is busy with a slow task (e.g. downloading 500 stocks from yfinance).
    """
    from datetime import datetime
    from app.models.account import Organization
    now_str = datetime.utcnow().isoformat()
    try:
        with get_db() as db:
            # Global heartbeat
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_heartbeat",
                SystemConfig.organization_id == None,
            ).first()
            if cfg:
                cfg.value = now_str
            else:
                db.add(SystemConfig(
                    key="last_heartbeat", value=now_str,
                    label="Last Worker Heartbeat", group="system",
                    organization_id=None,
                ))
            # Per-org heartbeats
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            for org in orgs:
                cfg_org = db.query(SystemConfig).filter(
                    SystemConfig.key == "last_heartbeat",
                    SystemConfig.organization_id == org.id,
                ).first()
                if cfg_org:
                    cfg_org.value = now_str
                else:
                    db.add(SystemConfig(
                        key="last_heartbeat", value=now_str,
                        label="Last Worker Heartbeat", group="system",
                        organization_id=org.id,
                    ))
            if progress_msg:
                db.add(AuditLog(action=AuditAction.TASK_RUN, message=progress_msg))
    except Exception as e:
        logger.warning(f"Task heartbeat write failed (non-fatal): {e}")


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
            db.add(AuditLog(action=AuditAction.TASK_RUN,
                message=f"[{exchange_key or 'ALL'}] Price data refresh started — downloading 2yr OHLCV history..."))
        with get_db() as db:
            query = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False)
            if exchange_key:
                if exchange_key == "CRYPTO":
                    # All crypto exchanges
                    query = query.filter(Stock.asset_type == "CRYPTO")
                elif exchange_key in ("US", "NYSE", "NASDAQ"):
                    # NYSE beat task covers both NYSE and NASDAQ-100 stocks
                    query = query.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
                else:
                    query = query.filter(Stock.exchange_key == exchange_key)
            _stocks = query.all()
            tickers = [s.ticker for s in _stocks]
            # Authoritative ticker -> exchange_key map so each PriceBar is labelled
            # with its real exchange (the column default "ASX" was silently applied
            # to every ticker before, breaking per-exchange regime breadth queries).
            stock_exchange_map = {s.ticker: (s.exchange_key or "ASX") for s in _stocks}

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
                    _stocks = query.all()
                    tickers = [s.ticker for s in _stocks]
                    stock_exchange_map = {s.ticker: (s.exchange_key or "ASX") for s in _stocks}
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
        total_batches = (len(tickers) + batch_size - 1) // batch_size
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            prices = get_batch_prices(batch, period="2y")
            all_prices.update(prices)
            batch_num = i // batch_size + 1
            logger.debug(f"Fetched {len(prices)}/{len(batch)} in batch {batch_num}")
            # Heartbeat every batch — prevents false 'offline' during long downloads
            _write_task_heartbeat(
                f"[{exchange_key or 'ALL'}] Price fetch: batch {batch_num}/{total_batches} "
                f"({len(all_prices)}/{len(tickers)} stocks)"
            )

        # Compute RS ratings across all stocks
        rs_ratings = compute_rs_ratings(all_prices)

        # Store latest bar for each stock
        today = get_current_date()
        stored = 0
        skipped_empty = 0
        skipped_stale = 0
        stale_sample: list[str] = []
        with get_db() as db:
            for ticker, df in all_prices.items():
                if df is None or df.empty:
                    skipped_empty += 1
                    continue
                latest = df.iloc[-1]
                bar_date_str = str(latest["date"])
                # For crypto (24/7), accept yesterday's bar too (yfinance lag).
                # For equities, require today's date to avoid storing stale data.
                # Bug #13 fix: when exchange_key=None (global refresh), also skip the
                # date gate for individual stocks whose asset_type is CRYPTO.
                _stock_is_crypto = _is_crypto or (
                    ticker.endswith(("-AUD", "-USD", "-USDT", "-BTC", "-ETH"))
                )
                # ASX closes before AEST EOD so bar_date == AEST today.
                # NYSE/NASDAQ close at 4pm ET = 6am AEST next calendar day, so
                # yfinance returns the US session date (always 1 AEST day behind).
                # Accept today OR yesterday for anything that isn't ASX.
                from datetime import timedelta as _td
                _asx_only = exchange_key in ("ASX", None) and not _stock_is_crypto
                _acceptable_dates = {str(today), str(today - _td(days=1))} if not _asx_only else {str(today)}
                if bar_date_str not in _acceptable_dates:
                    skipped_stale += 1
                    if len(stale_sample) < 15:
                        stale_sample.append(f"{ticker}@{bar_date_str}")
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

                # Label the bar with its real exchange (fixes/backfills the old
                # default-"ASX"-for-everything bug that broke regime breadth).
                bar.exchange_key = stock_exchange_map.get(ticker) or bar.exchange_key or "ASX"
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
                stored += 1

            # Full breakdown: yfinance can return nothing for some tickers
            # (fetched < universe) and the freshness gate can reject old bars
            # (stored < fetched) — "refreshed for N stocks" alone hid both gaps.
            _unfetched = len(tickers) - len(all_prices)
            db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                message=(f"[{exchange_key or 'ALL'}] Price data refreshed: {stored}/{len(tickers)} "
                         f"bars stored ({_unfetched} no data from source, {skipped_empty} empty, "
                         f"{skipped_stale} stale/old-date skipped)"),
                detail={"universe": len(tickers), "fetched": len(all_prices), "stored": stored,
                        "no_data": _unfetched, "empty": skipped_empty,
                        "stale_date": skipped_stale, "stale_sample": stale_sample},
            ))

        logger.info(f"Price data refreshed: {stored}/{len(tickers)} bars stored "
                    f"({_unfetched} unfetched, {skipped_stale} stale)")

        # After the daily price refresh, opportunistically top up Stock Story
        # data — but only for EQUITIES, only what's stale, and capped/throttled
        # inside refresh_stock_fundamentals so we never burst yfinance. Crypto
        # has no fundamentals so it's skipped. Fire-and-forget via Celery so a
        # broker hiccup can't fail the price refresh itself.
        if not _is_crypto:
            try:
                refresh_stock_fundamentals.delay(exchange_key=exchange_key)
            except Exception as _sf_exc:
                logger.warning(f"Could not queue refresh_stock_fundamentals: {_sf_exc}")

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
        if index_df is None or len(index_df) == 0:
            logger.warning(f"Could not fetch benchmark {benchmark} for regime check ({exchange_key})")
            # Surface in the Task Log — otherwise the regime silently stays
            # stale/"Not evaluated" and the Evaluate button appears dead.
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    message=(f"[{exchange_key}] Market regime NOT evaluated: benchmark "
                             f"{benchmark} price fetch failed/empty (yfinance outage?)"),
                ))
            return

        today = get_current_date()
        from datetime import timedelta as _td
        with get_db() as db:
            # Resolve the breadth universe via the authoritative Stock table.
            # PriceBar.exchange_key was historically left at its "ASX" default for
            # every ticker, so filtering bars on PriceBar.exchange_key silently
            # returned nothing for US/crypto (and a polluted set for ASX) — which
            # zeroed out breadth and biased every equity market toward BEAR.
            stock_q = db.query(Stock.ticker).filter(
                Stock.is_active == True, Stock.blacklisted == False
            )
            if exchange_key == "CRYPTO" or (exchange_key or "").startswith("CRYPTO_"):
                stock_q = stock_q.filter(Stock.asset_type == "CRYPTO")
            elif exchange_key in ("US", "NYSE", "NASDAQ"):
                stock_q = stock_q.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
            else:
                stock_q = stock_q.filter(Stock.exchange_key == exchange_key)
            exch_tickers = [t[0] for t in stock_q.all()]

            # Use each stock's MOST RECENT bar rather than requiring an exact
            # date==today match — US/crypto bars lag AEST by a calendar day and a
            # missing same-day refresh must not collapse breadth to 0%.
            latest: dict = {}
            cutoff = today - _td(days=14)
            for _i in range(0, len(exch_tickers), 500):  # chunk to stay within SQL param limits
                chunk = exch_tickers[_i:_i + 500]
                if not chunk:
                    continue
                rows = db.query(
                    PriceBar.ticker, PriceBar.date, PriceBar.close, PriceBar.ma_200
                ).filter(
                    PriceBar.ticker.in_(chunk), PriceBar.date >= cutoff
                ).all()
                for _tk, _dt, _close, _ma200 in rows:
                    if _tk not in latest or _dt > latest[_tk][0]:
                        latest[_tk] = (_dt, _close, _ma200)
            universe_data = [
                {"ticker": _tk, "close": float(_c), "ma_200": float(_m)}
                for _tk, (_d, _c, _m) in latest.items() if _c and _m
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
            # Build list of config keys to update for this org.
            # When exchange_key="CRYPTO" (generic alias from manual trigger), also write
            # to each specific CRYPTO_* key the org has active, so the health page reads it.
            with get_db() as db:
                ae_row = db.query(SystemConfig).filter(
                    SystemConfig.key == "active_exchanges",
                    SystemConfig.organization_id == org.id
                ).first()
                ae_str = (ae_row.value if ae_row else "") or ""
                active_exc = [e.strip() for e in ae_str.split(",") if e.strip()]

            cfg_keys_to_write = [f"last_market_regime_{exchange_key}"]
            if exchange_key == "CRYPTO":
                # Also write to each specific CRYPTO_* key the org has enabled
                for aek in active_exc:
                    if aek.startswith("CRYPTO_"):
                        specific_key = f"last_market_regime_{aek}"
                        if specific_key not in cfg_keys_to_write:
                            cfg_keys_to_write.append(specific_key)
            elif exchange_key == "NYSE":
                # Bug #14 fix: NYSE and NASDAQ share the same benchmark — write both keys
                if "last_market_regime_NASDAQ" not in cfg_keys_to_write:
                    cfg_keys_to_write.append("last_market_regime_NASDAQ")

            with get_db() as db:
                for cfg_key in cfg_keys_to_write:
                    cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == cfg_key,
                        SystemConfig.organization_id == org.id
                    ).first()
                    if cfg:
                        cfg.value = regime.value
                    else:
                        db.add(SystemConfig(
                            key=cfg_key, value=regime.value,
                            label=f"{cfg_key.replace('last_market_regime_', '')} Market Regime",
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
        # Surface in the Task Log — otherwise the Evaluate Market button appears
        # to do nothing and the regime silently stays "Not evaluated"/stale.
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    message=f"[{exchange_key}] Market regime evaluation FAILED: {str(exc)[:200]}",
                ))
        except Exception:
            pass


@app.task(name="app.tasks.screening.run_daily_screen", bind=True, max_retries=2)
def run_daily_screen(self, exchange_key: str = "ASX"):
    """
    Main AstraTrade screening task. Runs all enabled rules against the full universe.
    Auto-bootstraps the universe if the stocks table is empty (first run).
    """
    if exchange_key != "CRYPTO" and not today_is_trading_day(exchange_key):
        logger.info(f"Not a trading day for {exchange_key} — skipping screener")
        return

    logger.info(f"Starting AstraTrade daily screen for {exchange_key}...")
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
                elif exchange_key in ("NYSE", "NASDAQ", "US"):
                    # NYSE beat task covers both NYSE and NASDAQ-100 stocks
                    stock_query = stock_query.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
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
        elif not tickers and exchange_key in ("NYSE", "NASDAQ", "US"):
            # Bug #15 fix: auto-bootstrap US universe on first run
            logger.warning(f"[{exchange_key}] US stock universe empty — auto-fetching S&P 500 + NASDAQ-100...")
            refresh_us_universe.run()
            with get_db() as db:
                sq = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False,
                                            Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
                tickers = [s.ticker for s in sq.all()]
                stocks_map = {s.ticker: s for s in db.query(Stock).filter(Stock.is_active == True).all()}
            logger.info(f"US universe bootstrapped: {len(tickers)} tickers")

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
                    ticker_asset_type = stock_obj.asset_type if stock_obj else "EQUITY"
                    ticker_engine = engine_crypto if ticker_asset_type == "CRYPTO" else engine

                    # --- Share Price Range Filter (equity only, opt-in) ---
                    # Hard exclude before running any other rule — this is a
                    # portfolio-construction preference, not a "partial pass"
                    # signal, so out-of-range tickers are skipped entirely
                    # (not added to watchlist).
                    last_close = float(df["close"].iloc[-1])
                    in_range, range_reason = price_in_range(ticker, last_close, ticker_engine, ticker_asset_type)
                    if not in_range:
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"SCREENER_SKIP price_range: {range_reason}",
                            ))
                        continue

                    # --- Minimum Liquidity Filter (R2 / CLAUDE.md #42) ---
                    avg_vol_50 = float(df["avg_vol_50"].iloc[-1] or 0) if "avg_vol_50" in df.columns else 0.0
                    liq_ok, liq_reason = liquidity_ok(ticker, last_close, avg_vol_50, ticker_engine, ticker_asset_type)
                    if not liq_ok:
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"SCREENER_SKIP liquidity: {liq_reason}",
                            ))
                        continue

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
                            _upsert_watchlist(ticker, {**trend_results, **fund_results, **vcp_rules}, db, organization_id=org.id, vcp_result=vcp_result)
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
                    currency = stock_obj.currency if (stock_obj and stock_obj.currency) else ("USD" if asset_type == "CRYPTO" or exchange_key in ("NYSE", "NASDAQ", "US") else "AUD")
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

                    # Use stock's actual exchange_key so NASDAQ stocks don't get tagged NYSE
                    signal_exchange_key = (stock_obj.exchange_key if stock_obj and stock_obj.exchange_key else exchange_key) or exchange_key

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
                    message=f"[{exchange_key or 'ASX'}] Screen complete: {signals_generated} signals, {watchlist_added} added/confirmed to watchlist this run",
                    detail={"date": str(today), "universe_size": len(tickers)},
                ))

            logger.info(f"Screen complete for {org.name}: {signals_generated} signals | {watchlist_added} added/confirmed to watchlist this run")
            notifier.send(f"🔍 Screen done for *{org.name}*: *{signals_generated} signals*, {watchlist_added} added/confirmed to watchlist this run")

    except Exception as exc:
        logger.error(f"Daily screen failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@app.task(name="app.tasks.screening.run_full_setup", bind=True)
def run_full_setup(self):
    """
    First-time setup chain — runs for every active exchange across all orgs:
      ASX:    universe → price data → regime → screener
      Crypto: seed universe → price data → regime → screener
    Designed to be triggered manually from the dashboard on first run.
    """
    from celery import chain as celery_chain
    from app.models.account import Organization
    logger.info("Starting full AstraTrade setup sequence...")

    with get_db() as db:
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    # Collect active exchanges across all orgs
    has_asx = False
    has_us  = False
    crypto_keys: set[str] = set()
    for org in orgs:
        with get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "active_exchanges",
                SystemConfig.organization_id == org.id,
            ).first()
            if cfg and cfg.value:
                for exc in cfg.value.split(","):
                    exc = exc.strip()
                    if exc == "ASX":
                        has_asx = True
                    elif exc in ("NYSE", "NASDAQ"):
                        has_us = True
                    elif exc.startswith("CRYPTO_"):
                        crypto_keys.add(exc)

    # Default to ASX if nothing configured
    if not has_asx and not has_us and not crypto_keys:
        has_asx = True

    exchanges_desc = (["ASX"] if has_asx else []) + (["NYSE/NASDAQ"] if has_us else []) + sorted(crypto_keys)
    for org in orgs:
        try:
            notifier = get_notifier(organization_id=org.id)
            notifier.send(f"⚙️ AstraTrade full setup starting for: {', '.join(exchanges_desc)}")
        except Exception as org_err:
            logger.error(f"Failed to notify Org {org.name} (ID: {org.id}) of full setup: {org_err}")

    with get_db() as db:
        db.add(AuditLog(
            action=AuditAction.TASK_RUN,
            message=f"⚙️ Full setup started for exchanges: {', '.join(exchanges_desc)} (15–25 min per exchange)",
        ))

    # ASX chain
    if has_asx:
        celery_chain(
            refresh_universe.si(),
            refresh_price_data.si("ASX"),
            evaluate_market_regime_task.si("ASX"),
            _run_screen_force.si(exchange_key="ASX"),
        ).delay()
        logger.info("ASX setup chain queued")

    # US chain — refreshes price data for NYSE+NASDAQ, evaluates NYSE regime, screens all US
    if has_us:
        celery_chain(
            refresh_us_universe.si(),
            refresh_price_data.si("NYSE"),
            evaluate_market_regime_task.si("NYSE"),
            _run_screen_force.si(exchange_key="US"),
        ).delay()
        logger.info("US (NYSE/NASDAQ) setup chain queued")

    # Crypto chains — one independent chain per exchange key
    for crypto_key in sorted(crypto_keys):
        celery_chain(
            refresh_crypto_universe.si(exchange_key=crypto_key),
            refresh_price_data.si(crypto_key),
            evaluate_market_regime_task.si(crypto_key),
            _run_screen_force.si(exchange_key=crypto_key),
        ).delay()
        logger.info(f"{crypto_key} setup chain queued")


@app.task(name="app.tasks.screening._run_screen_force", bind=True, max_retries=1)
def _run_screen_force(self, organization_id: int = None, exchange_key: str = "ASX"):
    """
    Full AstraTrade screen bypassing the trading-day gate. For manual triggers.
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
                elif exchange_key in ("US", "NYSE", "NASDAQ"):
                    # Bug #8 fix: NYSE beat task covers both NYSE and NASDAQ-100
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

                    # ── Share Price Range Filter (equity only, opt-in) ──────────
                    # Hard exclude before any other rule runs — portfolio-construction
                    # preference, not a "partial pass" signal.
                    last_close = float(df["close"].iloc[-1])
                    in_range, range_reason = price_in_range(ticker, last_close, ticker_engine, asset_type)
                    if not in_range:
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.SCREENER_TICKER,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"⚪ SKIP — {range_reason}",
                                detail={"reason": "price_out_of_range"},
                            ))
                        continue

                    # --- Minimum Liquidity Filter (R2 / CLAUDE.md #42) ---
                    avg_vol_50 = float(df["avg_vol_50"].iloc[-1] or 0) if "avg_vol_50" in df.columns else 0.0
                    liq_ok, liq_reason = liquidity_ok(ticker, last_close, avg_vol_50, ticker_engine, asset_type)
                    if not liq_ok:
                        with get_db() as db:
                            db.add(AuditLog(
                                action=AuditAction.SCREENER_TICKER,
                                organization_id=org.id,
                                ticker=ticker,
                                message=f"⚪ SKIP — {liq_reason}",
                                detail={"reason": "insufficient_liquidity"},
                            ))
                        continue

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
                                currency = stock_obj.currency if (stock_obj and stock_obj.currency) else ("USD" if asset_type == "CRYPTO" or exchange_key in ("NYSE", "NASDAQ", "US") else "AUD")
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
                                    _upsert_watchlist(ticker, {**trend_results, **fund_results, **vcp_rules, **crypto_rule_results}, db, organization_id=org.id, vcp_result=vcp_result)
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
                    message=f"[{exchange_key or 'ASX'}] Force screen done: {signals_generated} signals, {watchlist_added} added/confirmed to watchlist this run, {skipped_no_data} skipped (no data)",
                    detail={"mode": "force_complete", "tickers_checked": len(tickers),
                            "signals": signals_generated, "watchlist": watchlist_added},
                ))

            notifier.send(f"🔍 Force screen done for *{org.name}*: *{signals_generated} signals*, {watchlist_added} added/confirmed to watchlist this run, {skipped_no_data} skipped")
            logger.info(f"Force screen complete for {org.name}: {signals_generated} signals | {watchlist_added} added/confirmed to watchlist this run")

    except Exception as exc:
        logger.error(f"Force screen failed: {exc}")
        # Surface in the Task Log — otherwise the Run Screener button appears
        # to do nothing when the task dies (e.g. data source outage).
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    organization_id=organization_id,
                    message=f"[{exchange_key or 'ASX'}] Force screen FAILED: {str(exc)[:200]}",
                ))
        except Exception:
            pass


# Colour map for every label name the auto-classifier can assign. Module-level
# on purpose: its key set doubles as the authoritative "auto-assigned label
# names" set — recategorise_watchlist_labels uses it to distinguish
# classifier-owned labels (always re-assignable) from user-chosen labels
# (protected unless force=True).
SECTOR_LABEL_COLOURS = {
        "Gold":               "#f59e0b",   # amber
        "Lithium":            "#10b981",   # emerald
        "Rare Earth":         "#8b5cf6",   # violet
        "Uranium":            "#f97316",   # orange
        "Silver":             "#94a3b8",   # slate
        "Copper":             "#d97706",   # amber-dark
        "Nickel & Cobalt":    "#0ea5e9",   # sky
        "Iron & Steel":       "#64748b",   # slate-500
        "Coal":               "#374151",   # gray-dark
        "Oil & Gas":          "#dc2626",   # red
        "Energy":             "#f97316",   # orange
        "Mining (General)":   "#92400e",   # brown
        "Biotech":            "#ec4899",   # pink
        "Healthcare / Pharma":"#06b6d4",   # cyan
        "FinTech":            "#6366f1",   # indigo
        "Technology":         "#3b82f6",   # blue
        "Banks":              "#1e40af",   # blue-dark
        "Insurance":          "#0f766e",   # teal
        "Financials":         "#1d4ed8",   # blue
        "Real Estate (REIT)": "#7c3aed",   # violet
        "Consumer":           "#16a34a",   # green
        "Industrials":        "#78716c",   # stone
        "Telco / Media":      "#0891b2",   # cyan-dark
        "Utilities":          "#65a30d",   # lime
        "Crypto Core":        "#06b6d4",   # cyan
        "Layer 1":            "#2563eb",   # blue
        "Layer 2":            "#7c3aed",   # violet
        "DeFi":               "#8b5cf6",   # violet
        "Stablecoin":         "#16a34a",   # green
        "Exchange Token":     "#0f766e",   # teal
        "Meme Coin":          "#f59e0b",   # amber
        "Gaming & Metaverse": "#db2777",   # pink
        "AI & Data":          "#9333ea",   # purple
        "Oracle & Infra":     "#0ea5e9",   # sky
        "Privacy Coin":       "#475569",   # slate-dark
        "Payments":           "#ca8a04",   # yellow-dark
        "Altcoins":           "#6b7280",   # gray
}


def _get_or_create_sector_label(label_name: str, organization_id: int, db) -> int | None:
    """
    Look up a WatchlistLabel by name for the given org.
    Creates it if it doesn't exist.
    Returns the label id or None on error.

    Sector labels use the consistent SECTOR_LABEL_COLOURS map above.
    """
    from app.models.signal import WatchlistLabel

    try:
        existing = db.query(WatchlistLabel).filter(
            WatchlistLabel.organization_id == organization_id,
            WatchlistLabel.name == label_name,
        ).first()
        if existing:
            return existing.id

        # Create new sector label
        colour = SECTOR_LABEL_COLOURS.get(label_name, "#6b7280")  # default gray
        new_label = WatchlistLabel(
            organization_id=organization_id,
            name=label_name,
            color=colour,
            is_default=False,
            sort_order=100,  # sector labels after default labels (0–13)
        )
        db.add(new_label)
        db.flush()  # get the id without full commit
        return new_label.id
    except Exception as e:
        logger.warning(f"Could not get/create sector label '{label_name}' for org {organization_id}: {e}")
        return None


def _auto_assign_sector_label(ticker: str, wl_item, organization_id: int, db, force: bool = False) -> str | None:
    """
    Auto-assign a sector WatchlistLabel to the watchlist item based on the
    stock's ticker (deterministic override map) or sector/industry data.

    Returns the resolved label NAME when a label was determined (whether or not
    it differs from the current one), or None when no label could be resolved
    (item keeps whatever label it had). Callers doing bulk runs use this to
    report changed / already-correct / unmatched counts.

    Only assigns if:
    - The item has no label yet (or force=True to override any non-default label)
    - A label can be determined — either via the ASX_TICKER_SECTOR_OVERRIDES
      map (instant, no data dependency) or via sector/industry keyword
      matching (fetched live and persisted to the Stock row if missing).

    Does NOT override Favourites, High Priority, VCP Forming, or Under Review unless force=True.

    Note: most WATCHING items are, by definition, NOT a full 8/8 trend pass —
    run_daily_screen's get_fundamentals() call (the only place that normally
    populates Stock.industry) is gated behind a full trend-template pass and
    is therefore never reached for the bulk of the watchlist. The ticker
    override map and the live-fetch fallback below exist specifically to
    close that gap.
    """
    from app.models.market import Stock
    from app.data.fetcher import infer_sector_label_for_ticker, get_fundamentals

    # Skip if already has a label and not forcing
    if wl_item.label_id and not force:
        return None

    stock = db.query(Stock).filter(Stock.ticker == ticker).first()
    sector     = (stock.sector     if stock else "") or ""
    industry   = (stock.industry   if stock else "") or ""
    asset_type = (stock.asset_type if stock else None)

    # Deterministic ticker override (and crypto routing) is checked first
    # inside infer_sector_label_for_ticker() — works even with blank
    # sector/industry, and always resolves for crypto.
    label_name = infer_sector_label_for_ticker(ticker, sector, industry, asset_type=asset_type)

    # No match and no sector/industry data yet — try a live, throttled fetch
    # before giving up. Throttled to once per 24h per ticker so a bulk
    # re-categorise run doesn't hammer yfinance for every miss. Skipped for
    # crypto entirely since infer_sector_label_for_ticker always resolves a
    # crypto ticker via infer_crypto_category() and yfinance has no
    # sector/industry data for crypto anyway.
    if not label_name and not sector and not industry and stock and stock.asset_type != "CRYPTO":
        from app.utils.cache import cache
        _throttle_key = f"sector_fetch_attempted:{ticker}"
        if not cache.get(_throttle_key):
            cache.set(_throttle_key, "1", expire_seconds=86400)
            try:
                fundamentals = get_fundamentals(ticker)
                f_sector   = fundamentals.get("sector") or ""
                f_industry = fundamentals.get("industry") or ""
                if f_sector or f_industry:
                    stock.sector   = f_sector   or stock.sector
                    stock.industry = f_industry or stock.industry
                    label_name = infer_sector_label_for_ticker(ticker, f_sector, f_industry, asset_type=asset_type)
            except Exception as e:
                logger.warning(f"Live sector/industry fetch failed for {ticker}: {e}")

    if not label_name:
        return None

    label_id = _get_or_create_sector_label(label_name, organization_id, db)
    if label_id:
        wl_item.label_id = label_id
        return label_name
    return None


def _watchlist_geometry_fields(ticker: str, vcp_result, db) -> dict:
    """
    Resolve persisted VCP geometry for a watchlist row from `vcp_result` plus the
    latest PriceBar (for the fallback pivot/stop and the freshness date). Returns a
    dict of column values ready to splat onto a Watchlist row, or {} when there is
    no price data / no vcp_result to persist.
    """
    if vcp_result is None:
        return {}
    from app.models.market import PriceBar
    from app.screener.vcp import resolve_watchlist_geometry

    bar = (
        db.query(PriceBar)
        .filter(PriceBar.ticker == ticker)
        .order_by(PriceBar.date.desc())
        .first()
    )
    if not bar:
        return {}
    geo = resolve_watchlist_geometry(
        vcp_result,
        close=float(bar.close or 0),
        high_52w=float(bar.high_52w or 0),
        atr_14=float(bar.atr_14 or 0),
    )
    geo["vcp_computed_date"] = bar.date
    return geo


def _upsert_watchlist(ticker: str, rule_results: dict, db, organization_id: int, vcp_result=None):
    """Add or update a stock on the watchlist.

    When `vcp_result` is supplied (the screener already ran detect_vcp for this
    ticker), the resolved VCP geometry is persisted on the row so the dashboard can
    render pivot/stop/target without recomputing.
    """
    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.market import Stock

    # Query stock metadata to avoid default ASX/EQUITY/AUD values for non-ASX assets
    stock = db.query(Stock).filter(Stock.ticker == ticker).first()
    exchange_key = stock.exchange_key if stock else "ASX"
    asset_type = stock.asset_type if stock else "EQUITY"
    currency = stock.currency if stock else "AUD"

    geo = _watchlist_geometry_fields(ticker, vcp_result, db)

    existing = db.query(Watchlist).filter(
        Watchlist.ticker == ticker,
        Watchlist.organization_id == organization_id,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()
    if not existing:
        new_item = Watchlist(
            ticker=ticker,
            exchange_key=exchange_key,
            asset_type=asset_type,
            currency=currency,
            organization_id=organization_id,
            rule_results=serialize_rule_results(rule_results),
            added_by="screener",
            **geo,
        )
        db.add(new_item)
        db.flush()  # so new_item.id is populated
        _auto_assign_sector_label(ticker, new_item, organization_id, db)
    else:
        existing.rule_results = serialize_rule_results(rule_results)
        # Ensure metadata is updated if it was previously default
        existing.exchange_key = exchange_key
        existing.asset_type = asset_type
        existing.currency = currency
        for _k, _v in geo.items():
            setattr(existing, _k, _v)
        # Assign sector label if not already set
        _auto_assign_sector_label(ticker, existing, organization_id, db)


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
      4. Runs AstraTrade rules (trend template + fundamentals + VCP)
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
            # A USER explicitly asked for this ticker — never silently drop it.
            # We can't fully screen without ~50+ bars, but we still add it to the
            # watchlist in a WATCHING "awaiting data" state so it's visible and
            # gets re-screened automatically on the next data refresh / screen run.
            bars = 0 if df is None else len(df)
            logger.warning(f"Insufficient price history for {ticker} ({bars} bars) — adding to watchlist as awaiting-data")
            note = (notes + " | " if notes else "") + f"⏳ Awaiting price data ({bars} bars) — will re-screen automatically"
            with get_db() as db:
                wl = db.query(Watchlist).filter(
                    Watchlist.ticker == ticker,
                    Watchlist.organization_id == organization_id,
                    Watchlist.status == WatchlistStatus.WATCHING,
                ).first()
                if not wl:
                    wl = Watchlist(
                        ticker=ticker,
                        exchange_key=exchange_key,
                        asset_type=asset_type,
                        currency=currency,
                        organization_id=organization_id,
                        added_date=today,
                        status=WatchlistStatus.WATCHING,
                        added_by="admin_manual",
                        notes=note,
                        label_id=label_id,
                        rule_results=serialize_rule_results({}),
                    )
                    db.add(wl)
                    db.flush()
                    if not label_id:
                        _auto_assign_sector_label(ticker, wl, organization_id, db)
                else:
                    wl.exchange_key = exchange_key
                    wl.asset_type = asset_type
                    wl.currency = currency
                    wl.notes = note
                    if label_id is not None:
                        wl.label_id = label_id
                db.add(AuditLog(
                    action=AuditAction.SCREENER_TICKER,
                    organization_id=organization_id,
                    ticker=ticker,
                    message=f"⏳ WATCHLIST (Manual) — awaiting data, only {bars} price bars available; will re-screen",
                    detail={"result": "watchlist_awaiting_data", "bars": bars},
                ))
                db.commit()
                logger.info(f"Added {ticker} to watchlist as awaiting-data (Manually added)")
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
        if fundamentals.get("company_name") or fundamentals.get("sector") or fundamentals.get("industry"):
            with get_db() as db:
                stock_db = db.query(Stock).filter(Stock.ticker == ticker).first()
                if stock_db:
                    if fundamentals.get("company_name"):
                        stock_db.name = fundamentals["company_name"]
                    if fundamentals.get("sector") and not stock_db.sector:
                        stock_db.sector = fundamentals["sector"]
                    if fundamentals.get("industry") and not stock_db.industry:
                        stock_db.industry = fundamentals["industry"]
                    db.add(stock_db)
                    db.commit()

        # 4a. Stock Story — fetch & persist the CommSec-style payload on add.
        # One ticker = one fetch; the eye-icon modal reads this straight from DB.
        # force=True because the user just added it and expects data immediately.
        try:
            with get_db() as db:
                upsert_stock_story(
                    ticker, db, exchange_key=exchange_key,
                    asset_type=asset_type, currency=currency, force=True,
                )
                db.commit()
        except Exception as _story_exc:
            logger.warning(f"Stock story upsert failed for {ticker}: {_story_exc}")

        # 4b. Share Price Range Filter (equity only, opt-in) — hard exclude,
        # even on manual add, so a configured price-band preference is never
        # bypassed by adding a ticker directly.
        last_close = float(df["close"].iloc[-1])
        in_range, range_reason = price_in_range(ticker, last_close, engine, asset_type)
        if not in_range:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_TICKER,
                    organization_id=organization_id,
                    ticker=ticker,
                    message=f"⚪ SKIP manual add — {range_reason}",
                    detail={"result": "skip_price_out_of_range"},
                ))
                db.commit()
            return

        # 4c. Minimum Liquidity Filter (R2 / CLAUDE.md #42) — same reasoning:
        # never bypassed by adding a ticker directly.
        avg_vol_50 = float(df["avg_vol_50"].iloc[-1] or 0) if "avg_vol_50" in df.columns else 0.0
        liq_ok, liq_reason = liquidity_ok(ticker, last_close, avg_vol_50, engine, asset_type)
        if not liq_ok:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.SCREENER_TICKER,
                    organization_id=organization_id,
                    ticker=ticker,
                    message=f"⚪ SKIP manual add — {liq_reason}",
                    detail={"result": "skip_insufficient_liquidity"},
                ))
                db.commit()
            return

        # 5. Run AstraTrade Screener check
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
                currency=currency,
                base_currency="AUD",
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
                    new_wl = Watchlist(
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
                    )
                    db.add(new_wl)
                    db.flush()
                    # Auto-assign sector label only if no explicit label was provided
                    if not label_id:
                        _auto_assign_sector_label(ticker, new_wl, organization_id, db)
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
                    elif not existing_wl.label_id:
                        _auto_assign_sector_label(ticker, existing_wl, organization_id, db)
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


_RECAT_CRYPTO_SUFFIXES = ("-AUD", "-USD", "-USDT", "-BTC", "-ETH")


def _watchlist_item_market(item) -> str:
    """
    Classify a watchlist row into a market bucket: "ASX" | "US" | "CRYPTO".

    Ticker format is authoritative (canonical yfinance: "BHP.AX", "AAPL",
    "BTC-USD") and is checked before the DB columns — a known Jun 2026 bug
    left some rows with exchange_key="ASX"/asset_type="EQUITY" for crypto and
    US tickers, so the columns alone can't be trusted for old rows.
    """
    t = (item.ticker or "").upper()
    if t.endswith(_RECAT_CRYPTO_SUFFIXES) or (item.asset_type or "").upper() == "CRYPTO" \
            or (item.exchange_key or "").upper().startswith("CRYPTO"):
        return "CRYPTO"
    if t.endswith(".AX"):
        return "ASX"
    if (item.exchange_key or "").upper() in ("NYSE", "NASDAQ", "US"):
        return "US"
    # No .AX suffix and not crypto — canonical yfinance US ticker
    return "US" if "." not in t else "ASX"


@app.task(name="app.tasks.screening.recategorise_watchlist_labels", bind=True)
def recategorise_watchlist_labels(self, organization_id: int = None, force: bool = False,
                                  market: str = "ALL"):
    """
    Bulk-assign sector/category labels to WATCHING watchlist items for the
    given org (or all active orgs), optionally scoped to one market.

    market: "ALL" | "ASX" | "US" | "CRYPTO" — items outside the selected
    market are left completely untouched.

    Label protection model:
    - Labels the auto-classifier itself owns (any name in SECTOR_LABEL_COLOURS,
      e.g. "Banks", "Gold", "Crypto Core", "Altcoins") are ALWAYS eligible for
      re-categorisation — this is what lets an "Altcoins" coin upgrade to
      "Meme Coin" after the category map improves. (Previously "Crypto Core"/
      "DeFi"/"Altcoins" were wrongly protected, so crypto items could never be
      re-categorised without force.)
    - Any other label (Favourites, High Priority, VCP Forming, Under Review,
      Crypto Watch, or any user-created custom label) is user intent —
      preserved unless force=True.
    """
    from app.models.account import Organization
    from app.models.signal import Watchlist, WatchlistStatus

    market = (market or "ALL").strip().upper()

    try:
        with get_db() as db:
            if organization_id:
                orgs = db.query(Organization).filter(Organization.id == organization_id).all()
            else:
                orgs = db.query(Organization).filter(Organization.is_active == True).all()

        for org in orgs:
            scoped_count = 0
            with get_db() as db:
                from app.models.signal import WatchlistLabel
                items = db.query(Watchlist).filter(
                    Watchlist.organization_id == org.id,
                    Watchlist.status == WatchlistStatus.WATCHING,
                ).all()

                if market != "ALL":
                    items = [i for i in items if _watchlist_item_market(i) == market]
                scoped_count = len(items)

                # Auto-classifier-owned labels are always re-assignable; every
                # other label is user-chosen and protected unless force=True.
                _auto_label_ids: set = {
                    lbl.id for lbl in db.query(WatchlistLabel).filter(
                        WatchlistLabel.organization_id == org.id,
                        WatchlistLabel.name.in_(SECTOR_LABEL_COLOURS.keys()),
                    ).all()
                }

                changed = 0
                already_correct = 0
                unmatched = 0
                protected = 0
                unmatched_tickers: list[str] = []
                for item in items:
                    # Skip if current label is user-chosen (non-auto) and not forcing
                    if not force and item.label_id and item.label_id not in _auto_label_ids:
                        protected += 1
                        continue
                    before = item.label_id
                    # Guard already applied above — force=True internally so any
                    # stale auto-assigned label can be overwritten
                    resolved = _auto_assign_sector_label(item.ticker, item, org.id, db, force=True)
                    if item.label_id != before:
                        changed += 1
                    elif resolved:
                        already_correct += 1
                    else:
                        unmatched += 1
                        if len(unmatched_tickers) < 25:
                            unmatched_tickers.append(item.ticker)

                # "changed" alone reads like a failure when most items are already
                # correctly labelled (e.g. "5/754") — report the full breakdown.
                db.add(AuditLog(
                    action=AuditAction.TASK_RUN,
                    organization_id=org.id,
                    message=(f"Watchlist re-categorised [{market}]: {scoped_count} in scope — "
                             f"{changed} changed, {already_correct} already correct, "
                             f"{unmatched} unmatched (no sector data)"
                             + (f", {protected} user-labelled (protected)" if protected else "")
                             + f" (force={force})"),
                    detail={"market": market, "force": force, "items_in_scope": scoped_count,
                            "changed": changed, "already_correct": already_correct,
                            "unmatched": unmatched, "protected": protected,
                            "unmatched_sample": unmatched_tickers},
                ))

            logger.info(f"Org {org.name}: re-categorised [{market}] — {changed} changed, "
                        f"{already_correct} same, {unmatched} unmatched of {scoped_count}")

    except Exception as exc:
        logger.error(f"Watchlist re-categorisation failed: {exc}")
        # Surface the failure in the Task Log — a silent failure here is why the
        # feature previously looked like "the button does nothing".
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    organization_id=organization_id,
                    message=f"Watchlist re-categorisation failed [{market}]: {str(exc)[:200]}",
                ))
        except Exception:
            pass


@app.task(name="app.tasks.screening.refresh_asx_sector_data", bind=True)
def refresh_asx_sector_data(self, organization_id: int = None):
    """
    Backfill Stock.sector / Stock.industry for every ASX-listed Stock row using
    the ASX's own official GICS industry-group export (get_asx_gics_map()).

    This is the data-layer half of the sector classification fix: it doesn't
    touch any WatchlistLabel directly — it just makes sure Stock.industry
    carries a precise GICS Level-2 string (e.g. "Banks", "Insurance",
    "Real Estate Investment Trusts (REITs)") for the ASX universe, so that
    the keyword matcher in infer_sector_label() — which previously had only
    a blank or coarse Level-1 string ("Financials") to work with for most
    ASX stocks — has something precise to match against.

    Deliberately a separate, explicitly-invoked task (chained before
    recategorise_watchlist_labels at the dashboard route level) rather than
    embedded inside _auto_assign_sector_label or recategorise_watchlist_labels
    themselves, so that those hot-path / unit-tested functions stay free of
    live network calls.

    Only fills in *blank* sector/industry — never overwrites a value already
    populated by a more specific source (e.g. live yfinance fundamentals).
    """
    from app.models.market import Stock
    from app.data.fetcher import get_asx_gics_map

    updated = 0
    total = 0
    try:
        gics_map = get_asx_gics_map()
        if not gics_map:
            logger.warning("refresh_asx_sector_data: GICS map empty — ASX fetch failed or unavailable")
            # Visible in the Task Log — previously this failed silently and the
            # subsequent re-categorise had only coarse Level-1 sectors to work with.
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    organization_id=organization_id,
                    message="ASX GICS sector backfill skipped: ASX export fetch failed/empty — "
                            "ASX labels will fall back to coarse Level-1 sectors",
                ))
            return

        with get_db() as db:
            stocks = db.query(Stock).filter(Stock.exchange_key == "ASX").all()
            total = len(stocks)
            for stock in stocks:
                gics_value = gics_map.get(stock.ticker)
                if not gics_value:
                    continue
                changed = False
                if not (stock.sector or "").strip():
                    stock.sector = gics_value
                    changed = True
                if not (stock.industry or "").strip():
                    stock.industry = gics_value
                    changed = True
                if changed:
                    updated += 1

            db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                organization_id=organization_id,
                message=f"ASX sector data refreshed from GICS export: {updated}/{total} stocks updated",
            ))

        logger.info(f"refresh_asx_sector_data: updated {updated}/{total} ASX stocks")

    except Exception as exc:
        logger.error(f"refresh_asx_sector_data failed: {exc}")
        # Surface in the Task Log — a silent crash here means the chained
        # re-categorise runs with coarse/blank ASX sector data and most ASX
        # items come back "unmatched" with no explanation.
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    organization_id=organization_id,
                    message=f"ASX GICS sector backfill FAILED: {str(exc)[:200]}",
                ))
        except Exception:
            pass


# ===========================================================================
# Stock Story (CommSec-style) — persistence + rate-limit-safe refresh
# ===========================================================================

# Default cadence: only re-fetch a ticker's story if the stored copy is older
# than this many days. Fundamentals barely change day-to-day, so a weekly
# refresh is plenty and keeps us well under yfinance's soft rate limits.
STORY_STALE_DAYS_DEFAULT = 7
# Hard cap on how many tickers a single daily-triggered refresh will fetch, so
# the daily price refresh never fans out into hundreds of .info calls at once.
STORY_MAX_PER_RUN_DEFAULT = 40
# Polite delay between per-ticker fetches (seconds) to avoid bursting yfinance.
STORY_FETCH_DELAY_SECS = 1.5


def upsert_stock_story(ticker: str, db, *, exchange_key: str = "ASX",
                       asset_type: str = "EQUITY", currency: str = "AUD",
                       force: bool = False, stale_days: int = STORY_STALE_DAYS_DEFAULT) -> bool:
    """
    Fetch (if needed) and persist the CommSec-style Stock Story for one ticker
    into the shared `stock_fundamentals` table.

    Staleness-gated: returns False WITHOUT any yfinance call when an existing row
    was fetched within `stale_days` and force=False. This is the mechanism that
    keeps the daily price refresh from re-hitting the network for every stock.

    Crypto assets have no fundamentals — we store a minimal stub row so the UI
    can render a graceful "not available" panel and we never retry the network.

    Returns True if a network fetch + write happened, False if skipped.
    Caller is responsible for committing the session.
    """
    from datetime import datetime as _dt, timedelta as _td

    row = db.query(StockFundamentals).filter(StockFundamentals.ticker == ticker).first()

    # Staleness gate
    if row and not force and row.fetched_at:
        if row.fetched_at > _dt.utcnow() - _td(days=stale_days):
            return False

    if row is None:
        row = StockFundamentals(ticker=ticker)
        db.add(row)

    row.exchange_key = exchange_key or row.exchange_key or "ASX"
    row.asset_type   = asset_type or row.asset_type or "EQUITY"
    row.currency     = currency or row.currency or "AUD"

    # Crypto: no fundamentals to fetch — store a stub, no network call.
    if (asset_type or "").upper() == "CRYPTO":
        row.data = {"ticker": ticker, "asset_type": "CRYPTO", "unavailable": True}
        row.fetch_ok = True
        row.fetch_error = None
        row.fetched_at = _dt.utcnow()
        return True

    try:
        story = get_stock_story(ticker)
        row.data = story
        row.company_name = story.get("company_name") or row.company_name
        # "ok" if we got at least a name or a summary or any headline figure
        has_data = bool(
            story.get("company_name") or story.get("summary")
            or story.get("market_cap") or story.get("net_income_history")
        )
        row.fetch_ok = has_data
        row.fetch_error = None if has_data else "no usable data returned"
        row.fetched_at = _dt.utcnow()
        return True
    except Exception as exc:
        row.fetch_ok = False
        row.fetch_error = str(exc)[:500]
        row.fetched_at = _dt.utcnow()
        logger.warning(f"upsert_stock_story failed for {ticker}: {exc}")
        return True


@app.task(name="app.tasks.screening.refresh_stock_fundamentals", bind=True, max_retries=1)
def refresh_stock_fundamentals(self, exchange_key: str = None,
                               max_per_run: int = STORY_MAX_PER_RUN_DEFAULT,
                               stale_days: int = STORY_STALE_DAYS_DEFAULT,
                               force: bool = False):
    """
    Rate-limit-safe batch refresh of Stock Story data for EQUITY instruments.

    Selection: active, non-blacklisted equities whose `stock_fundamentals` row is
    missing OR older than `stale_days`. Capped at `max_per_run` per invocation and
    throttled with a short sleep between tickers, so even when chained off the
    daily price refresh this never bursts the yfinance endpoints.

    Crypto is skipped entirely (no fundamentals). Run weekly via Celery Beat and
    opportunistically after each daily price refresh — the staleness gate makes
    repeat runs cheap (most rows are skipped with zero network calls).

    Args:
        exchange_key: limit to one venue ("ASX", "NYSE"/"NASDAQ"/"US"); None = all equities.
        max_per_run:  hard cap on fetches this run.
        stale_days:   re-fetch threshold.
        force:        ignore staleness (use sparingly — full universe = many calls).
    """
    import time as _time
    from datetime import datetime as _dt, timedelta as _td

    logger.info(f"refresh_stock_fundamentals start (exchange={exchange_key}, "
                f"max={max_per_run}, stale_days={stale_days}, force={force})")

    # 1. Build candidate ticker list (equities only)
    with get_db() as db:
        q = db.query(Stock).filter(
            Stock.is_active == True,
            Stock.blacklisted == False,
            Stock.asset_type == "EQUITY",
        )
        if exchange_key:
            if exchange_key in ("US", "NYSE", "NASDAQ"):
                q = q.filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
            elif exchange_key == "ASX":
                q = q.filter(Stock.exchange_key == "ASX")
            else:
                q = q.filter(Stock.exchange_key == exchange_key)
        stocks = [(s.ticker, s.exchange_key, s.currency) for s in q.all()]

        # Existing fetch timestamps for staleness filtering
        existing = {
            r.ticker: r.fetched_at
            for r in db.query(StockFundamentals.ticker, StockFundamentals.fetched_at).all()
        }

    cutoff = _dt.utcnow() - _td(days=stale_days)
    candidates = []
    for tk, exk, cur in stocks:
        ft = existing.get(tk)
        if force or ft is None or ft < cutoff:
            candidates.append((tk, exk, cur))

    candidates = candidates[:max_per_run]
    if not candidates:
        logger.info("refresh_stock_fundamentals: nothing stale — skipping")
        return {"fetched": 0, "skipped": True}

    # 2. Fetch + persist, one ticker per DB session, throttled
    fetched = 0
    for tk, exk, cur in candidates:
        try:
            with get_db() as db:
                did = upsert_stock_story(
                    tk, db, exchange_key=exk or "ASX",
                    asset_type="EQUITY", currency=cur or "AUD",
                    force=True, stale_days=stale_days,
                )
                if did:
                    fetched += 1
        except Exception as exc:
            logger.warning(f"refresh_stock_fundamentals: {tk} failed: {exc}")
        _time.sleep(STORY_FETCH_DELAY_SECS)

    with get_db() as db:
        db.add(AuditLog(
            action=AuditAction.TASK_RUN,
            message=f"[{exchange_key or 'ALL'}] Stock Story refresh: "
                    f"{fetched}/{len(candidates)} fetched (of {len(stocks)} equities)",
        ))

    logger.info(f"refresh_stock_fundamentals done: fetched {fetched}/{len(candidates)}")
    return {"fetched": fetched, "candidates": len(candidates)}
