"""
Trading tasks — intraday entry triggers and exit rule checks.
Runs every 5 minutes during ASX market hours.
"""
from __future__ import annotations
from datetime import date
from loguru import logger

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, Order, Trade, TradeStatus, ExitReason, OrderAction, OrderType, OrderStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.data.fetcher import get_price_history
from app.data.calendar import market_is_open_now
from app.screener.rules import RuleEngine
from app.screener.vcp import check_breakout
from app.screener.exit_rules import evaluate_exit_rules
from app.risk.manager import calculate_position_size
from app.broker.ibkr import IBKRBroker
from app.notifications.whatsapp import WhatsAppNotifier


def _is_trading_paused(org_id: int) -> bool:
    with get_db() as db:
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "trading_paused",
            SystemConfig.organization_id == org_id
        ).first()
        return cfg and cfg.value.lower() == "true"


@app.task(name="app.tasks.trading.check_entry_triggers", bind=True)
def check_entry_triggers(self):
    """
    Check pending signals for intraday breakout confirmation.
    If price ≥ pivot AND volume confirms → submit bracket order.
    """
    if not market_is_open_now():
        return

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        if _is_trading_paused(org.id):
            logger.debug(f"Trading paused for Org '{org.name}' — skipping entry check")
            continue

        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = WhatsAppNotifier(organization_id=org.id)
        today    = date.today()

        with get_db() as db:
            pending_signals = db.query(Signal).filter(
                Signal.organization_id == org.id,
                Signal.signal_date == today,
                Signal.status == SignalStatus.PENDING,
            ).all()

            if not pending_signals:
                continue

            # Check market regime (global)
            regime_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_market_regime",
                SystemConfig.organization_id == None
            ).first()
            regime = regime_cfg.value if regime_cfg else "UNKNOWN"

            if regime == "BEAR":
                logger.info(f"Market in BEAR regime — skipping Org '{org.name}' new entries")
                continue

            # Get account capital
            from app.models.account import Account
            account = db.query(Account).filter(
                Account.organization_id == org.id, 
                Account.is_active == True
            ).first()
            capital = float(account.capital_aud) if account else 1000.0
            is_paper= account.is_paper if account else True

            # Count open positions
            open_count = db.query(Position).filter(
                Position.organization_id == org.id, 
                Position.status == TradeStatus.OPEN
            ).count()
            max_positions = int(engine.threshold("portfolio_max_positions") or 5)

            if open_count >= max_positions:
                logger.debug(f"Max positions reached for '{org.name}' ({open_count}/{max_positions})")
                continue

        for signal in pending_signals:
            try:
                df = get_price_history(signal.ticker, period="3mo")
                if df is None or df.empty:
                    continue

                latest = df.iloc[-1]
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)

                # Check breakout conditions
                breakout_rules = check_breakout(
                    signal.ticker, df,
                    float(signal.pivot_price),
                    avg_vol, engine
                )
                all_passed = all(r.passed for r in breakout_rules.values())

                if not all_passed:
                    continue

                # Recalculate sizing with latest price
                entry_price = float(latest["close"])
                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=entry_price,
                    stop_price=float(signal.stop_price),
                    engine=engine,
                    regime_multiplier=0.5 if regime == "CAUTION" else 1.0,
                )

                if sizing.shares < 1:
                    logger.warning(f"Signal {signal.ticker} (Org: {org.name}): position size too small ({sizing.message})")
                    continue

                # Submit bracket order via IBKR
                with IBKRBroker(organization_id=org.id) as broker:
                    result = broker.submit_bracket_order(
                        ticker=signal.ticker.replace(".AX", ""),
                        action="BUY",
                        qty=sizing.shares,
                        entry_price=entry_price,
                        stop_price=float(signal.stop_price),
                        target_price=float(signal.target_price_1 or entry_price * 1.20),
                        order_ref=f"vcpilot-{signal.id}",
                    )

                # Record order and update signal
                with get_db() as db:
                    order = Order(
                        ticker=signal.ticker,
                        account_id=account.id if account else 1,
                        organization_id=org.id,
                        signal_id=signal.id,
                        action=OrderAction.BUY,
                        order_type=OrderType.BRACKET,
                        status=OrderStatus.SUBMITTED,
                        qty_ordered=sizing.shares,
                        limit_price=entry_price,
                        stop_price=float(signal.stop_price),
                        is_paper=is_paper,
                        ibkr_order_id=result.get("ibkr_parent_id"),
                        raw_ibkr_response=result,
                    )
                    db.add(order)

                    signal_obj = db.query(Signal).get(signal.id)
                    if signal_obj:
                        signal_obj.status = SignalStatus.TRIGGERED

                    db.add(AuditLog(
                        action=AuditAction.ORDER_SUBMITTED,
                        organization_id=org.id,
                        ticker=signal.ticker,
                        message=f"Bracket order submitted: {sizing.shares}x{signal.ticker} @ {entry_price:.3f}",
                        detail=result,
                    ))

                notifier.send_order_fill(
                    signal.ticker, "BUY", sizing.shares, entry_price, is_paper
                )
                logger.info(f"Entry submitted for {org.name}: {signal.ticker} {sizing.shares}x @ {entry_price:.3f}")

            except Exception as e:
                logger.error(f"Entry trigger error for {signal.ticker} (Org: {org.name}): {e}")


@app.task(name="app.tasks.trading.check_exit_rules_task", bind=True)
def check_exit_rules_task(self):
    """
    Evaluate exit rules for all open positions.
    Submits sell orders when exit conditions are met.
    """
    if not market_is_open_now():
        return

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = WhatsAppNotifier(organization_id=org.id)
        today    = date.today()

        with get_db() as db:
            positions = db.query(Position).filter(
                Position.organization_id == org.id,
                Position.status == TradeStatus.OPEN
            ).all()

            if not positions:
                continue

        for pos in positions:
            try:
                df = get_price_history(pos.ticker, period="6mo")
                if df is None or df.empty:
                    continue

                latest     = df.iloc[-1]
                current    = float(latest["close"])
                avg_vol    = float(df["avg_vol_50"].iloc[-1] or 0)

                # Weekly closes (last 5 weeks)
                weekly_df      = df.set_index("date").resample("W-FRI")["close"].last().dropna()
                weekly_closes  = weekly_df.tail(5).tolist()[::-1]  # Latest first

                # Fetch next earnings date from fundamentals
                from app.data.fetcher import get_fundamentals
                fund_data      = get_fundamentals(pos.ticker)
                next_earnings  = fund_data.get("next_earnings_date")

                exit_signals = evaluate_exit_rules(
                    ticker=pos.ticker,
                    entry_price=float(pos.entry_price),
                    current_price=current,
                    current_stop=float(pos.current_stop),
                    entry_date=pos.entry_date,
                    today=today,
                    weekly_closes=weekly_closes,
                    df_daily=df,
                    avg_vol_50=avg_vol,
                    next_earnings_date=next_earnings,
                    engine=engine,
                )

                for exit_sig in exit_signals:
                    if not exit_sig.should_exit:
                        continue

                    qty_to_sell = pos.qty
                    if exit_sig.exit_type == "PARTIAL":
                        qty_to_sell = max(1, int(pos.qty * exit_sig.partial_pct / 100))

                    with IBKRBroker(organization_id=org.id) as broker:
                        result = broker.submit_bracket_order(
                            ticker=pos.ticker.replace(".AX", ""),
                            action="SELL",
                            qty=qty_to_sell,
                            entry_price=current,
                            stop_price=0,
                            target_price=0,
                            order_ref=f"exit-{pos.id}-{exit_sig.reason}",
                        )

                    pnl_aud = (current - float(pos.entry_price)) * qty_to_sell
                    pnl_pct = (current - float(pos.entry_price)) / float(pos.entry_price) * 100

                    with get_db() as db:
                        if exit_sig.exit_type == "FULL":
                            position_obj = db.query(Position).get(pos.id)
                            if position_obj:
                                position_obj.status = TradeStatus.CLOSED

                            trade = Trade(
                                ticker=pos.ticker,
                                account_id=pos.account_id,
                                organization_id=org.id,
                                signal_id=pos.signal_id,
                                entry_date=pos.entry_date,
                                exit_date=today,
                                hold_days=(today - pos.entry_date).days,
                                entry_price=pos.entry_price,
                                exit_price=current,
                                qty=qty_to_sell,
                                gross_pnl_aud=round(pnl_aud, 2),
                                net_pnl_aud=round(pnl_aud - 6.0, 2),  # Subtract commission
                                pnl_pct=round(pnl_pct, 4),
                                initial_stop=pos.initial_stop,
                                exit_reason=exit_sig.reason,
                                is_paper=pos.is_paper,
                                cgt_eligible_discount=(today - pos.entry_date).days > 365,
                            )
                            db.add(trade)

                        db.add(AuditLog(
                            action=AuditAction.POSITION_CLOSED,
                            organization_id=org.id,
                            ticker=pos.ticker,
                            message=f"Exit: {exit_sig.reason} | P&L ${pnl_aud:+.0f} ({pnl_pct:+.1f}%)",
                            detail={"reason": str(exit_sig.reason), "message": exit_sig.message},
                        ))

                    notifier.send_exit_alert(pos.ticker, str(exit_sig.reason),
                                             pnl_pct, pnl_aud, pos.is_paper)
                    logger.info(f"Exit for {org.name}: {pos.ticker} {exit_sig.reason} P&L ${pnl_aud:+.0f}")
                    break  # Process one exit signal at a time

            except Exception as e:
                logger.error(f"Exit check error for {pos.ticker} (Org: {org.name}): {e}")



@app.task(name="app.tasks.trading.sync_stop_orders", bind=True)
def sync_stop_orders(self):
    """Sync stop prices from DB to IBKR open orders."""
    # TODO: Implement stop order modification via IBKR modify order API
    # For now: log and alert if stop has been moved
    logger.debug("Stop sync task: placeholder — implement IBKR modify order")
