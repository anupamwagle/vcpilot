"""
WhatsApp notifications via WAHA (WhatsApp HTTP API).
WAHA runs as a Docker container and exposes a REST API.

Usage:
    notifier = WhatsAppNotifier()
    notifier.send("🚨 Signal: BHP.AX — pivot at $46.20")
"""
from __future__ import annotations
import httpx
from loguru import logger
from app.config import settings


class WhatsAppNotifier:
    """Send messages to the admin WhatsApp via WAHA."""

    def __init__(self):
        self.base_url = settings.waha_api_url.rstrip("/")
        self.session  = settings.waha_session
        self.api_key  = settings.waha_api_key
        self.admin_jid= settings.whatsapp_admin_jid
        self._headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def send(self, message: str, chat_id: str = None) -> bool:
        """Send a text message. Defaults to admin chat."""
        target = chat_id or self.admin_jid
        if not target:
            logger.warning("No WhatsApp target configured")
            return False

        url = f"{self.base_url}/api/sendText"
        payload = {
            "session": self.session,
            "chatId": target,
            "text": message,
        }
        try:
            resp = httpx.post(url, json=payload, headers=self._headers, timeout=10)
            if resp.status_code in (200, 201):
                logger.debug(f"WhatsApp sent: {message[:60]}...")
                return True
            else:
                logger.warning(f"WhatsApp send failed: {resp.status_code} {resp.text[:100]}")
                return False
        except Exception as e:
            logger.error(f"WhatsApp send error: {e}")
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
        date      = report.get("date", "")
        signals   = report.get("signals_count", 0)
        positions = report.get("open_positions", 0)
        pnl_today = report.get("pnl_today_aud", 0)
        pnl_total = report.get("pnl_total_aud", 0)
        regime    = report.get("market_regime", "UNKNOWN")
        msg = (
            f"📊 *VCPilot Daily Report — {date}*\n"
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
