"""
WhatsApp Agent — Command handler for remote control of AstraTrade.

Supported commands (case-insensitive):
  STATUS              — System status overview
  POSITIONS           — List all open positions with P&L
  SIGNALS             — Today's generated signals
  WATCHLIST           — Current watchlist
  MARKET              — Current market regime
  PAUSE               — Suspend new trade entries
  RESUME              — Resume new trade entries
  REPORT              — Generate daily P&L report
  BUY <TICKER>        — Stage a live trade: shows pivot/stop/target/size/risk
                        for today's PENDING signal and asks for confirmation
  CONFIRM <TICKER>    — Execute a staged BUY as a bracket order (live or paper,
                        routed through app.trading.order_executor — same audited
                        path as the dashboard and MCP place_order)
  SKIP <TICKER>       — Cancel a pending signal for today
  UNSKIP <TICKER>     — Restore a skipped signal back to PENDING
  EXIT <TICKER>       — Emergency close an open position (next open)
  STOP <TICKER> <PRICE> — Update stop loss for a position
  RULE <RULE_ID> ON|OFF — Toggle a rule globally
  CONFIG <KEY> <VALUE>  — Update a system config value
  HELP                — List all commands
"""
from __future__ import annotations
import re
from datetime import date
from loguru import logger
from app.utils.time_helper import get_current_date

from app.database import get_db
from app.models.config import SystemConfig, RuleConfig
from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.notifications import get_notifier
from app.notifications.base import BaseNotifier


class AgentCommandHandler:
    """
    Parses incoming messages and executes commands.
    Each method returns a response string sent back to the user.
    """

    def __init__(self, organization_id: int = None, notifier: BaseNotifier = None):
        self.organization_id = organization_id
        self.notifier = notifier or get_notifier(organization_id=organization_id)

    def handle(self, message: str, sender_jid: str) -> str:
        """
        Main entry point. Parse message and dispatch to handler.
        Returns response string.
        """
        self._audit(AuditAction.AGENT_COMMAND, detail={"message": message, "sender": sender_jid})

        text = message.strip().upper()
        parts = text.split()
        if not parts:
            return "No command received."

        cmd = parts[0]
        args = parts[1:]

        handlers = {
            "STATUS":    self.cmd_status,
            "POSITIONS": self.cmd_positions,
            "SIGNALS":   self.cmd_signals,
            "WATCHLIST": self.cmd_watchlist,
            "MARKET":    self.cmd_market,
            "PAUSE":     self.cmd_pause,
            "RESUME":    self.cmd_resume,
            "REPORT":    self.cmd_report,
            "BUY":       self.cmd_buy,
            "TRADE":     self.cmd_buy,
            "CONFIRM":   self.cmd_confirm,
            "SKIP":      self.cmd_skip,
            "UNSKIP":    self.cmd_unskip,
            "EXIT":      self.cmd_exit,
            "STOP":      self.cmd_stop,
            "RULE":      self.cmd_rule,
            "CONFIG":    self.cmd_config,
            "HELP":      self.cmd_help,
        }

        handler = handlers.get(cmd)
        if not handler:
            return f"Unknown command: {cmd}\nSend HELP for list of commands."

        try:
            return handler(args)
        except Exception as e:
            logger.error(f"Agent command error [{cmd}]: {e}")
            return f"⚠️ Error executing {cmd}: {str(e)[:100]}"

    # =========================================================================
    # Command Implementations
    # =========================================================================

    def cmd_status(self, args) -> str:
        with get_db() as db:
            trading_paused = self._get_config(db, "trading_paused", "false").lower() == "true"
            open_positions = db.query(Position).filter(
                Position.status == TradeStatus.OPEN,
                Position.organization_id == self.organization_id
            ).count()
            today_signals  = db.query(Signal).filter(
                Signal.signal_date == get_current_date(),
                Signal.organization_id == self.organization_id
            ).count()

        status = "⏸ PAUSED" if trading_paused else "▶️ ACTIVE"
        return (
            f"🤖 *AstraTrade Status*\n"
            f"Trading: {status}\n"
            f"Open positions: {open_positions}\n"
            f"Today's signals: {today_signals}\n"
            f"Mode: {'📄 PAPER' if self._is_paper() else '💰 LIVE'}"
        )

    def cmd_positions(self, args) -> str:
        with get_db() as db:
            positions = db.query(Position).filter(
                Position.status == TradeStatus.OPEN,
                Position.organization_id == self.organization_id
            ).all()
        if not positions:
            return "No open positions."
        lines = ["📋 *Open Positions*"]
        for p in positions:
            pnl_pct = ((p.current_price - p.entry_price) / p.entry_price * 100) \
                if p.current_price and p.entry_price else 0
            
            # Format based on currency and asset type
            currency = p.currency or "AUD"
            symbol = "$"
            if currency == "AUD":
                symbol = "A$"
            elif currency == "USD":
                symbol = "US$"
            elif currency == "USDT":
                symbol = "USDT "

            is_crypto = (p.asset_type == "CRYPTO" or "-" in p.ticker)
            unit_label = "units" if is_crypto else "shares"
            price_fmt = f"{p.entry_price:.4f}" if is_crypto or p.entry_price < 1.0 else f"{p.entry_price:.2f}"
            curr_fmt = f"{(p.current_price or 0.0):.4f}" if is_crypto or (p.current_price or 0.0) < 1.0 else f"{(p.current_price or 0.0):.2f}"
            stop_fmt = f"{(p.current_stop or 0.0):.4f}" if is_crypto or (p.current_stop or 0.0) < 1.0 else f"{(p.current_stop or 0.0):.2f}"

            lines.append(
                f"• *{p.ticker}*: {p.qty:.6g} {unit_label} @ {symbol}{price_fmt} "
                f"| Now {symbol}{curr_fmt} "
                f"({pnl_pct:+.1f}%) | Stop {symbol}{stop_fmt}"
            )
        return "\n".join(lines)

    def cmd_signals(self, args) -> str:
        with get_db() as db:
            signals = db.query(Signal).filter(
                Signal.signal_date == get_current_date(),
                Signal.organization_id == self.organization_id
            ).all()
        if not signals:
            return "No signals generated today."
        lines = [f"📈 *Today's Signals ({get_current_date()})*"]
        for s in signals:
            lines.append(
                f"• *{s.ticker}* — pivot ${s.pivot_price:.3f} | "
                f"stop ${s.stop_price:.3f} | RS {s.rs_rating:.0f} | {s.status}"
            )
        return "\n".join(lines)

    def cmd_watchlist(self, args) -> str:
        from app.models.signal import Watchlist, WatchlistStatus
        with get_db() as db:
            items = db.query(Watchlist).filter(
                Watchlist.status == WatchlistStatus.WATCHING,
                Watchlist.organization_id == self.organization_id
            ).all()
        if not items:
            return "Watchlist is empty."
        tickers = [w.ticker for w in items]
        return f"👀 *Watchlist ({len(tickers)})*\n" + ", ".join(tickers)

    def cmd_market(self, args) -> str:
        with get_db() as db:
            regime_val = self._get_config(db, "last_market_regime", "UNKNOWN")
            checked_at = self._get_config(db, "last_regime_check", "?")
        emoji = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime_val, "⚪")
        return f"{emoji} *Market Regime*: {regime_val}\nChecked: {checked_at}"

    def cmd_pause(self, args) -> str:
        self._set_config("trading_paused", "true", "agent")
        self._audit(AuditAction.TRADING_PAUSED)
        return "⏸ Trading PAUSED. No new entries will be placed."

    def cmd_resume(self, args) -> str:
        self._set_config("trading_paused", "false", "agent")
        self._audit(AuditAction.TRADING_RESUMED)
        return "▶️ Trading RESUMED. System will process new signals."

    def cmd_report(self, args) -> str:
        from app.tasks.reporting import generate_daily_report
        try:
            report = generate_daily_report(organization_id=self.organization_id)
            return (
                f"📊 *Daily Report — {report.get('date')}*\n"
                f"Market: {report.get('market_regime')}\n"
                f"Signals: {report.get('signals_count')}\n"
                f"Positions: {report.get('open_positions')}\n"
                f"Today P&L: ${report.get('pnl_today_aud', 0):+.0f}\n"
                f"Total P&L: ${report.get('pnl_total_aud', 0):+.0f}"
            )
        except Exception as e:
            return f"Report generation failed: {e}"

    def _resolve_ticker(self, db, user_input: str) -> str:
        """
        Resolves a user-input ticker (e.g. "BHP", "BTC", "BHP.AX", "BTC-AUD")
        to its canonical form stored in the database by searching active positions,
        watchlist, signals, and stocks. Falls back to user_input.
        """
        if not user_input:
            return ""
        raw = user_input.strip().upper()
        
        # 1. Exact match in positions
        pos = db.query(Position).filter(
            Position.organization_id == self.organization_id,
            Position.status == TradeStatus.OPEN,
            Position.ticker == raw
        ).first()
        if pos:
            return pos.ticker

        # 2. Exact match in today's signals
        sig = db.query(Signal).filter(
            Signal.organization_id == self.organization_id,
            Signal.signal_date == get_current_date(),
            Signal.ticker == raw
        ).first()
        if sig:
            return sig.ticker

        # 3. Exact match in watchlist
        from app.models.signal import Watchlist
        wl = db.query(Watchlist).filter(
            Watchlist.organization_id == self.organization_id,
            Watchlist.ticker == raw
        ).first()
        if wl:
            return wl.ticker

        # 4. Try suffixes: e.g. "BHP" -> "BHP.AX", "BTC" -> "BTC-AUD" or "BTC-USD"
        candidates = [
            raw,
            f"{raw}.AX",
            f"{raw}-AUD",
            f"{raw}-USD",
            f"{raw}-USDT",
        ]
        
        # Check active positions first for any candidate
        pos = db.query(Position).filter(
            Position.organization_id == self.organization_id,
            Position.status == TradeStatus.OPEN,
            Position.ticker.in_(candidates)
        ).first()
        if pos:
            return pos.ticker

        # Check signals
        sig = db.query(Signal).filter(
            Signal.organization_id == self.organization_id,
            Signal.signal_date == get_current_date(),
            Signal.ticker.in_(candidates)
        ).first()
        if sig:
            return sig.ticker

        # Check watchlist
        wl = db.query(Watchlist).filter(
            Watchlist.organization_id == self.organization_id,
            Watchlist.ticker.in_(candidates)
        ).first()
        if wl:
            return wl.ticker

        # Check stocks table
        try:
            from app.models.market import Stock
            stk = db.query(Stock).filter(Stock.ticker.in_(candidates)).first()
            if stk:
                return stk.ticker
        except Exception:
            pass

        # Default fallback: if it doesn't have a suffix and looks like an equity, append .AX
        if "." not in raw and "-" not in raw:
            return f"{raw}.AX"
            
        return raw

    def cmd_skip(self, args) -> str:
        if not args:
            return "Usage: SKIP <TICKER>"
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            signal = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.signal_date == get_current_date(),
                Signal.status == SignalStatus.PENDING,
                Signal.organization_id == self.organization_id
            ).first()
            if signal:
                signal.status = SignalStatus.SKIPPED
                db.add(signal)
            else:
                return f"No pending signal found for {ticker} today."
        self._audit(AuditAction.MANUAL_OVERRIDE, ticker=ticker,
                    detail={"action": "skip_signal"})
        return f"✅ Signal for *{ticker}* skipped for today."

    def cmd_unskip(self, args) -> str:
        if not args:
            return "Usage: UNSKIP <TICKER>"
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            signal = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.signal_date == get_current_date(),
                Signal.status == SignalStatus.SKIPPED,
                Signal.organization_id == self.organization_id
            ).first()
            if signal:
                signal.status = SignalStatus.PENDING
                db.add(signal)
            else:
                return f"No skipped signal found for {ticker} today."
        self._audit(AuditAction.MANUAL_OVERRIDE, ticker=ticker,
                    detail={"action": "unskip_signal"})
        return f"↩ Signal for *{ticker}* restored to PENDING."

    def cmd_exit(self, args) -> str:
        if not args:
            return "Usage: EXIT <TICKER>"
        # Flag position for exit on next market open
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            pos = db.query(Position).filter(
                Position.ticker == ticker,
                Position.status == TradeStatus.OPEN,
                Position.organization_id == self.organization_id
            ).first()
            if not pos:
                return f"No open position for {ticker}."
            pos.current_stop = pos.current_price or pos.entry_price  # Set stop to current = immediate
            pos.stop_type = "MANUAL"
            db.add(pos)
        self._audit(AuditAction.MANUAL_OVERRIDE, ticker=ticker,
                    detail={"action": "manual_exit"})
        return f"🔴 *{ticker}* flagged for exit at next opportunity."

    def cmd_stop(self, args) -> str:
        if len(args) < 2:
            return "Usage: STOP <TICKER> <NEW_STOP_PRICE>"
        try:
            new_stop = float(args[1])
        except ValueError:
            return f"Invalid stop price: {args[1]}"
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            pos = db.query(Position).filter(
                Position.ticker == ticker,
                Position.status == TradeStatus.OPEN,
                Position.organization_id == self.organization_id
            ).first()
            if not pos:
                return f"No open position for {ticker}."
            old_stop = float(pos.current_stop)
            pos.current_stop = new_stop
            db.add(pos)
        self._audit(AuditAction.STOP_UPDATED, ticker=ticker,
                    before_value=str(old_stop), after_value=str(new_stop))
        return f"✅ Stop for *{ticker}* updated: ${old_stop:.4f} → ${new_stop:.4f}"

    def cmd_buy(self, args) -> str:
        """
        Stage a live trade for confirmation.

        Looks up today's PENDING signal for the ticker and replies with the
        AstraTrade-calculated entry/stop/target/size/risk so the user can review
        before committing capital. Nothing is submitted to the broker here —
        send `CONFIRM <TICKER>` to actually execute via the same audited
        execute_signal_order() path the dashboard and MCP use.
        """
        if not args:
            return "Usage: BUY <TICKER>  (then CONFIRM <TICKER> to execute)"
        raw = args[0].upper()
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            signal = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.signal_date == get_current_date(),
                Signal.status == SignalStatus.PENDING,
                Signal.organization_id == self.organization_id
            ).first()
            if not signal:
                return f"No PENDING signal found for {raw} today. Send SIGNALS to see what's live."

            mode = "📄 PAPER" if self._is_paper() else "💰 LIVE"
            size = signal.suggested_size_shares or 0
            risk = float(signal.risk_per_trade_aud) if signal.risk_per_trade_aud else 0.0
            target = float(signal.target_price_1) if signal.target_price_1 else None
            risk_pct = ((float(signal.pivot_price) - float(signal.stop_price))
                        / float(signal.pivot_price) * 100) if signal.pivot_price and signal.stop_price else 0

            return (
                f"🟡 *Confirm trade — {mode}*\n"
                f"Ticker: *{signal.ticker}*\n"
                f"Pivot/entry ~: ${float(signal.pivot_price):.4f}\n"
                f"Stop: ${float(signal.stop_price):.4f} ({risk_pct:.1f}% risk)\n"
                + (f"Target: ${target:.4f}\n" if target else "")
                + f"Suggested size: {size} units (≈${risk:.0f} at risk)\n"
                f"RS Rating: {(signal.rs_rating or 0):.0f}/100\n\n"
                f"⚠️ Live price & size are recalculated at execution — this may differ slightly.\n"
                f"Reply *CONFIRM {raw}* to submit the order, or *SKIP {raw}* to cancel."
            )

    def cmd_confirm(self, args) -> str:
        """
        Execute the bracket order for a previously staged BUY.
        Routes through app.trading.order_executor.execute_signal_order — the
        same audited path used by the dashboard and the MCP place_order tool.
        """
        if not args:
            return "Usage: CONFIRM <TICKER>  (after BUY <TICKER>)"
        raw = args[0].upper()
        with get_db() as db:
            ticker = self._resolve_ticker(db, args[0])
            signal = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.signal_date == get_current_date(),
                Signal.status == SignalStatus.PENDING,
                Signal.organization_id == self.organization_id
            ).first()
            if not signal:
                return f"No PENDING signal found for {raw}. It may have already been triggered, skipped, or expired."
            signal_id = signal.id
            ticker = signal.ticker

        self._audit(AuditAction.MANUAL_OVERRIDE, ticker=ticker,
                    detail={"action": "whatsapp_confirm_buy", "signal_id": signal_id})

        from app.trading.order_executor import execute_signal_order
        result = execute_signal_order(
            signal_id=signal_id,
            organization_id=self.organization_id,
            actor=f"whatsapp:{self.organization_id}",
            notes="Confirmed via WhatsApp",
        )

        if not result.get("ok"):
            return f"⛔ Order NOT placed for *{ticker}*: {result.get('error') or result.get('warning')}"

        mode = "📄 PAPER" if self._is_paper() else "💰 LIVE"
        return (
            f"🟢 *Order submitted — {mode}*\n"
            f"*{result['ticker']}* BUY {result['qty']:.6g}\n"
            f"Entry: ${result['entry_price']:.4f}\n"
            f"Stop: ${result['stop_price']:.4f} ({result['risk_pct']:.1f}% risk)\n"
            f"Target: ${result['target_price']:.4f}\n"
            f"Broker: {result['broker']} | Ref: {result['order_ref']}"
        )

    def cmd_rule(self, args) -> str:
        if len(args) < 2:
            return "Usage: RULE <RULE_ID> ON|OFF"
        rule_id = args[0].lower()
        state = args[1].upper()
        if state not in ("ON", "OFF"):
            return "State must be ON or OFF"
        enabled = (state == "ON")
        with get_db() as db:
            rule = db.query(RuleConfig).filter(
                RuleConfig.rule_id == rule_id,
                RuleConfig.organization_id == self.organization_id
            ).first()
            if not rule:
                return f"Rule '{rule_id}' not found."
            if rule.is_mandatory and not enabled:
                return f"⛔ Rule '{rule_id}' is mandatory and cannot be disabled."
            old = rule.enabled_globally
            rule.enabled_globally = enabled
            rule.updated_by = "agent"
            db.add(rule)
        self._audit(AuditAction.RULE_TOGGLED, entity_type="RuleConfig", entity_id=rule_id,
                    before_value=str(old), after_value=str(enabled))
        return f"✅ Rule *{rule_id}* {'ENABLED' if enabled else 'DISABLED'}."

    def cmd_config(self, args) -> str:
        if len(args) < 2:
            return "Usage: CONFIG <KEY> <VALUE>"
        key   = args[0].lower()
        value = " ".join(args[1:])
        with get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == key,
                SystemConfig.organization_id == self.organization_id
            ).first()
            if not cfg:
                return f"Config key '{key}' not found."
            old = cfg.value
            cfg.value = value
            cfg.updated_by = "agent"
            db.add(cfg)

            # Synchronize working capital configuration with active Account capital.
            # NOTE: only working_capital_aud drives account.capital_aud — it is the
            # position-sizing basis. weekly_injection_aud is a reference/planning value
            # only and must NOT overwrite capital_aud (previously it did, which meant
            # setting CONFIG WEEKLY_INJECTION_AUD <amount> would silently replace the
            # account's entire capital with that amount instead of adding to it).
            # Matches the dashboard's /admin/config behaviour in dashboard/main.py.
            if key == "working_capital_aud" and self.organization_id:
                from app.models.account import Account
                account = db.query(Account).filter(
                    Account.is_active == True,
                    Account.organization_id == self.organization_id
                ).first()
                if account:
                    try:
                        account.capital_aud = float(value)
                        db.add(account)
                    except ValueError:
                        pass
        self._audit(AuditAction.CONFIG_CHANGED, entity_type="SystemConfig", entity_id=key,
                    before_value=old, after_value=value)
        return f"✅ Config *{key}* updated: {old} → {value}"

    def cmd_help(self, args) -> str:
        return (
            "🤖 *AstraTrade Commands*\n"
            "STATUS — System overview\n"
            "POSITIONS — Open positions\n"
            "SIGNALS — Today's signals\n"
            "WATCHLIST — Stocks being watched\n"
            "MARKET — Regime status\n"
            "PAUSE / RESUME — Toggle trading\n"
            "REPORT — Daily P&L report\n"
            "BUY <TICKER> — Stage a live trade for review (then CONFIRM)\n"
            "CONFIRM <TICKER> — Execute a staged BUY as a bracket order\n"
            "SKIP <TICKER> — Cancel today's signal\n"
            "UNSKIP <TICKER> — Restore skipped signal\n"
            "EXIT <TICKER> — Emergency exit position\n"
            "STOP <TICKER> <PRICE> — Update stop loss\n"
            "RULE <ID> ON|OFF — Toggle a rule\n"
            "CONFIG <KEY> <VAL> — Update system config\n"
            "HELP — This message"
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _get_config(self, db, key: str, default: str = "") -> str:
        is_global_key = key in ('last_market_regime', 'last_regime_check', 'last_heartbeat')
        org_filter = None if is_global_key else self.organization_id
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == key,
            SystemConfig.organization_id == org_filter
        ).first()
        return cfg.value if cfg else default

    def _set_config(self, key: str, value: str, actor: str = "agent"):
        is_global_key = key in ('last_market_regime', 'last_regime_check', 'last_heartbeat')
        org_filter = None if is_global_key else self.organization_id
        with get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == key,
                SystemConfig.organization_id == org_filter
            ).first()
            if cfg:
                cfg.value = value
                cfg.updated_by = actor
                db.add(cfg)

    def _is_paper(self) -> bool:
        with get_db() as db:
            return self._get_config(db, "ibkr_paper_mode", "true").lower() == "true"

    def _audit(self, action: AuditAction, ticker: str = None, **kwargs):
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=action,
                    actor="agent",
                    ticker=ticker,
                    organization_id=self.organization_id,
                    **kwargs
                ))
        except Exception as e:
            logger.warning(f"Agent audit log failed: {e}")
