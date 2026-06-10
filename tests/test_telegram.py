"""Tests for app/notifications/telegram.py — TelegramNotifier."""
import pytest
from unittest.mock import patch, MagicMock


def _make_notifier(enabled=True, token="testtoken", chat_id="12345"):
    """Build a TelegramNotifier bypassing __init__ to avoid DB calls."""
    from app.notifications.telegram import TelegramNotifier
    n = TelegramNotifier.__new__(TelegramNotifier)
    n.organization_id = None
    n.telegram_enabled = enabled
    n.token = token
    n.chat_id = chat_id
    return n


# --- send() disabled path ---

def test_send_returns_false_when_disabled():
    n = _make_notifier(enabled=False)
    assert n.send("hello") is False


def test_send_returns_false_when_no_token():
    n = _make_notifier(token="", chat_id="12345")
    assert n.send("hello") is False


def test_send_returns_false_when_no_chat_id():
    n = _make_notifier(chat_id="")
    assert n.send("hello") is False


# --- send() success path ---

def test_send_calls_httpx_post():
    import httpx
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        result = n.send("Test message")
    mock_post.assert_called_once()
    assert result is True


def test_send_returns_false_on_non_200():
    n = _make_notifier()
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    with patch("httpx.post", return_value=mock_resp):
        result = n.send("Test message")
    assert result is False


def test_send_swallows_httpx_error():
    n = _make_notifier()
    with patch("httpx.post", side_effect=Exception("connection refused")):
        result = n.send("Test")
    assert result is False


# --- Higher-level alert methods ---

def test_send_signal_alert_formats_ticker():
    n = _make_notifier()
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_signal_alert({"ticker": "BHP.AX", "pivot_price": 45.0, "stop_price": 42.0,
                             "rs_rating": 80, "suggested_size_shares": 100, "risk_per_trade_aud": 400})
    mock_send.assert_called_once()
    assert "BHP.AX" in mock_send.call_args[0][0]


def test_send_order_fill_buy():
    n = _make_notifier()
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_order_fill("BHP.AX", "BUY", 100, 45.0, True)
    mock_send.assert_called_once()
    assert "BUY" in mock_send.call_args[0][0]


def test_send_exit_alert_profit():
    n = _make_notifier()
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_exit_alert("BHP.AX", "PROFIT_TARGET_1", 22.0, 440.0, True)
    msg = mock_send.call_args[0][0]
    assert "BHP.AX" in msg
    assert "+22.0%" in msg


def test_send_exit_alert_loss():
    n = _make_notifier()
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_exit_alert("BHP.AX", "STOP_LOSS", -7.5, -300.0, False)
    msg = mock_send.call_args[0][0]
    assert "-7.5%" in msg


def test_send_regime_change_bull():
    n = _make_notifier()
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_regime_change("CAUTION", "BULL")
    msg = mock_send.call_args[0][0]
    assert "BULL" in msg


def test_send_daily_report():
    n = _make_notifier()
    report = {
        "date": "2026-06-10",
        "signals_count": 3,
        "open_positions": 2,
        "pnl_today_aud": 150.0,
        "pnl_total_aud": 500.0,
        "market_regime": "BULL",
    }
    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_daily_report(report)
    msg = mock_send.call_args[0][0]
    assert "2026-06-10" in msg
    assert "BULL" in msg


# --- Construction with DB (via test db_session) ---

def test_telegram_notifier_init_no_org(db_session):
    from app.notifications.telegram import TelegramNotifier
    n = TelegramNotifier(organization_id=None)
    assert n is not None
    assert hasattr(n, "telegram_enabled")


def test_telegram_notifier_init_with_org(db_session, org_and_account):
    from app.notifications.telegram import TelegramNotifier
    org, _ = org_and_account
    n = TelegramNotifier(organization_id=org.id)
    assert n is not None
