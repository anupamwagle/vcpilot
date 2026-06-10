"""Tests for app/notifications/whatsapp.py — WhatsAppNotifier."""
import pytest
from unittest.mock import patch, MagicMock
import httpx


def _make_notifier(enabled=False):
    """Create a WhatsAppNotifier with whatsapp disabled to avoid real HTTP calls."""
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.base_url = "http://localhost:3000"
    n.api_key = "testkey"
    n.session = "default"
    n.whatsapp_enabled = enabled
    n.admin_jid = "61400000000@c.us" if enabled else None
    n._headers = {"X-Api-Key": "testkey", "Content-Type": "application/json"}
    return n


# ---- send() -----------------------------------------------------------------

def test_send_disabled_returns_false():
    n = _make_notifier(enabled=False)
    result = n.send("Hello")
    assert result is False


def test_send_no_jid_returns_false():
    n = _make_notifier(enabled=True)
    n.admin_jid = None
    result = n.send("Hello")
    assert result is False


def test_send_success():
    n = _make_notifier(enabled=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        result = n.send("Test message")
    assert result is True
    mock_post.assert_called_once()


def test_send_non_200_returns_false():
    n = _make_notifier(enabled=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server Error"
    with patch("httpx.post", return_value=mock_resp):
        result = n.send("Test message")
    assert result is False


def test_send_exception_returns_false():
    n = _make_notifier(enabled=True)
    with patch("httpx.post", side_effect=Exception("Connection refused")):
        result = n.send("Test message")
    assert result is False


def test_send_201_accepted():
    n = _make_notifier(enabled=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    with patch("httpx.post", return_value=mock_resp):
        result = n.send("Test message")
    assert result is True


# ---- send_signal_alert() ----------------------------------------------------

def test_send_signal_alert_calls_send():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        result = n.send_signal_alert({
            "ticker": "BHP.AX", "pivot_price": 45.0, "stop_price": 42.0,
            "rs_rating": 80, "suggested_size_shares": 100, "risk_per_trade_aud": 250
        })
    assert result is True
    msg = mock_send.call_args[0][0]
    assert "BHP.AX" in msg
    assert "45.000" in msg


# ---- send_order_fill() -------------------------------------------------------

def test_send_order_fill_buy():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_order_fill("BHP.AX", "BUY", 100, 45.5, is_paper=True)
    msg = mock_send.call_args[0][0]
    assert "BUY" in msg
    assert "BHP.AX" in msg
    assert "PAPER" in msg


def test_send_order_fill_sell_live():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_order_fill("BHP.AX", "SELL", 100, 50.0, is_paper=False)
    msg = mock_send.call_args[0][0]
    assert "SELL" in msg
    assert "LIVE" in msg


# ---- send_exit_alert() -------------------------------------------------------

def test_send_exit_alert_profit():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_exit_alert("BHP.AX", "PROFIT_TARGET_1", 22.5, 500.0, is_paper=False)
    msg = mock_send.call_args[0][0]
    assert "BHP.AX" in msg
    assert "+22.5%" in msg


def test_send_exit_alert_loss():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_exit_alert("BHP.AX", "STOP_LOSS", -8.0, -200.0, is_paper=True)
    msg = mock_send.call_args[0][0]
    assert "-8.0%" in msg


# ---- send_regime_change() ---------------------------------------------------

def test_send_regime_change_bull():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_regime_change("CAUTION", "BULL")
    msg = mock_send.call_args[0][0]
    assert "BULL" in msg
    assert "CAUTION" in msg
    assert "ALLOWED" in msg


def test_send_regime_change_bear():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_regime_change("BULL", "BEAR")
    msg = mock_send.call_args[0][0]
    assert "BEAR" in msg
    assert "SUSPENDED" in msg


# ---- send_daily_report() ----------------------------------------------------

def test_send_daily_report_formats_message():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_daily_report({
            "date": "2026-06-10",
            "signals_count": 3,
            "open_positions": 2,
            "pnl_today_aud": 150.0,
            "pnl_total_aud": 500.0,
            "market_regime": "BULL",
        })
    msg = mock_send.call_args[0][0]
    assert "2026-06-10" in msg
    assert "BULL" in msg


# ---- send_health_alert() ----------------------------------------------------

def test_send_health_alert_sends_message():
    n = _make_notifier(enabled=True)
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_health_alert("Worker", "Worker offline for 20 minutes")
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "Worker" in msg


# ---- get_session_status() ---------------------------------------------------

def test_get_session_status_success():
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "WORKING", "name": "default"}
    with patch("httpx.get", return_value=mock_resp):
        result = n.get_session_status()
    assert result["status"] == "WORKING"


def test_get_session_status_error_returns_unknown():
    n = _make_notifier()
    with patch("httpx.get", side_effect=Exception("Connection refused")):
        result = n.get_session_status()
    assert result["status"] == "UNKNOWN"


# ---- ensure_session() -------------------------------------------------------

def test_ensure_session_already_working():
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "WORKING"}
    with patch("httpx.get", return_value=mock_resp):
        result = n.ensure_session()
    assert result is True


def test_ensure_session_needs_qr():
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "SCAN_QR_CODE"}
    with patch("httpx.get", return_value=mock_resp):
        result = n.ensure_session()
    assert result is False


def test_ensure_session_starts_when_not_found():
    n = _make_notifier()
    get_resp = MagicMock()
    get_resp.status_code = 404

    start_resp = MagicMock()
    start_resp.status_code = 201
    start_resp.json.return_value = {"status": "WORKING"}

    with patch("httpx.get", return_value=get_resp), \
         patch("httpx.post", return_value=start_resp):
        result = n.ensure_session()
    assert result is True


def test_ensure_session_exception_returns_false():
    n = _make_notifier()
    with patch("httpx.get", side_effect=Exception("boom")):
        result = n.ensure_session()
    assert result is False


# ---- get_qr() ---------------------------------------------------------------

def test_get_qr_returns_base64():
    import base64
    n = _make_notifier()
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "image/png"}
    mock_resp.content = fake_png
    with patch("httpx.get", return_value=mock_resp):
        result = n.get_qr()
    assert result == base64.b64encode(fake_png).decode("utf-8")


def test_get_qr_returns_none_on_error():
    n = _make_notifier()
    with patch("httpx.get", side_effect=Exception("boom")):
        result = n.get_qr()
    assert result is None


# ---- _get_waha_tier() -------------------------------------------------------

def test_get_waha_tier_plus():
    from app.notifications.whatsapp import WhatsAppNotifier
    WhatsAppNotifier._waha_tier = None  # Reset class-level cache
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"tier": "PLUS", "version": "2025"}
    with patch("httpx.get", return_value=mock_resp):
        tier = n._get_waha_tier()
    assert tier == "PLUS"
    WhatsAppNotifier._waha_tier = None  # Clean up


def test_get_waha_tier_defaults_core_on_error():
    from app.notifications.whatsapp import WhatsAppNotifier
    WhatsAppNotifier._waha_tier = None
    n = _make_notifier()
    with patch("httpx.get", side_effect=Exception("boom")):
        tier = n._get_waha_tier()
    assert tier == "CORE"
    WhatsAppNotifier._waha_tier = None
