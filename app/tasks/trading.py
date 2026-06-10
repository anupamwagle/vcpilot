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
from app.notifications import get_notifier


def _is_trading_paused(org_id: int) -> bool:
    with get_db() as db:
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "trading_paused",
            SystemConfig.organization_id == org_id
        ).first()
        return cfg and cfg.value.lower() == "true"


@app.task(name="app.tasks.trading.check_entry_triggers", bind=True)
def check_entry_triggers(self, exchange_key: str = "ASX"):
    """
    Check pending signals for intraday breakout confirmation.
    If price ≥ pivot AND volume confirms → submit bracket order.
    """
    from app.utils.time_helper import get_current_time, get_current_date
    now_dt = get_current_time()
    now_str = now_dt.strftime("%H:%M")
    if not market_is_open_now(exchange_key):
        # Write a lightweight audit entry so Task Log shows the task fired even outside market hours
        try:
            with get_db() as _db:
                from app.models.account import Organization
                for org in _db.query(Organization).filter(Organization.is_active == True).all():
                    _db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        message=f"[{exchange_key}] Entry check @ {now_str}: market closed — skipping",
                    ))
                _db.commit()
        except Exception:
            pass
        logger.debug(f"check_entry_triggers [{exchange_key}]: market closed — skipping")
        return
    logger.info(f"check_entry_triggers [{exchange_key}]: running intraday entry scan")

    # Write task-started audit entry for every org so Task Log shows it
    try:
        with get_db() as _db:
            from app.models.account import Organization as _Org
            for org in _db.query(_Org).filter(_Org.is_active == True).all():
                _db.add(AuditLog(
                    action=AuditAction.TASK_RUN,
                    organization_id=org.id,
                    message=f"[{exchange_key}] Entry check @ {now_str}: scanning for breakout triggers",
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
                    if exchange_key == "CRYPTO":
                        pending_signals = _db.query(Signal).filter(
                            Signal.organization_id == org.id,
                            Signal.status == SignalStatus.PENDING,
                            Signal.exchange_key.in_(["CRYPTO"] + [f"CRYPTO_{x}" for x in ["BINANCE","COINBASE","KRAKEN","INDEPENDENTRESERVE"]])
                        ).all()
                    else:
                        pending_signals = _db.query(Signal).filter(
                            Signal.organization_id == org.id,
                            Signal.status == SignalStatus.PENDING,
                            Signal.exchange_key == exchange_key
                        ).all()
                    for sig in pending_signals:
                        _db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=sig.ticker,
                            message=f"[{exchange_key}] Entry check: skipped because trading is paused for this organization",
                            detail={"signal_id": sig.id, "result": "skipped_paused"}
                        ))
                    _db.commit()
            except Exception as e:
                logger.error(f"Failed to log paused check for {org.name}: {e}")
            continue

        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = get_notifier(organization_id=org.id)

        with get_db() as db:
            if exchange_key == "CRYPTO":
                pending_signals = db.query(Signal).filter(
                    Signal.organization_id == org.id,
                    Signal.status == SignalStatus.PENDING,
                    Signal.exchange_key.in_(["CRYPTO"] + [f"CRYPTO_{x}" for x in ["BINANCE","COINBASE","KRAKEN","INDEPENDENTRESERVE"]])
                ).all()
            else:
                pending_signals = db.query(Signal).filter(
                    Signal.organization_id == org.id,
                    Signal.status == SignalStatus.PENDING,
                    Signal.exchange_key == exchange_key
                ).all()

            if not pending_signals:
                # Write a brief task run entry so Task Log shows activity
                try:
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        message=f"[{exchange_key}] Entry check: 0 pending signals — nothing to do",
                    ))
                except Exception:
                    pass
                continue

            # Check market regime for this exchange
            # For crypto: use the crypto-specific regime key per org (not ASX global)
            # For equities: use global last_market_regime (ASX/NYSE as applicable)
            mock_enabled_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "mock_time_enabled",
                SystemConfig.organization_id == None
            ).first()
            mock_enabled = mock_enabled_cfg and mock_enabled_cfg.value.lower() == "true"

            is_crypto_exchange = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
            if is_crypto_exchange:
                # Use per-org crypto regime key — never use ASX global regime to block crypto
                effective_exc = exchange_key if exchange_key != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
                regime_key_crypto = f"last_market_regime_{effective_exc}"
                regime_cfg = db.query(SystemConfig).filter(
                    SystemConfig.key == regime_key_crypto,
                    SystemConfig.organization_id == org.id
                ).first()
                regime = regime_cfg.value if regime_cfg else "BULL"  # default BULL for crypto if not yet evaluated
            else:
                if mock_enabled:
                    regime_cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == "mock_market_regime",
                        SystemConfig.organization_id == None
                    ).first()
                else:
                    regime_cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == "last_market_regime",
                        SystemConfig.organization_id == None
                    ).first()
                regime = regime_cfg.value if regime_cfg else "UNKNOWN"

            bear_block_rule = "regime_bear_block_crypto" if is_crypto_exchange else "regime_bear_block_equities"
            bear_block_enabled = engine.is_enabled(bear_block_rule)
            # BEAR regime blocking is checked per-signal below so per-signal overrides are respected

            # Get account capital
            from app.models.account import Account
            account = db.query(Account).filter(
                Account.organization_id == org.id, 
                Account.is_active == True
            ).first()
            capital = float(account.capital_aud) if account else 1000.0
            is_paper = account.is_paper if account else True  # IBKR/equity only

            # Crypto paper mode is controlled by crypto_testnet SystemConfig, NOT account.is_paper
            crypto_testnet_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "crypto_testnet",
                SystemConfig.organization_id == org.id,
            ).first()
            crypto_testnet = (crypto_testnet_cfg.value or "").lower() not in ("false", "0", "no") if crypto_testnet_cfg else True

            # Get working capital currency from SystemConfig
            currency_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "working_capital_currency",
                SystemConfig.organization_id == org.id
            ).first()
            base_currency = currency_cfg.value if currency_cfg else "AUD"

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
                intraday = get_intraday_price(signal.ticker, organization_id=org.id, asset_type=signal.asset_type)
                if intraday["ok"] and intraday["price"]:
                    close_price  = intraday["price"]
                    vol_current  = intraday["volume"] or int(eod_latest.get("volume", 0))
                    data_source  = intraday["data_source"]
                    delay_mins   = intraday["delay_mins"] if intraday["delay_mins"] is not None else 20
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

                # Per-signal BEAR regime check (after overrides — so per-signal override bypasses it)
                bear_overridden = overrides.get(bear_block_rule) is False  # False = user disabled (bypassed) this rule
                if regime == "BEAR" and bear_block_enabled and not bear_overridden:
                    with get_db() as _db:
                        _db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"Entry check: skipped because market is in BEAR regime (rule '{bear_block_rule}' enabled)",
                            detail={"signal_id": signal.id, "result": "skipped_bear_regime"}
                        ))
                    engine.clear_signal_overrides()
                    continue

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
                    summary = f"✅ {signal.ticker}: breakout confirmed @ ${close_price:.3f} [{data_source}] — checking position"
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

                # Guard: skip if an open position in this ticker already exists
                with get_db() as _pos_db:
                    already_open = _pos_db.query(Position).filter(
                        Position.ticker == signal.ticker,
                        Position.organization_id == org.id,
                        Position.status == TradeStatus.OPEN,
                    ).first()
                if already_open:
                    logger.debug(f"Entry check: {signal.ticker} skipped — open position already exists")
                    with get_db() as _skip_db:
                        _skip_db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"⏭ {signal.ticker}: breakout confirmed but skipped — position already open",
                            detail={"signal_id": signal.id, "result": "skipped_open_position"},
                        ))
                    engine.clear_signal_overrides()
                    continue

                # Recalculate sizing with intraday price
                entry_price = close_price
                is_crypto_asset = (signal.asset_type == "CRYPTO" or (signal.exchange_key and signal.exchange_key.startswith("CRYPTO_")))
                # Use the signal's own currency (AUD for IR, USD for Binance/Coinbase/Kraken)
                from app.data.fetcher import CRYPTO_AUD_EXCHANGES
                _sig_exchange = signal.exchange_key or ""
                if is_crypto_asset:
                    asset_currency = "AUD" if (_sig_exchange in CRYPTO_AUD_EXCHANGES or signal.ticker.endswith("-AUD")) else "USD"
                else:
                    asset_currency = "USD" if signal.exchange_key in ("NYSE", "NASDAQ") else "AUD"

                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=entry_price,
                    stop_price=float(signal.stop_price),
                    engine=engine,
                    currency=asset_currency,
                    base_currency=base_currency,
                    is_crypto=is_crypto_asset,
                    regime_multiplier=0.5 if regime == "CAUTION" else 1.0,
                )

                min_shares = 0.000001 if is_crypto_asset else 1.0
                if sizing.shares < min_shares:
                    logger.warning(f"Signal {signal.ticker} (Org: {org.name}): position size too small ({sizing.message})")
                    engine.clear_signal_overrides()
                    continue

                # Log that we are actually proceeding to submit
                with get_db() as _submit_db:
                    _submit_db.add(AuditLog(
                        action=AuditAction.TASK_RUN,
                        organization_id=org.id,
                        ticker=signal.ticker,
                        message=f"🚀 {signal.ticker}: submitting order @ ${entry_price:.3f}",
                        detail={"signal_id": signal.id, "result": "submitting"},
                    ))

                # ── Broker order placement ──────────────────────────────────────
                # Paper mode is exchange-specific:
                #   Crypto  → controlled by crypto_testnet SystemConfig
                #   Equity  → controlled by account.is_paper
                is_crypto = (signal.asset_type == "CRYPTO" or (signal.exchange_key and signal.exchange_key.startswith("CRYPTO_")))
                signal_is_paper = crypto_testnet if is_crypto else is_paper

                if signal_is_paper:
                    # Paper / simulation mode: bypass real broker entirely
                    result = {
                        "status":         "simulated",
                        "ticker":         signal.ticker,
                        "qty":            sizing.shares,
                        "entry_price":    entry_price,
                        "stop_price":     float(signal.stop_price),
                        "ibkr_parent_id": None,
                        "entry_order_id": None,
                    }
                elif is_crypto:
                    from app.broker.crypto import get_crypto_broker_for_org
                    with get_crypto_broker_for_org(org.id) as broker:
                        result = broker.submit_bracket_order(
                            ticker=signal.ticker,
                            action="BUY",
                            qty=sizing.shares,
                            entry_price=entry_price,
                            stop_price=float(signal.stop_price),
                            target_price=float(signal.target_price_1 or entry_price * 1.20),
                            order_ref=f"vcpilot-{signal.id}",
                        )
                else:
                    # Submit bracket order via IBKR
                    with IBKRBroker(organization_id=org.id) as broker:
                        result = broker.submit_bracket_order(
                            ticker=signal.ticker.replace(".AX", ""),
                            action="BUY",
                            qty=sizing.shares,
                            entry_price=entry_price,
                            stop_price=float(signal.stop_price),
                            target_price=float(signal.target_price_1 or entry_price * 1.20),
                            exchange_key=signal.exchange_key or "ASX",
                            order_ref=f"vcpilot-{signal.id}",
                        )

                # ── Handle broker error — log it and leave signal PENDING ───────
                # A "error" result means the broker rejected the order (e.g.
                # insufficient balance, bad params).  We must NOT mark the signal
                # TRIGGERED or create a Position — the signal stays PENDING and
                # the next 5-min check cycle will retry automatically.
                if result.get("status") == "error":
                    error_msg = result.get("error", "Unknown broker error")
                    logger.error(f"❌ Order FAILED for {signal.ticker} (Org: {org.name}): {error_msg}")
                    with get_db() as db:
                        db.add(AuditLog(
                            action=AuditAction.TASK_ERROR,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"❌ Order FAILED for {signal.ticker}: {error_msg}",
                            detail=result,
                        ))
                    notifier.send_health_alert(
                        signal.ticker,
                        f"Order FAILED — signal stays PENDING for retry.\nReason: {error_msg}"
                    )
                    engine.clear_signal_overrides()
                    continue  # Skip — signal remains PENDING, retried on next cycle

                # ── Record order + position (only reached on success/simulation) ─
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
                        is_paper=signal_is_paper,
                        ibkr_order_id=result.get("entry_order_id") if is_crypto else result.get("ibkr_parent_id"),
                        raw_ibkr_response=result,
                        submitted_at=_dt.utcnow(),
                        filled_at=filled_at,
                    )
                    db.add(order)

                    if is_simulated:
                        pos = Position(
                            ticker=signal.ticker,
                            exchange_key=signal.exchange_key or "ASX",
                            asset_type=signal.asset_type or "EQUITY",
                            currency=signal.currency or "AUD",
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
                            is_paper=signal_is_paper,
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

                notifier.send_order_fill(signal.ticker, "BUY", sizing.shares, entry_price, signal_is_paper)
                logger.info(f"Entry {'filled (simulated)' if is_simulated else 'submitted'} for {org.name}: {signal.ticker} {sizing.shares}x @ {entry_price:.3f}")

            except Exception as e:
                engine.clear_signal_overrides()
                logger.error(f"Entry trigger error for {signal.ticker} (Org: {org.name}): {e}")


@app.task(name="app.tasks.trading.check_exit_rules_task", bind=True)
def check_exit_rules_task(self, exchange_key: str = "ASX"):
    """Evaluate exit rules for all open positions."""
    from app.utils.time_helper import get_current_time, get_current_date
    now_dt = get_current_time()
    now_str = now_dt.strftime("%H:%M")
    if not market_is_open_now(exchange_key):
        try:
            with get_db() as _db:
                from app.models.account import Organization
                for org in _db.query(Organization).filter(Organization.is_active == True).all():
                    _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                     message=f"[{exchange_key}] Exit check @ {now_str}: market closed — skipping"))
                _db.commit()
        except Exception:
            pass
        return

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = get_notifier(organization_id=org.id)
        today    = get_current_date()

        with get_db() as db:
            if exchange_key == "CRYPTO":
                positions = db.query(Position).filter(
                    Position.organization_id == org.id,
                    Position.status == TradeStatus.OPEN,
                    Position.exchange_key.in_(["CRYPTO"] + [f"CRYPTO_{x}" for x in ["BINANCE","COINBASE","KRAKEN","INDEPENDENTRESERVE"]])
                ).all()
            else:
                positions = db.query(Position).filter(
                    Position.organization_id == org.id,
                    Position.status == TradeStatus.OPEN,
                    Position.exchange_key == exchange_key
                ).all()
            if not positions:
                continue

        try:
            with get_db() as _db:
                _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                 message=f"[{exchange_key}] Exit check: evaluating {len(positions)} open position(s)"))
        except Exception:
            pass

        for pos in positions:
            try:
                if not pos.current_stop:
                    try:
                        with get_db() as _db:
                            _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                             ticker=pos.ticker, message=f"Exit check @ {now_str}: skipped — no stop price set",
                                             entity_type="Position", entity_id=str(pos.id),
                                             detail={"result": "skipped", "reason": "no_stop_price"}))
                    except Exception:
                        pass
                    continue

                df = get_price_history(pos.ticker, period="6mo")
                if df is None or df.empty:
                    try:
                        with get_db() as _db:
                            _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                             ticker=pos.ticker, message=f"Exit check @ {now_str}: skipped — no price data",
                                             entity_type="Position", entity_id=str(pos.id),
                                             detail={"result": "skipped", "reason": "no_price_data"}))
                    except Exception:
                        pass
                    continue

                latest  = df.iloc[-1]
                current = float(latest["close"])
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)

                with get_db() as _db:
                    db_pos = _db.query(Position).get(pos.id)
                    if db_pos:
                        db_pos.current_price   = current
                        db_pos.unrealised_pnl  = round((current - float(db_pos.entry_price)) * float(db_pos.qty), 2)
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

                is_crypto = (pos.asset_type == "CRYPTO" or (pos.exchange_key and pos.exchange_key.startswith("CRYPTO_")))
                for exit_sig in exit_signals:
                    if not exit_sig.should_exit:
                        continue

                    qty_to_sell = pos.qty
                    if exit_sig.exit_type == "PARTIAL":
                        qty_to_sell = max(1, int(pos.qty * exit_sig.partial_pct / 100))

                    if is_crypto:
                        from app.broker.crypto import get_crypto_broker_for_org
                        with get_crypto_broker_for_org(org.id) as broker:
                            result = broker.submit_bracket_order(
                                ticker=pos.ticker,
                                action="SELL",
                                qty=qty_to_sell,
                                entry_price=current,
                                stop_price=0,
                                target_price=0,
                                order_ref=f"exit-{pos.id}-{exit_sig.reason}",
                            )
                    else:
                        with IBKRBroker(organization_id=org.id) as broker:
                            result = broker.submit_bracket_order(
                                ticker=pos.ticker.replace(".AX", ""),
                                action="SELL",
                                qty=qty_to_sell,
                                entry_price=current,
                                stop_price=0,
                                target_price=0,
                                exchange_key=pos.exchange_key or "ASX",
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
                                net_pnl_aud=round(pnl_aud - (0.0 if is_crypto else 6.0), 2),
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
                try:
                    with get_db() as _db:
                        _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                         ticker=pos.ticker,
                                         message=f"Exit check @ {now_str}: error — {type(e).__name__}: {e}",
                                         entity_type="Position", entity_id=str(pos.id),
                                         detail={"result": "error", "error": str(e)}))
                except Exception:
                    pass


@app.task(name="app.tasks.trading.sync_stop_orders", bind=True)
def sync_stop_orders(self):
    """
    Stop-order sync and trailing stop updater.

    For crypto positions (IR via ccxt):
      - Fetches current live price for each open position
      - Checks if price has fallen below the stop level → closes the position
      - Applies ATR-based trailing stop: if price has moved up by ≥1 ATR from entry,
        raises the stop to entry price (lock-in breakeven) or higher

    For equity positions (IBKR):
      - Placeholder (full IBKR modify-order API to be implemented in Phase 4)
    """
    from app.utils.time_helper import get_current_time, get_current_date
    from app.models.account import Organization
    from app.models.trade import Position, Trade, TradeStatus, ExitReason

    logger.info("sync_stop_orders: running stop sync + trailing stop update...")

    with get_db() as db:
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        with get_db() as db:
            open_positions = db.query(Position).filter(
                Position.organization_id == org.id,
                Position.status == TradeStatus.OPEN,
            ).all()

        if not open_positions:
            continue

        for pos in open_positions:
            try:
                is_crypto = getattr(pos, "asset_type", "EQUITY") == "CRYPTO" or (
                    pos.exchange_key and pos.exchange_key.startswith("CRYPTO")
                )
                if not is_crypto:
                    continue  # IBKR trailing stop modification not yet implemented

                # ── Fetch live price ──────────────────────────────────────
                from app.data.fetcher import get_intraday_price
                price_result = get_intraday_price(pos.ticker, org.id, asset_type=getattr(pos, "asset_type", "EQUITY"))
                if not price_result.get("ok") or not price_result.get("price"):
                    logger.debug(f"sync_stop_orders: no live price for {pos.ticker} — skip")
                    continue

                current_price = float(price_result["price"])
                stop_price    = float(pos.current_stop) if pos.current_stop else None
                entry_price   = float(pos.entry_price) if pos.entry_price else None

                if not stop_price or not entry_price:
                    continue

                # ── Stopped out? ──────────────────────────────────────────
                if current_price <= stop_price:
                    logger.warning(f"sync_stop_orders: {pos.ticker} stopped out — price {current_price:.4f} ≤ stop {stop_price:.4f}")
                    realised_pnl = (current_price - entry_price) * float(pos.qty)
                    today_d = get_current_date()
                    with get_db() as db2:
                        p = db2.query(Position).get(pos.id)
                        if p and p.status == TradeStatus.OPEN:
                            # NOTE: Position has no exit_price/exit_reason/closed_at/realised_pnl
                            # columns — those belong on Trade. Only `status` is mapped here;
                            # the rest of the exit detail is recorded on the Trade row below.
                            # (Previously this set non-persisted phantom attributes on Position
                            # and passed invalid kwargs to Trade(), raising AttributeError/TypeError
                            # that was swallowed by the outer except — so stopped-out crypto
                            # positions never actually closed.)
                            p.status = TradeStatus.CLOSED

                            db2.add(Trade(
                                organization_id=org.id,
                                account_id=p.account_id,
                                ticker=p.ticker,
                                exchange_key=p.exchange_key,
                                asset_type=getattr(p, "asset_type", "CRYPTO"),
                                currency=getattr(p, "currency", "AUD"),
                                signal_id=p.signal_id,
                                entry_date=p.entry_date,
                                exit_date=today_d,
                                hold_days=(today_d - p.entry_date).days,
                                qty=p.qty,
                                entry_price=p.entry_price,
                                exit_price=current_price,
                                gross_pnl_aud=round(realised_pnl, 2),
                                net_pnl_aud=round(realised_pnl, 2),  # crypto — no commission
                                pnl_pct=round((current_price - entry_price) / entry_price * 100, 4) if entry_price else 0,
                                initial_stop=p.initial_stop,
                                exit_reason=ExitReason.STOP_LOSS,
                                is_paper=p.is_paper,
                                cgt_eligible_discount=(today_d - p.entry_date).days > 365,
                            ))
                            db2.add(AuditLog(
                                action=AuditAction.POSITION_CLOSED,
                                organization_id=org.id,
                                ticker=pos.ticker,
                                message=f"🛑 STOP triggered — {pos.ticker} @ A${current_price:.4f} "
                                        f"stop was A${stop_price:.4f} P&L A${realised_pnl:+.2f}",
                            ))
                            db2.commit()
                    # WhatsApp alert
                    try:
                        notifier = get_notifier(organization_id=org.id)
                        notifier.send(
                            f"🛑 *Stop Loss Triggered*\n"
                            f"{pos.ticker} closed @ A${current_price:.4f}\n"
                            f"Stop was A${stop_price:.4f}\n"
                            f"P&L: A${realised_pnl:+.2f}"
                        )
                    except Exception:
                        pass
                    continue

                # ── ATR-based trailing stop ───────────────────────────────
                # If price has risen ≥ 1 ATR above entry, trail stop up to breakeven (entry)
                # If price has risen ≥ 2 ATR above entry, trail stop to entry + 0.5 ATR
                try:
                    from app.data.fetcher import get_price_history
                    df = get_price_history(pos.ticker, period="1mo")
                    if df is not None and not df.empty and "atr_14" in df.columns:
                        atr = float(df["atr_14"].iloc[-1] or 0)
                        if atr > 0 and current_price > entry_price:
                            gain_atrs = (current_price - entry_price) / atr
                            new_stop = stop_price

                            if gain_atrs >= 2.0:
                                # Trail to entry + 0.5 ATR (lock in small profit)
                                candidate = entry_price + (0.5 * atr)
                                new_stop = max(stop_price, candidate)
                            elif gain_atrs >= 1.0:
                                # Trail to breakeven (entry price)
                                new_stop = max(stop_price, entry_price)

                            if new_stop > stop_price:
                                with get_db() as db3:
                                    p3 = db3.query(Position).get(pos.id)
                                    if p3 and p3.status == TradeStatus.OPEN:
                                        old_stop = float(p3.current_stop)
                                        p3.current_stop = new_stop
                                        db3.add(AuditLog(
                                            action=AuditAction.CONFIG_CHANGED,
                                            organization_id=org.id,
                                            ticker=pos.ticker,
                                            message=f"📈 Trailing stop raised: {pos.ticker} "
                                                    f"A${old_stop:.4f} → A${new_stop:.4f} "
                                                    f"(gain {gain_atrs:.1f} ATRs, price A${current_price:.4f})",
                                        ))
                                        db3.commit()
                                logger.info(f"Trailing stop updated: {pos.ticker} stop A${stop_price:.4f} → A${new_stop:.4f}")
                except Exception as trail_err:
                    logger.debug(f"sync_stop_orders trailing stop error for {pos.ticker}: {trail_err}")

            except Exception as e:
                logger.error(f"sync_stop_orders error for {pos.ticker}: {e}")


@app.task(name="app.tasks.trading.update_position_pnl_task", bind=True)
def update_position_pnl_task(self):
    """
    Refresh current_price, unrealised_pnl, and unrealised_pct for all open positions.

    Runs every 5 minutes. Fetches live prices (IR API for crypto, yfinance fallback)
    and writes updated values to the Position rows so the dashboard UI shows live P&L
    without a page reload.

    For crypto positions with IR: uses the free IR public API (0-delay).
    For equity positions: uses yfinance 15-min bars.
    """
    from app.models.account import Organization
    from app.models.trade import Position, TradeStatus
    from app.data.fetcher import get_intraday_price, get_fx_rate

    logger.debug("update_position_pnl_task: refreshing open position P&L...")

    with get_db() as db:
        all_open = db.query(Position).filter(
            Position.status == TradeStatus.OPEN
        ).all()

    if not all_open:
        return

    # Group by ticker to avoid duplicate API calls for same ticker across orgs
    ticker_prices: dict[str, float | None] = {}

    for pos in all_open:
        ticker = pos.ticker
        if ticker not in ticker_prices:
            result = get_intraday_price(ticker, asset_type=getattr(pos, "asset_type", "EQUITY"))
            ticker_prices[ticker] = result.get("price") if result.get("ok") else None

        price = ticker_prices[ticker]
        if price is None:
            continue

        try:
            entry_price = float(pos.entry_price) if pos.entry_price else None
            qty         = float(pos.qty) if pos.qty else 0
            currency    = getattr(pos, "currency", "AUD") or "AUD"

            if not entry_price or qty <= 0:
                continue

            pnl_local = (price - entry_price) * qty
            pnl_pct   = ((price - entry_price) / entry_price) * 100

            # Convert P&L to AUD if position is in a foreign currency
            if currency != "AUD":
                fx = get_fx_rate(currency, "AUD")
                pnl_aud = pnl_local * fx
            else:
                pnl_aud = pnl_local

            with get_db() as db2:
                p = db2.query(Position).get(pos.id)
                if p and p.status == TradeStatus.OPEN:
                    p.current_price          = round(price, 8)
                    p.unrealised_pnl_local   = round(pnl_local, 4)
                    p.unrealised_pnl         = round(pnl_aud, 2)
                    p.unrealised_pct         = round(pnl_pct, 4)
                    db2.commit()

        except Exception as e:
            logger.debug(f"update_position_pnl_task error for {pos.ticker}: {e}")

    logger.debug(f"update_position_pnl_task: updated {len(all_open)} positions")


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

    ticker_for_log = None
    try:
      with get_db() as db:
        w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == organization_id).first()
        if not w:
            logger.error(f"Watchlist item {item_id} not found for Org {organization_id}")
            return
        ticker_for_log = w.ticker

        is_crypto = bool(w.asset_type == "CRYPTO" or
                        w.exchange_key == "CRYPTO" or
                        (w.exchange_key and w.exchange_key.startswith("CRYPTO_")))

        # Ensure the Stock row has a company name (skip for crypto — no earnings data)
        stock_row = db.query(Stock).filter(Stock.ticker == w.ticker).first()
        if stock_row and not stock_row.name and not is_crypto:
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
        close_price = float(bar.close) if bar and bar.close else 0.0

        if close_price <= 0:
            logger.error(f"No valid price bar found for {w.ticker} — cannot promote to signal")
            w.status = WatchlistStatus.WATCHING   # revert so user can retry
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=w.ticker,
                actor=user_email,
                organization_id=organization_id,
                message=f"Promotion failed — no price data found for {w.ticker}. Refresh price data first.",
            ))
            db.commit()
            return

        pivot = close_price
        # Crypto uses wider initial stop (20%) to account for volatility; equities use 8%
        stop = close_price * (0.80 if is_crypto else 0.92)
        
        today = get_current_date()
        existing = db.query(Signal).filter(
            Signal.ticker == w.ticker,
            Signal.signal_date == today,
            Signal.organization_id == organization_id
        ).first()

        if existing:
            # A Signal for this ticker/date already exists (e.g. created earlier by the
            # screener and possibly SKIPPED/EXPIRED). Previously we silently flipped the
            # watchlist item to SIGNALLED here with no new visible Signal — from the user's
            # perspective "nothing happened". Surface this clearly via the audit log instead.
            logger.info(f"Manual promotion of {w.ticker}: existing signal #{existing.id} "
                        f"(status={existing.status.value}) for {today} — not creating a duplicate.")
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=w.ticker,
                actor=user_email,
                organization_id=organization_id,
                message=(f"Manual promotion of {w.ticker} found an existing signal #{existing.id} "
                         f"(status={existing.status.value}) for {today} — no new signal created. "
                         f"Check the Signals page for the existing entry."),
            ))

        if not existing:
            # Sizing calculations
            from app.models.account import Account
            from app.screener.rules import RuleEngine
            from app.risk.manager import calculate_position_size

            account = db.query(Account).filter(
                Account.organization_id == organization_id, 
                Account.is_active == True
            ).first()
            capital = float(account.capital_aud) if account else 1000.0
            
            # Get working capital currency from SystemConfig
            currency_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "working_capital_currency",
                SystemConfig.organization_id == organization_id
            ).first()
            base_currency = currency_cfg.value if currency_cfg else "AUD"

            asset_currency = w.currency or ("AUD" if w.exchange_key == "CRYPTO_INDEPENDENTRESERVE" else
                             "USD" if is_crypto else
                             "USD" if w.exchange_key in ("NYSE", "NASDAQ") else "AUD")

            engine = RuleEngine(organization_id=organization_id, asset_type=("CRYPTO" if is_crypto else "EQUITY"))
            
            sizing = calculate_position_size(
                capital_aud=capital,
                entry_price=pivot,
                stop_price=stop,
                engine=engine,
                currency=asset_currency,
                base_currency=base_currency,
                is_crypto=is_crypto,
            )

            sig = Signal(
                ticker=w.ticker,
                exchange_key=w.exchange_key or "ASX",
                asset_type=w.asset_type or "EQUITY",
                currency=w.currency or asset_currency,
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
                suggested_size_shares=sizing.shares,
                suggested_size_aud=sizing.capital_aud,
                risk_per_trade_aud=sizing.risk_aud,
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
            message=("Watchlist item manually promoted — existing signal reused (no duplicate created)"
                     if existing else
                     "Watchlist item manually promoted to Signal (background task)"),
            organization_id=organization_id
        ))
        db.commit()

      # Send WhatsApp notification using the background task (best-effort — never fails the promotion)
      try:
          from app.tasks.reporting import send_whatsapp_message
          send_whatsapp_message.delay(
              organization_id,
              "send",
              [f"🚀 *Manual Promotion*: {ticker_for_log} has been manually promoted from Watchlist to Signals for entry!"]
          )
      except Exception as e:
          logger.error(f"Failed to send WhatsApp notification for promotion: {e}")

    except Exception as exc:
        # Anything going wrong above must NOT leave the watchlist item stuck in SIGNALLED
        # with no Signal ever created (this was the root cause of "promoted item vanishes").
        # Revert to WATCHING so the user can see it and retry, and log it loudly.
        logger.exception(f"promote_watchlist_item_task failed for watchlist item {item_id} "
                         f"({ticker_for_log}) Org {organization_id}: {exc}")
        try:
            with get_db() as db:
                w2 = db.query(Watchlist).filter(
                    Watchlist.id == item_id,
                    Watchlist.organization_id == organization_id
                ).first()
                if w2 and w2.status == WatchlistStatus.SIGNALLED:
                    w2.status = WatchlistStatus.WATCHING
                db.add(AuditLog(
                    action=AuditAction.TASK_ERROR,
                    ticker=ticker_for_log,
                    actor=user_email,
                    user_id=user_id,
                    organization_id=organization_id,
                    message=(f"Promotion of {ticker_for_log or f'item #{item_id}'} failed with an unexpected error "
                             f"and was reverted to WATCHING — please retry. Error: {exc}"),
                ))
                db.commit()
        except Exception as revert_exc:
            logger.exception(f"Failed to revert watchlist item {item_id} to WATCHING after promotion error: {revert_exc}")


@app.task(name="app.tasks.trading.sync_ibkr_positions_task", bind=True)
def sync_ibkr_positions_task(self):
    """Sync position data from IBKR to DB."""
    logger.debug("Position sync task: placeholder — implement IBKR position sync")

