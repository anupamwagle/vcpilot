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
from app.notifications.base import BaseNotifier


class WhatsAppNotifier(BaseNotifier):
    """Send messages to the admin WhatsApp via WAHA."""

    _waha_tier = None  # Cache tier ("CORE" or "PLUS") to avoid hitting the endpoint on every instance

    def _get_waha_tier(self) -> str:
        """Query WAHA /api/version to determine the tier (CORE or PLUS)."""
        if WhatsAppNotifier._waha_tier is not None:
            return WhatsAppNotifier._waha_tier
        try:
            headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}
            resp = httpx.get(f"{self.base_url}/api/version", headers=headers, timeout=0.5)
            if resp.status_code == 200:
                tier = resp.json().get("tier", "CORE").upper()
                WhatsAppNotifier._waha_tier = tier
                return tier
        except Exception as e:
            logger.debug(f"Failed to query WAHA version/tier: {e}")
        return "CORE"

    def __init__(self, organization_id=None):
        self.organization_id = organization_id
        self.base_url  = settings.waha_api_url.rstrip("/")
        self.api_key   = settings.waha_api_key
        self.session   = settings.waha_session or "default"
        
        # Default fallback configurations
        self.whatsapp_enabled = settings.whatsapp_enabled
        self.admin_jid = settings.admin_jid
        
        session_val = None
        if organization_id:
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
                        return c.value if c else None
                        
                    enabled_val = cfg("whatsapp_enabled")
                    if enabled_val is not None:
                        self.whatsapp_enabled = enabled_val.lower() in ("true", "1", "yes")
                        
                    number_val = cfg("whatsapp_admin_number")
                    if number_val:
                        num = number_val.lstrip("+").replace(" ", "")
                        self.admin_jid = f"{num}@c.us"

                    api_key_val = cfg("whatsapp_api_key")
                    if api_key_val:
                        self.api_key = api_key_val

                    session_val = cfg("whatsapp_session_name")
                finally:
                    db.close()
            except Exception:
                pass

        # Determine WAHA tier to see if we should enforce "default"
        is_plus = False
        try:
            is_plus = (self._get_waha_tier() == "PLUS")
        except Exception:
            pass

        if is_plus:
            if session_val:
                self.session = session_val
            elif organization_id:
                self.session = f"org_{organization_id}"
        else:
            self.session = "default"

        self._headers  = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}


    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    def ensure_session(self) -> bool:
        """
        Start the WAHA session if it isn't running yet.
        Should be called once on app startup (or from a Celery task).
        Returns True if session is/becomes WORKING.
        Does NOT force-restart — use restart_session() for that.
        """
        try:
            # Check current status
            resp = httpx.get(
                f"{self.base_url}/api/sessions/{self.session}",
                headers=self._headers, timeout=10,
            )
            if resp.status_code == 200:
                status = resp.json().get("status", "")
                if status == "WORKING":
                    logger.info(f"WAHA session '{self.session}' already WORKING")
                    return True
                elif status == "SCAN_QR_CODE":
                    logger.warning("WAHA session needs QR code scan — visit /admin/whatsapp to scan")
                    return False

            # Session not started — start it
            start = httpx.post(
                f"{self.base_url}/api/sessions/start",
                json={"name": self.session},
                headers=self._headers, timeout=15,
            )
            if start.status_code in (200, 201):
                new_status = start.json().get("status", "")
                logger.info(f"WAHA session started — status: {new_status}")
                return new_status == "WORKING"
            else:
                logger.warning(f"WAHA session start failed: {start.status_code} {start.text[:100]}")
                return False

        except Exception as e:
            logger.error(f"WAHA session ensure failed: {e}")
            return False

    def restart_session(self) -> str:
        """
        Force-stop the existing session (if any) and start a fresh one.
        A fresh start returns SCAN_QR_CODE which triggers the QR display.
        Returns the new session status string.
        """
        try:
            # 1. Try to stop the existing session gracefully
            stop = httpx.post(
                f"{self.base_url}/api/sessions/{self.session}/stop",
                json={},
                headers=self._headers, timeout=10,
            )
            logger.info(f"WAHA stop → {stop.status_code}")
        except Exception as e:
            logger.debug(f"WAHA stop failed (non-fatal): {e}")

        try:
            # 2. Delete session to fully clean state (WAHA Core)
            httpx.delete(
                f"{self.base_url}/api/sessions/{self.session}",
                headers=self._headers, timeout=10,
            )
        except Exception:
            pass

        import time
        time.sleep(1)   # brief pause for WAHA to clean up

        try:
            # 3. Start fresh session — should return SCAN_QR_CODE
            start = httpx.post(
                f"{self.base_url}/api/sessions/start",
                json={"name": self.session},
                headers=self._headers, timeout=15,
            )
            if start.status_code in (200, 201):
                new_status = start.json().get("status", "STARTING")
                logger.info(f"WAHA session restarted — status: {new_status}")
                return new_status
            else:
                logger.warning(f"WAHA restart start failed: {start.status_code} {start.text[:100]}")
                return "FAILED"
        except Exception as e:
            logger.error(f"WAHA restart failed: {e}")
            return "ERROR"

    def get_qr(self) -> str | None:
        """Return base64 QR code PNG for the current session, or None."""
        try:
            resp = httpx.get(
                f"{self.base_url}/api/{self.session}/auth/qr",
                headers=self._headers, timeout=10,
            )
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "image" in content_type or resp.content.startswith(b"\x89PNG"):
                    import base64
                    return base64.b64encode(resp.content).decode("utf-8")
                
                try:
                    data = resp.json()
                    return data.get("value")   # base64 PNG
                except Exception:
                    import base64
                    return base64.b64encode(resp.content).decode("utf-8")
        except Exception as e:
            logger.debug(f"QR fetch failed: {e}")
        return None

    def get_session_status(self) -> dict:
        """Return raw session status dict from WAHA."""
        try:
            resp = httpx.get(
                f"{self.base_url}/api/sessions/{self.session}",
                headers=self._headers, timeout=8,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"WAHA status check failed: {e}")
        return {"status": "UNKNOWN", "error": "Could not reach WAHA"}

    # -------------------------------------------------------------------------
    # Sending
    # -------------------------------------------------------------------------

    def send(self, message: str, chat_id: str = None) -> bool:
        """Send a text message. Defaults to admin chat."""
        if not self.whatsapp_enabled:
            logger.info(f"WhatsApp Alerts are disabled for Org {self.organization_id}. Did not send message: {message[:60]}...")
            return False


        target = chat_id or self.admin_jid
        if not target:
            logger.warning("WhatsApp: no target JID configured — set WHATSAPP_ADMIN_NUMBER in .env")
            return False

        url = f"{self.base_url}/api/sendText"
        payload = {
            "session": self.session,
            "chatId":  target,
            "text":    message,
        }
        try:
            resp = httpx.post(url, json=payload, headers=self._headers, timeout=10)
            if resp.status_code in (200, 201):
                logger.debug(f"WhatsApp sent OK to {target}: {message[:60]}...")
                return True
            else:
                logger.warning(
                    f"WhatsApp send failed ({resp.status_code}): {resp.text[:120]}\n"
                    f"  → session='{self.session}' target='{target}' waha='{self.base_url}'"
                )
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
