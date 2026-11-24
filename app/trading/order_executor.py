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
import math
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
    from app.tasks.trading import _is_kill_switch_on, _get_org_config_value, _todays_pnl_aud
    if _is_kill_switch_on(organization_id):
        return {"ok": False, "error": "Trading kill switch is enabled"}
    daily_limit = _get_org_config_value(organization_id, "max_daily_loss_aud")
    if daily_limit:
        try:
            limit = float(daily_limit)
            if not math.isfinite(limit) or limit < 0:
                raise ValueError()
        except (TypeError, ValueError):
            return {"ok": False, "error": "Daily loss limit is invalid; execution blocked"}
        if limit > 0 and _todays_pnl_aud(organization_id) <= -limit:
            return {"ok": False, "error": "Daily loss limit reached"}

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
                if price_result.get("delayed") or float(price_result.get("delay_mins") or 0) > 0:
                    return {"ok": False, "error": "Delayed quote cannot be used for automated entry"}
                entry_price = float(price_result["price"])
                data_source = price_result.get("data_source", "live")
            else:
                return {"ok": False, "error": "No current quote available; order not submitted"}
        except Exception as pe:
            logger.warning(f"execute_signal_order: price fetch failed — {pe}")
            return {"ok": False, "error": "Price retrieval failed; order not submitted"}

    if not all(math.isfinite(p) and p > 0 for p in (entry_price, pivot_price, stop_price)):
        return {"ok": False, "error": "Entry, pivot and stop must be finite positive prices"}
    if stop_price >= entry_price or (target_1 is not None and (not math.isfinite(target_1) or target_1 <= entry_price)):
        return {"ok": False, "error": "BUY bracket requires stop below entry and target above entry"}

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
            account_is_paper = acct.is_paper if acct else True
            if not org or not org.is_active or not acct:
                return {"ok": False, "error": "An active organisation and configured trading account are required"}
            capital = float(acct.capital_aud or 0)
            if not math.isfinite(capital) or capital <= 0:
                return {"ok": False, "error": "Trading capital must be explicitly configured and positive"}
            paused = db3.query(_SC).filter(_SC.key == "trading_paused", _SC.organization_id == organization_id).first()
            if paused and str(paused.value).lower() in {"true", "1", "yes"}:
                return {"ok": False, "error": "Trading is paused"}
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

    order_ref = f"astratrade-{signal_id}"

    # Persist the intent BEFORE crossing the broker boundary. A lost reply or
    # failed result commit must never make a retry submit a second order.
    from app.models.signal import Signal, SignalStatus
    from app.models.trade import Order, OrderAction, OrderType, OrderStatus
    with get_db() as intent_db:
        claimed = intent_db.query(Signal).filter(
            Signal.id == signal_id, Signal.organization_id == organization_id,
            Signal.status == SignalStatus.PENDING,
        ).update({Signal.status: SignalStatus.TRIGGERED}, synchronize_session=False)
        if claimed != 1:
            return {"ok": False, "error": "Signal already claimed; reconcile the existing order before retrying"}
        intent = Order(
            ticker=ticker, exchange_key=exchange_key, asset_type=asset_type,
            currency=currency, account_id=acct_id, organization_id=organization_id,
            signal_id=signal_id, action=OrderAction.BUY, order_type=OrderType.BRACKET,
            status=OrderStatus.PENDING, qty_ordered=qty, qty_filled=0,
            limit_price=entry_price, stop_price=stop_price, is_paper=account_is_paper,
            raw_ibkr_response={"order_ref": order_ref, "state": "intent_persisted"},
        )
        intent_db.add(intent)
        intent_db.commit()
        intent_id = intent.id

    # ── 4. Submit bracket order ──────────────────────────────────────────────
    is_crypto = asset_type == "CRYPTO" or (exchange_key and exchange_key.startswith("CRYPTO"))
    broker_name = "simulation"
    result: dict = {}

    try:
        if is_crypto:
            from app.broker.crypto import get_crypto_broker_for_org
            # A signal can belong to any enabled crypto venue.  Passing only
            # the org would silently route it through the legacy default venue
            # instead of the venue selected when the signal was screened.
            with get_crypto_broker_for_org(organization_id, exchange_key=exchange_key) as broker:
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
            # Must connect() before submit_bracket_order() — it checks
            # self.is_connected and silently falls through to a simulated fill
            # otherwise (see _simulate_order in app/broker/ibkr.py). The crypto
            # branch above gets this via get_crypto_broker_for_org()'s __enter__;
            # this branch previously built a bare IBKRBroker() and called
            # submit_bracket_order() directly with no connect() at all, so every
            # equity order silently simulated regardless of whether IBKR Gateway
            # was actually up and configured.
            with IBKRBroker(organization_id=organization_id) as ibkr:
                result = ibkr.submit_bracket_order(
                    ticker=ticker,
                    action="BUY",
                    qty=int(qty),
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_price=target_1 or entry_price * 1.20,
                    exchange_key=exchange_key,
                    order_ref=order_ref,
                    simulate_on_disconnect=account_is_paper,
                )
                broker_name = result.get("broker", "ibkr")
    except Exception as oe:
        logger.error(f"execute_signal_order: broker submit failed — {oe}")
        return {"ok": False, "error": f"Broker order submission failed: {oe}"}

    # submit_bracket_order() on both brokers catches its own exceptions and
    # returns {"status": "error", "error": ...} instead of raising — so a
    # rejected/unqualified/stuck order does NOT hit the except above. Without
    # this check, a broker-side rejection would fall straight through to
    # step 5 and create an OPEN Position for an order that was never actually
    # accepted, with a Telegram "Order Placed" confirmation to match.
    if result.get("status") == "error":
        logger.error(f"execute_signal_order: broker rejected order for {ticker} — {result.get('error')}")
        return {
            "ok": False,
            "error": f"Broker rejected the order: {result.get('error', 'unknown error')}",
            "ticker": ticker,
            "broker": broker_name,
        }

    # ── 5. Create Position record ────────────────────────────────────────────
    try:
        with get_db() as db4:
            from app.models.signal import Signal as _S2, SignalStatus as _SS
            from app.models.trade import (
                Position, Order, TradeStatus, OrderAction, OrderType, OrderStatus,
            )

            sig = db4.query(_S2).filter(
                _S2.id == signal_id, _S2.organization_id == organization_id
            ).first()
            if sig:
                sig.status = _SS.TRIGGERED

            is_simulated = result.get("status") == "simulated" or result.get("simulated") is True
            order = db4.query(Order).filter(Order.id == intent_id, Order.organization_id == organization_id).one()
            order.status = OrderStatus.FILLED if is_simulated else OrderStatus.SUBMITTED
            order.qty_filled = qty if is_simulated else 0
            order.limit_price = result.get("limit_price", entry_price)
            order.avg_fill_price = entry_price if is_simulated else None
            order.ibkr_order_id = None if is_crypto else result.get("ibkr_parent_id")
            order.perm_id = None if is_crypto else result.get("ibkr_parent_perm_id")
            order.external_order_id = result.get("entry_order_id") if is_crypto else None
            order.raw_ibkr_response = {**result, "order_ref": order_ref}
            order.submitted_at = datetime.utcnow()
            order.filled_at = datetime.utcnow() if is_simulated else None

            # IBKR's acknowledgement is not a fill. Leave the signal in its
            # triggered state and let sync_order_status create the position from
            # the execution report. Only explicit simulation has a local fill.
            if is_simulated:
                db4.add(Position(
                    organization_id=organization_id, account_id=acct_id,
                    ticker=ticker, exchange_key=exchange_key, asset_type=asset_type,
                    currency=currency, entry_date=datetime.utcnow().date(), qty=qty,
                    entry_price=entry_price, initial_stop=stop_price, current_stop=stop_price,
                    target_1=target_1 or entry_price * 1.20,
                    target_2=(target_1 or entry_price * 1.20) * 1.167,
                    current_price=entry_price, status=TradeStatus.OPEN, signal_id=signal_id,
                ))

            _audit(
                db4, AuditAction.ORDER_FILLED if is_simulated else AuditAction.ORDER_SUBMITTED,
                f"{ticker} BUY {'filled (simulated)' if is_simulated else 'submitted'} {qty:.6g} @ A${entry_price:.4f} "
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
    except Exception as e:
        logger.warning(f"execute_signal_order: order-placed notification failed for {ticker} (order itself already succeeded): {e}", exc_info=True)

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
