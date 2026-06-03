"""
WhatsApp Agent — Command handler for remote control of VCPilot.

Supported commands (case-insensitive):
  STATUS              — System status overview
  POSITIONS           — List all open positions with P&L
  SIGNALS             — Today's generated signals
  WATCHLIST           — Current watchlist
  MARKET              — Current market regime
  PAUSE               — Suspend new trade entries
  RESUME              — Resume new trade entries
  REPORT              — Generate daily P&L report
  SKIP <TICKER>       — Cancel a pending signal for today
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

from app.database import get_db
from app.models.config import SystemConfig, RuleConfig
from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.notifications.whatsapp import WhatsAppNotifier


class AgentCommandHandler:
    """
    Parses incoming WhatsApp messages and executes commands.
    Each method returns a response string sent back to the user.
    """

    def __init__(self, notifier: WhatsAppNotifier = None):
        self.notifier = notifier or WhatsAppNotifier()

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
            "SKIP":      self.cmd_skip,
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
            open_positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).count()
            today_signals  = db.query(Signal).filter(Signal.signal_date == date.today()).count()

        status = "⏸ PAUSED" if trading_paused else "▶️ ACTIVE"
        return (
            f"🤖 *VCPilot Status*\n"
            f"Trading: {status}\n"
            f"Open positions: {open_positions}\n"
            f"Today's signals: {today_signals}\n"
            f"Mode: {'📄 PAPER' if self._is_paper() else '💰 LIVE'}"
        )

    def cmd_positions(self, args) -> str:
        with get_db() as db:
            positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).all()
        if not positions:
            return "No open positions."
        lines = ["📋 *Open Positions*"]
        for p in positions:
            pnl_pct = ((p.current_price - p.entry_price) / p.entry_price * 100) \
                if p.current_price and p.entry_price else 0
            lines.append(
                f"• *{p.ticker}*: {p.qty} shares @ ${p.entry_price:.3f} "
                f"| Now ${p.current_price:.3f if p.current_price else 0:.3f} "
                f"({pnl_pct:+.1f}%) | Stop ${p.current_stop:.3f}"
            )
        return "\n".join(lines)

    def cmd_signals(self, args) -> str:
        with get_db() as db:
            signals = db.query(Signal).filter(
                Signal.signal_date == date.today()
            ).all()
        if not signals:
            return "No signals generated today."
        lines = [f"📈 *Today's Signals ({date.today()})*"]
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
                Watchlist.status == WatchlistStatus.WATCHING
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
            report = generate_daily_report()
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

    def cmd_skip(self, args) -> str:
        if not args:
            return "Usage: SKIP <TICKER>"
        ticker = args[0].replace(".AX", "") + ".AX"
        with get_db() as db:
            signal = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.signal_date == date.today(),
                Signal.status == SignalStatus.PENDING,
            ).first()
            if signal:
                signal.status = SignalStatus.SKIPPED
                db.add(signal)
            else:
                return f"No pending signal found for {ticker} today."
        self._audit(AuditAction.MANUAL_OVERRIDE, ticker=ticker,
                    detail={"action": "skip_signal"})
        return f"✅ Signal for *{ticker}* skipped for today."

    def cmd_exit(self, args) -> str:
        if not args:
            return "Usage: EXIT <TICKER>"
        ticker = args[0].replace(".AX", "") + ".AX"
        # Flag position for exit on next market open
        with get_db() as db:
            pos = db.query(Position).filter(
                Position.ticker == ticker,
                Position.status == TradeStatus.OPEN
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
        ticker = args[0].replace(".AX", "") + ".AX"
        try:
            new_stop = float(args[1])
        except ValueError:
            return f"Invalid stop price: {args[1]}"
        with get_db() as db:
            pos = db.query(Position).filter(
                Position.ticker == ticker,
                Position.status == TradeStatus.OPEN
            ).first()
            if not pos:
                return f"No open position for {ticker}."
            old_stop = float(pos.current_stop)
            pos.current_stop = new_stop
            db.add(pos)
        self._audit(AuditAction.STOP_UPDATED, ticker=ticker,
                    before_value=str(old_stop), after_value=str(new_stop))
        return f"✅ Stop for *{ticker}* updated: ${old_stop:.3f} → ${new_stop:.3f}"

    def cmd_rule(self, args) -> str:
        if len(args) < 2:
            return "Usage: RULE <RULE_ID> ON|OFF"
        rule_id = args[0].lower()
        state = args[1].upper()
        if state not in ("ON", "OFF"):
            return "State must be ON or OFF"
        enabled = (state == "ON")
        with get_db() as db:
            rule = db.query(RuleConfig).filter(RuleConfig.rule_id == rule_id).first()
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
            cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if not cfg:
                return f"Config key '{key}' not found."
            old = cfg.value
            cfg.value = value
            cfg.updated_by = "agent"
            db.add(cfg)
        self._audit(AuditAction.CONFIG_CHANGED, entity_type="SystemConfig", entity_id=key,
                    before_value=old, after_value=value)
        return f"✅ Config *{key}* updated: {old} → {value}"

    def cmd_help(self, args) -> str:
        return (
            "🤖 *VCPilot Commands*\n"
            "STATUS — System overview\n"
            "POSITIONS — Open positions\n"
            "SIGNALS — Today's signals\n"
            "WATCHLIST — Stocks being watched\n"
            "MARKET — Regime status\n"
            "PAUSE / RESUME — Toggle trading\n"
            "REPORT — Daily P&L report\n"
            "SKIP <TICKER> — Cancel today's signal\n"
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
        cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return cfg.value if cfg else default

    def _set_config(self, key: str, value: str, actor: str = "agent"):
        with get_db() as db:
            cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if cfg:
                cfg.value = value
                cfg.updated_by = actor
                db.add(cfg)

    def _is_paper(self) -> bool:
        from app.config import settings
        return settings.ibkr_paper_mode

    def _audit(self, action: AuditAction, ticker: str = None, **kwargs):
        try:
            with get_db() as db:
                db.add(AuditLog(action=action, actor="agent", ticker=ticker, **kwargs))
        except Exception as e:
            logger.warning(f"Agent audit log failed: {e}")
