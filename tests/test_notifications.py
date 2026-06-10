"""Tests for app/notifications/whatsapp.py and app/notifications/__init__.py."""
import pytest
from unittest.mock import patch, MagicMock


# --- get_notifier factory ---

def test_get_notifier_returns_object():
    from app.notifications import get_notifier
    notifier = get_notifier(organization_id=None)
    assert notifier is not None


def test_get_notifier_has_send_method():
    from app.notifications import get_notifier
    notifier = get_notifier(organization_id=None)
    assert callable(getattr(notifier, "send", None))


# --- WhatsAppNotifier construction ---

def test_whatsapp_notifier_init_no_org():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier(organization_id=None)
    assert n is not None


def test_whatsapp_notifier_init_with_org(db_session, org_and_account):
    from app.notifications.whatsapp import WhatsAppNotifier
    org, _ = org_and_account
    # Should not raise even if no SystemConfig keys exist
    n = WhatsAppNotifier(organization_id=org.id)
    assert n is not None


# --- send() ---

def test_send_returns_false_when_disabled():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = False
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}
    result = n.send("test")
    assert result is False


def test_send_returns_false_when_no_jid():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = ""
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}
    result = n.send("test")
    assert result is False


def test_send_calls_httpx_post_on_success():
    import httpx
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}

    mock_resp = MagicMock()
    mock_resp.status_code = 201

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        result = n.send("Hello test")
    mock_post.assert_called_once()
    assert result is True


def test_send_swallows_connection_error():
    import httpx
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}

    with patch("httpx.post", side_effect=Exception("unreachable")):
        result = n.send("Should not raise")
    assert result is False


# --- Higher-level alert methods ---

def test_send_signal_alert_delegates_to_send():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}

    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_signal_alert({"ticker": "BHP.AX", "pivot_price": 45.0, "stop_price": 41.0,
                              "rs_rating": 80, "suggested_size_shares": 100, "risk_per_trade_aud": 400})
    mock_send.assert_called_once()
    assert "BHP.AX" in mock_send.call_args[0][0]


def test_send_order_fill_delegates_to_send():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}

    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_order_fill("BHP.AX", "BUY", 100, 45.0, True)
    mock_send.assert_called_once()


def test_send_exit_alert_delegates_to_send():
    from app.notifications.whatsapp import WhatsAppNotifier
    n = WhatsAppNotifier.__new__(WhatsAppNotifier)
    n.organization_id = None
    n.whatsapp_enabled = True
    n.admin_jid = "61400000000@c.us"
    n.base_url = "http://waha:3000"
    n.api_key = "k"
    n.session = "default"
    n._headers = {"X-Api-Key": "k", "Content-Type": "application/json"}

    with patch.object(n, "send", return_value=True) as mock_send:
        n.send_exit_alert("BHP.AX", "STOP_LOSS", -5.2, -234.0, True)
    mock_send.assert_called_once()
    assert "BHP.AX" in mock_send.call_args[0][0]
