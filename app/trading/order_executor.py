"""
Shared order-execution helper.

Extracted from app.mcp.tools.place_order so that BOTH the MCP tool surface
and the Telegram agent (app.agent.commands) submit live/paper bracket orders
through one single, audited code path. Do not duplicate this logic elsewhere —
any new entry point (dashboard button, Telegram command, MCP tool, etc.) should
call execute_signal_order().

Flow:
  1. Load & validate the PENDING signal (must belong to the org).
  2. Fetch a live price (or use force_entry_price); reject if >10% extended
     past the pivot (AstraTrade "don't chase" rule).
  3. Calculate AstraTrade risk-based position size from account capital.
  4. Submit a bracket order (entry + stop + target) to the correct broker
     (CryptoBroker/ccxt for crypto, IBKRBroker — with simulation fallback —
     for equities).
  5. Persist a Position row, mark the Signal TRIGGERED, write an AuditLog row.
  6. Send a Telegram confirmation to the org's configured chat(s).

Returns the same {"ok": ..., ...} shape regardless of caller.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from loguru import logger
from app.database import get_db


def execute_signal_order(
    signal_id: int,
    organization_id: int,
    actor: str = "system",
    notes: str = "Order placed",
    force_entry_price: Optional[float] = None,
) -> dict:
    """
    Execute a AstraTrade bracket order for an existing PENDING signal.

    Args:
        signal_id:         ID of the PENDING signal to trade.
        organization_id:   Org that owns the signal — every query is scoped to it.
        actor:             Audit actor string, e.g. "mcp:claude-desktop" or
                           "telegram:123456789". Shown in the audit trail.
        notes:             Free-text note appended to the audit log message.
        force_entry_price: Override the entry price (limit-order style). If
                           omitted, the live exchange price is fetched.

    Returns:
        {
          "ok": true,
          "signal_id": N, "ticker": "...", "qty": N,
          "entry_price": N, "stop_price": N, "target_price": N,
          "risk_pct": N, "data_source": "...", "broker": "ccxt|ibkr|simulation",
          "order_ref": "...", "message": "..."
        }
        or {"ok": False, "error": "..."} on any failure.
    """
    from app.models.audit import AuditLog, AuditAction

    def _audit(db, action, message: str, ticker: str = None):
        db.add(AuditLog(
            action=action,
            actor=actor,
            organization_id=organization_id,
            message=message,
            ticker=ticker,
        ))

    # ── 1. Load & validate signal ────────────────────────────────────────────
    with get_db() as db:
        from app.models.signal import Signal, SignalStatus
        s = db.query(Signal).filter(
            Signal.id == signal_id,
            Signal.organization_id == organization_id,
        ).first()
        if not s:
            return {"ok": False, "error": f"Signal {signal_id} not found"}
        if s.status != SignalStatus.PENDING:
            return {"ok": False, "error": f"Signal is {s.status.value}, not PENDING"}

        ticker       = s.ticker
        exchange_key = s.exchange_key
        asset_type   = s.asset_type
        currency     = getattr(s, "currency", "AUD")
        pivot_price  = float(s.pivot_price) if s.pivot_price else None
        stop_price   = float(s.stop_price)  if s.stop_price  else None
        target_1     = float(s.target_price_1) if s.target_price_1 else None

    if not pivot_price or not stop_price:
        return {"ok": False, "error": "Signal is missing pivot or stop price"}

    # ── 2. Fetch live price ──────────────────────────────────────────────────
    entry_price = force_entry_price
    data_source = "manual"
    if entry_price is None:
        try:
            from app.data.fetcher import get_intraday_price
            price_result = get_intraday_price(ticker, organization_id, asset_type=asset_type)
            if price_result.get("ok") and price_result.get("price"):
                entry_price = float(price_result["price"])
                data_source = price_result.get("data_source", "live")
            else:
                with get_db() as db2:
                    from app.models.signal import Signal as _S
                    _sig = db2.query(_S).get(signal_id)
                    entry_price = float(_sig.close_price) if _sig and _sig.close_price else pivot_price
                    data_source = "eod_fallback"
        except Exception as pe:
            logger.warning(f"execute_signal_order: price fetch failed — {pe}. Using pivot.")
            entry_price = pivot_price
            data_source = "pivot_fallback"

    # Reject if price is more than 10% above pivot (over-extended)
    extension_pct = (entry_price - pivot_price) / pivot_price * 100
    if extension_pct > 10:
        return {
            "ok": False,
            "error": f"Price A${entry_price:.4f} is {extension_pct:.1f}% above pivot "
                     f"A${pivot_price:.4f} — over-extended. Wait for a pullback.",
        }

    # ── 3. Calculate position size ───────────────────────────────────────────
    try:
        from app.risk.manager import calculate_position_size
        from app.screener.rules import RuleEngine
        from app.models.account import Account, Organization
        from app.models.config import SystemConfig as _SC

        with get_db() as db3:
            org  = db3.query(Organization).get(organization_id)
            acct = db3.query(Account).filter(
                Account.organization_id == organization_id, Account.is_active == True
            ).first()
            acct_id = acct.id if acct else None
            capital = float(acct.capital_aud) if acct and acct.capital_aud else 5000.0
            base_currency_cfg = db3.query(_SC).filter(
                _SC.key == "working_capital_currency", _SC.organization_id == organization_id
            ).first()
            base_currency = base_currency_cfg.value if base_currency_cfg else "AUD"

        engine = RuleEngine(
            organization_id=organization_id,
            tier=org.tier.value if org else "BRONZE",
            asset_type=asset_type,
        )

        # Share Price Range Filter (equity only, opt-in) — final defensive
        # gate before any capital is committed. Applies to every caller of
        # this function: MCP place_order, Telegram agent, and any future
        # entry point, since this is the single shared order-submission
        # path (see module docstring).
        if asset_type != "CRYPTO":
            from app.screener.price_filter import price_in_range
            in_range, range_reason = price_in_range(ticker, entry_price, engine, asset_type)
            if not in_range:
                with get_db() as db_pf:
                    _audit(db_pf, AuditAction.TASK_RUN, f"Order rejected — {range_reason}", ticker=ticker)
                    db_pf.commit()
                return {"ok": False, "error": f"Price out of configured range — {range_reason}"}

        sizing = calculate_position_size(
            capital_aud=capital,
            entry_price=entry_price,
            stop_price=stop_price,
            engine=engine,
            currency=currency,
            base_currency=base_currency,
            is_crypto=(asset_type == "CRYPTO"),
        )
        qty = sizing.shares
    except Exception as se:
        logger.error(f"execute_signal_order: sizing failed — {se}")
        return {"ok": False, "error": f"Position sizing failed: {se}"}

    if not qty or qty <= 0:
        return {"ok": False, "error": "Position size calculated as zero — check capital and risk settings"}

    order_ref = f"AGENT-{signal_id}-{int(datetime.utcnow().timestamp())}"

    # ── 4. Submit bracket order ──────────────────────────────────────────────
    is_crypto = asset_type == "CRYPTO" or (exchange_key and exchange_key.startswith("CRYPTO"))
    broker_name = "simulation"
    result: dict = {}

    try:
        if is_crypto:
            from app.broker.crypto import get_crypto_broker_for_org
            with get_crypto_broker_for_org(organization_id) as broker:
                result = broker.submit_bracket_order(
                    ticker=ticker,
                    action="BUY",
                    qty=qty,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_price=target_1 or entry_price * 1.20,
                    order_ref=order_ref,
                )
                broker_name = result.get("broker", "ccxt")
        else:
            from app.broker.ibkr import IBKRBroker
            ibkr = IBKRBroker()
            result = ibkr.submit_bracket_order(
                ticker=ticker,
                action="BUY",
                qty=int(qty),
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_1 or entry_price * 1.20,
                exchange_key=exchange_key,
                order_ref=order_ref,
            )
            broker_name = result.get("broker", "ibkr")
    except Exception as oe:
        logger.error(f"execute_signal_order: broker submit failed — {oe}")
        return {"ok": False, "error": f"Broker order submission failed: {oe}"}

    # ── 5. Create Position record ────────────────────────────────────────────
    try:
        with get_db() as db4:
            from app.models.signal import Signal as _S2, SignalStatus as _SS
            from app.models.trade import Position, TradeStatus

            sig = db4.query(_S2).filter(
                _S2.id == signal_id, _S2.organization_id == organization_id
            ).first()
            if sig:
                sig.status = _SS.TRIGGERED

            pos = Position(
                organization_id  = organization_id,
                account_id       = acct_id,
                ticker           = ticker,
                exchange_key     = exchange_key,
                asset_type       = asset_type,
                currency         = currency,
                entry_date       = datetime.utcnow().date(),
                qty              = qty,
                entry_price      = entry_price,
                initial_stop     = stop_price,
                current_stop     = stop_price,
                target_1         = target_1 or entry_price * 1.20,
                target_2         = (target_1 or entry_price * 1.20) * 1.167,
                current_price    = entry_price,
                status           = TradeStatus.OPEN,
                signal_id        = signal_id,
            )
            db4.add(pos)

            _audit(
                db4, AuditAction.ORDER_FILLED,
                f"{ticker} BUY {qty:.6g} @ A${entry_price:.4f} "
                f"stop A${stop_price:.4f} target A${(target_1 or entry_price*1.20):.4f} "
                f"broker={broker_name} ref={order_ref} — {notes}",
                ticker=ticker,
            )
            db4.commit()
    except Exception as pe2:
        logger.error(f"execute_signal_order: DB position save failed — {pe2}")
        return {
            "ok": False,
            "warning": "ORDER MAY HAVE BEEN PLACED but position DB record failed to save. Check exchange manually!",
            "ticker": ticker,
            "order_ref": order_ref,
            "error": str(pe2),
        }

    # ── 6. Alert notification ────────────────────────────────────────────────
    try:
        from app.notifications import get_notifier
        notifier = get_notifier(organization_id=organization_id)
        notifier.send(
            f"🟢 *Order Placed*\n"
            f"*{ticker}* BUY {qty:.6g}\n"
            f"Entry: A${entry_price:.4f}\n"
            f"Stop: A${stop_price:.4f} ({((entry_price-stop_price)/entry_price*100):.1f}% risk)\n"
            f"Target: A${(target_1 or entry_price*1.20):.4f}\n"
            f"Source: {data_source} | Broker: {broker_name}"
        )
    except Exception:
        pass

    return {
        "ok":           True,
        "signal_id":    signal_id,
        "ticker":       ticker,
        "qty":          qty,
        "entry_price":  entry_price,
        "stop_price":   stop_price,
        "target_price": target_1 or entry_price * 1.20,
        "risk_pct":     round((entry_price - stop_price) / entry_price * 100, 2),
        "data_source":  data_source,
        "broker":       broker_name,
        "order_ref":    order_ref,
        "message":      f"Bracket order submitted for {ticker}: {qty:.6g} units @ A${entry_price:.4f}",
    }
