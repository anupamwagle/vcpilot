"""
AstraTrade MCP Tool Implementations.

All tools read org context from the async ContextVar set by the auth middleware.
Every write action appends an AuditLog row so the full trail is preserved.

Tool catalogue
──────────────
Market / Regime
  get_market_regime      → current BULL/CAUTION/BEAR per exchange
  evaluate_market_regime → queue regime evaluation task

Screener / Signals
  get_signals            → list pending/triggered signals
  run_screener           → queue force-screen task
  skip_signal            → mark a signal SKIPPED
  unskip_signal          → restore a SKIPPED signal to PENDING

Watchlist
  get_watchlist          → list watchlist items (with optional label/exchange filter)
  add_to_watchlist       → add a ticker (queues screen_single_ticker)
  remove_from_watchlist  → remove a ticker from watchlist

Positions & Trading
  get_positions          → list open positions with live P&L
  get_portfolio_stats    → summary: capital, heat, P&L
  place_order            → submit a AstraTrade bracket order for a signal
  pyramid_position       → submit a controlled add-on to a winning position
  close_position         → close an open position with an exit reason
  pause_trading          → halt automated trading for the org
  resume_trading         → re-enable automated trading for the org

Rules & Config
  get_rules              → list AstraTrade rule configs (with optional category filter)
  update_rule            → enable/disable a rule or adjust its threshold
  get_config             → read non-secret SystemConfig values
"""
# from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.mcp.auth import get_mcp_context, assert_scope
from app.database import get_db
from loguru import logger


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ctx():
    """Return current MCP context (raises if not set)."""
    ctx = get_mcp_context()
    if ctx is None:
        raise RuntimeError("No MCP context — request did not pass authentication")
    return ctx


def _audit(db, action_str: str, message: str, ticker: str = None):
    """Append an audit log row for the current MCP operation."""
    from app.models.audit import AuditLog, AuditAction
    ctx = _ctx()
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,  # generic — specific actions below
        actor=f"mcp:{ctx.client_id}",
        organization_id=ctx.org_id,
        message=message,
        ticker=ticker,
    ))


def _audit_action(db, action, message: str, ticker: str = None):
    from app.models.audit import AuditLog
    ctx = _ctx()
    db.add(AuditLog(
        action=action,
        actor=f"mcp:{ctx.client_id}",
        organization_id=ctx.org_id,
        message=message,
        ticker=ticker,
    ))


# ---------------------------------------------------------------------------
# Market / Regime
# ---------------------------------------------------------------------------

def get_market_regime(exchange_key: str = "ASX") -> dict:
    """
    Return the current market regime for the given exchange.

    Args:
        exchange_key: Exchange to query. One of ASX, NYSE, NASDAQ, CRYPTO_INDEPENDENTRESERVE.
                      Defaults to ASX.

    Returns:
        {
          "exchange_key": "ASX",
          "regime": "BULL",          # BULL | CAUTION | BEAR | Not evaluated
          "evaluated_at": "...",     # ISO UTC timestamp or null
          "description": "..."
        }
    """
    assert_scope("market:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.config import SystemConfig
        key = f"last_market_regime_{exchange_key}"
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == key,
            SystemConfig.organization_id == ctx.org_id,
        ).first()
        regime_value = cfg.value if cfg else "Not evaluated"

        # Also try to fetch the MarketRegimeRecord timestamp if available
        evaluated_at = None
        try:
            from app.models.exchange import MarketRegimeRecord
            record = db.query(MarketRegimeRecord).filter(
                MarketRegimeRecord.exchange_key == exchange_key
            ).order_by(MarketRegimeRecord.evaluated_at.desc()).first()
            if record:
                evaluated_at = record.evaluated_at.isoformat() if record.evaluated_at else None
        except Exception:
            pass

    descriptions = {
        "BULL":   "Market is trending strongly above key moving averages. Full position sizing allowed.",
        "CAUTION": "Market showing mixed signals. Reduced position sizes recommended.",
        "BEAR":   "Market in correction or downtrend. No new long entries. Protect existing positions.",
    }

    return {
        "exchange_key": exchange_key,
        "regime": regime_value,
        "evaluated_at": evaluated_at,
        "description": descriptions.get(regime_value, "Regime has not been evaluated yet. Run 'evaluate_market_regime' first."),
    }


def evaluate_market_regime(exchange_key: str = "ASX") -> dict:
    """
    Queue a market regime evaluation task for the given exchange.

    Args:
        exchange_key: Exchange to evaluate. Defaults to ASX.

    Returns:
        {"queued": true, "exchange_key": "ASX", "message": "..."}
    """
    assert_scope("signals:write")
    ctx = _ctx()
    try:
        from app.tasks.screening import evaluate_market_regime_task
        evaluate_market_regime_task.delay(exchange_key=exchange_key)
        msg = f"Market regime evaluation queued for {exchange_key}"
        with get_db() as db:
            from app.models.audit import AuditAction
            _audit_action(db, AuditAction.TASK_RUN, f"[MCP] {msg}")
            db.commit()
        return {"queued": True, "exchange_key": exchange_key, "message": msg}
    except Exception as e:
        logger.error(f"MCP evaluate_market_regime error: {e}")
        return {"queued": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def get_signals(
    status: Optional[str] = None,
    exchange_key: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """
    Return today's AstraTrade signals for the organisation.

    Args:
        status:       Filter by status: PENDING, TRIGGERED, SKIPPED, EXPIRED, CANCELLED.
                      Omit to return all.
        exchange_key: Filter by exchange (ASX, NYSE, NASDAQ, CRYPTO_INDEPENDENTRESERVE). Omit for all.
        limit:        Maximum rows to return. Default 50.

    Returns:
        {"signals": [...], "total": N}
    """
    assert_scope("signals:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.signal import Signal, SignalStatus
        from app.utils.time_helper import get_current_date

        q = db.query(Signal).filter(
            Signal.organization_id == ctx.org_id,
            Signal.signal_date == get_current_date(),
        )
        if status:
            try:
                q = q.filter(Signal.status == SignalStatus[status.upper()])
            except KeyError:
                pass
        if exchange_key:
            q = q.filter(Signal.exchange_key == exchange_key.upper())

        signals = q.order_by(Signal.id.desc()).limit(limit).all()
        total   = q.count()

        rows = []
        for s in signals:
            rows.append({
                "id":            s.id,
                "ticker":        s.ticker,
                "exchange_key":  s.exchange_key,
                "asset_type":    s.asset_type,
                "currency":      s.currency,
                "status":        s.status.value,
                "signal_date":   s.signal_date.isoformat() if s.signal_date else None,
                "close_price":   float(s.close_price) if s.close_price else None,
                "pivot_price":   float(s.pivot_price) if s.pivot_price else None,
                "rs_rating":     float(s.rs_rating) if hasattr(s, "rs_rating") and s.rs_rating else None,
                "vcp_contractions": getattr(s, "vcp_contractions", None),
            })

    return {"signals": rows, "total": total}


def run_screener(exchange_key: str = "ASX") -> dict:
    """
    Immediately trigger the AstraTrade screener for the organisation.

    This bypasses the trading-day gate (safe to call any time).
    Results appear in /signals within a few minutes.

    Args:
        exchange_key: Exchange to screen. Defaults to ASX.

    Returns:
        {"queued": true, "message": "..."}
    """
    assert_scope("signals:write")
    ctx = _ctx()
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=ctx.org_id, exchange_key=exchange_key)
        msg = f"[{exchange_key}] Screener queued for org {ctx.org_id}"
        with get_db() as db:
            from app.models.audit import AuditAction
            _audit_action(db, AuditAction.TASK_RUN, f"[MCP] {msg}")
            db.commit()
        return {"queued": True, "message": msg}
    except Exception as e:
        logger.error(f"MCP run_screener error: {e}")
        return {"queued": False, "error": str(e)}


def skip_signal(signal_id: int, reason: str = "Skipped via MCP") -> dict:
    """
    Mark a PENDING signal as SKIPPED so it won't trigger an order.

    Args:
        signal_id: ID of the signal to skip.
        reason:    Optional reason note (logged to audit).

    Returns:
        {"ok": true, "signal_id": N, "ticker": "..."}
    """
    assert_scope("signals:write")
    ctx = _ctx()

    with get_db() as db:
        from app.models.signal import Signal, SignalStatus
        s = db.query(Signal).filter(
            Signal.id == signal_id,
            Signal.organization_id == ctx.org_id,
        ).first()
        if not s:
            return {"ok": False, "error": f"Signal {signal_id} not found"}
        if s.status != SignalStatus.PENDING:
            return {"ok": False, "error": f"Signal is {s.status.value}, not PENDING"}

        s.status = SignalStatus.SKIPPED
        from app.models.audit import AuditAction
        _audit_action(db, AuditAction.MANUAL_OVERRIDE,
                      f"[MCP] Signal {signal_id} skipped — {reason}", ticker=s.ticker)
        db.commit()
        return {"ok": True, "signal_id": signal_id, "ticker": s.ticker}


def unskip_signal(signal_id: int) -> dict:
    """
    Restore a SKIPPED signal back to PENDING so it can trigger an order again.

    Args:
        signal_id: ID of the signal to restore.

    Returns:
        {"ok": true, "signal_id": N, "ticker": "..."}
    """
    assert_scope("signals:write")
    ctx = _ctx()

    with get_db() as db:
        from app.models.signal import Signal, SignalStatus
        s = db.query(Signal).filter(
            Signal.id == signal_id,
            Signal.organization_id == ctx.org_id,
        ).first()
        if not s:
            return {"ok": False, "error": f"Signal {signal_id} not found"}
        if s.status != SignalStatus.SKIPPED:
            return {"ok": False, "error": f"Signal is {s.status.value}, not SKIPPED"}

        s.status = SignalStatus.PENDING
        from app.models.audit import AuditAction
        _audit_action(db, AuditAction.CONFIG_CHANGED,
                      f"[MCP] Signal {signal_id} restored to PENDING", ticker=s.ticker)
        db.commit()
        return {"ok": True, "signal_id": signal_id, "ticker": s.ticker}


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist(
    exchange_key: Optional[str] = None,
    label_name: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """
    Return the organisation's watchlist.

    Args:
        exchange_key: Filter by exchange. Omit for all.
        label_name:   Filter by watchlist label name (e.g. "Favourites").
        limit:        Max rows. Default 100.

    Returns:
        {"watchlist": [...], "total": N}
    """
    assert_scope("watchlist:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.signal import Watchlist
        from app.models.market import PriceBar
        from sqlalchemy.orm import joinedload
        from sqlalchemy import func

        q = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
            Watchlist.organization_id == ctx.org_id,
        )
        if exchange_key:
            q = q.filter(Watchlist.exchange_key == exchange_key.upper())
        if label_name:
            from app.models.signal import WatchlistLabel
            lbl = db.query(WatchlistLabel).filter(
                WatchlistLabel.organization_id == ctx.org_id,
                WatchlistLabel.name.ilike(label_name),
            ).first()
            if lbl:
                q = q.filter(Watchlist.label_id == lbl.id)

        items = q.order_by(Watchlist.created_at.desc()).limit(limit).all()
        total = q.count()

        # Fetch latest price bar for each ticker in one query
        tickers = [w.ticker for w in items]
        latest_bars: dict = {}
        if tickers:
            # Subquery: max date per ticker
            sub = (
                db.query(PriceBar.ticker, func.max(PriceBar.date).label("max_date"))
                .filter(PriceBar.ticker.in_(tickers))
                .group_by(PriceBar.ticker)
                .subquery()
            )
            bars = db.query(PriceBar).join(
                sub,
                (PriceBar.ticker == sub.c.ticker) & (PriceBar.date == sub.c.max_date)
            ).all()
            latest_bars = {b.ticker: b for b in bars}

        rows = []
        for w in items:
            bar = latest_bars.get(w.ticker)
            # Summarise trend template criteria (keys prefixed "trend_") from rule_results JSON
            rule_res = w.rule_results or {}
            trend_rules = {k: v for k, v in rule_res.items() if k.startswith("trend_")}
            passed = sum(1 for v in trend_rules.values() if isinstance(v, dict) and v.get("passed"))
            total_rules = len(trend_rules)
            rows.append({
                "id":            w.id,
                "ticker":        w.ticker,
                "exchange_key":  getattr(w, "exchange_key", "ASX"),
                "asset_type":    getattr(w, "asset_type", "EQUITY"),
                "label":         w.label.name if w.label else None,
                "label_color":   w.label.color if w.label else None,
                "criteria_met":  f"{passed}/{total_rules}" if total_rules else None,
                "added_at":      w.created_at.isoformat() if w.created_at else None,
                "close_price":   float(bar.close) if bar and bar.close else None,
                "ma_50":         float(bar.ma_50) if bar and bar.ma_50 else None,
                "ma_200":        float(bar.ma_200) if bar and bar.ma_200 else None,
                "pct_from_52w_high": float(bar.pct_from_52w_high) if bar and bar.pct_from_52w_high else None,
                "rs_rating":     float(bar.rs_rating) if bar and bar.rs_rating else None,
                "price_date":    bar.date.isoformat() if bar and bar.date else None,
                "pivot_price":   float(w.pivot_price) if getattr(w, "pivot_price", None) else None,
            })

    return {"watchlist": rows, "total": total}


def add_to_watchlist(
    ticker: str,
    exchange_key: str = "ASX",
    label_name: Optional[str] = None,
) -> dict:
    """
    Add a ticker to the organisation's watchlist and trigger a AstraTrade screen.

    The ticker will be fetched from yfinance and screened against all enabled rules.
    If it passes 6+/8 trend criteria it lands in the watchlist; a full VCP pass
    generates a signal.

    Args:
        ticker:       Ticker symbol in yfinance format (e.g. "BHP.AX", "AAPL", "BTC-USD").
        exchange_key: Exchange the ticker trades on. Defaults to ASX.
        label_name:   Optional watchlist label to assign (must already exist for the org).

    Returns:
        {"queued": true, "ticker": "...", "message": "..."}
    """
    assert_scope("watchlist:write")
    ctx = _ctx()

    # Resolve label_id
    label_id = None
    with get_db() as db:
        if label_name:
            from app.models.signal import WatchlistLabel
            lbl = db.query(WatchlistLabel).filter(
                WatchlistLabel.organization_id == ctx.org_id,
                WatchlistLabel.name.ilike(label_name),
            ).first()
            if lbl:
                label_id = lbl.id

    try:
        from app.tasks.screening import screen_single_ticker
        kwargs: dict = {
            "ticker":          ticker,
            "organization_id": ctx.org_id,
            "exchange_key":    exchange_key,
        }
        if label_id:
            kwargs["label_id"] = label_id
        screen_single_ticker.delay(**kwargs)
        msg = f"[MCP] Queued screen for {ticker} ({exchange_key}) org={ctx.org_id}"
        with get_db() as db:
            from app.models.audit import AuditAction
            _audit_action(db, AuditAction.TASK_RUN, msg, ticker=ticker)
            db.commit()
        return {"queued": True, "ticker": ticker, "exchange_key": exchange_key, "message": msg}
    except Exception as e:
        logger.error(f"MCP add_to_watchlist error: {e}")
        return {"queued": False, "error": str(e)}


def remove_from_watchlist(ticker: str) -> dict:
    """
    Remove a ticker from the organisation's watchlist.

    Args:
        ticker: yfinance canonical ticker (e.g. "BHP.AX").

    Returns:
        {"ok": true, "ticker": "...", "removed": N}
    """
    assert_scope("watchlist:write")
    ctx = _ctx()

    with get_db() as db:
        from app.models.signal import Watchlist

        deleted = db.query(Watchlist).filter(
            Watchlist.organization_id == ctx.org_id,
            Watchlist.ticker == ticker,
        ).delete(synchronize_session="fetch")

        if deleted:
            from app.models.audit import AuditAction
            _audit_action(db, AuditAction.CONFIG_CHANGED,
                          f"[MCP] Removed {ticker} from watchlist", ticker=ticker)
            db.commit()

    return {"ok": True, "ticker": ticker, "removed": deleted}


# ---------------------------------------------------------------------------
# Positions & Trading
# ---------------------------------------------------------------------------

def get_positions(
    exchange_key: Optional[str] = None,
    include_closed: bool = False,
    limit: int = 50,
) -> dict:
    """
    Return open (and optionally recent closed) positions for the organisation.

    Args:
        exchange_key:    Filter by exchange. Omit for all.
        include_closed:  If true, also return closed positions from the last 30 days.
        limit:           Max rows per status. Default 50.

    Returns:
        {"open": [...], "closed": [...], "open_count": N}
    """
    assert_scope("trading:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.trade import Position, Trade, TradeStatus

        open_q = db.query(Position).filter(
            Position.organization_id == ctx.org_id,
            Position.status == TradeStatus.OPEN,
        )
        if exchange_key:
            open_q = open_q.filter(Position.exchange_key == exchange_key.upper())

        open_positions = open_q.limit(limit).all()

        # NOTE: Position/Trade field names corrected below — the previous version
        # referenced columns that don't exist on these models (Position has no
        # stop_price/target_price/opened_at/pnl_pct; Trade has no closed_at/realised_pnl),
        # which made this tool raise AttributeError any time include_closed=True.
        closed_positions = []
        if include_closed:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=30)
            closed_q = db.query(Trade).filter(
                Trade.organization_id == ctx.org_id,
                Trade.created_at >= cutoff,
            )
            if exchange_key:
                closed_q = closed_q.filter(Trade.exchange_key == exchange_key.upper())
            closed_positions = closed_q.order_by(Trade.id.desc()).limit(limit).all()

        def _pos_dict(p):
            d = {
                "id":             p.id,
                "ticker":         p.ticker,
                "exchange_key":   getattr(p, "exchange_key", "ASX"),
                "asset_type":     getattr(p, "asset_type", "EQUITY"),
                "currency":       getattr(p, "currency", "AUD"),
                "qty":            float(p.qty) if p.qty else None,
                "entry_price":    float(p.entry_price) if p.entry_price else None,
                "stop_price":     float(p.current_stop) if getattr(p, "current_stop", None) else None,
                "target_price":   float(p.target_1) if getattr(p, "target_1", None) else None,
                "current_price":  float(p.current_price) if getattr(p, "current_price", None) else None,
                "unrealised_pnl": float(p.unrealised_pnl) if getattr(p, "unrealised_pnl", None) else None,
                "pnl_pct":        float(p.unrealised_pct) if getattr(p, "unrealised_pct", None) else None,
                "opened_at":      p.entry_date.isoformat() if p.entry_date else None,
            }
            return d

        def _trade_dict(t):
            return {
                "id":          t.id,
                "ticker":      t.ticker,
                "exchange_key": getattr(t, "exchange_key", "ASX"),
                "realised_pnl": float(t.net_pnl_aud) if getattr(t, "net_pnl_aud", None) is not None else None,
                "exit_reason":  t.exit_reason.value if t.exit_reason else None,
                "closed_at":    t.exit_date.isoformat() if t.exit_date else None,
            }

        return {
            "open":        [_pos_dict(p) for p in open_positions],
            "closed":      [_trade_dict(t) for t in closed_positions],
            "open_count":  len(open_positions),
        }


def get_portfolio_stats() -> dict:
    """
    Return high-level portfolio statistics for the organisation.

    Returns:
        {
          "capital_aud": N,
          "open_positions": N,
          "portfolio_heat_pct": N,   # % of capital at risk in open positions
          "total_unrealised_pnl": N,
          "trading_paused": bool,
          "currency": "AUD"
        }
    """
    assert_scope("trading:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.account import Account
        from app.models.trade import Position, TradeStatus
        from app.models.config import SystemConfig

        account = db.query(Account).filter(
            Account.organization_id == ctx.org_id,
            Account.is_active == True,
        ).first()

        capital = float(account.capital_aud) if account and account.capital_aud else 0.0
        currency_cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "working_capital_currency",
            SystemConfig.organization_id == ctx.org_id,
        ).first()
        currency = currency_cfg.value if currency_cfg else "AUD"

        positions = db.query(Position).filter(
            Position.organization_id == ctx.org_id,
            Position.status == TradeStatus.OPEN,
        ).all()

        total_unrealised = sum(
            float(p.unrealised_pnl) for p in positions
            if getattr(p, "unrealised_pnl", None) is not None
        )

        # Portfolio heat: sum of (entry_price - current_stop) * qty per position
        heat = 0.0
        for p in positions:
            if p.entry_price and p.current_stop and p.qty:
                risk_per_share = float(p.entry_price) - float(p.current_stop)
                heat += max(0.0, risk_per_share * float(p.qty))
        heat_pct = (heat / capital * 100) if capital > 0 else 0.0

        paused_cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "trading_paused",
            SystemConfig.organization_id == ctx.org_id,
        ).first()
        paused = paused_cfg and paused_cfg.value.lower() == "true"

    return {
        "capital":              capital,
        "currency":             currency,
        "open_positions":       len(positions),
        "portfolio_heat_pct":   round(heat_pct, 2),
        "total_unrealised_pnl": round(total_unrealised, 2),
        "trading_paused":       paused,
    }


def place_order(
    signal_id: int,
    notes: str = "Placed via MCP",
    force_entry_price: Optional[float] = None,
) -> dict:
    """
    Execute a bracket order for an existing PENDING signal.

    Fetches the live price, calculates AstraTrade position size, and submits
    a bracket order (entry + stop-loss + take-profit) to the exchange.
    For crypto signals, routes to CryptoBroker (Independent Reserve via ccxt).
    For equity signals, routes to IBKRBroker (simulation fallback if not connected).

    Args:
        signal_id:         ID of the PENDING signal to trade.
        notes:             Audit note shown in the activity log.
        force_entry_price: Override the entry price (useful for limit orders or
                           when you want to enter at a specific level). If omitted,
                           uses the live price from the exchange.

    Returns:
        {
          "ok": true,
          "signal_id": N,
          "ticker": "...",
          "qty": N,
          "entry_price": N,
          "stop_price": N,
          "target_price": N,
          "order_ref": "...",
          "broker": "ccxt|ibkr|simulation",
          "message": "..."
        }
    """
    assert_scope("trading:write")
    ctx = _ctx()

    from app.trading.order_executor import execute_signal_order
    return execute_signal_order(
        signal_id=signal_id,
        organization_id=ctx.org_id,
        actor=f"mcp:{ctx.client_id}",
        notes=f"[MCP] {notes}",
        force_entry_price=force_entry_price,
    )


def pyramid_position(position_id: int) -> dict:
    """Submit one controlled add-on to an explicitly enabled winning position.

    The broker fill, not this request, updates the position quantity and
    ``pyramid_count``.  The configured Minervini profit and maximum-add rules
    are enforced before submission.
    """
    assert_scope("trading:write")
    ctx = _ctx()
    from app.trading.pyramid_executor import request_pyramid_add_on
    return request_pyramid_add_on(position_id, ctx.org_id, actor=f"mcp:{ctx.client_id}")


def close_position(
    position_id: int,
    exit_reason: str,
    exit_price: Optional[float] = None,
) -> dict:
    """
    Close an open position with a AstraTrade exit reason.

    Args:
        position_id: ID of the open Position to close.
        exit_reason: One of: STOP_LOSS, TRAILING_STOP, PROFIT_TARGET_1,
                     PROFIT_TARGET_2, TIME_STOP, MARKET_REGIME, EARNINGS_AVOID,
                     CLIMAX_TOP, MANUAL, THREE_WEEKS_TIGHT.
        exit_price:  Optional override price. Uses last close if omitted.

    Returns:
        {"ok": true, "status": "submitted|filled", "ticker": "..."}
    """
    assert_scope("trading:write")
    ctx = _ctx()

    # A live close must be submitted to the broker and reconciled from its
    # fill.  Do not let an MCP caller create a fictitious closed Trade locally.
    from app.trading.exit_executor import request_position_exit
    return request_position_exit(
        position_id=position_id,
        organization_id=ctx.org_id,
        exit_reason=exit_reason.upper(),
        actor=f"mcp:{ctx.client_id}",
        requested_price=exit_price,
    )

    with get_db() as db:
        from app.models.trade import Position, Trade, TradeStatus, ExitReason

        pos = db.query(Position).filter(
            Position.id == position_id,
            Position.organization_id == ctx.org_id,
            Position.status == TradeStatus.OPEN,
        ).first()
        if not pos:
            return {"ok": False, "error": f"Open position {position_id} not found"}

        try:
            reason_enum = ExitReason[exit_reason.upper()]
        except KeyError:
            valid = [e.value for e in ExitReason]
            return {"ok": False, "error": f"Invalid exit_reason. Must be one of: {valid}"}

        # Resolve exit price
        price = exit_price
        if price is None:
            try:
                from app.data.fetcher import get_intraday_price
                result = get_intraday_price(pos.ticker, ctx.org_id, asset_type=getattr(pos, "asset_type", "EQUITY"))
                if result.get("ok"):
                    price = result["price"]
            except Exception:
                pass
        if price is None and pos.entry_price:
            price = float(pos.entry_price)

        realised_pnl = None
        if price and pos.entry_price and pos.qty:
            realised_pnl = (price - float(pos.entry_price)) * float(pos.qty)

        # Close the position.
        # NOTE: Position has no exit_price/exit_reason/closed_at/realised_pnl columns —
        # those live on Trade (as exit_price/exit_reason/exit_date/gross_pnl_aud/net_pnl_aud).
        # Setting them on `pos` previously created non-persisted phantom attributes, and
        # passing opened_at/closed_at/realised_pnl to Trade() raised AttributeError/TypeError
        # (Trade has entry_date/exit_date/hold_days/gross_pnl_aud/net_pnl_aud instead) —
        # which meant close_position() always failed and never actually closed anything.
        from app.utils.time_helper import get_current_date
        today = get_current_date()
        pos.status = TradeStatus.CLOSED

        pnl_pct = None
        if price and pos.entry_price:
            pnl_pct = (price - float(pos.entry_price)) / float(pos.entry_price) * 100

        # Create Trade record (closed trade for history/CGT)
        trade = Trade(
            organization_id = ctx.org_id,
            account_id      = pos.account_id,
            ticker          = pos.ticker,
            exchange_key    = getattr(pos, "exchange_key", "ASX"),
            asset_type      = getattr(pos, "asset_type", "EQUITY"),
            currency        = getattr(pos, "currency", "AUD"),
            signal_id       = pos.signal_id,
            entry_date      = pos.entry_date,
            exit_date       = today,
            hold_days       = (today - pos.entry_date).days,
            qty             = pos.qty,
            entry_price     = pos.entry_price,
            exit_price      = price,
            gross_pnl_aud   = round(realised_pnl, 2) if realised_pnl is not None else None,
            net_pnl_aud     = round(realised_pnl, 2) if realised_pnl is not None else None,
            pnl_pct         = round(pnl_pct, 4) if pnl_pct is not None else None,
            initial_stop    = pos.initial_stop,
            exit_reason     = reason_enum,
            is_paper        = pos.is_paper,
            cgt_eligible_discount = (today - pos.entry_date).days > 365,
        )
        db.add(trade)

        from app.models.audit import AuditAction
        _audit_action(
            db, AuditAction.POSITION_CLOSED,
            f"[MCP] Position {position_id} closed — {exit_reason} @ {price}",
            ticker=pos.ticker,
        )
        db.commit()

        # Alert notification (non-blocking)
        try:
            from app.notifications import get_notifier
            notifier = get_notifier(organization_id=ctx.org_id)
            pnl_str = f"{realised_pnl:+.2f}" if realised_pnl is not None else "N/A"
            notifier.send(
                f"📤 *Position Closed (MCP)*\n"
                f"Ticker: {pos.ticker}\n"
                f"Reason: {exit_reason}\n"
                f"Exit: ${price:.4f}\n"
                f"P&L: {pnl_str}"
            )
        except Exception as notify_err:
            try:
                _audit_action(
                    db, AuditAction.TASK_ERROR,
                    f"⚠️ [MCP] Position {position_id} closed but alert failed to send: {notify_err}",
                    ticker=pos.ticker,
                )
                db.commit()
            except Exception:
                pass

        return {
            "ok":           True,
            "position_id":  position_id,
            "ticker":       pos.ticker,
            "exit_price":   price,
            "exit_reason":  exit_reason,
            "realised_pnl": round(realised_pnl, 2) if realised_pnl is not None else None,
        }


def pause_trading(reason: str = "Paused via MCP") -> dict:
    """
    Halt automated trading for the organisation.

    No new bracket orders will be placed while trading is paused.
    Existing positions continue to be monitored for exit conditions.

    Args:
        reason: Audit note.

    Returns:
        {"ok": true, "trading_paused": true}
    """
    assert_scope("trading:write")
    ctx = _ctx()
    return _set_trading_pause(ctx.org_id, True, reason)


def resume_trading(reason: str = "Resumed via MCP") -> dict:
    """
    Re-enable automated trading for the organisation.

    Args:
        reason: Audit note.

    Returns:
        {"ok": true, "trading_paused": false}
    """
    assert_scope("trading:write")
    ctx = _ctx()
    return _set_trading_pause(ctx.org_id, False, reason)


def _set_trading_pause(org_id: int, paused: bool, reason: str) -> dict:
    with get_db() as db:
        from app.models.config import SystemConfig
        from app.models.audit import AuditAction

        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "trading_paused",
            SystemConfig.organization_id == org_id,
        ).first()
        if cfg:
            cfg.value = "true" if paused else "false"
        else:
            db.add(SystemConfig(
                key="trading_paused",
                organization_id=org_id,
                value="true" if paused else "false",
                label="Trading Paused",
                group="trading",
            ))

        ctx = get_mcp_context()
        client_id = ctx.client_id if ctx else "mcp"
        db.add(__import__("app.models.audit", fromlist=["AuditLog"]).AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=f"mcp:{client_id}",
            organization_id=org_id,
            message=f"[MCP] trading_paused={'true' if paused else 'false'} — {reason}",
        ))
        db.commit()

    return {"ok": True, "trading_paused": paused}


# ---------------------------------------------------------------------------
# Rules & Config
# ---------------------------------------------------------------------------

def get_rules(category: Optional[str] = None) -> dict:
    """
    Return the AstraTrade rule configurations for the organisation.

    Args:
        category: Filter by category: TREND_TEMPLATE, FUNDAMENTAL, VCP,
                  MARKET_REGIME, ENTRY, EXIT_DEFENSIVE, EXIT_OFFENSIVE,
                  POSITION_SIZING, PORTFOLIO, CRYPTO. Omit for all.

    Returns:
        {"rules": [...], "total": N}
    """
    assert_scope("rules:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.config import RuleConfig, RuleCategory

        q = db.query(RuleConfig).filter(
            RuleConfig.organization_id == ctx.org_id,
        )
        if category:
            try:
                q = q.filter(RuleConfig.category == RuleCategory[category.upper()])
            except KeyError:
                pass

        rules = q.order_by(RuleConfig.category, RuleConfig.sort_order).all()

        rows = []
        for r in rules:
            rows.append({
                "id":               r.id,
                "rule_id":          r.rule_id,
                "category":         r.category.value,
                "label":            r.label,
                "description":      r.description,
                "enabled_globally": r.enabled_globally,
                "is_mandatory":     r.is_mandatory,
                "threshold":        float(r.threshold) if r.threshold else None,
                "threshold_label":  r.threshold_label,
                "threshold_min":    float(r.threshold_min) if r.threshold_min else None,
                "threshold_max":    float(r.threshold_max) if r.threshold_max else None,
                "asset_types":      r.asset_types,
            })

    return {"rules": rows, "total": len(rows)}


def update_rule(
    rule_id: str,
    enabled: Optional[bool] = None,
    threshold: Optional[float] = None,
) -> dict:
    """
    Enable/disable a AstraTrade rule or adjust its threshold for the organisation.

    Args:
        rule_id:    Rule identifier string (e.g. "trend_price_above_200ma").
        enabled:    True to enable, False to disable. Omit to leave unchanged.
        threshold:  New numeric threshold value. Omit to leave unchanged.

    Returns:
        {"ok": true, "rule_id": "...", "changes": {...}}
    """
    assert_scope("rules:write")
    ctx = _ctx()

    if enabled is None and threshold is None:
        return {"ok": False, "error": "Provide at least one of: enabled, threshold"}

    with get_db() as db:
        from app.models.config import RuleConfig
        from app.models.audit import AuditAction

        rule = db.query(RuleConfig).filter(
            RuleConfig.rule_id == rule_id,
            RuleConfig.organization_id == ctx.org_id,
        ).first()
        if not rule:
            return {"ok": False, "error": f"Rule '{rule_id}' not found for this organisation"}
        if rule.is_mandatory and enabled is False:
            return {"ok": False, "error": f"Rule '{rule_id}' is mandatory and cannot be disabled"}

        changes = {}
        if enabled is not None:
            changes["enabled_globally"] = {"from": rule.enabled_globally, "to": enabled}
            rule.enabled_globally = enabled
        if threshold is not None:
            if rule.threshold_min and threshold < float(rule.threshold_min):
                return {"ok": False, "error": f"Threshold below minimum {rule.threshold_min}"}
            if rule.threshold_max and threshold > float(rule.threshold_max):
                return {"ok": False, "error": f"Threshold above maximum {rule.threshold_max}"}
            changes["threshold"] = {"from": float(rule.threshold) if rule.threshold else None, "to": threshold}
            rule.threshold = threshold

        _audit_action(
            db, AuditAction.RULE_THRESHOLD_SET,
            f"[MCP] Rule '{rule_id}' updated: {changes}",
        )
        db.commit()

    return {"ok": True, "rule_id": rule_id, "changes": changes}


def get_config(keys: Optional[list] = None) -> dict:
    """
    Return non-secret SystemConfig values for the organisation.

    Args:
        keys: Optional list of specific config keys to retrieve.
              Omit to return all non-secret config values.

    Returns:
        {"config": {"key": "value", ...}}
    """
    assert_scope("config:read")
    ctx = _ctx()

    with get_db() as db:
        from app.models.config import SystemConfig

        q = db.query(SystemConfig).filter(
            SystemConfig.organization_id == ctx.org_id,
            SystemConfig.is_secret == False,
        )
        if keys:
            q = q.filter(SystemConfig.key.in_(keys))

        configs = q.all()
        return {"config": {c.key: c.value for c in configs}}
