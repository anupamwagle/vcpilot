"""Controlled Minervini pyramid add-on submissions.

The function deliberately submits only after an operator/MCP call and an
explicit account opt-in.  Quantity and pyramid count change only on a confirmed
broker fill (or explicit paper simulation).
"""
from __future__ import annotations

from datetime import datetime

from app.database import get_db
from app.models.audit import AuditAction, AuditLog
from app.models.trade import Order, OrderAction, OrderStatus, OrderType, Position, TradeStatus
from app.risk.manager import SizingResult, calculate_pyramid_size
from app.screener.rules import RuleEngine


def request_pyramid_add_on(position_id: int, organization_id: int, actor: str = "system", price: float | None = None) -> dict:
    from app.models.account import Account, Organization
    from app.data.fetcher import get_intraday_price

    with get_db() as db:
        pos = db.query(Position).filter(
            Position.id == position_id, Position.organization_id == organization_id,
            Position.status == TradeStatus.OPEN,
        ).first()
        account = db.query(Account).filter(Account.id == pos.account_id).first() if pos else None
        org = db.query(Organization).filter(Organization.id == organization_id).first()
        if not pos or not account or not org:
            return {"ok": False, "error": "Open position or trading account not found"}
        if not account.tier or not account.tier.allow_pyramid:
            return {"ok": False, "error": "Pyramiding is disabled for this account"}
        existing = db.query(Order).filter(
            Order.organization_id == organization_id, Order.action == OrderAction.BUY,
            Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PARTIAL]),
        ).all()
        if any((o.raw_ibkr_response or {}).get("pyramid_position_id") == pos.id for o in existing):
            return {"ok": False, "error": "A pyramid add-on is already awaiting a broker fill"}

        engine = RuleEngine(organization_id=organization_id, tier=org.tier.value, asset_type=pos.asset_type)
        live_price = float(price or 0)
        if live_price <= 0:
            snap = get_intraday_price(pos.ticker, organization_id, asset_type=pos.asset_type)
            live_price = float(snap.get("price") or 0)
        if live_price <= 0:
            return {"ok": False, "error": "No reliable live price for pyramid add-on"}
        profit_pct = (live_price - float(pos.entry_price)) / float(pos.entry_price) * 100
        count = int(pos.pyramid_count or 0)
        # Recover the original tranche size from the controlled 1.0 / 1.5
        # pyramid geometry.  A second add-on is only 25% of that original.
        original_qty = float(pos.qty) if count == 0 else float(pos.qty) / 1.5
        original = SizingResult(original_qty, 0, 0, float(pos.risk_aud or 0), 0, 0,
                                float(pos.current_stop), float(pos.entry_price), pos.currency, 1.0, "original tranche")
        sizing = calculate_pyramid_size(original, profit_pct, count + 1, engine)
        if not sizing or sizing.shares <= 0:
            return {"ok": False, "error": "Position has not met controlled pyramid profit/count requirements"}
        ticker, exchange_key, qty, asset_type = pos.ticker, pos.exchange_key, sizing.shares, pos.asset_type
        account_id, currency, paper = pos.account_id, pos.currency, pos.is_paper

    is_crypto = asset_type == "CRYPTO" or (exchange_key or "").startswith("CRYPTO")
    if is_crypto:
        from app.broker.crypto import get_crypto_broker_for_org
        with get_crypto_broker_for_org(organization_id, exchange_key=exchange_key) as broker:
            result = broker.submit_market_buy(ticker, qty, order_ref=f"pyramid-{position_id}-{count + 1}")
    else:
        from app.broker.ibkr import IBKRBroker
        with IBKRBroker(organization_id=organization_id) as broker:
            result = broker.submit_market_buy(ticker.replace(".AX", ""), int(qty), exchange_key,
                                              order_ref=f"pyramid-{position_id}-{count + 1}", simulate_on_disconnect=paper)
    if result.get("status") == "error":
        return {"ok": False, "error": result.get("error", "Broker rejected pyramid add-on")}

    simulated = result.get("status") == "simulated" or result.get("simulated") is True
    with get_db() as db:
        pos = db.query(Position).filter(Position.id == position_id, Position.status == TradeStatus.OPEN).first()
        raw = dict(result)
        raw.update({"pyramid_position_id": position_id, "pyramid_number": count + 1})
        db.add(Order(
            ticker=ticker, exchange_key=exchange_key, asset_type=asset_type, currency=currency,
            account_id=account_id, organization_id=organization_id, action=OrderAction.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED if simulated else OrderStatus.SUBMITTED,
            qty_ordered=qty, qty_filled=qty if simulated else 0,
            avg_fill_price=live_price if simulated else None,
            ibkr_order_id=None if is_crypto else result.get("ibkr_order_id"),
            perm_id=None if is_crypto else result.get("ibkr_perm_id"),
            external_order_id=result.get("entry_order_id") if is_crypto else None,
            raw_ibkr_response=raw, is_paper=paper, submitted_at=datetime.utcnow(),
            filled_at=datetime.utcnow() if simulated else None,
        ))
        if simulated and pos:
            old_qty = float(pos.qty)
            pos.qty = old_qty + float(qty)
            pos.avg_cost = round((old_qty * float(pos.entry_price) + float(qty) * live_price) / float(pos.qty), 4)
            pos.pyramid_count = count + 1
        db.add(AuditLog(
            action=AuditAction.ORDER_FILLED if simulated else AuditAction.ORDER_SUBMITTED,
            actor=actor, organization_id=organization_id, ticker=ticker,
            message=f"Pyramid #{count + 1} {'filled (simulated)' if simulated else 'submitted'}: {qty:g}x {ticker}",
            detail={"source": "pyramid_executor", "position_id": position_id, "result": result},
        ))
    return {"ok": True, "status": "filled" if simulated else "submitted", "ticker": ticker, "qty": qty}
