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
from app.screener.price_filter import price_in_range
from app.screener.liquidity_filter import liquidity_ok
from app.screener.exit_rules import evaluate_exit_rules
from app.risk.manager import calculate_position_size, check_portfolio_heat
from app.broker.ibkr import IBKRBroker
from app.notifications import get_notifier


def _is_trading_paused(org_id: int) -> bool:
    with get_db() as db:
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "trading_paused",
            SystemConfig.organization_id == org_id
        ).first()
        return cfg and cfg.value.lower() == "true"


def _get_org_config_value(org_id: int, key: str) -> str | None:
    with get_db() as db:
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == key, SystemConfig.organization_id == org_id
        ).first()
        return cfg.value if cfg else None


def _is_kill_switch_on(org_id: int) -> bool:
    val = _get_org_config_value(org_id, "trading_kill_switch")
    return bool(val) and val.lower() == "true"


def _get_pending_signals_for_exchange(db, org_id: int, exchange_key: str):
    if exchange_key == "CRYPTO":
        return db.query(Signal).filter(
            Signal.organization_id == org_id, Signal.status == SignalStatus.PENDING,
            Signal.asset_type == "CRYPTO",
        ).all()
    elif exchange_key in ("NYSE", "NASDAQ", "US"):
        return db.query(Signal).filter(
            Signal.organization_id == org_id, Signal.status == SignalStatus.PENDING,
            Signal.exchange_key.in_(["NYSE", "NASDAQ"]),
        ).all()
    else:
        return db.query(Signal).filter(
            Signal.organization_id == org_id, Signal.status == SignalStatus.PENDING,
            Signal.exchange_key == exchange_key,
        ).all()


def _audit_skip_all_pending(org_id: int, exchange_key: str, message: str, result_tag: str):
    """Write a per-signal skip AuditLog for every PENDING signal in scope, so
    the Signals/Data Log UI shows why nothing happened this run."""
    try:
        with get_db() as _db:
            for sig in _get_pending_signals_for_exchange(_db, org_id, exchange_key):
                _db.add(AuditLog(
                    action=AuditAction.TASK_RUN, organization_id=org_id, ticker=sig.ticker,
                    message=message, detail={"signal_id": sig.id, "result": result_tag},
                ))
            _db.commit()
    except Exception as e:
        logger.error(f"Failed to log skip ({result_tag}) for org {org_id}: {e}")


def _todays_pnl_aud(org_id: int) -> float:
    """Sum today's realised (closed Trade rows) + unrealised (open Position)
    P&L for an org, in AUD — used by the max-daily-loss halt."""
    from app.utils.time_helper import get_current_date
    today = get_current_date()
    with get_db() as db:
        realised = db.query(Trade).filter(
            Trade.organization_id == org_id, Trade.exit_date == today,
        ).all()
        realised_pnl = sum(float(t.net_pnl_aud or 0) for t in realised)

        open_positions = db.query(Position).filter(
            Position.organization_id == org_id, Position.status == TradeStatus.OPEN,
        ).all()
        unrealised_pnl = sum(float(p.unrealised_pnl or 0) for p in open_positions)
    return realised_pnl + unrealised_pnl


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
        except Exception as e:
            logger.warning(f"check_entry_triggers [{exchange_key}]: market-closed audit write failed: {e}", exc_info=True)
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
    except Exception as e:
        logger.warning(f"check_entry_triggers [{exchange_key}]: scan-started audit write failed: {e}", exc_info=True)

    with get_db() as db:
        from app.models.account import Organization
        orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        today = get_current_date()

        # Overlap lock (T9 / CLAUDE.md #40): with the available-capital math in
        # this task, two overlapping runs for the same org+exchange (a slow run
        # still going when the next 5-min tick fires) could double-spend
        # capital or double-submit orders. Fails open on Redis outage.
        if not _acquire_org_lock(f"check_entry_triggers_lock:{org.id}:{exchange_key}"):
            logger.debug(f"check_entry_triggers: org {org.id} [{exchange_key}] already running — skipping")
            continue

        if _is_kill_switch_on(org.id):
            logger.warning(f"Kill switch ON for Org '{org.name}' — skipping entry check")
            _audit_skip_all_pending(
                org.id, exchange_key,
                f"[{exchange_key}] Entry check: skipped — trading kill switch is ON for this organization",
                "skipped_kill_switch",
            )
            continue

        if _is_trading_paused(org.id):
            logger.debug(f"Trading paused for Org '{org.name}' — skipping entry check")
            _audit_skip_all_pending(
                org.id, exchange_key,
                f"[{exchange_key}] Entry check: skipped because trading is paused for this organization",
                "skipped_paused",
            )
            continue

        _max_daily_loss = _get_org_config_value(org.id, "max_daily_loss_aud")
        if _max_daily_loss:
            try:
                _max_daily_loss_f = float(_max_daily_loss)
            except (TypeError, ValueError):
                _max_daily_loss_f = 0.0
            if _max_daily_loss_f > 0:
                _pnl_today = _todays_pnl_aud(org.id)
                if _pnl_today <= -_max_daily_loss_f:
                    logger.warning(f"Max daily loss halt for Org '{org.name}': P&L ${_pnl_today:.2f} <= -${_max_daily_loss_f:.2f}")
                    # Telegram alert throttled to once per 6h — the halt re-fires this
                    # same skip on every 5-min tick for the rest of the day otherwise.
                    from datetime import timedelta as _td
                    with get_db() as _halt_db:
                        _already_alerted = _halt_db.query(AuditLog).filter(
                            AuditLog.organization_id == org.id,
                            AuditLog.message.like("%daily loss halt%ALERT%"),
                            AuditLog.created_at >= _dt.utcnow() - _td(hours=6),
                        ).first()
                        if not _already_alerted:
                            _halt_db.add(AuditLog(
                                action=AuditAction.TASK_RUN, organization_id=org.id,
                                message=(f"[{exchange_key}] 🛑 ALERT: max daily loss halt triggered — "
                                         f"today's P&L ${_pnl_today:+.2f} breached -${_max_daily_loss_f:.2f} limit"),
                            ))
                        _halt_db.commit()
                    if not _already_alerted:
                        try:
                            notifier = get_notifier(organization_id=org.id)
                            notifier.send(
                                f"🛑 *Max Daily Loss Halt*\n"
                                f"Today's P&L: ${_pnl_today:+.2f} (limit: -${_max_daily_loss_f:.2f})\n"
                                f"New entries are halted for the rest of the day."
                            )
                        except Exception as _ne:
                            logger.error(f"Failed to send daily loss halt alert for {org.name}: {_ne}")
                    _audit_skip_all_pending(
                        org.id, exchange_key,
                        (f"[{exchange_key}] Entry check: skipped — max daily loss halt "
                         f"(today's P&L ${_pnl_today:+.2f} breached -${_max_daily_loss_f:.2f} limit)"),
                        "skipped_daily_loss_halt",
                    )
                    continue

        # Opening-noise guard (ASX only): the 10:00–10:09 staggered auction can
        # confirm "breakouts" on auction prints and partial-day volume.
        if exchange_key == "ASX":
            _skip_open_min_raw = _get_org_config_value(org.id, "entry_skip_open_minutes")
            try:
                _skip_open_min = float(_skip_open_min_raw) if _skip_open_min_raw else 10.0
            except (TypeError, ValueError):
                _skip_open_min = 10.0
            if _skip_open_min > 0:
                _mins_since_open = (now_dt.hour - 10) * 60 + now_dt.minute
                if 0 <= _mins_since_open < _skip_open_min:
                    logger.debug(f"Entry check: within opening-noise window ({_mins_since_open}min since open) — skipping org {org.id}")
                    _audit_skip_all_pending(
                        org.id, exchange_key,
                        (f"[{exchange_key}] Entry check: skipped — within the opening-noise window "
                         f"({_mins_since_open} min since 10:00 open, guard={_skip_open_min:.0f} min)"),
                        "skipped_opening_noise",
                    )
                    continue

        engine   = RuleEngine(organization_id=org.id, tier=org.tier.value)
        notifier = get_notifier(organization_id=org.id)

        with get_db() as db:
            if exchange_key == "CRYPTO":
                pending_signals = db.query(Signal).filter(
                    Signal.organization_id == org.id,
                    Signal.status == SignalStatus.PENDING,
                    Signal.asset_type == "CRYPTO"
                ).all()
            elif exchange_key in ("NYSE", "NASDAQ", "US"):
                # NYSE beat task covers both NYSE and NASDAQ-100 stocks
                pending_signals = db.query(Signal).filter(
                    Signal.organization_id == org.id,
                    Signal.status == SignalStatus.PENDING,
                    Signal.exchange_key.in_(["NYSE", "NASDAQ"])
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
                except Exception as e:
                    logger.warning(f"check_entry_triggers [{exchange_key}]: no-pending-signals audit write failed: {e}", exc_info=True)
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
                # Bug #6 fix: resolve active crypto exchange key from org config instead of hardcoding IR
                if exchange_key != "CRYPTO":
                    effective_exc = exchange_key
                else:
                    _ae_cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == "active_exchanges",
                        SystemConfig.organization_id == org.id,
                    ).first()
                    _ae_str = (_ae_cfg.value if _ae_cfg else "") or ""
                    _crypto_keys = [e.strip() for e in _ae_str.split(",") if e.strip().startswith("CRYPTO_")]
                    effective_exc = _crypto_keys[0] if _crypto_keys else "CRYPTO_INDEPENDENTRESERVE"
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
                    # Read per-org, per-exchange regime key (written by evaluate_market_regime_task)
                    # NYSE beat tasks cover both NYSE and NASDAQ — try NYSE key first
                    regime_exc = exchange_key if exchange_key != "NASDAQ" else "NYSE"
                    regime_cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == f"last_market_regime_{regime_exc}",
                        SystemConfig.organization_id == org.id,
                    ).first()
                    if not regime_cfg:
                        # Fallback: try the other US key
                        fallback_exc = "NASDAQ" if regime_exc == "NYSE" else "NYSE"
                        regime_cfg = db.query(SystemConfig).filter(
                            SystemConfig.key == f"last_market_regime_{fallback_exc}",
                            SystemConfig.organization_id == org.id,
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

            # Buffer above the stop trigger for the automated BUY STOP-LIMIT entry
            # (CLAUDE.md #39) — how far above max(pivot, confirm price) the limit
            # sits, capping slippage instead of chasing with no ceiling.
            _buffer_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "entry_limit_buffer_pct",
                SystemConfig.organization_id == org.id
            ).first()
            entry_limit_buffer_pct = float(_buffer_cfg.value) if _buffer_cfg and _buffer_cfg.value else 1.0

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

            # Portfolio heat gate — previously calculate_portfolio_heat()/check_portfolio_heat()
            # in app/risk/manager.py were fully implemented and unit-tested but never called
            # from any production code path; the only pre-trade portfolio-level brake actually
            # enforced was the raw position-count cap above. This mirrors the same
            # total_risk / account_capital % already shown on the dashboard's Portfolio Heat
            # gauge (previously informational only) and finally enforces portfolio_max_heat_pct.
            open_positions_for_heat = db.query(Position).filter(
                Position.organization_id == org.id,
                Position.status == TradeStatus.OPEN,
            ).all()
            total_open_risk_aud = 0.0
            for _p in open_positions_for_heat:
                _entry = float(_p.entry_price or 0)
                _stop  = float(_p.current_stop or 0)
                _qty   = float(_p.qty or 0)
                _fx    = float(_p.current_fx_rate or _p.entry_fx_rate or 0) or 0.0
                if _fx == 0.0:
                    # Bug #18 fix: fall back to live FX rate for USD positions
                    _p_curr = getattr(_p, "currency", "AUD") or "AUD"
                    if _p_curr == "USD":
                        try:
                            from app.data.fetcher import get_fx_rate as _hfx
                            _fx = float(_hfx("USD", "AUD") or 1.0)
                        except Exception:
                            _fx = 1.0
                    else:
                        _fx = 1.0
                if _entry > 0 and _stop > 0:
                    total_open_risk_aud += ((_entry - _stop) * _qty) / _fx
            current_heat = (total_open_risk_aud / capital * 100) if capital else 0.0
            heat_ok, heat_msg = check_portfolio_heat(current_heat, engine)
            if not heat_ok:
                logger.warning(f"Portfolio heat check for '{org.name}': {heat_msg}")
                try:
                    for signal in pending_signals:
                        db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"Entry check: skipped — {heat_msg}",
                            detail={"signal_id": signal.id, "result": "skipped_portfolio_heat",
                                    "heat_pct": round(current_heat, 2)},
                        ))
                except Exception as e:
                    logger.error(f"Failed to log portfolio heat check for {org.name}: {e}")
                continue
        for signal in pending_signals:
            try:
                # Self-heal: a PENDING signal for a ticker we already hold can never
                # trigger (the post-breakout guard below always skips held tickers),
                # so mark it SKIPPED instead of leaving it pending forever next to
                # the open position. Reversible via the Signals page "unskip".
                with get_db() as _held_db:
                    _held = _held_db.query(Position).filter(
                        Position.ticker == signal.ticker,
                        Position.organization_id == org.id,
                        Position.status == TradeStatus.OPEN,
                    ).first()
                    if _held:
                        _sig = _held_db.query(Signal).filter(Signal.id == signal.id).first()
                        if _sig:
                            _sig.status = SignalStatus.SKIPPED
                            _sig.notes = ((_sig.notes or "") + " | auto-skipped: position already open").strip(" |")
                        _held_db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=(f"⏭ {signal.ticker}: pending signal auto-skipped — an open position "
                                     f"already exists for this ticker (a signal cannot trigger while held; "
                                     f"unskip it after the position is closed if still valid)"),
                            detail={"signal_id": signal.id, "result": "auto_skipped_position_open"},
                        ))
                if _held:
                    continue

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

                # Share Price Range Filter (equity only, opt-in) — re-check live
                # price before confirming breakout. signal.asset_type (not the
                # engine's own asset_type, which is hardcoded to EQUITY above and
                # shared across crypto signals too) is the authoritative gate
                # here. Per-signal rule_overrides are already applied to `engine`
                # above (line ~297), so is_enabled() inside price_in_range()
                # automatically respects any override for these two rule_ids.
                if signal.asset_type != "CRYPTO":
                    in_range, range_reason = price_in_range(signal.ticker, close_price, engine, signal.asset_type)
                    if not in_range:
                        with get_db() as _db:
                            _db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=signal.ticker,
                                message=f"Entry check: skipped — {range_reason}",
                                detail={"signal_id": signal.id, "result": "skipped_price_out_of_range"},
                            ))
                        engine.clear_signal_overrides()
                        continue

                    # Minimum Liquidity Filter (R2 / CLAUDE.md #42) — re-check live,
                    # same reasoning as the price-range re-check above.
                    liq_ok, liq_reason = liquidity_ok(signal.ticker, close_price, avg_vol, engine, signal.asset_type)
                    if not liq_ok:
                        with get_db() as _db:
                            _db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=signal.ticker,
                                message=f"Entry check: skipped — {liq_reason}",
                                detail={"signal_id": signal.id, "result": "skipped_insufficient_liquidity"},
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

                # Guard: skip if an open position or active buy order already exists
                from app.models.trade import Order, OrderAction, OrderStatus
                with get_db() as _pos_db:
                    already_open = _pos_db.query(Position).filter(
                        Position.ticker == signal.ticker,
                        Position.organization_id == org.id,
                        Position.status == TradeStatus.OPEN,
                    ).first()
                    already_ordered = _pos_db.query(Order).filter(
                        Order.ticker == signal.ticker,
                        Order.organization_id == org.id,
                        Order.action == OrderAction.BUY,
                        Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PENDING]),
                    ).first()

                if already_open or already_ordered:
                    reason = "position already open" if already_open else "buy order already pending/submitted"
                    logger.debug(f"Entry check: {signal.ticker} skipped — {reason}")
                    with get_db() as _skip_db:
                        _skip_db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=f"⏭ {signal.ticker}: breakout confirmed but skipped — {reason}",
                            detail={"signal_id": signal.id, "result": "skipped_already_held_or_ordered"},
                        ))
                    engine.clear_signal_overrides()
                    continue

                # Hard extension guard (CLAUDE.md #39): don't chase a breakout that
                # has already run too far past the pivot by the time we get here —
                # check_breakout's own price-vs-pivot rule only validated the price
                # at the moment it ran; price can keep moving before submission
                # actually happens. Reads the seeded vcp_max_extension threshold
                # fresh (not hardcoded) so an admin's change takes effect immediately.
                _pivot_for_guard = float(signal.pivot_price) if signal.pivot_price else None
                if _pivot_for_guard:
                    _max_ext_pct = float(engine.threshold("vcp_max_extension") or 5.0)
                    _pct_above_pivot = (close_price - _pivot_for_guard) / _pivot_for_guard * 100
                    if _pct_above_pivot > _max_ext_pct:
                        logger.info(f"Entry check: {signal.ticker} skipped — extended {_pct_above_pivot:.1f}% past pivot (max {_max_ext_pct:.0f}%)")
                        with get_db() as _skip_db:
                            _skip_db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=signal.ticker,
                                message=(f"⏭ {signal.ticker}: breakout extended {_pct_above_pivot:.1f}% past pivot "
                                         f"${_pivot_for_guard:.3f} (max {_max_ext_pct:.0f}%) — not chasing "
                                         f"(Minervini extension rule)"),
                                detail={"signal_id": signal.id, "result": "skipped_extended_past_pivot",
                                        "pct_above_pivot": round(_pct_above_pivot, 2), "max_pct": _max_ext_pct},
                            ))
                        continue

                is_crypto_asset = (signal.asset_type == "CRYPTO" or (signal.exchange_key and signal.exchange_key.startswith("CRYPTO_")))
                # Use the signal's own currency (AUD for IR, USD for Binance/Coinbase/Kraken)
                from app.data.fetcher import CRYPTO_AUD_EXCHANGES
                _sig_exchange = signal.exchange_key or ""
                if is_crypto_asset:
                    asset_currency = "AUD" if (_sig_exchange in CRYPTO_AUD_EXCHANGES or signal.ticker.endswith("-AUD")) else "USD"
                else:
                    asset_currency = "USD" if signal.exchange_key in ("NYSE", "NASDAQ") else "AUD"

                # Calculate available capital
                # (total capital - currently invested cost basis - outstanding buy orders value)
                # NOTE: deliberately no separate "submitted this run" running total —
                # each order/position committed earlier in this same loop is already
                # visible to this fresh query (get_db() commits at the end of every
                # `with` block), so tracking it separately would double-subtract it.
                with get_db() as _cap_db:
                    _open_pos = _cap_db.query(Position).filter(
                        Position.organization_id == org.id,
                        Position.status == TradeStatus.OPEN,
                    ).all()
                    _total_invested = 0.0
                    for _p in _open_pos:
                        _entry = float(_p.entry_price or 0)
                        _qty   = float(_p.qty or 0)
                        _fx    = float(_p.entry_fx_rate or _p.current_fx_rate or 1.0) or 1.0
                        if _entry > 0 and _qty > 0:
                            _total_invested += (_entry * _qty) / _fx

                    _outstanding_buys = _cap_db.query(Order).filter(
                        Order.organization_id == org.id,
                        Order.action == OrderAction.BUY,
                        Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PENDING])
                    ).all()
                    _total_ordered = 0.0
                    for _o in _outstanding_buys:
                        _price = float(_o.limit_price or _o.stop_price or 0)
                        _qty   = float(_o.qty_ordered or 0)
                        _fx    = float(_o.fx_rate_aud or 1.0) or 1.0
                        if _price > 0 and _qty > 0:
                            _total_ordered += (_price * _qty) / _fx

                current_avail_capital = capital - _total_invested - _total_ordered
                _min_required = 100.0 if (signal.exchange_key in ("NYSE", "NASDAQ") or is_crypto_asset) else 600.0
                if current_avail_capital < _min_required:
                    logger.debug(f"Entry check: {signal.ticker} skipped — insufficient capital (${current_avail_capital:.2f} < ${_min_required:.0f})")
                    with get_db() as _skip_db:
                        _skip_db.add(AuditLog(
                            action=AuditAction.TASK_RUN,
                            organization_id=org.id,
                            ticker=signal.ticker,
                            message=(f"Entry check: {signal.ticker} breakout confirmed but skipped — "
                                     f"insufficient available capital (${current_avail_capital:.2f} available, "
                                     f"minimum ${_min_required:.0f} required)"),
                            detail={"signal_id": signal.id, "result": "skipped_insufficient_capital",
                                    "avail_capital": current_avail_capital}
                        ))
                    engine.clear_signal_overrides()
                    continue

                # Recalculate sizing with intraday price
                entry_price = close_price

                # ── Equity stop-width cap (Minervini: max ~7–8% stop, never beyond 10%) ──
                # The VCP stop (low of the final contraction) can occasionally sit further
                # than a prudent maximum below the actual entry. Tighten it to the cap so no
                # equity trade risks more than `equity_stop_width_max_pct` from entry. This
                # also lifts position size while holding the 2% capital-risk rule constant.
                # Crypto keeps its own (wider) stop via crypto_stop_width_pct — untouched here.
                # `signal` is detached (session closed), so this mutation is in-memory only and
                # is consumed consistently by sizing, the broker order, and the Position below.
                if not is_crypto_asset and engine.is_enabled("equity_stop_width_max_pct"):
                    _cap_pct = float(engine.threshold("equity_stop_width_max_pct") or 8.0)
                    _min_stop = entry_price * (1.0 - _cap_pct / 100.0)
                    _raw_stop = float(signal.stop_price)
                    if _raw_stop < _min_stop:
                        signal.stop_price = round(_min_stop, 6)
                        with get_db() as _cap_db:
                            _cap_db.add(AuditLog(
                                action=AuditAction.TASK_RUN,
                                organization_id=org.id,
                                ticker=signal.ticker,
                                message=(f"🛡 {signal.ticker}: equity stop tightened "
                                         f"${_raw_stop:.3f} → ${_min_stop:.3f} "
                                         f"(capped at {_cap_pct:.0f}% below entry ${entry_price:.3f})"),
                                detail={"signal_id": signal.id, "orig_stop": _raw_stop,
                                        "capped_stop": round(_min_stop, 6),
                                        "cap_pct": _cap_pct, "entry": entry_price},
                            ))

                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=entry_price,
                    stop_price=float(signal.stop_price),
                    engine=engine,
                    currency=asset_currency,
                    base_currency=base_currency,
                    is_crypto=is_crypto_asset,
                    regime_multiplier=0.5 if regime == "CAUTION" else 1.0,
                    avg_vol_50=avg_vol if not is_crypto_asset else None,
                )

                # Cap the position value by the available capital
                from app.data.fetcher import get_fx_rate
                try:
                    if base_currency == asset_currency:
                        fx_rate = 1.0
                    else:
                        fx_rate = get_fx_rate(base_currency, asset_currency)
                except Exception:
                    fx_rate = 0.65 if (base_currency == "AUD" and asset_currency == "USD") else 1.0

                avail_capital_local = current_avail_capital * (fx_rate if fx_rate else 1.0)
                max_shares_by_avail = avail_capital_local / entry_price
                if not is_crypto_asset:
                    import math
                    max_shares_by_avail = max(1.0, math.floor(max_shares_by_avail))

                if sizing.shares > max_shares_by_avail:
                    _orig_shares = sizing.shares
                    sizing.shares = max_shares_by_avail
                    sizing.capital_local = sizing.shares * entry_price
                    sizing.capital_aud = sizing.capital_local / (fx_rate if fx_rate else 1.0)
                    sizing.risk_aud = (sizing.shares * (entry_price - float(signal.stop_price))) / (fx_rate if fx_rate else 1.0)
                    logger.info(f"Capping position size for {signal.ticker} to available capital: {_orig_shares} -> {sizing.shares} shares")

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

                # Order routing:
                #   Equity (paper OR live) → IBKR gateway. "Paper" means the
                #     gateway is logged into a paper account (e.g. DUR…), so these
                #     are REAL orders on the IBKR paper account, NOT internal
                #     fakes. IBKRBroker.submit_bracket_order only falls back to an
                #     internal simulation if it genuinely can't connect.
                #   Crypto testnet → internal simulation (no live exchange order)
                #   Crypto live    → ccxt crypto broker
                detected_paper_mode = None   # I1 (CLAUDE.md #41): set below only for the equity/IBKR path
                if is_crypto and crypto_testnet:
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
                            order_ref=f"astratrade-{signal.id}",
                        )
                else:
                    # Equity — submit a real bracket to the IBKR gateway (which is
                    # itself in paper or live mode per the org's ibkr_paper_mode).
                    # pivot_price switches the entry leg to BUY STOP-LIMIT instead
                    # of a plain LIMIT at the (already-passed) confirm price — see
                    # CLAUDE.md #39 / IBKRBroker.submit_bracket_order's docstring.
                    with IBKRBroker(organization_id=org.id) as broker:
                        result = broker.submit_bracket_order(
                            ticker=signal.ticker.replace(".AX", ""),
                            action="BUY",
                            qty=sizing.shares,
                            entry_price=entry_price,
                            stop_price=float(signal.stop_price),
                            target_price=float(signal.target_price_1 or entry_price * 1.20),
                            exchange_key=signal.exchange_key or "ASX",
                            order_ref=f"astratrade-{signal.id}",
                            pivot_price=float(signal.pivot_price) if signal.pivot_price else None,
                            limit_buffer_pct=entry_limit_buffer_pct,
                        )
                        detected_paper_mode = broker.detected_paper_mode

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
                    is_simulated = (result.get("status") == "simulated" or result.get("simulated") is True)
                    order_status = OrderStatus.FILLED if is_simulated else OrderStatus.SUBMITTED
                    qty_filled   = sizing.shares if is_simulated else 0
                    avg_fill_price = entry_price if is_simulated else None
                    filled_at    = _dt.utcnow() if is_simulated else None
                    # I1 (CLAUDE.md #41): prefer the gateway-detected paper/live state
                    # over Account.is_paper when we actually connected and could tell —
                    # it can never disagree with what the order really was.
                    _effective_is_paper = detected_paper_mode if detected_paper_mode is not None else signal_is_paper

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
                        # For an equity STP LMT entry, result["limit_price"] is the real
                        # working limit (trigger * (1 + entry_limit_buffer_pct)) — the
                        # babysitter in sync_order_status compares live price against
                        # this, not the pre-buffer confirm price.
                        limit_price=result.get("limit_price", entry_price) if not is_crypto else entry_price,
                        stop_price=float(signal.stop_price),
                        avg_fill_price=avg_fill_price,
                        is_paper=_effective_is_paper,
                        ibkr_order_id=result.get("entry_order_id") if is_crypto else result.get("ibkr_parent_id"),
                        perm_id=None if is_crypto else result.get("ibkr_parent_perm_id"),
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
                            pivot_price=float(signal.pivot_price) if signal.pivot_price else None,
                            risk_aud=round((entry_price - float(signal.stop_price)) * sizing.shares, 2),
                            is_paper=_effective_is_paper,
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
                            message=f"Position opened {'(simulated)' if is_simulated else '(IBKR bracket submitted)'}: {sizing.shares}x{signal.ticker} @ {entry_price:.3f} | Stop ${float(signal.stop_price):.3f}",
                            detail={"initial_stop": float(signal.stop_price)},
                        ))

                # Simulated fills are final immediately (no broker to reconcile
                # against), so send_order_fill is correct here. Real broker
                # orders are only SUBMITTED at this point — the actual fill
                # confirmation comes later from sync_order_status once the
                # broker reports the execution, so send the "submitted" notice
                # instead of prematurely announcing a fill that hasn't happened.
                if is_simulated:
                    notifier.send_order_fill(signal.ticker, "BUY", sizing.shares, entry_price, _effective_is_paper)
                else:
                    notifier.send_order_submitted(signal.ticker, "BUY", sizing.shares, entry_price, _effective_is_paper)
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
        except Exception as e:
            logger.warning(f"check_exit_rules_task [{exchange_key}]: market-closed audit write failed: {e}", exc_info=True)
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
                    Position.asset_type == "CRYPTO"
                ).all()
            elif exchange_key in ("NYSE", "NASDAQ", "US"):
                positions = db.query(Position).filter(
                    Position.organization_id == org.id,
                    Position.status == TradeStatus.OPEN,
                    Position.exchange_key.in_(["NYSE", "NASDAQ"])
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
        except Exception as e:
            logger.warning(f"check_exit_rules_task [{exchange_key}]: evaluating-count audit write failed: {e}", exc_info=True)

        for pos in positions:
            try:
                if not pos.current_stop:
                    try:
                        with get_db() as _db:
                            _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                             ticker=pos.ticker, message=f"Exit check @ {now_str}: skipped — no stop price set",
                                             entity_type="Position", entity_id=str(pos.id),
                                             detail={"result": "skipped", "reason": "no_stop_price"}))
                    except Exception as e:
                        logger.warning(f"check_exit_rules_task: no-stop-price audit write failed for {pos.ticker}: {e}", exc_info=True)
                    continue

                df = get_price_history(pos.ticker, period="6mo")
                if df is None or df.empty:
                    try:
                        with get_db() as _db:
                            _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                             ticker=pos.ticker, message=f"Exit check @ {now_str}: skipped — no price data",
                                             entity_type="Position", entity_id=str(pos.id),
                                             detail={"result": "skipped", "reason": "no_price_data"}))
                    except Exception as e:
                        logger.warning(f"check_exit_rules_task: no-price-data audit write failed for {pos.ticker}: {e}", exc_info=True)
                    continue

                latest  = df.iloc[-1]
                avg_vol = float(df["avg_vol_50"].iloc[-1] or 0)

                # Bug #3 fix: use intraday price for stop/exit decisions; fall back to EOD close.
                _asset_type = getattr(pos, "asset_type", "EQUITY") or "EQUITY"
                _intraday = get_intraday_price(pos.ticker, org.id, asset_type=_asset_type)
                if _intraday.get("ok") and _intraday.get("price"):
                    current = float(_intraday["price"])
                else:
                    current = float(latest["close"])

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
                    pivot_price=float(pos.pivot_price) if getattr(pos, "pivot_price", None) else None,
                )

                _is_crypto_pos = (pos.asset_type == "CRYPTO" or (pos.exchange_key and pos.exchange_key.startswith("CRYPTO_")))
                has_exit = any(s.should_exit for s in exit_signals)
                # For non-crypto equity positions the STOP_LOSS signal is delegated to
                # sync_stop_orders; don't label it as an "EXIT triggered" in the audit log.
                _actionable_exits = [
                    s for s in exit_signals
                    if s.should_exit and not (not _is_crypto_pos and s.reason == ExitReason.STOP_LOSS)
                ]
                _stop_deferred = (
                    not _is_crypto_pos
                    and any(s.should_exit and s.reason == ExitReason.STOP_LOSS for s in exit_signals)
                )
                pnl_pct_val = round((current - float(pos.entry_price)) / float(pos.entry_price) * 100, 2)
                if _actionable_exits:
                    triggered_sig = _actionable_exits[0]
                    msg = (f"Exit check @ {now_str}: EXIT triggered — {triggered_sig.reason} | "
                           f"Price ${current:.3f} | P&L {pnl_pct_val:+.1f}% | Reason: {triggered_sig.message}")
                elif _stop_deferred:
                    msg = (f"Exit check @ {now_str}: stop breach — price ${current:.3f} ≤ stop "
                           f"${float(pos.current_stop):.3f} | P&L {pnl_pct_val:+.1f}% | "
                           f"(deferred to sync_stop_orders — broker bracket stop active)")
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
                                                 "result": ("exit_triggered" if _actionable_exits
                                                            else "stop_breach_deferred" if _stop_deferred
                                                            else "holding"),
                                                 "hold_days": (today - pos.entry_date).days}))
                        _db.commit()
                except Exception as e:
                    logger.error(f"Exit check audit write failed for {pos.ticker}: {e}")

                is_crypto = _is_crypto_pos   # reuse — already computed above
                for exit_sig in exit_signals:
                    if not exit_sig.should_exit:
                        continue

                    # CLAUDE.md #37/#30: equity stop execution is owned entirely by
                    # sync_stop_orders, which checks for a live broker bracket stop
                    # before acting and never closes the DB position optimistically.
                    # If check_exit_rules_task also acted on STOP_LOSS for equities it
                    # would (a) submit a duplicate SELL on top of the existing bracket
                    # stop — creating a naked short when both fill — and (b) mark the
                    # position CLOSED before the real fill is confirmed by
                    # sync_order_status (T1). Skip STOP_LOSS here for non-crypto
                    # positions; sync_stop_orders handles it correctly.
                    if not is_crypto and exit_sig.reason == ExitReason.STOP_LOSS:
                        logger.debug(
                            f"check_exit_rules_task: skipping STOP_LOSS for equity {pos.ticker} "
                            f"— delegated to sync_stop_orders (CLAUDE.md #37/#30)"
                        )
                        continue

                    qty_to_sell = float(pos.qty)
                    if exit_sig.exit_type == "PARTIAL":
                        qty_to_sell = max(1, int(float(pos.qty) * exit_sig.partial_pct / 100))

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

                    # Bug #4 fix: apply FX conversion for USD positions
                    _pos_currency = getattr(pos, "currency", None) or "AUD"
                    _fx_rate = 1.0
                    if _pos_currency == "USD" and not is_crypto:
                        try:
                            from app.data.fetcher import get_fx_rate as _gfxr
                            _fx_rate = float(_gfxr("USD", "AUD") or 1.0)
                        except Exception:
                            _fx_rate = 1.0
                    pnl_native = (current - float(pos.entry_price)) * qty_to_sell
                    pnl_aud    = pnl_native / _fx_rate
                    pnl_pct    = (current - float(pos.entry_price)) / float(pos.entry_price) * 100
                    # Bug #5 fix: commission in AUD regardless of position currency
                    if is_crypto:
                        commission_aud = 0.0
                    elif _pos_currency == "USD":
                        commission_aud = round(6.0 / max(_fx_rate, 0.01), 2)
                    else:
                        commission_aud = 6.0

                    with get_db() as db:
                        if exit_sig.exit_type == "FULL":
                            position_obj = db.query(Position).get(pos.id)
                            if position_obj:
                                position_obj.status = TradeStatus.CLOSED
                            trade = Trade(
                                ticker=pos.ticker, account_id=pos.account_id,
                                organization_id=org.id, signal_id=pos.signal_id,
                                exchange_key=pos.exchange_key,
                                asset_type=getattr(pos, "asset_type", "EQUITY"),
                                currency=_pos_currency,
                                entry_date=pos.entry_date, exit_date=today,
                                hold_days=(today - pos.entry_date).days,
                                entry_price=pos.entry_price, exit_price=current,
                                qty=qty_to_sell,
                                gross_pnl_aud=round(pnl_aud, 2),
                                net_pnl_aud=round(pnl_aud - commission_aud, 2),
                                # Trade.pnl_pct is stored as a FRACTION (e.g. -0.0443 for -4.43%),
                                # matching the convention used by the manual-close route
                                # (dashboard/main.py) and the closed-trades display, which
                                # multiplies by 100 when rendering. pnl_pct above is already a
                                # raw percentage (computed with `* 100`), so divide back down —
                                # previously this stored the raw percentage directly, which the
                                # display then multiplied by 100 again (e.g. -4.43% rendered as
                                # -443%).
                                pnl_pct=round(pnl_pct / 100, 4),
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
                logger.error(f"Exit check error for {pos.ticker} (Org: {org.name}): {e}", exc_info=True)
                try:
                    with get_db() as _db:
                        _db.add(AuditLog(action=AuditAction.TASK_RUN, organization_id=org.id,
                                         ticker=pos.ticker,
                                         message=f"Exit check @ {now_str}: error — {type(e).__name__}: {e}",
                                         entity_type="Position", entity_id=str(pos.id),
                                         detail={"result": "error", "error": str(e)}))
                except Exception as audit_e:
                    logger.warning(f"check_exit_rules_task: error-audit write failed for {pos.ticker}: {audit_e}", exc_info=True)


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
        # Overlap lock (T9 / CLAUDE.md #40): this task is scheduled every 5 min
        # for crypto — a slow run still going when the next tick fires could
        # double-submit a market sell or double-process a stop. Fails open on
        # Redis outage.
        if not _acquire_org_lock(f"sync_stop_orders_lock:{org.id}"):
            logger.debug(f"sync_stop_orders: org {org.id} already running — skipping")
            continue

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
                pos_currency = getattr(pos, "currency", None) or "AUD"

                if not is_crypto:
                    # Equity stop-loss detection — fetch intraday price and act if breached.
                    from app.data.fetcher import get_intraday_price as _gip
                    price_result = _gip(pos.ticker, org.id, asset_type="EQUITY")
                    if not price_result.get("ok") or not price_result.get("price"):
                        logger.debug(f"sync_stop_orders: no live price for equity {pos.ticker} — skip")
                        continue
                    _eq_price = float(price_result["price"])
                    _eq_stop  = float(pos.current_stop) if pos.current_stop else None
                    _eq_entry = float(pos.entry_price) if pos.entry_price else None
                    if not _eq_stop or not _eq_entry:
                        continue
                    if _eq_price <= _eq_stop:
                        logger.warning(f"sync_stop_orders: equity {pos.ticker} price through stop — price {_eq_price:.3f} ≤ stop {_eq_stop:.3f}")
                        _sym = "US$" if pos_currency == "USD" else "A$"
                        _bare_ticker = pos.ticker.replace(".AX", "")

                        # CLAUDE.md #37: never fire our own sell while the broker's real
                        # bracket stop-loss child is still working — whichever of the two
                        # fills first would leave the other trying to sell shares already
                        # gone, a naked short in either direction. sync_order_status (T1)
                        # is the one true fill-detector for a live bracket; this task only
                        # alerts in that case. Only a position with NO working stop at the
                        # broker (an imported position, or a bracket that was cancelled) is
                        # ever acted on directly here, and even then the DB position is left
                        # OPEN until sync_order_status confirms the real fill — never closed
                        # optimistically (see CLAUDE.md #30).
                        has_live_stop = False
                        sell_result = None
                        try:
                            with IBKRBroker(organization_id=org.id) as broker:
                                if broker.is_connected:
                                    live_orders = broker.get_open_orders() or []
                                    has_live_stop = any(
                                        (o.get("ticker") or "").upper() == _bare_ticker.upper()
                                        and o.get("action") == "SELL"
                                        for o in live_orders
                                    )
                                    if not has_live_stop:
                                        sell_result = broker.submit_market_sell(
                                            ticker=_bare_ticker, qty=float(pos.qty),
                                            exchange_key=pos.exchange_key or "ASX",
                                            order_ref=f"stopsell-{pos.id}",
                                        )
                                else:
                                    logger.warning(f"sync_stop_orders: IBKR not connected — alert only for {pos.ticker}")
                        except Exception as _be:
                            logger.warning(f"sync_stop_orders: IBKR check/sell failed for {pos.ticker}: {_be}")
                            sell_result = {"status": "error", "error": str(_be)}

                        if has_live_stop:
                            # Alert only, throttled to once per 20 min per position so a
                            # slow-filling broker stop doesn't spam every 15-min tick.
                            from datetime import timedelta as _td
                            with get_db() as _db:
                                recent_alert = _db.query(AuditLog).filter(
                                    AuditLog.organization_id == org.id, AuditLog.ticker == pos.ticker,
                                    AuditLog.message.like("%stop breach alert%"),
                                    AuditLog.created_at >= _dt.utcnow() - _td(minutes=20),
                                ).first()
                                _db.add(AuditLog(
                                    action=AuditAction.TASK_RUN, organization_id=org.id, ticker=pos.ticker,
                                    message=(f"⚠ stop breach alert — {pos.ticker} price {_sym}{_eq_price:.4f} is "
                                             f"through stop {_sym}{_eq_stop:.4f}; the broker's own bracket stop "
                                             f"should be executing — check IBKR Gateway if this persists"),
                                    detail={"source": "sync_stop_orders", "result": "breach_alert_only",
                                            "price": _eq_price, "stop": _eq_stop},
                                ))
                                _db.commit()
                            if not recent_alert:
                                try:
                                    notifier = get_notifier(organization_id=org.id)
                                    notifier.send(
                                        f"⚠️ *Stop Breach — Broker Stop Should Fire*\n"
                                        f"{pos.ticker} price {_sym}{_eq_price:.4f} is through stop {_sym}{_eq_stop:.4f}\n"
                                        f"The broker's own bracket order should execute shortly — "
                                        f"check IBKR Gateway if this persists."
                                    )
                                except Exception as _ne:
                                    logger.error(f"sync_stop_orders: breach alert failed for {pos.ticker}: {_ne}")
                        elif sell_result and sell_result.get("status") != "error":
                            with get_db() as _db:
                                _db.add(Order(
                                    ticker=pos.ticker, exchange_key=pos.exchange_key or "ASX",
                                    asset_type="EQUITY", currency=pos_currency,
                                    account_id=pos.account_id, organization_id=org.id,
                                    signal_id=pos.signal_id, action=OrderAction.SELL,
                                    order_type=OrderType.MARKET, status=OrderStatus.SUBMITTED,
                                    qty_ordered=pos.qty, qty_filled=0, is_paper=pos.is_paper,
                                    ibkr_order_id=sell_result.get("ibkr_order_id"),
                                    perm_id=sell_result.get("ibkr_perm_id"),
                                    raw_ibkr_response=sell_result, submitted_at=_dt.utcnow(),
                                ))
                                _db.add(AuditLog(
                                    action=AuditAction.ORDER_SUBMITTED, organization_id=org.id, ticker=pos.ticker,
                                    message=(f"🛑 {pos.ticker}: price through stop with no working broker stop — "
                                             f"submitted a market sell (qty {float(pos.qty):g}); position stays "
                                             f"OPEN until the fill is confirmed by order reconciliation"),
                                    detail={"source": "sync_stop_orders", "result": sell_result},
                                ))
                                _db.commit()
                        else:
                            with get_db() as _db:
                                _db.add(AuditLog(
                                    action=AuditAction.TASK_ERROR, organization_id=org.id, ticker=pos.ticker,
                                    message=(f"❌ {pos.ticker}: price through stop, no working broker stop, and "
                                             f"the market sell FAILED — "
                                             f"{(sell_result or {}).get('error', 'unknown error')}. Position "
                                             f"remains OPEN and unprotected — check IBKR Gateway immediately."),
                                    detail={"source": "sync_stop_orders", "result": sell_result},
                                ))
                                _db.commit()
                            try:
                                notifier = get_notifier(organization_id=org.id)
                                notifier.send_health_alert(
                                    pos.ticker,
                                    "Price through stop, no working broker stop, and the market sell FAILED — "
                                    "position is unprotected, check IBKR Gateway immediately."
                                )
                            except Exception as _ne:
                                logger.error(f"sync_stop_orders: failure alert failed for {pos.ticker}: {_ne}")
                    continue  # equity handled — skip crypto trailing-stop block

                # ── Fetch live price (crypto) ─────────────────────────────
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
                                # Stored as a FRACTION (e.g. -0.0443 for -4.43%), not a raw
                                # percentage — see the matching note in check_exit_rules_task
                                # above. (current-entry)/entry IS the fraction already; no extra
                                # *100 here. This matches the manual-close convention and the
                                # closed-trades display (which multiplies by 100 to render %).
                                pnl_pct=round((current_price - entry_price) / entry_price, 6) if entry_price else 0,
                                initial_stop=p.initial_stop,
                                exit_reason=ExitReason.STOP_LOSS,
                                is_paper=p.is_paper,
                                cgt_eligible_discount=(today_d - p.entry_date).days > 365,
                            ))
                            _csym = "USDT " if pos_currency == "USDT" else ("US$" if pos_currency == "USD" else "A$")
                            db2.add(AuditLog(
                                action=AuditAction.POSITION_CLOSED,
                                organization_id=org.id,
                                ticker=pos.ticker,
                                message=f"🛑 STOP triggered — {pos.ticker} @ {_csym}{current_price:.4f} "
                                        f"stop was {_csym}{stop_price:.4f} P&L {_csym}{realised_pnl:+.2f}",
                            ))
                            db2.commit()
                    # Telegram alert
                    try:
                        notifier = get_notifier(organization_id=org.id)
                        _csym = "USDT " if pos_currency == "USDT" else ("US$" if pos_currency == "USD" else "A$")
                        notifier.send(
                            f"🛑 *Stop Loss Triggered*\n"
                            f"{pos.ticker} closed @ {_csym}{current_price:.4f}\n"
                            f"Stop was {_csym}{stop_price:.4f}\n"
                            f"P&L: {_csym}{realised_pnl:+.2f}"
                        )
                    except Exception as notify_err:
                        logger.error(f"sync_stop_orders: notification failed for {pos.ticker}: {notify_err}", exc_info=True)
                        try:
                            with get_db() as _db3:
                                _db3.add(AuditLog(
                                    action=AuditAction.TASK_ERROR,
                                    organization_id=org.id,
                                    ticker=pos.ticker,
                                    entity_type="TelegramNotification",
                                    message=f"⚠️ Stop-out alert failed to send for {pos.ticker}: {notify_err}",
                                ))
                        except Exception as audit_e:
                            logger.warning(f"sync_stop_orders: notification-failure audit write failed for {pos.ticker}: {audit_e}", exc_info=True)
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

    Runs every 5 minutes. Fetches live prices (IR/MEXC API for crypto, yfinance fallback)
    and writes updated values to:
      1. Position rows in DB (for the dashboard P&L display)
      2. live_price:{ticker} Redis cache (for the trader terminal and watchlist live feed)

    For IR crypto: uses the free IR public API (0-delay, AUD pairs).
    For MEXC/Binance crypto: uses the free MEXC public API (0-delay, USDT pairs).
    For equity positions: uses yfinance 15-min bars.
    """
    from app.models.account import Organization
    from app.models.trade import Position, TradeStatus
    from app.data.fetcher import get_intraday_price, get_fx_rate
    from app.utils.cache import cache

    logger.debug("update_position_pnl_task: refreshing open position P&L...")

    with get_db() as db:
        all_open = db.query(Position).filter(
            Position.status == TradeStatus.OPEN
        ).all()

    if not all_open:
        return

    # Group by ticker to avoid duplicate API calls for same ticker across orgs
    ticker_prices: dict[str, dict | None] = {}  # ticker → full result dict

    for pos in all_open:
        ticker     = pos.ticker
        asset_type = getattr(pos, "asset_type", "EQUITY") or "EQUITY"

        if ticker not in ticker_prices:
            # organization_id is required for the IBKR branch inside get_intraday_price
            # (it needs an org-scoped broker connection) — omitting it silently forced
            # every equity price here onto the 15-min-delayed yfinance fallback instead
            # of live IBKR data, even when the gateway was connected and working (T4.1).
            result = get_intraday_price(ticker, organization_id=pos.organization_id, asset_type=asset_type)
            ticker_prices[ticker] = result if result.get("ok") else None

            # ── Write to Redis live_price cache (drives trader terminal + watchlist) ──
            if result.get("ok") and result.get("price"):
                price_val = float(result["price"])
                live_cache_payload = {
                    "price":      price_val,
                    "close":      price_val,
                    "live_price": price_val,  # presence of this key signals it's real-time
                    "data_source": result.get("data_source", "unknown"),
                    "delay_mins": result.get("delay_mins", 0),
                    "_failed":    False,
                }
                cache.set(f"live_price:{ticker}", live_cache_payload, expire_seconds=360)  # 6 min TTL
            else:
                # Write a failure sentinel so the UI can show "EOD" instead of stale live data
                cache.set(f"live_price:{ticker}", {"_failed": True}, expire_seconds=120)

        price_result = ticker_prices.get(ticker)
        price = price_result.get("price") if price_result else None
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


@app.task(name="app.tasks.trading.refresh_live_prices_cache_task", bind=True)
def refresh_live_prices_cache_task(self):
    """
    Refresh the live_price:{ticker} Redis cache for ALL active crypto tickers:
    watchlist items + pending signals (not just open positions).

    Runs every 5 minutes alongside update_position_pnl_task. Together these two
    tasks ensure the trader terminal and watchlist page show live prices without
    page reloads — even before a position is open.

    Routes:
      -AUD tickers → Independent Reserve free API (0-delay)
      -USD/-USDT tickers → MEXC free public API (0-delay, broadest coverage)
      equity tickers → skipped here (covered by EOD PriceBar from daily screener)
    """
    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.data.fetcher import get_intraday_price
    from app.utils.cache import cache

    logger.debug("refresh_live_prices_cache_task: seeding live price cache for watchlist+signals...")

    crypto_tickers: set[str] = set()

    with get_db() as db:
        # Watchlist crypto items
        wl_rows = db.query(Watchlist.ticker, Watchlist.asset_type).filter(
            Watchlist.status == WatchlistStatus.WATCHING,
        ).all()
        for row in wl_rows:
            ticker_val, at_val = row[0], (row[1] or "EQUITY")
            # Fallback: infer CRYPTO from ticker format when asset_type was not stored
            # correctly (e.g. NULL from the exchange-filter bug fixed in Jun 2026).
            if at_val != "CRYPTO" and (
                ticker_val.endswith("-AUD") or ticker_val.endswith("-USD") or ticker_val.endswith("-USDT")
            ):
                at_val = "CRYPTO"
            if at_val == "CRYPTO":
                crypto_tickers.add(ticker_val)

        # Pending signal crypto items
        sig_rows = db.query(Signal.ticker, Signal.asset_type).filter(
            Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
        ).all()
        for row in sig_rows:
            ticker_val, at_val = row[0], (row[1] or "EQUITY")
            if at_val != "CRYPTO" and (
                ticker_val.endswith("-AUD") or ticker_val.endswith("-USD") or ticker_val.endswith("-USDT")
            ):
                at_val = "CRYPTO"
            if at_val == "CRYPTO":
                crypto_tickers.add(ticker_val)

    updated = 0
    failed: list[str] = []
    for ticker in crypto_tickers:
        try:
            result = get_intraday_price(ticker, asset_type="CRYPTO")
            if result.get("ok") and result.get("price"):
                price_val = float(result["price"])
                cache.set(f"live_price:{ticker}", {
                    "price":      price_val,
                    "close":      price_val,
                    "live_price": price_val,
                    "data_source": result.get("data_source", "unknown"),
                    "delay_mins": result.get("delay_mins", 0),
                    "_failed":    False,
                }, expire_seconds=360)
                updated += 1
            else:
                cache.set(f"live_price:{ticker}", {"_failed": True}, expire_seconds=120)
                failed.append(ticker)
        except Exception as e:
            logger.debug(f"refresh_live_prices_cache_task: error for {ticker}: {e}")
            failed.append(ticker)

    logger.debug(f"refresh_live_prices_cache_task: refreshed {updated}/{len(crypto_tickers)} crypto tickers")

    # Write audit log so the health page can display last-run time for this task.
    # Name the failures — "8/21" alone looks like a bug, but the misses are
    # usually coins with no live source on the configured exchange (e.g. -USD
    # pairs on an AUD-only exchange, or coins not in IR_SYMBOL_MAP that should
    # be purged via "Re-seed Crypto Universe").
    _fail_note = ""
    if failed:
        _sample = ", ".join(sorted(failed)[:10])
        _fail_note = f" — {len(failed)} no live source: {_sample}" + ("…" if len(failed) > 10 else "")
    try:
        with get_db() as _db:
            _db.add(AuditLog(
                action=AuditAction.TASK_RUN,
                message=(f"[CRYPTO] Live price cache: refreshed {updated}/{len(crypto_tickers)} "
                         f"watchlist tickers{_fail_note}"),
                detail={"updated": updated, "total": len(crypto_tickers), "failed": sorted(failed)},
            ))
    except Exception as e:
        logger.warning(f"refresh_live_prices_cache_task: audit write failed: {e}", exc_info=True)


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

        # Guard: refuse promotion while an open position exists for this ticker.
        # A PENDING signal for an already-held ticker can never trigger (the entry
        # check skips held tickers) — it would just sit on the Signals page
        # confusing the user next to the live position.
        open_pos = db.query(Position).filter(
            Position.ticker == w.ticker,
            Position.organization_id == organization_id,
            Position.status == TradeStatus.OPEN,
        ).first()
        if open_pos:
            w.status = WatchlistStatus.WATCHING   # revert the optimistic SIGNALLED flip
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=w.ticker,
                actor=user_email,
                user_id=user_id,
                organization_id=organization_id,
                message=(f"Promotion of {w.ticker} refused — an OPEN position already exists "
                         f"(entry ${float(open_pos.entry_price or 0):.3f} on {open_pos.entry_date}). "
                         f"A pending signal cannot trigger while the position is held; close the "
                         f"position first if you want a new entry signal."),
            ))
            db.commit()
            logger.info(f"Promotion of {w.ticker} refused for Org {organization_id} — open position exists")
            return

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
        # Dedup against (a) any signal at all for today — preserves the original
        # intent of not re-litigating a ticker the screener/a prior promotion
        # already evaluated today, regardless of how it resolved — and (b) any
        # still-PENDING/TRIGGERED signal regardless of date. (b) is the fix: a
        # PENDING signal from an earlier day that never triggered/expired was
        # previously invisible to this check (it only looked at
        # signal_date == today), letting a second promotion create a duplicate
        # live PENDING signal for the same ticker. Matches the screener's own
        # dedup pattern in app/tasks/screening.py for the PENDING/TRIGGERED half.
        from sqlalchemy import or_
        existing = db.query(Signal).filter(
            Signal.ticker == w.ticker,
            Signal.organization_id == organization_id,
            or_(
                Signal.signal_date == today,
                Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
            ),
        ).first()

        if existing:
            # A live signal for this ticker already exists (e.g. created earlier by the
            # screener, or a prior manual promotion). Previously we silently flipped the
            # watchlist item to SIGNALLED here with no new visible Signal — from the user's
            # perspective "nothing happened". Surface this clearly via the audit log instead.
            logger.info(f"Manual promotion of {w.ticker}: existing signal #{existing.id} "
                        f"(status={existing.status.value}, date={existing.signal_date}) — not creating a duplicate.")
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=w.ticker,
                actor=user_email,
                organization_id=organization_id,
                message=(f"Manual promotion of {w.ticker} found an existing signal #{existing.id} "
                         f"(status={existing.status.value}, from {existing.signal_date}) — no new signal "
                         f"created. Check the Signals page for the existing entry."),
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

      # Send notification using the background task (best-effort — never fails the promotion)
      try:
          from app.tasks.reporting import send_notification_message
          send_notification_message.delay(
              organization_id,
              "send",
              [f"🚀 *Manual Promotion*: {ticker_for_log} has been manually promoted from Watchlist to Signals for entry!"]
          )
      except Exception as e:
          logger.error(f"Failed to send notification for promotion: {e}")

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


def _norm_symbol(ticker: str) -> str:
    """Normalise a ticker to a bare symbol for cross-source matching.
    'BHP.AX' -> 'BHP', 'AAPL' -> 'AAPL'. Crypto suffixes stripped too for safety."""
    t = (ticker or "").upper()
    for suffix in (".AX", "-USD", "-AUD", "-USDT", "-BTC", "-ETH"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    return t


@app.task(name="app.tasks.trading.sync_ibkr_positions_task", bind=True)
def sync_ibkr_positions_task(self, organization_id: int | None = None):
    """
    Reconcile DB open positions against LIVE IBKR positions.

    Scope: equities only (ASX / NYSE / NASDAQ). Crypto positions are NEVER
    touched here — IBKR doesn't hold them, so they'd look like orphans and get
    wrongly closed. Crypto is reconciled separately via ccxt.

    Reconciliation:
      • IBKR holding not in DB   → import as OPEN Position (avg cost from IBKR,
                                    stop defaulted to -10% and flagged for review)
      • DB position not in IBKR  → auto-close as ExitReason.BROKER_SYNC (Trade row,
                                    per the Position→Trade pattern in CLAUDE.md #30)
      • Both present, qty drift  → DB qty reconciled to IBKR qty

    Every action writes an AuditLog row. Multi-tenant: pass organization_id to
    scope to one org (dashboard button); omit to loop all active orgs (scheduled).
    """
    from app.models.account import Organization, Account
    from app.utils.time_helper import get_current_date

    with get_db() as db:
        if organization_id:
            orgs = db.query(Organization).filter(Organization.id == organization_id).all()
        else:
            orgs = db.query(Organization).filter(Organization.is_active == True).all()

        for org in orgs:
            summary = {"imported": 0, "closed": 0, "updated": 0, "matched": 0}
            try:
                # SAFETY: only reconcile orgs that have EXPLICITLY configured their
                # own ibkr_account. IBKRBroker silently falls back to the global
                # .env IBKR_ACCOUNT default when an org hasn't set one — which
                # means a brand-new org (no ibkr_account yet) would otherwise
                # resolve to whichever account a DIFFERENT org actually owns on
                # the shared gateway, and this task would then "reconcile" that
                # other org's real IBKR holdings into its own Position table.
                # This bit the AW org (id=10) on 2 Jul 2026 — a new org showed an
                # open position that actually belonged to a different org.
                from app.models.config import SystemConfig as _SC
                _own_account_cfg = db.query(_SC).filter(
                    _SC.key == "ibkr_account", _SC.organization_id == org.id,
                ).first()
                if not (_own_account_cfg and (_own_account_cfg.value or "").strip()):
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN, organization_id=org.id,
                        message="IBKR position sync skipped — no ibkr_account configured for this org "
                                "(refusing to reconcile against the shared gateway's default account)",
                        detail={"source": "ibkr_sync", "reason": "no_org_ibkr_account"},
                    ))
                    db.commit()
                    continue

                # --- Pull live IBKR positions for this org's account ---
                broker = IBKRBroker(organization_id=org.id)
                broker.connect()
                if not broker.is_connected:
                    reason = getattr(broker, "last_error", "") or "gateway not reachable"
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN, organization_id=org.id,
                        message=f"IBKR position sync skipped — {reason}",
                        detail={"source": "ibkr_sync", "reason": reason,
                                "host": broker.host, "port": broker.port,
                                "client_id": broker.client_id, "account": broker.account},
                    ))
                    db.commit()
                    continue

                _check_paper_live_mismatch(org, broker, db)

                org_account = (broker.account or "").strip()
                gateway_accounts = list(getattr(broker._ib, "managedAccounts", lambda: [])() or []) \
                    if getattr(broker, "_ib", None) else []
                raw_positions = broker.get_open_positions()
                broker.disconnect()

                # Filter to this org's sub-account (when account tagging present)
                # and drop zero-qty rows IBKR sometimes returns.
                ib_positions = [
                    p for p in raw_positions
                    if float(p.get("qty") or 0) != 0
                    and (not org_account or not p.get("account") or p.get("account") == org_account)
                ]

                # SAFETY: if the org's configured ibkr_account doesn't match the
                # account the gateway is actually logged into, the filter above
                # can wipe out every IBKR position — which would then auto-close
                # all DB equity positions as orphans. Detect that and skip the
                # destructive orphan-close step (still allow imports of nothing).
                account_mismatch = bool(
                    org_account and gateway_accounts and org_account not in gateway_accounts
                )
                if account_mismatch:
                    db.add(AuditLog(
                        action=AuditAction.TASK_RUN, organization_id=org.id,
                        message=(f"IBKR sync: configured account '{org_account}' is not in the "
                                 f"gateway's logged-in accounts {gateway_accounts} — skipping "
                                 f"orphan auto-close to avoid mass-closure. Fix ibkr_account in Config."),
                        detail={"source": "ibkr_sync", "org_account": org_account,
                                "gateway_accounts": gateway_accounts},
                    ))

                account = db.query(Account).filter(
                    Account.organization_id == org.id, Account.is_active == True,
                ).first()

                # DB open positions — EQUITY ONLY (never touch crypto here)
                db_positions = [
                    p for p in db.query(Position).filter(
                        Position.organization_id == org.id,
                        Position.status == TradeStatus.OPEN,
                    ).all()
                    if (p.asset_type or "EQUITY").upper() != "CRYPTO"
                    and not (p.exchange_key or "").upper().startswith("CRYPTO")
                ]

                db_by_sym = {_norm_symbol(p.ticker): p for p in db_positions}
                ib_by_sym = {_norm_symbol(p["ticker"]): p for p in ib_positions}
                today = get_current_date()

                # --- IBKR → DB: import new / reconcile qty drift ---
                for sym, ibp in ib_by_sym.items():
                    qty = abs(float(ibp.get("qty") or 0))
                    avg = float(ibp.get("avg_cost") or 0)
                    currency = (ibp.get("currency") or "AUD").upper()
                    if sym in db_by_sym:
                        pos = db_by_sym[sym]
                        if abs(float(pos.qty or 0) - qty) > 1e-6:
                            old_qty = float(pos.qty or 0)
                            pos.qty = qty
                            summary["updated"] += 1
                            db.add(AuditLog(
                                action=AuditAction.POSITION_UPDATED, organization_id=org.id,
                                ticker=pos.ticker,
                                message=f"IBKR sync: qty reconciled {old_qty:g} → {qty:g}",
                                detail={"source": "ibkr_sync", "old_qty": old_qty, "new_qty": qty},
                            ))
                        else:
                            summary["matched"] += 1
                    else:
                        exchange_key = "ASX" if currency == "AUD" else ("NYSE" if currency == "USD" else "ASX")
                        ticker = ibp["ticker"].upper() + (".AX" if exchange_key == "ASX" else "")
                        entry_price = avg if avg > 0 else 0.0
                        stop = round(entry_price * 0.90, 4) if entry_price else 0.0
                        db.add(Position(
                            ticker=ticker,
                            exchange_key=exchange_key,
                            asset_type="EQUITY",
                            currency=currency,
                            account_id=account.id if account else 1,
                            organization_id=org.id,
                            entry_date=today,
                            entry_price=entry_price,
                            qty=qty,
                            current_price=entry_price,
                            initial_stop=stop,
                            current_stop=stop,
                            target_1=round(entry_price * 1.20, 4) if entry_price else None,
                            target_2=round(entry_price * 1.40, 4) if entry_price else None,
                            is_paper=(account.is_paper if account else True),
                            status=TradeStatus.OPEN,
                        ))
                        summary["imported"] += 1
                        db.add(AuditLog(
                            action=AuditAction.POSITION_OPENED, organization_id=org.id, ticker=ticker,
                            message=(f"IBKR sync: imported {qty:g}x{ticker} @ {entry_price:.4f} "
                                     f"(avg cost from IBKR); stop defaulted to -10% — review"),
                            detail={"source": "ibkr_sync", "avg_cost": avg, "qty": qty},
                        ))

                # --- DB → IBKR: orphans auto-close as BROKER_SYNC ---
                # Guarded: never mass-close when the account is mismatched or the
                # broker returned no positions at all (likely a config/transient
                # issue, not a real "everything was sold" event).
                skip_orphan_close = account_mismatch or not raw_positions
                for sym, pos in (db_by_sym.items() if not skip_orphan_close else []):
                    if sym in ib_by_sym:
                        continue
                    entry_price = float(pos.entry_price or 0)
                    close_price = float(pos.current_price or pos.entry_price or 0)
                    qty = float(pos.qty or 0)
                    pnl_aud = (close_price - entry_price) * qty
                    pnl_pct = (close_price - entry_price) / entry_price * 100 if entry_price else 0
                    pos.status = TradeStatus.CLOSED
                    db.add(Trade(
                        ticker=pos.ticker,
                        account_id=pos.account_id,
                        organization_id=org.id,
                        signal_id=pos.signal_id,
                        entry_date=pos.entry_date,
                        exit_date=today,
                        hold_days=(today - pos.entry_date).days if pos.entry_date else 0,
                        entry_price=pos.entry_price,
                        exit_price=close_price,
                        qty=pos.qty,
                        gross_pnl_aud=round(pnl_aud, 2),
                        net_pnl_aud=round(pnl_aud, 2),
                        pnl_pct=round(pnl_pct / 100, 4),
                        initial_stop=pos.initial_stop,
                        exit_reason=ExitReason.BROKER_SYNC,
                        is_paper=pos.is_paper,
                        cgt_eligible_discount=((today - pos.entry_date).days > 365) if pos.entry_date else False,
                    ))
                    summary["closed"] += 1
                    db.add(AuditLog(
                        action=AuditAction.POSITION_CLOSED, organization_id=org.id, ticker=pos.ticker,
                        message=(f"IBKR sync: not found in IBKR — auto-closed as BROKER_SYNC @ "
                                 f"${close_price:.4f} | P&L ${pnl_aud:+.0f}"),
                        detail={"source": "ibkr_sync", "reason": "orphan_not_in_ibkr"},
                    ))

                db.add(AuditLog(
                    action=AuditAction.TASK_RUN, organization_id=org.id,
                    message=(f"IBKR position sync complete — imported {summary['imported']}, "
                             f"closed {summary['closed']}, updated {summary['updated']}, "
                             f"matched {summary['matched']}"),
                    detail=summary,
                ))
                db.commit()
                logger.info(f"sync_ibkr_positions_task org={org.id}: {summary}")
            except Exception as e:
                db.rollback()
                logger.error(f"sync_ibkr_positions_task failed for org {org.id}: {e}")
                try:
                    db.add(AuditLog(
                        action=AuditAction.TASK_ERROR, organization_id=org.id,
                        message=f"IBKR position sync error: {e}",
                    ))
                    db.commit()
                except Exception:
                    db.rollback()
# end sync_ibkr_positions_task


def _acquire_org_lock(lock_key: str, ttl: int = 240) -> bool:
    """
    Redis SET NX EX mutual-exclusion lock so overlapping runs of a task can't
    double-process the same org (e.g. a slow run still going when the next
    beat tick fires). Fails OPEN (returns True — proceed) if Redis itself is
    unreachable, since refusing to reconcile orders is worse than the small
    risk of a double-run; state-guards on each mutation make re-processing
    a no-op anyway.
    """
    try:
        import redis as _redis
        from app.config import settings as _settings
        r = _redis.from_url(_settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        return bool(r.set(lock_key, "1", nx=True, ex=ttl))
    except Exception:
        return True


def _check_paper_live_mismatch(org, broker, db):
    """
    I1 (CLAUDE.md #41): paper-vs-live must come from the IBKR login itself
    (IBKR separates them by account prefix — DU*/DF* paper, U* live), never
    from a separate app-side flag that can silently disagree with reality.
    Called right after a successful connect(). If the org's Account.is_paper
    label doesn't match what the gateway login actually is, auto-correct the
    label (it's just a label — the real order already happened as whatever
    the gateway is) and alert loudly, throttled to once per day.
    """
    if broker.detected_paper_mode is None:
        return  # couldn't determine (managedAccounts() failed) — nothing to compare
    from app.models.account import Account
    account = db.query(Account).filter(
        Account.organization_id == org.id, Account.is_active == True,
    ).first()
    if not account or bool(account.is_paper) == bool(broker.detected_paper_mode):
        return

    old_label = "PAPER" if account.is_paper else "LIVE"
    real_mode = "PAPER" if broker.detected_paper_mode else "LIVE"
    account.is_paper = broker.detected_paper_mode

    from datetime import timedelta as _td
    already_alerted = db.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%paper/live MISMATCH%"),
        AuditLog.created_at >= _dt.utcnow() - _td(hours=24),
    ).first()
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED, organization_id=org.id,
        message=(f"⚠️ paper/live MISMATCH — app labelled this org's account {old_label}, but the "
                 f"IBKR gateway is logged into a {real_mode} account. Auto-corrected the label to "
                 f"{real_mode} (the account/orders were always real {real_mode} regardless of the "
                 f"label — this only fixes what AstraTrade calls it)."),
        detail={"source": "paper_live_check", "old_label": old_label, "detected": real_mode},
    ))
    if not already_alerted:
        try:
            notifier = get_notifier(organization_id=org.id)
            notifier.send(
                f"⚠️ *Paper/Live Mismatch Detected*\n"
                f"AstraTrade labelled this account *{old_label}*, but the IBKR gateway is logged "
                f"into a *{real_mode}* account.\n"
                f"The label has been auto-corrected to {real_mode} — this doesn't change anything "
                f"at the broker, only what AstraTrade calls it. Verify this is the account you intend."
            )
        except Exception as e:
            logger.error(f"Failed to send paper/live mismatch alert for {org.name}: {e}")


def _classify_sell_exit_reason(fill_price: float, stop: float, target: float | None) -> ExitReason:
    """
    A bracket's stop-loss and take-profit children share the same orderRef, and
    IBKR executions don't carry the order's type/purpose — so the fill is
    classified by which threshold it's closer to. A stop-limit/stop-market
    fill lands at-or-below the stop; a limit take-profit fill lands at-or-above
    the target, so "nearest" is a reliable discriminator in practice.
    """
    dist_stop = abs(fill_price - stop) if stop else float("inf")
    dist_target = abs(fill_price - target) if target else float("inf")
    return ExitReason.STOP_LOSS if dist_stop <= dist_target else ExitReason.PROFIT_TARGET_1


@app.task(name="app.tasks.trading.sync_order_status", bind=True)
def sync_order_status(self, organization_id: int | None = None):
    """
    Reconcile DB Order rows against live IBKR order status + recent executions.

    This is the missing fill-detection step: previously an Order was stamped
    SUBMITTED and nothing ever updated it. A real fill only became a DB
    Position when sync_ibkr_positions_task later "imported an orphan" with a
    defaulted -10% stop, losing the signal's real VCP stop/targets/linkage,
    and an unfilled DAY order silently evaporated at the close with the Order
    stuck SUBMITTED and the Signal stuck TRIGGERED forever, with zero
    telemetry. Equities only — crypto orders aren't routed through IBKR.

    Per org with its own configured ibkr_account (same guard as
    sync_ibkr_positions_task — never fall back to the shared gateway's
    default account):
      - BUY parent filled (fully or partially) → Order FILLED/PARTIAL with the
        real qty/price/commission; the linked Position is created (or, if the
        position-sync safety net already imported it as an orphan with a
        defaulted stop, REPAIRED) with the signal's real stop/targets/linkage.
      - BUY parent no longer open at the broker and no execution found for it
        → Order CANCELLED (DAY order expired unfilled), and the Signal is
        reverted TRIGGERED → PENDING so the next session re-validates the
        breakout from scratch rather than leaving a dead signal invisible.
      - A bracket's SELL child fills → the linked Position (matched by the
        shared orderRef, since child legs are never given their own DB Order
        row) is closed via the Position→Trade pattern (CLAUDE.md #30) using
        the REAL execution price/commission, with exit_reason inferred from
        which threshold (stop vs target) the fill price is closer to.

    Idempotent: a Redis SET NX EX lock per org prevents overlapping runs, and
    every mutation is guarded by the row's current state, so a re-run (or a
    lock-miss under Redis outage) is a no-op rather than double-processing.
    """
    from app.models.account import Organization, Account
    from app.utils.time_helper import get_current_date

    with get_db() as db:
        if organization_id:
            orgs = db.query(Organization).filter(Organization.id == organization_id).all()
        else:
            orgs = db.query(Organization).filter(Organization.is_active == True).all()

    for org in orgs:
        if not _acquire_org_lock(f"sync_order_status_lock:{org.id}"):
            logger.debug(f"sync_order_status: org {org.id} already being reconciled — skipping")
            continue

        summary = {"filled": 0, "partial": 0, "repaired": 0, "cancelled": 0, "closed": 0, "errors": 0}
        with get_db() as db:
            try:
                from app.models.config import SystemConfig as _SC
                own_account_cfg = db.query(_SC).filter(
                    _SC.key == "ibkr_account", _SC.organization_id == org.id,
                ).first()
                if not (own_account_cfg and (own_account_cfg.value or "").strip()):
                    # sync_ibkr_positions_task already audits this every 5 min —
                    # no need to duplicate the log line here.
                    continue

                open_db_orders = db.query(Order).filter(
                    Order.organization_id == org.id,
                    Order.action == OrderAction.BUY,
                    Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL]),
                    Order.exchange_key.in_(["ASX", "NYSE", "NASDAQ"]),
                ).all()
                open_positions = db.query(Position).filter(
                    Position.organization_id == org.id,
                    Position.status == TradeStatus.OPEN,
                    Position.exchange_key.in_(["ASX", "NYSE", "NASDAQ"]),
                ).all()
                if not open_db_orders and not open_positions:
                    continue

                broker = IBKRBroker(organization_id=org.id)
                broker.connect()
                if not broker.is_connected:
                    continue  # gateway down — leave state as-is, retry next run

                live_open = broker.get_open_orders()
                executions = broker.get_executions(days=2)
                broker.disconnect()

                # Defence in depth against a (rare) orderId collision across
                # sub-accounts under a multi-org (FA/linked) gateway login —
                # reqAllOpenOrders()/reqExecutions() return every sub-account's
                # data in one call. Matching purely by ibkr_order_id (which is
                # already scoped to this org's own tracked Order rows) is the
                # primary guard; this is a second check on the data itself.
                # Permissive fallback when account isn't populated (the normal
                # single-account case), matching the pattern in
                # web/main.py's /positions/open-orders filter (CLAUDE.md #41).
                _own_acct = (broker.account or "").strip()
                if _own_acct:
                    live_open = [o for o in live_open if not o.get("account") or o.get("account") == _own_acct]
                    executions = [e for e in executions if not e.get("account") or e.get("account") == _own_acct]

                live_open_ids = {o["ibkr_order_id"] for o in live_open}
                execs_by_order_id: dict[int, list[dict]] = {}
                for e in executions:
                    execs_by_order_id.setdefault(e["order_id"], []).append(e)

                account = db.query(Account).filter(
                    Account.organization_id == org.id, Account.is_active == True,
                ).first()
                today = get_current_date()
                notifier = get_notifier(organization_id=org.id)

                # ── Pass A: BUY parent orders — fill or expiry ──────────────
                for order in open_db_orders:
                    try:
                        fills = [f for f in execs_by_order_id.get(order.ibkr_order_id, []) if f["side"] == "BOT"]
                        if fills:
                            total_qty = sum(f["qty"] for f in fills)
                            if total_qty <= 0:
                                continue
                            weighted_price = sum(f["qty"] * f["avg_price"] for f in fills) / total_qty
                            total_commission = sum(f["commission"] for f in fills)
                            is_full = total_qty >= float(order.qty_ordered) - 1e-6

                            order.status = OrderStatus.FILLED if is_full else OrderStatus.PARTIAL
                            order.qty_filled = total_qty
                            order.avg_fill_price = round(weighted_price, 4)
                            order.commission_local = round(total_commission, 4)
                            order.filled_at = _dt.utcnow()
                            if fills[0].get("perm_id"):
                                order.perm_id = fills[0]["perm_id"]
                            summary["filled" if is_full else "partial"] += 1

                            signal = db.query(Signal).filter(Signal.id == order.signal_id).first() \
                                if order.signal_id else None
                            existing_pos = db.query(Position).filter(
                                Position.organization_id == org.id,
                                Position.ticker == order.ticker,
                                Position.status == TradeStatus.OPEN,
                            ).first()

                            if existing_pos:
                                # The position-sync safety net may have already imported this
                                # as an "orphan" with a defaulted -10% stop and no signal
                                # linkage (race with this task). Repair it with the real
                                # signal-derived stop/targets/linkage rather than duplicating.
                                if signal and not existing_pos.signal_id:
                                    existing_pos.signal_id = signal.id
                                    existing_pos.initial_stop = float(signal.stop_price)
                                    existing_pos.current_stop = float(signal.stop_price)
                                    if signal.target_price_1:
                                        existing_pos.target_1 = float(signal.target_price_1)
                                    if signal.target_price_2:
                                        existing_pos.target_2 = float(signal.target_price_2)
                                    if signal.pivot_price:
                                        existing_pos.pivot_price = float(signal.pivot_price)
                                    summary["repaired"] += 1
                                    db.add(AuditLog(
                                        action=AuditAction.POSITION_UPDATED, organization_id=org.id,
                                        ticker=order.ticker,
                                        message=(f"Order fill reconciliation: repaired {order.ticker} with the "
                                                 f"real signal stop/targets/linkage (real fill ${weighted_price:.3f}), "
                                                 f"replacing the position-sync safety net's defaulted values"),
                                        detail={"source": "sync_order_status", "signal_id": signal.id},
                                    ))
                                # A prior run may have already created/repaired this position
                                # from an earlier partial fill — keep qty/entry in sync as more
                                # fills arrive (weighted_price/total_qty are always recomputed
                                # from the FULL set of matching executions, not incrementally,
                                # so this is idempotent across repeated runs).
                                if abs(float(existing_pos.qty or 0) - total_qty) > 1e-6:
                                    existing_pos.qty = total_qty
                                    existing_pos.entry_price = round(weighted_price, 4)
                                    db.add(AuditLog(
                                        action=AuditAction.POSITION_UPDATED, organization_id=org.id,
                                        ticker=order.ticker,
                                        message=(f"Order fill reconciliation: {order.ticker} qty updated to "
                                                 f"{total_qty:g} (additional fill @ ${weighted_price:.3f})"),
                                        detail={"source": "sync_order_status", "order_id": order.id},
                                    ))
                            else:
                                db.add(Position(
                                    ticker=order.ticker, exchange_key=order.exchange_key,
                                    asset_type=order.asset_type, currency=order.currency,
                                    account_id=account.id if account else order.account_id,
                                    organization_id=org.id, signal_id=signal.id if signal else None,
                                    entry_date=today, entry_price=round(weighted_price, 4), qty=total_qty,
                                    current_price=round(weighted_price, 4),
                                    initial_stop=float(signal.stop_price) if signal else round(weighted_price * 0.90, 4),
                                    current_stop=float(signal.stop_price) if signal else round(weighted_price * 0.90, 4),
                                    target_1=float(signal.target_price_1) if signal and signal.target_price_1 else None,
                                    target_2=float(signal.target_price_2) if signal and signal.target_price_2 else None,
                                    pivot_price=float(signal.pivot_price) if signal and signal.pivot_price else None,
                                    risk_aud=(round((weighted_price - float(signal.stop_price)) * total_qty, 2)
                                              if signal else None),
                                    is_paper=order.is_paper, status=TradeStatus.OPEN,
                                ))
                                db.add(AuditLog(
                                    action=AuditAction.POSITION_OPENED, organization_id=org.id, ticker=order.ticker,
                                    message=(f"✅ {order.ticker}: real fill confirmed via broker reconciliation — "
                                             f"{total_qty:g}x @ ${weighted_price:.3f}"),
                                    detail={"source": "sync_order_status", "signal_id": signal.id if signal else None},
                                ))

                            db.add(AuditLog(
                                action=AuditAction.ORDER_FILLED, organization_id=org.id, ticker=order.ticker,
                                message=(f"Order fill confirmed: {total_qty:g}x{order.ticker} @ ${weighted_price:.3f}"
                                         + ("" if is_full else " (partial)")),
                                detail={"source": "sync_order_status", "order_id": order.id},
                            ))
                            try:
                                notifier.send_order_fill(order.ticker, "BUY", total_qty,
                                                          weighted_price, order.is_paper)
                            except Exception as ne:
                                logger.warning(f"sync_order_status: fill notification failed for {order.ticker}: {ne}")

                        elif order.ibkr_order_id not in live_open_ids:
                            # No longer working at the broker and no fill found — a DAY
                            # order that expired unfilled at the session close.
                            order.status = OrderStatus.CANCELLED
                            order.cancelled_at = _dt.utcnow()
                            summary["cancelled"] += 1
                            db.add(AuditLog(
                                action=AuditAction.ORDER_CANCELLED, organization_id=org.id, ticker=order.ticker,
                                message=(f"⏹ Entry order for {order.ticker} expired unfilled (DAY order) — "
                                         f"signal re-armed for next session"),
                                detail={"source": "sync_order_status", "order_id": order.id},
                            ))
                            if order.signal_id:
                                signal = db.query(Signal).filter(Signal.id == order.signal_id).first()
                                if signal and signal.status == SignalStatus.TRIGGERED:
                                    signal.status = SignalStatus.PENDING
                                    db.add(AuditLog(
                                        action=AuditAction.TASK_RUN, organization_id=org.id, ticker=order.ticker,
                                        message=(f"Signal {order.ticker} reverted TRIGGERED → PENDING "
                                                 f"(entry order expired unfilled)"),
                                        detail={"source": "sync_order_status", "signal_id": signal.id},
                                    ))
                        else:
                            # Working-order babysitter (T2 / CLAUDE.md #39): a stop-limit
                            # that hasn't triggered costs nothing and simply expires at the
                            # close if the breakout fails, so a pullback is never cancelled
                            # here — only a price that keeps running well beyond the entry's
                            # own limit, where waiting for a fill would mean paying far more
                            # than the Minervini "don't chase" ceiling ever intended.
                            if order.limit_price:
                                try:
                                    _live = get_intraday_price(order.ticker, organization_id=org.id, asset_type="EQUITY")
                                    if _live.get("ok") and _live.get("price"):
                                        _max_ext_pct = float(RuleEngine(organization_id=org.id, tier=org.tier.value)
                                                              .threshold("vcp_max_extension") or 5.0)
                                        _cancel_above = float(order.limit_price) * (1 + _max_ext_pct / 100.0)
                                        if float(_live["price"]) > _cancel_above:
                                            try:
                                                with IBKRBroker(organization_id=org.id) as _cancel_broker:
                                                    if _cancel_broker.is_connected:
                                                        _cancel_broker.cancel_order(order.ibkr_order_id)
                                            except Exception as ce:
                                                logger.warning(f"sync_order_status: cancel failed for {order.ticker}: {ce}")
                                            order.status = OrderStatus.CANCELLED
                                            order.cancelled_at = _dt.utcnow()
                                            summary["cancelled"] += 1
                                            db.add(AuditLog(
                                                action=AuditAction.ORDER_CANCELLED, organization_id=org.id, ticker=order.ticker,
                                                message=(f"⏹ {order.ticker}: cancelled — extended beyond buy range "
                                                         f"(price ${_live['price']:.3f} > ${_cancel_above:.3f}); "
                                                         f"signal re-armed"),
                                                detail={"source": "sync_order_status", "order_id": order.id,
                                                        "result": "cancelled_extended"},
                                            ))
                                            if order.signal_id:
                                                _sig = db.query(Signal).filter(Signal.id == order.signal_id).first()
                                                if _sig and _sig.status == SignalStatus.TRIGGERED:
                                                    _sig.status = SignalStatus.PENDING
                                                    db.add(AuditLog(
                                                        action=AuditAction.TASK_RUN, organization_id=org.id, ticker=order.ticker,
                                                        message=(f"Signal {order.ticker} reverted TRIGGERED → PENDING "
                                                                 f"(entry order cancelled — extended beyond buy range)"),
                                                        detail={"source": "sync_order_status", "signal_id": _sig.id},
                                                    ))
                                except Exception as be:
                                    logger.debug(f"sync_order_status: babysitter check failed for {order.ticker}: {be}")
                    except Exception as oe:
                        summary["errors"] += 1
                        logger.error(f"sync_order_status: error reconciling order {order.id} ({order.ticker}): {oe}")

                # ── Pass B: SELL fills — close the linked Position ──────────
                # Bracket child legs are never given their own DB Order row, but
                # every leg of a bracket shares the same orderRef
                # ("astratrade-{signal_id}"), so a SELL execution is matched back
                # to its Position via that ref. A sync_stop_orders market-sell
                # fallback (submitted only when there's no working broker stop —
                # see CLAUDE.md #37) uses its own "stopsell-{position_id}" ref,
                # which works even for positions with no signal_id (orphan imports).
                sell_execs_by_ref: dict[str, list[dict]] = {}
                for e in executions:
                    ref = e.get("order_ref") or ""
                    if e["side"] == "SLD" and (ref.startswith("astratrade-") or ref.startswith("stopsell-")):
                        sell_execs_by_ref.setdefault(ref, []).append(e)

                for pos in open_positions:
                    try:
                        fills = (sell_execs_by_ref.get(f"astratrade-{pos.signal_id}")
                                 or sell_execs_by_ref.get(f"stopsell-{pos.id}"))
                        if not fills:
                            continue
                        total_qty = sum(f["qty"] for f in fills)
                        if total_qty <= 0:
                            continue
                        weighted_price = sum(f["qty"] * f["avg_price"] for f in fills) / total_qty
                        total_commission = sum(f["commission"] for f in fills)

                        entry_price = float(pos.entry_price or 0)
                        fx = float(pos.entry_fx_rate or pos.current_fx_rate or 1.0) or 1.0
                        pnl_local = (weighted_price - entry_price) * total_qty
                        pnl_aud = pnl_local / fx
                        pnl_pct = ((weighted_price - entry_price) / entry_price) if entry_price else 0.0
                        commission_aud = total_commission / fx

                        exit_reason = _classify_sell_exit_reason(
                            weighted_price, float(pos.current_stop or 0),
                            float(pos.target_1) if pos.target_1 else None,
                        )

                        pos.status = TradeStatus.CLOSED
                        db.add(Trade(
                            ticker=pos.ticker, exchange_key=pos.exchange_key, asset_type=pos.asset_type,
                            currency=pos.currency, account_id=pos.account_id, organization_id=org.id,
                            signal_id=pos.signal_id, entry_date=pos.entry_date, exit_date=today,
                            hold_days=(today - pos.entry_date).days if pos.entry_date else 0,
                            entry_price=pos.entry_price, exit_price=round(weighted_price, 4), qty=total_qty,
                            gross_pnl_aud=round(pnl_aud, 2),
                            commission_aud=round(commission_aud, 2),
                            net_pnl_aud=round(pnl_aud - commission_aud, 2),
                            pnl_pct=round(pnl_pct, 6),
                            initial_stop=pos.initial_stop, exit_reason=exit_reason, is_paper=pos.is_paper,
                            cgt_eligible_discount=((today - pos.entry_date).days > 365) if pos.entry_date else False,
                        ))
                        summary["closed"] += 1
                        emoji = "🛑" if exit_reason == ExitReason.STOP_LOSS else "✅"
                        db.add(AuditLog(
                            action=AuditAction.POSITION_CLOSED, organization_id=org.id, ticker=pos.ticker,
                            message=(f"{emoji} {pos.ticker} closed via real broker fill @ ${weighted_price:.3f} "
                                     f"({exit_reason.value}) | P&L ${pnl_aud:+.2f}"),
                            detail={"source": "sync_order_status", "signal_id": pos.signal_id},
                        ))
                        try:
                            notifier.send_exit_alert(pos.ticker, exit_reason.value,
                                                      pnl_pct * 100, pnl_aud, pos.is_paper)
                        except Exception as ne:
                            logger.warning(f"sync_order_status: exit notification failed for {pos.ticker}: {ne}")
                    except Exception as pe:
                        summary["errors"] += 1
                        logger.error(f"sync_order_status: error closing position {pos.id} ({pos.ticker}): {pe}")

                db.add(AuditLog(
                    action=AuditAction.TASK_RUN, organization_id=org.id,
                    message=(f"Order status sync complete — filled {summary['filled']}, "
                             f"partial {summary['partial']}, repaired {summary['repaired']}, "
                             f"cancelled {summary['cancelled']}, closed {summary['closed']}, "
                             f"errors {summary['errors']}"),
                    detail=summary,
                ))
                db.commit()
                logger.info(f"sync_order_status org={org.id}: {summary}")
            except Exception as e:
                db.rollback()
                logger.error(f"sync_order_status failed for org {org.id}: {e}")
                try:
                    db.add(AuditLog(
                        action=AuditAction.TASK_ERROR, organization_id=org.id,
                        message=f"Order status sync error: {e}",
                    ))
                    db.commit()
                except Exception:
                    db.rollback()
# end sync_order_status
