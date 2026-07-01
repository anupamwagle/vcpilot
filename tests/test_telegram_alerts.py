"""
Regression tests for the Telegram "can't parse entities" failure.

THE BUG: alerts were sent with parse_mode=Markdown but dynamic values were not
escaped, so an exit reason like 'STOP_LOSS' (the '_' opens an italic entity that
never closes) made Telegram reject the whole message with HTTP 400
"Bad Request: can't parse entities". Exit alerts for stopped-out positions were
silently lost — bad when real money is on the line.

The fix: (1) escape Markdown specials in interpolated values, and (2) a plain-text
retry in send() so an alert is never lost even if an unescaped char slips through.
"""
import pytest

import app.notifications.telegram as tg
from app.notifications.telegram import TelegramNotifier, _esc


def _notifier():
    n = TelegramNotifier.__new__(TelegramNotifier)  # bypass DB-resolving __init__
    n.organization_id = 1
    n.telegram_enabled = True
    n.token = "TESTTOKEN"
    n.chat_ids = ["123456"]
    return n


class _Resp:
    def __init__(self, status_code, text="ok"):
        self.status_code = status_code
        self.text = text


# ── escaping ────────────────────────────────────────────────────────────────
def test_esc_escapes_markdown_specials():
    assert _esc("STOP_LOSS") == "STOP\\_LOSS"
    assert _esc("a*b`c[d") == "a\\*b\\`c\\[d"
    assert _esc(None) == ""
    assert _esc("BHP.AX") == "BHP.AX"  # '.' is not special in legacy Markdown


def test_exit_alert_escapes_reason(monkeypatch):
    captured = {}
    monkeypatch.setattr(tg.TelegramNotifier, "send",
                        lambda self, msg, chat_id=None: captured.setdefault("msg", msg) or True)
    _notifier().send_exit_alert("CGS.AX", "STOP_LOSS", -1.2, -7, True)
    # The raw 'STOP_LOSS' must NOT appear unescaped (that's what broke Telegram)
    assert "STOP\\_LOSS" in captured["msg"]
    assert "STOP_LOSS" not in captured["msg"].replace("STOP\\_LOSS", "")


# ── send() behaviour ────────────────────────────────────────────────────────
def test_send_ok_first_try(monkeypatch):
    calls = []
    monkeypatch.setattr(tg.httpx, "post",
                        lambda url, json, timeout: calls.append(json) or _Resp(200))
    assert _notifier().send("hello *world*") is True
    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "Markdown"


def test_send_falls_back_to_plain_text_on_parse_error(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json)
        if "parse_mode" in json:  # first (Markdown) attempt fails
            return _Resp(400, '{"ok":false,"description":"Bad Request: can\'t parse entities: ..."}')
        return _Resp(200)         # plain-text retry succeeds

    monkeypatch.setattr(tg.httpx, "post", fake_post)
    ok = _notifier().send("bad _entity")
    assert ok is True
    assert len(calls) == 2
    assert "parse_mode" in calls[0]
    assert "parse_mode" not in calls[1]   # retry dropped Markdown


def test_send_non_parse_400_does_not_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(tg.httpx, "post",
                        lambda url, json, timeout: calls.append(json) or _Resp(400, '{"description":"chat not found"}'))
    monkeypatch.setattr(tg.TelegramNotifier, "_audit_send_failure",
                        lambda self, reason, message: None)
    assert _notifier().send("hello") is False
    assert len(calls) == 1   # no plain-text retry for unrelated 400s
