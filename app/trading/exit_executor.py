"""One safe execution path for discretionary position exits.

Manual dashboard/MCP/Telegram exits must not mutate a live Position merely
because the user clicked a button.  This module records the broker submission
and lets the normal reconciliation tasks create the final Trade from fill truth.
"""
from __future__ import annotations

from datetime import datetime

from app.database import get_db
from app.models.audit import AuditAction, AuditLog
from app.models.trade import ExitReason, Order, OrderAction, OrderStatus, OrderType, Position, Trade, TradeStatus


def request_position_exit(
    position_id: int, organization_id: int, exit_reason: str | ExitReason,
    actor: str = "system", requested_price: float | None = None,
) -> dict:
    """Submit a full exit and return submission state; never fake a live fill."""
    try:
        reason = exit_reason if isinstance(exit_reason, ExitReason) else ExitReason(str(exit_reason).replace("ExitReason.", ""))
    except ValueError:
        return {"ok": False, "error": f"Unknown exit reason: {exit_reason}"}

    with get_db() as db:
        pos = db.query(Position).filter(
            Position.id == position_id, Position.organization_id == organization_id,
            Position.status == TradeStatus.OPEN,
        ).first()
        if not pos:
            return {"ok": False, "error": "Open position not found"}
        working_sells = db.query(Order).filter(
            Order.organization_id == organization_id, Order.ticker == pos.ticker,
            Order.action == OrderAction.SELL,
            Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PARTIAL]),
        ).all()
        protective_orders = [o for o in working_sells if (o.raw_ibkr_response or {}).get("protective_stop")]
        already = next((o for o in working_sells if o not in protective_orders), None)
        if already:
            return {"ok": False, "error": "A sell order is already working for this position"}

        # Simulated fills are immediately final by definition.  All real fills
        # follow the submit-and-reconcile branch below.
        if pos.is_paper:
            fill_price = float(requested_price or pos.current_price or pos.entry_price)
            qty = float(pos.qty)
            entry = float(pos.entry_price)
            pnl_local = (fill_price - entry) * qty
            pos.status = TradeStatus.CLOSED
            db.add(Trade(
                ticker=pos.ticker, exchange_key=pos.exchange_key, asset_type=pos.asset_type,
                currency=pos.currency, account_id=pos.account_id, organization_id=organization_id,
                signal_id=pos.signal_id, position_id=pos.id, entry_date=pos.entry_date,
                exit_date=datetime.utcnow().date(),
                hold_days=(datetime.utcnow().date() - pos.entry_date).days if pos.entry_date else 0,
                entry_price=pos.entry_price, exit_price=fill_price, qty=qty,
                gross_pnl_aud=round(pnl_local, 2), commission_aud=0,
                net_pnl_aud=round(pnl_local, 2), pnl_pct=round((fill_price-entry)/entry, 6) if entry else 0,
                initial_stop=pos.initial_stop, exit_reason=reason, is_paper=True,
                cgt_eligible_discount=False,
            ))
            db.add(AuditLog(
                action=AuditAction.POSITION_CLOSED, actor=actor, organization_id=organization_id,
                ticker=pos.ticker, message=f"Simulated exit filled: {reason.value} @ ${fill_price:.4f}",
                detail={"source": "exit_executor", "position_id": pos.id},
            ))
            return {"ok": True, "status": "filled", "ticker": pos.ticker, "price": fill_price}

        ticker, exchange_key, asset_type, qty = pos.ticker, pos.exchange_key, pos.asset_type, float(pos.qty)
        account_id, signal_id, currency = pos.account_id, pos.signal_id, pos.currency
        protective_ids = [o.id for o in protective_orders]

    is_crypto = asset_type == "CRYPTO" or (exchange_key or "").startswith("CRYPTO")
    if is_crypto:
        from app.broker.crypto import get_crypto_broker_for_org
        with get_crypto_broker_for_org(organization_id, exchange_key=exchange_key) as broker:
            for protective in protective_orders:
                if not broker.cancel_order(protective.external_order_id, ticker):
                    return {"ok": False, "error": "Unable to cancel existing native protective stop; exit not submitted"}
            result = broker.submit_market_sell(ticker, qty, order_ref=f"exit-{position_id}-{reason.value}")
    else:
        from app.broker.ibkr import IBKRBroker
        with IBKRBroker(organization_id=organization_id) as broker:
            result = broker.submit_market_sell(
                ticker=ticker.replace(".AX", ""), qty=qty, exchange_key=exchange_key,
                order_ref=f"exit-{position_id}-{reason.value}", simulate_on_disconnect=False,
            )
    if result.get("status") == "error":
        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR, actor=actor, organization_id=organization_id, ticker=ticker,
                message=f"Manual exit rejected; position remains OPEN: {result.get('error', 'unknown error')}",
                detail={"source": "exit_executor", "position_id": position_id, "result": result},
            ))
        return {"ok": False, "error": result.get("error", "Broker rejected exit")}

    with get_db() as db:
        pos = db.query(Position).filter(Position.id == position_id, Position.status == TradeStatus.OPEN).first()
        if not pos:
            return {"ok": False, "error": "Position changed while submitting exit; check broker"}
        raw = dict(result)
        raw.update({"position_id": pos.id, "exit_reason": reason.value})
        for protective_id in protective_ids:
            protective = db.query(Order).filter(Order.id == protective_id).first()
            if protective and protective.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL):
                protective.status = OrderStatus.CANCELLED
                protective.cancelled_at = datetime.utcnow()
        db.add(Order(
            ticker=ticker, exchange_key=exchange_key, asset_type=asset_type, currency=currency,
            account_id=account_id, organization_id=organization_id, signal_id=signal_id,
            action=OrderAction.SELL, order_type=OrderType.MARKET, status=OrderStatus.SUBMITTED,
            qty_ordered=qty, qty_filled=0,
            ibkr_order_id=None if is_crypto else result.get("ibkr_order_id"),
            perm_id=None if is_crypto else result.get("ibkr_perm_id"),
            external_order_id=result.get("entry_order_id") if is_crypto else None,
            raw_ibkr_response=raw, is_paper=False, submitted_at=datetime.utcnow(),
        ))
        db.add(AuditLog(
            action=AuditAction.ORDER_SUBMITTED, actor=actor, organization_id=organization_id, ticker=ticker,
            message=f"Manual exit submitted; awaiting broker fill: {qty:g}x {ticker} ({reason.value})",
            detail={"source": "exit_executor", "position_id": position_id, "result": result},
        ))
    return {"ok": True, "status": "submitted", "ticker": ticker, "qty": qty}
