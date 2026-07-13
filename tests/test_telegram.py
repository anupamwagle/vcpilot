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
    n.chat_ids = [chat_id] if chat_id else []
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


# --- Multi-user chat_ids (comma-separated telegram_chat_id) ---

def test_chat_id_property_returns_first_configured():
    n = _make_notifier(chat_id="")
    n.chat_ids = ["111", "222"]
    assert n.chat_id == "111"


def test_chat_id_property_empty_when_no_chats():
    n = _make_notifier(chat_id="")
    assert n.chat_ids == []
    assert n.chat_id == ""


def test_send_broadcasts_to_all_configured_chats():
    n = _make_notifier(chat_id="")
    n.chat_ids = ["111", "222", "333"]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        result = n.send("Broadcast message")
    assert result is True
    assert mock_post.call_count == 3
    sent_chat_ids = {call.kwargs["json"]["chat_id"] for call in mock_post.call_args_list}
    assert sent_chat_ids == {"111", "222", "333"}


def test_send_succeeds_if_at_least_one_chat_succeeds():
    n = _make_notifier(chat_id="")
    n.chat_ids = ["111", "222"]
    ok_resp = MagicMock(status_code=200)
    fail_resp = MagicMock(status_code=500, text="Internal error")
    with patch("httpx.post", side_effect=[fail_resp, ok_resp]):
        result = n.send("Broadcast message")
    assert result is True


def test_send_with_explicit_chat_id_only_sends_to_that_chat():
    """Command replies (webhook) target only the sender's chat, not a broadcast."""
    n = _make_notifier(chat_id="")
    n.chat_ids = ["111", "222", "333"]
    mock_resp = MagicMock(status_code=200)
    with patch("httpx.post", return_value=mock_resp) as mock_post:
        n.send("Reply to sender", chat_id="222")
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["chat_id"] == "222"


class _FakeTelegramRequest:
    """Minimal stand-in for FastAPI's Request — webhook_telegram only awaits
    .json() and reads .headers."""
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def test_webhook_telegram_resolves_org_from_any_configured_chat_id(db_session, org_and_account):
    """A second org user DMing the bot from their own chat must still route to the
    org and get a reply — this is the exact multi-user bug: previously only the
    single chat_id stored verbatim in SystemConfig would match."""
    import asyncio
    from unittest.mock import patch
    from web.main import webhook_telegram
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value="111,222,333", organization_id=org.id))
    db_session.add(SystemConfig(key="telegram_bot_token", value="tok", organization_id=org.id))
    db_session.commit()

    request = _FakeTelegramRequest({"message": {"chat": {"id": 222}, "text": "STATUS", "from": {"id": 999}}})

    with patch("app.agent.commands.AgentCommandHandler.handle", return_value="ok"), \
         patch("app.notifications.telegram.TelegramNotifier.send", return_value=True) as mock_send:
        response = asyncio.run(webhook_telegram(request, db=db_session))

    assert response.status_code == 200
    mock_send.assert_called_once()
    # Reply goes only to the sender's own chat, not a broadcast to all configured chats.
    assert mock_send.call_args.kwargs.get("chat_id") == "222"


def test_webhook_telegram_ignores_unknown_chat(db_session, org_and_account):
    import asyncio
    from unittest.mock import patch
    from web.main import webhook_telegram
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value="111,222", organization_id=org.id))
    db_session.commit()

    request = _FakeTelegramRequest({"message": {"chat": {"id": 999999}, "text": "STATUS", "from": {"id": 1}}})

    with patch("app.notifications.telegram.TelegramNotifier.send", return_value=True) as mock_send:
        response = asyncio.run(webhook_telegram(request, db=db_session))

    assert response.status_code == 200
    mock_send.assert_not_called()


def test_webhook_telegram_fails_open_when_secret_not_yet_configured(db_session, org_and_account):
    """Orgs that haven't re-registered their webhook since this check shipped
    have no telegram_webhook_secret row yet — must not be locked out."""
    import asyncio
    from unittest.mock import patch
    from web.main import webhook_telegram
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value="111,222", organization_id=org.id))
    db_session.commit()

    request = _FakeTelegramRequest({"message": {"chat": {"id": 222}, "text": "STATUS", "from": {"id": 1}}})

    with patch("app.agent.commands.AgentCommandHandler.handle", return_value="ok"), \
         patch("app.notifications.telegram.TelegramNotifier.send", return_value=True) as mock_send:
        response = asyncio.run(webhook_telegram(request, db=db_session))

    assert response.status_code == 200
    mock_send.assert_called_once()


def test_webhook_telegram_rejects_mismatched_secret_token(db_session, org_and_account):
    """Once an org has re-registered its webhook (secret configured), a forged
    POST with a guessed chat_id but the wrong/missing secret must be rejected."""
    import asyncio
    from unittest.mock import patch
    from web.main import webhook_telegram
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value="111,222", organization_id=org.id))
    db_session.add(SystemConfig(key="telegram_webhook_secret", value="realsecret", organization_id=org.id))
    db_session.commit()

    request = _FakeTelegramRequest(
        {"message": {"chat": {"id": 222}, "text": "STATUS", "from": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrongsecret"},
    )

    with patch("app.agent.commands.AgentCommandHandler.handle", return_value="ok"), \
         patch("app.notifications.telegram.TelegramNotifier.send", return_value=True) as mock_send:
        response = asyncio.run(webhook_telegram(request, db=db_session))

    assert response.status_code == 403
    mock_send.assert_not_called()


def test_webhook_telegram_accepts_matching_secret_token(db_session, org_and_account):
    import asyncio
    from unittest.mock import patch
    from web.main import webhook_telegram
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value="111,222", organization_id=org.id))
    db_session.add(SystemConfig(key="telegram_webhook_secret", value="realsecret", organization_id=org.id))
    db_session.commit()

    request = _FakeTelegramRequest(
        {"message": {"chat": {"id": 222}, "text": "STATUS", "from": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "realsecret"},
    )

    with patch("app.agent.commands.AgentCommandHandler.handle", return_value="ok"), \
         patch("app.notifications.telegram.TelegramNotifier.send", return_value=True) as mock_send:
        response = asyncio.run(webhook_telegram(request, db=db_session))

    assert response.status_code == 200
    mock_send.assert_called_once()


def test_get_or_create_telegram_webhook_secret_is_idempotent(db_session, org_and_account):
    from web.main import _get_or_create_telegram_webhook_secret
    org, _ = org_and_account

    secret1 = _get_or_create_telegram_webhook_secret(org.id, db_session)
    assert secret1
    secret2 = _get_or_create_telegram_webhook_secret(org.id, db_session)
    assert secret1 == secret2


def test_notifier_parses_comma_separated_chat_ids_from_db(db_session, org_and_account):
    from app.notifications.telegram import TelegramNotifier
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(key="telegram_chat_id", value=" 111 , 222 ,333", organization_id=org.id))
    db_session.add(SystemConfig(key="telegram_bot_token", value="tok", organization_id=org.id))
    db_session.commit()

    n = TelegramNotifier(organization_id=org.id)
    assert n.chat_ids == ["111", "222", "333"]
    assert n.chat_id == "111"


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
