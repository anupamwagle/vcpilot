from __future__ import annotations
import httpx
from loguru import logger
from app.notifications.base import BaseNotifier

class TelegramNotifier(BaseNotifier):
    """
    Send messages to Telegram via the Telegram Bot API.
    Resolves settings (bot token, chat ID) dynamically from database (SystemConfig) or env variables.
    """

    def __init__(self, organization_id: int | None = None):
        self.organization_id = organization_id
        self.telegram_enabled = False
        self.token = ""
        self.chat_id = ""

        # Resolve settings from database
        enabled_val = None
        try:
            from app.database import SessionLocal
            from app.models.config import SystemConfig
            db = SessionLocal()
            try:
                def cfg(key):
                    c = db.query(SystemConfig).filter(
                        SystemConfig.key == key,
                        SystemConfig.organization_id == organization_id
                    ).first()
                    # fallback to global if None
                    if not c and organization_id:
                        c = db.query(SystemConfig).filter(
                            SystemConfig.key == key,
                            SystemConfig.organization_id == None
                        ).first()
                    return c.value if c else None

                enabled_val = cfg("telegram_enabled")
                if enabled_val is not None:
                    self.telegram_enabled = enabled_val.lower() in ("true", "1", "yes")
                
                token_val = cfg("telegram_bot_token")
                if token_val:
                    self.token = token_val

                chat_id_val = cfg("telegram_chat_id")
                if chat_id_val:
                    self.chat_id = chat_id_val
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Failed to load Telegram database config: {e}")

        # Fallback to env variables if not set in DB
        import os
        if not self.token:
            self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not self.chat_id:
            self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if enabled_val is None:
            self.telegram_enabled = os.getenv("TELEGRAM_ENABLED", "true").lower() in ("true", "1", "yes")

    def send(self, message: str, chat_id: str | None = None) -> bool:
        """Send a markdown text message to Telegram."""
        if not self.telegram_enabled:
            logger.info(f"Telegram alerts are disabled for Org {self.organization_id}. Did not send message: {message[:60]}...")
            return False

        token = self.token
        target = chat_id or self.chat_id

        if not token or not target:
            logger.warning(f"Telegram send skipped: Bot Token or Chat ID not configured (Org: {self.organization_id})")
            return False

        # Clean markdown formatting issues (Telegram Markdown can be strict)
        # We ensure bold (*) and italic (_) syntax match
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": target,
            "text": message,
            "parse_mode": "Markdown"
        }
        try:
            resp = httpx.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.debug(f"Telegram sent OK to {target}: {message[:60]}...")
                return True
            else:
                logger.warning(f"Telegram send failed ({resp.status_code}): {resp.text[:120]}")
                return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_signal_alert(self, signal_data: dict) -> bool:
        ticker   = signal_data.get("ticker", "?")
        pivot    = signal_data.get("pivot_price", 0)
        stop     = signal_data.get("stop_price", 0)
        rs       = signal_data.get("rs_rating", 0)
        size     = signal_data.get("suggested_size_shares", 0)
        risk_aud = signal_data.get("risk_per_trade_aud", 0)

        msg = (
            f"📈 *VCPilot Signal*\n"
            f"Ticker: *{ticker}*\n"
            f"Pivot: ${pivot:.3f}\n"
            f"Stop: ${stop:.3f}\n"
            f"RS Rating: {rs:.0f}/100\n"
            f"Suggested size: {size} shares\n"
            f"Risk: ${risk_aud:.0f}\n"
            f"Reply *SKIP {ticker}* to cancel."
        )
        return self.send(msg)

    def send_order_fill(self, ticker: str, action: str, qty: int,
                        price: float, is_paper: bool) -> bool:
        emoji = "🟢" if action == "BUY" else "🔴"
        mode  = "📄 PAPER" if is_paper else "💰 LIVE"
        msg = (
            f"{emoji} *Order Filled* {mode}\n"
            f"{action} {qty}x *{ticker}* @ ${price:.3f}"
        )
        return self.send(msg)

    def send_exit_alert(self, ticker: str, exit_reason: str,
                        pnl_pct: float, pnl_aud: float, is_paper: bool) -> bool:
        emoji = "✅" if pnl_aud >= 0 else "❌"
        mode  = "📄 PAPER" if is_paper else "💰 LIVE"
        msg = (
            f"{emoji} *Position Closed* {mode}\n"
            f"*{ticker}* exited — {exit_reason}\n"
            f"P&L: {pnl_pct:+.1f}% (${pnl_aud:+.0f})"
        )
        return self.send(msg)

    def send_regime_change(self, old_regime: str, new_regime: str) -> bool:
        emoji_map = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}
        msg = (
            f"{emoji_map.get(new_regime, '⚪')} *Market Regime Change*\n"
            f"{old_regime} → *{new_regime}*\n"
            f"{'New entries ALLOWED.' if new_regime == 'BULL' else 'New entries SUSPENDED.'}"
        )
        return self.send(msg)

    def send_daily_report(self, report: dict) -> bool:
        date_str  = report.get("date", "")
        signals   = report.get("signals_count", 0)
        positions = report.get("open_positions", 0)
        pnl_today = report.get("pnl_today_aud", 0)
        pnl_total = report.get("pnl_total_aud", 0)
        regime    = report.get("market_regime", "UNKNOWN")
        msg = (
            f"📊 *VCPilot Daily Report — {date_str}*\n"
            f"Market: {regime}\n"
            f"Signals today: {signals}\n"
            f"Open positions: {positions}\n"
            f"Today P&L: ${pnl_today:+.0f}\n"
            f"Total P&L: ${pnl_total:+.0f}"
        )
        return self.send(msg)

    def send_health_alert(self, component: str, error: str) -> bool:
        msg = f"⚠️ *VCPilot Health Alert*\n{component} error:\n{error[:200]}"
        return self.send(msg)
