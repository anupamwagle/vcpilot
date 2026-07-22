"""Broker-truth stop amendments for manual trader controls."""
from __future__ import annotations

from datetime import datetime

from app.database import get_db
from app.models.audit import AuditAction, AuditLog
from app.models.trade import Order, OrderAction, OrderStatus, OrderType, Position, TradeStatus


def request_stop_update(position_id: int, organization_id: int, new_stop: float, actor: str = "system") -> dict:
    """Raise a protective stop and update local state only after broker acceptance."""
    with get_db() as db:
        pos = db.query(Position).filter(
            Position.id == position_id, Position.organization_id == organization_id,
            Position.status == TradeStatus.OPEN,
        ).first()
        if not pos:
            return {"ok": False, "error": "Open position not found"}
        old_stop = float(pos.current_stop)
        if new_stop <= old_stop:
            return {"ok": False, "error": "Stops may only be raised; lowering protection is refused"}
        if pos.current_price and new_stop >= float(pos.current_price):
            return {"ok": False, "error": "New stop must remain below the current market price"}
        if pos.is_paper:
            pos.current_stop = new_stop
            db.add(AuditLog(action=AuditAction.STOP_UPDATED, actor=actor, organization_id=organization_id,
                            ticker=pos.ticker, message=f"Paper stop updated: ${old_stop:.4f} → ${new_stop:.4f}"))
            return {"ok": True, "status": "updated", "old_stop": old_stop, "new_stop": new_stop}
        ticker, exchange_key, asset_type, qty = pos.ticker, pos.exchange_key, pos.asset_type, float(pos.qty)
        currency, account_id, signal_id = pos.currency, pos.account_id, pos.signal_id
        protective_orders = db.query(Order).filter(
            Order.organization_id == organization_id, Order.ticker == ticker,
            Order.action == OrderAction.SELL, Order.order_type == OrderType.STOP,
            Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PARTIAL]),
        ).all()

    is_crypto = asset_type == "CRYPTO" or (exchange_key or "").startswith("CRYPTO")
    if not is_crypto:
        from app.broker.ibkr import IBKRBroker
        with IBKRBroker(organization_id=organization_id) as broker:
            if not broker.is_connected:
                return {"ok": False, "error": "IBKR gateway is not connected"}
            stops = [o for o in broker.get_open_orders()
                     if (o.get("ticker") or "").upper() == ticker.replace(".AX", "").upper()
                     and o.get("action") == "SELL" and o.get("order_type") in ("STP", "STP LMT")]
            if not stops:
                return {"ok": False, "error": "No working IBKR protective stop found"}
            ok, reason = broker.modify_stop_order(stops[0]["ibkr_order_id"], new_stop, exchange_key or "ASX")
            if not ok:
                return {"ok": False, "error": reason}
    else:
        # CCXT does not provide portable in-place stop amendments.  Cancel all
        # tracked native protection, then replace it atomically as far as the
        # venue permits.  If replacement fails, submit an emergency exit rather
        # than silently retaining unprotected live inventory.
        from app.broker.crypto import get_crypto_broker_for_org
        with get_crypto_broker_for_org(organization_id, exchange_key=exchange_key) as broker:
            for order in protective_orders:
                if not broker.cancel_order(order.external_order_id, ticker):
                    return {"ok": False, "error": "Unable to cancel current native crypto stop"}
            result = broker.submit_protective_stop(ticker, qty, new_stop, order_ref=f"protect-{position_id}-manual")
            emergency = None
            if result.get("status") == "error":
                emergency = broker.submit_market_sell(ticker, qty, order_ref=f"exit-{position_id}-STOP_LOSS")
        if result.get("status") == "error":
            return {"ok": False, "error": "New crypto stop rejected; emergency exit workflow submitted" if emergency and emergency.get("status") != "error" else "New crypto stop rejected and emergency exit failed"}

    with get_db() as db:
        pos = db.query(Position).filter(Position.id == position_id, Position.status == TradeStatus.OPEN).first()
        if not pos:
            return {"ok": False, "error": "Position changed while amending stop; verify broker"}
        pos.current_stop = new_stop
        if is_crypto:
            for old in protective_orders:
                fresh = db.query(Order).filter(Order.id == old.id).first()
                if fresh:
                    fresh.status = OrderStatus.CANCELLED
                    fresh.cancelled_at = datetime.utcnow()
            raw = dict(result)
            raw.update({"position_id": position_id, "exit_reason": "STOP_LOSS", "protective_stop": True})
            db.add(Order(ticker=ticker, exchange_key=exchange_key, asset_type="CRYPTO", currency=currency,
                         account_id=account_id, organization_id=organization_id, signal_id=signal_id,
                         action=OrderAction.SELL, order_type=OrderType.STOP, status=OrderStatus.SUBMITTED,
                         qty_ordered=qty, qty_filled=0, stop_price=new_stop,
                         external_order_id=result.get("entry_order_id"), raw_ibkr_response=raw,
                         is_paper=False, submitted_at=datetime.utcnow()))
        db.add(AuditLog(action=AuditAction.STOP_UPDATED, actor=actor, organization_id=organization_id,
                        ticker=ticker, message=f"Broker stop updated: ${old_stop:.4f} → ${new_stop:.4f}",
                        detail={"source": "stop_executor", "position_id": position_id}))
    return {"ok": True, "status": "updated", "old_stop": old_stop, "new_stop": new_stop}
