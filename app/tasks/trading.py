"""
Trading tasks — intraday entry triggers and exit rule checks.
Runs every 5 minutes during ASX market hours.
"""
from __future__ import annotations
from datetime import date, datetime as _dt
from loguru import logger

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, Order, Trade, TradeStatus, ExitReason, OrderAction, OrderType, OrderStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.data.fetcher import get_price_history, get_intraday_price
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
    from app.utils.time_helper import get_current_time, get_current_date
    now_dt = get_current_time()
    now_str = now_dt.strftime("%H:%M")
    if not market_is_open_now():
        # Write a lightweight audit entry so Task Log shows the task fired even outside market hours
        try:
            with get_db() as _db:
                from app.models.account import Organization
                for org in _db.query(Organization).filter(Organization.is_active == True).all():
                    _db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        message=f"Entry check @ {now_str}: market closed — skipping",
                    ))
                _db.commit()
        except Exception:
            pass
        logger.debug("check_entry_triggers: market closed — skipping")
        return
    logger.info("check_entry_triggers: running intraday entry scan")

    # Write task-started audit entry for every org so Task Log shows it
    try:
        with get_db() as _db:
            from app.models.account import Organization as _Org
            for org in _db.query(_Org).filter(_Org.is_active == True).all():
                _db.add(AuditLog(
                    action=AuditAction.TASK_RUN,
                    organization_id=org.id,
                    message=f"Entry check @ {now_str}: scanning for breakout triggers",
                ))
            _db.commit()
    except Exception:
        pass

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        today = get_current_date()
        if _is_trading_paused(org.id):
            logger.debug(f"Trading paused for Org '{org.name}' — skipping entry check")
            try:
                with get_db() as _db:
                    pending_signals = _db.query(Signal).filter(
                        Signal.organization_id == org.id,
                        Signal.status == SignalStatus.PENDING,
                    ).all()
                    for sig in pending_signals:
                        _db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=sig.ticker,
                            message="Entry check: skipped because trading is paused for this organization",
                            detail={"signal_id": sig.id, "result": "skipped_paused"}
                        ))
                    _db.commit()
            except Exception as e:
                logger.error(f"Failed to log paused check for {org.name}: {e}")
            continue

        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = WhatsAppNotifier(organization_id=org.id)

        with get_db() as db:
            pending_signals = db.query(Signal).filter(
                Signal.organization_id == org.id,
                Signal.status == SignalStatus.PENDING,
            ).all()

            if not pending_signals:
                # Write a brief task run entry so Task Log shows activity
                try:
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        message=f"Entry check: 0 pending signals — nothing to do",
                    ))
                except Exception:
                    pass
                continue

            # Check market regime (global) — respect mock time if enabled
            mock_enabled_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "mock_time_enabled",
                SystemConfig.organization_id == None
            ).first()
            mock_enabled = mock_enabled_cfg and mock_enabled_cfg.value.lower() == "true"

            if mock_enabled:
                regime_key = "mock_market_regime"
            else:
                regime_key = "last_market_regime"

            regime_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == regime_key,
                SystemConfig.organization_id == None
            ).first()
            # Fall back to last_market_regime if mock_market_regime not seeded yet
            if not regime_cfg and mock_enabled:
                regime_cfg = db.query(SystemConfig).filter(
                    SystemConfig.key == "last_market_regime",
                    SystemConfig.organization_id == None
                ).first()
            regime = regime_cfg.value if regime_cfg else "UNKNOWN"

            if regime == "BEAR":
                logger.info(f"Market in BEAR regime — skipping Org '{org.name}' new entries")
                try:
                    for signal in pending_signals:
                        db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message="Entry check: skipped because market is in BEAR regime",
                            detail={"signal_id": signal.id, "result": "skipped_bear_regime"}
                        ))
                except Exception as e:
                    logger.error(f"Failed to log BEAR check for {org.name}: {e}")
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
                try:
                    for signal in pending_signals:
                        db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"Entry check: skipped because max positions reached ({open_count}/{max_positions})",
                            detail={"signal_id": signal.id, "result": "skipped_max_positions"}
                        ))
                except Exception as e:
                    logger.error(f"Failed to log max positions check for {org.name}: {e}")
                continue

        for signal in pending_signals:
            try:
                # Fetch EOD history for indicators (MAs, 52w range, ATR, avg vol)
                df = get_price_history(signal.ticker, period="3mo")
                if df is None or df.empty:
                    with get_db() as _db:
                        _db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"⚠ {signal.ticker}: no price data — skipping breakout check",
                            detail={"result": "no_data", "signal_id": signal.id},
                        ))
                    continue

                eod_latest = df.iloc[-1]
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)

                # Fetch intraday price (IBKR real-time → yfinance 15-min delayed → EOD fallback)
                intraday = get_intraday_price(signal.ticker, organization_id=org.id)
                if intraday["ok"] and intraday["price"]:
                    close_price  = intraday["price"]
                    vol_current  = intraday["volume"] or int(eod_latest.get("volume", 0))
                    data_source  = intraday["data_source"]
                    delay_mins   = intraday["delay_mins"] or 20
                    bar_ts       = intraday["bar_timestamp"]
                else:
                    # Fall back to last EOD close when intraday unavailable
                    close_price  = float(eod_latest["close"])
                    vol_current  = int(eod_latest.get("volume", 0))
                    data_source  = "eod_fallback"
                    delay_mins   = None
                    bar_ts       = None

                # Inject intraday close into the DataFrame so breakout check uses it
                df_check = df.copy()
                df_check.loc[df_check.index[-1], "close"] = close_price

                # Apply any per-signal rule overrides before checking breakout
                overrides = signal.rule_overrides or {}
                if overrides:
                    engine.apply_signal_overrides(overrides)

                # Check breakout conditions
                breakout_rules = check_breakout(
                    signal.ticker, df_check,
                    float(signal.pivot_price),
                    avg_vol, engine
                )
                all_passed = all(r.passed for r in breakout_rules.values())

                rule_detail = {
                    rid: {
                        "passed": r.passed,
                        "value": r.value,
                        "threshold": r.threshold,
                        "message": r.message,
                    }
                    for rid, r in breakout_rules.items()
                }
                failed = [r.message for r in breakout_rules.values() if not r.passed]
                pivot_price = float(signal.pivot_price)
                pct_vs_pivot = round((close_price - pivot_price) / pivot_price * 100, 4) if pivot_price else None

                if all_passed:
                    summary = f"✅ {signal.ticker}: breakout confirmed @ ${close_price:.3f} [{data_source}] — submitting order"
                else:
                    summary = (
                        f"❌ {signal.ticker} @ ${close_price:.3f} [{data_source}] | "
                        f"pivot ${pivot_price:.3f} — " + "; ".join(failed)
                    )

                with get_db() as _db:
                    from app.models.market import EntryCheckLog
                    from app.models.market import PriceBar
                    from sqlalchemy import desc as _desc
                    # Pull MA data from latest EOD bar in DB for storage
                    eod_bar = _db.query(PriceBar).filter(
                        PriceBar.ticker == signal.ticker
                    ).order_by(_desc(PriceBar.date)).first()

                    _db.add(EntryCheckLog(
                        organization_id=org.id,
                        signal_id=signal.id,
                        ticker=signal.ticker,
                        checked_at=now_dt,
                        price_current=round(close_price, 4),
                        price_pivot=pivot_price,
                        price_stop=float(signal.stop_price) if signal.stop_price else None,
                        price_vs_pivot=pct_vs_pivot,
                        vol_current=vol_current,
                        vol_avg_50=round(avg_vol, 2) if avg_vol else None,
                        vol_ratio=round(vol_current / avg_vol, 4) if avg_vol and vol_current else None,
                        ma_10=float(eod_bar.ma_10) if eod_bar and eod_bar.ma_10 else None,
                        ma_50=float(eod_bar.ma_50) if eod_bar and eod_bar.ma_50 else None,
                        ma_150=float(eod_bar.ma_150) if eod_bar and eod_bar.ma_150 else None,
                        ma_200=float(eod_bar.ma_200) if eod_bar and eod_bar.ma_200 else None,
                        high_52w=float(eod_bar.high_52w) if eod_bar and eod_bar.high_52w else None,
                        low_52w=float(eod_bar.low_52w) if eod_bar and eod_bar.low_52w else None,
                        pct_from_52w_high=float(eod_bar.pct_from_52w_high) if eod_bar and eod_bar.pct_from_52w_high else None,
                        rs_rating=float(eod_bar.rs_rating) if eod_bar and eod_bar.rs_rating else None,
                        breakout_confirmed=all_passed,
                        rule_results=rule_detail,
                        data_source=data_source,
                        data_delay_mins=delay_mins,
                        bar_timestamp=bar_ts,
                    ))

                    _db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        ticker=signal.ticker,
                        message=summary,
                        detail={
                            "signal_id": signal.id,
                            "close": close_price,
                            "pivot": pivot_price,
                            "avg_vol": round(avg_vol),
                            "data_source": data_source,
                            "delay_mins": delay_mins,
                            "result": "triggered" if all_passed else "not_triggered",
                            "rules": rule_detail,
                            "overrides_applied": overrides,
                        },
                    ))

                if not all_passed:
                    # Reset overrides for next signal
                    engine.clear_signal_overrides()
                    continue

                engine.clear_signal_overrides()

                # Recalculate sizing with intraday price
                entry_price = close_price
                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=entry_price,
                    stop_price=float(signal.stop_price),
                    engine=engine,
                    regime_multiplier=0.5 if regime == "CAUTION" else 1.0,
                )

                if sizing.shares < 1:
                    logger.warning(f"Signal {signal.ticker} (Org: {org.name}): position size too small ({sizing.message})")
                    engine.clear_signal_overrides()
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
                    is_simulated = (result.get("status") == "simulated")
                    order_status = OrderStatus.FILLED if is_simulated else OrderStatus.SUBMITTED
                    qty_filled   = sizing.shares if is_simulated else 0
                    avg_fill_price = entry_price if is_simulated else None
                    filled_at    = _dt.utcnow() if is_simulated else None

                    order = Order(
                        ticker=signal.ticker,
                        account_id=account.id if account else 1,
                        organization_id=org.id,
                        signal_id=signal.id,
                        action=OrderAction.BUY,
                        order_type=OrderType.BRACKET,
                        status=order_status,
                        qty_ordered=sizing.shares,
                        qty_filled=qty_filled,
                        limit_price=entry_price,
                        stop_price=float(signal.stop_price),
                        avg_fill_price=avg_fill_price,
                        is_paper=is_paper,
                        ibkr_order_id=result.get("ibkr_parent_id"),
                        raw_ibkr_response=result,
                        submitted_at=_dt.utcnow(),
                        filled_at=filled_at,
                    )
                    db.add(order)

                    if is_simulated:
                        pos = Position(
                            ticker=signal.ticker,
                            account_id=account.id if account else 1,
                            organization_id=org.id,
                            signal_id=signal.id,
                            entry_date=today,
                            entry_price=entry_price,
                            qty=sizing.shares,
                            current_price=entry_price,
                            initial_stop=float(signal.stop_price),
                            current_stop=float(signal.stop_price),
                            target_1=float(signal.target_price_1 or entry_price * 1.20),
                            target_2=float(signal.target_price_2 or entry_price * 1.40),
                            risk_aud=round((entry_price - float(signal.stop_price)) * sizing.shares, 2),
                            is_paper=is_paper,
                            status=TradeStatus.OPEN,
                        )
                        db.add(pos)

                    signal_obj = db.query(Signal).get(signal.id)
                    if signal_obj:
                        signal_obj.status = SignalStatus.TRIGGERED

                    db.add(AuditLog(
                        action=AuditAction.ORDER_FILLED if is_simulated else AuditAction.ORDER_SUBMITTED,
                        organization_id=org.id,
                        ticker=signal.ticker,
                        message=f"Bracket order {'filled (simulated)' if is_simulated else 'submitted'}: {sizing.shares}x{signal.ticker} @ {entry_price:.3f}",
                        detail=result,
                    ))

                    if is_simulated:
                        db.add(AuditLog(
                            action=AuditAction.POSITION_OPENED,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"Position opened (simulated): {sizing.shares}x{signal.ticker} @ {entry_price:.3f} | Stop ${float(signal.stop_price):.3f}",
                            detail={"initial_stop": float(signal.stop_price)},
                        ))

                notifier.send_order_fill(signal.ticker, "BUY", sizing.shares, entry_price, is_paper)
                logger.info(f"Entry {'filled (simulated)' if is_simulated else 'submitted'} for {org.name}: {signal.ticker} {sizing.shares}x @ {entry_price:.3f}")

            except Exception as e:
                engine.clear_signal_overrides()
                logger.error(f"Entry trigger error for {signal.ticker} (Org: {org.name}): {e}")


@app.task(name="app.tasks.trading.check_exit_rules_task", bind=True)
def check_exit_rules_task(self):
    """Evaluate exit rules for all open positions."""
    from app.utils.time_helper import get_current_time, get_current_date
    now_dt = get_current_time()
    now_str = now_dt.strftime("%H:%M")
    if not market_is_open_now():
        try:
            with get_db() as _db:
                from app.models.account import Organization
                for org in _db.query(Organization).filter(Organization.is_active == True).all():
                    _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                     message=f"Exit check @ {now_str}: market closed — skipping"))
                _db.commit()
        except Exception:
            pass
        return

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = WhatsAppNotifier(organization_id=org.id)
        today    = get_current_date()

        with get_db() as db:
            positions = db.query(Position).filter(
                Position.organization_id == org.id,
                Position.status == TradeStatus.OPEN
            ).all()
            if not positions:
                continue

        try:
            with get_db() as _db:
                _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                 message=f"Exit check: evaluating {len(positions)} open position(s)"))
        except Exception:
            pass

        for pos in positions:
            try:
                df = get_price_history(pos.ticker, period="6mo")
                if df is None or df.empty:
                    continue

                latest  = df.iloc[-1]
                current = float(latest["close"])
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)

                with get_db() as _db:
                    db_pos = _db.query(Position).get(pos.id)
                    if db_pos:
                        db_pos.current_price   = current
                        db_pos.unrealised_pnl  = round((current - float(db_pos.entry_price)) * db_pos.qty, 2)
                        db_pos.unrealised_pct  = round((current - float(db_pos.entry_price)) / float(db_pos.entry_price) * 100, 4)
                        _db.commit()

                import pandas as _pd
                df_weekly = df.copy()
                df_weekly["date"] = _pd.to_datetime(df_weekly["date"])
                weekly_df     = df_weekly.set_index("date").resample("W-FRI")["close"].last().dropna()
                weekly_closes = weekly_df.tail(5).tolist()[::-1]

                from app.data.fetcher import get_fundamentals
                fund_data     = get_fundamentals(pos.ticker)
                next_earnings = fund_data.get("next_earnings_date")

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

                has_exit = any(s.should_exit for s in exit_signals)
                pnl_pct_val = round((current - float(pos.entry_price)) / float(pos.entry_price) * 100, 2)
                if has_exit:
                    triggered_sig = next(s for s in exit_signals if s.should_exit)
                    msg = (f"Exit check @ {now_str}: EXIT triggered — {triggered_sig.reason} | "
                           f"Price ${current:.3f} | P&L {pnl_pct_val:+.1f}% | Reason: {triggered_sig.message}")
                else:
                    # Summarise which exit rules were evaluated and NOT triggered
                    not_triggered = [s.message for s in exit_signals if not s.should_exit and s.message]
                    criteria_summary = "; ".join(not_triggered[:3]) if not_triggered else "no exit criteria met"
                    msg = (f"Exit check @ {now_str}: holding | Price ${current:.3f} | P&L {pnl_pct_val:+.1f}% | ({criteria_summary})")

                try:
                    with get_db() as _db:
                        _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                         ticker=pos.ticker, message=msg,
                                         entity_type="Position", entity_id=str(pos.id),
                                         detail={"position_id": pos.id, "close": current,
                                                 "stop": float(pos.current_stop),
                                                 "pnl_pct": pnl_pct_val,
                                                 "result": "exit_triggered" if has_exit else "holding",
                                                 "hold_days": (today - pos.entry_date).days}))
                        _db.commit()
                except Exception as e:
                    logger.error(f"Exit check audit write failed for {pos.ticker}: {e}")

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
                                ticker=pos.ticker, account_id=pos.account_id,
                                organization_id=org.id, signal_id=pos.signal_id,
                                entry_date=pos.entry_date, exit_date=today,
                                hold_days=(today - pos.entry_date).days,
                                entry_price=pos.entry_price, exit_price=current,
                                qty=qty_to_sell,
                                gross_pnl_aud=round(pnl_aud, 2),
                                net_pnl_aud=round(pnl_aud - 6.0, 2),
                                pnl_pct=round(pnl_pct, 4),
                                initial_stop=pos.initial_stop, exit_reason=exit_sig.reason,
                                is_paper=pos.is_paper,
                                cgt_eligible_discount=(today - pos.entry_date).days > 365,
                            )
                            db.add(trade)

                        db.add(AuditLog(
                            action=AuditAction.POSITION_CLOSED,
                            organization_id=org.id, ticker=pos.ticker,
                            message=f"Exit: {exit_sig.reason} | P&L ${pnl_aud:+.0f} ({pnl_pct:+.1f}%)",
                            detail={"reason": str(exit_sig.reason), "message": exit_sig.message},
                        ))

                    notifier.send_exit_alert(pos.ticker, str(exit_sig.reason), pnl_pct, pnl_aud, pos.is_paper)
                    logger.info(f"Exit for {org.name}: {pos.ticker} {exit_sig.reason} P&L ${pnl_aud:+.0f}")
                    break

            except Exception as e:
                logger.error(f"Exit check error for {pos.ticker} (Org: {org.name}): {e}")


@app.task(name="app.tasks.trading.sync_stop_orders", bind=True)
def sync_stop_orders(self):
    """Sync stop prices from DB to IBKR open orders."""
    logger.debug("Stop sync task: placeholder — implement IBKR modify order")


@app.task(name="app.tasks.trading.promote_watchlist_item_task", bind=True)
def promote_watchlist_item_task(self, item_id: int, organization_id: int, user_email: str, user_id: int):
    """
    Asynchronously promotes a stock from the watchlist to a signal.
    """
    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.models.market import PriceBar, Stock
    from app.models.audit import AuditLog, AuditAction
    from app.utils.time_helper import get_current_date
    from sqlalchemy import desc

    with get_db() as db:
        w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == organization_id).first()
        if not w:
            logger.error(f"Watchlist item {item_id} not found for Org {organization_id}")
            return

        # Ensure the Stock row has a company name
        stock_row = db.query(Stock).filter(Stock.ticker == w.ticker).first()
        if stock_row and not stock_row.name:
            try:
                from app.data.fetcher import get_fundamentals
                fdata = get_fundamentals(w.ticker)
                if fdata.get("company_name"):
                    stock_row.name     = fdata["company_name"]
                    stock_row.sector   = fdata.get("sector") or stock_row.sector
                    stock_row.industry = fdata.get("industry") or stock_row.industry
            except Exception as e:
                logger.warning(f"Failed to fetch fundamentals for {w.ticker}: {e}")

        bar = db.query(PriceBar).filter(PriceBar.ticker == w.ticker).order_by(desc(PriceBar.date)).first()
        close_price = float(bar.close) if bar and bar.close else 1.0
        
        pivot = close_price
        stop = close_price * 0.92
        
        today = get_current_date()
        existing = db.query(Signal).filter(
            Signal.ticker == w.ticker, 
            Signal.signal_date == today, 
            Signal.organization_id == organization_id
        ).first()

        if not existing:
            sig = Signal(
                ticker=w.ticker,
                signal_date=today,
                status=SignalStatus.PENDING,
                close_price=close_price,
                pivot_price=pivot,
                stop_price=stop,
                target_price_1=pivot * 1.20,
                target_price_2=pivot * 1.40,
                rs_rating=float(bar.rs_rating or 0) if bar else 0,
                trend_score=w.rules_passed if hasattr(w, 'rules_passed') else 6,
                rule_results=w.rule_results or {},
                notes=f"[Manual Promotion] {user_email} | {w.notes or ''}".strip().rstrip('|').strip(),
                organization_id=organization_id,
            )
            db.add(sig)
            
        w.status = WatchlistStatus.SIGNALLED
        db.add(AuditLog(
            action=AuditAction.MANUAL_OVERRIDE,
            ticker=w.ticker,
            actor=user_email,
            user_id=user_id,
            message="Watchlist item manually promoted to Signal (background task)",
            organization_id=organization_id
        ))
        db.commit()

    # Send WhatsApp notification using the background task
    try:
        from app.tasks.reporting import send_whatsapp_message
        send_whatsapp_message.delay(
            organization_id, 
            "send", 
            [f"🚀 *Manual Promotion*: {w.ticker} has been manually promoted from Watchlist to Signals for entry!"]
        )
    except Exception as e:
        logger.error(f"Failed to send WhatsApp notification for promotion: {e}")

