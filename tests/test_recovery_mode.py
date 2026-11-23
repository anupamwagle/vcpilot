"""Recovery restrictions must run before any application action."""
import asyncio

import pytest

from app.recovery import RecoveryMiddleware


def invoke(monkeypatch, method, path, enabled="true"):
    monkeypatch.setenv("ASTRA_RECOVERY_MODE", enabled)
    calls, messages = [], []

    async def downstream(scope, receive, send):
        calls.append(scope)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    asyncio.run(RecoveryMiddleware(downstream)(
        {"type": "http", "method": method, "path": path}, receive, send
    ))
    return calls, messages


@pytest.mark.parametrize("method,path", [
    ("POST", "/positions/1/close"), ("POST", "/action/resume"),
    ("GET", "/action/resume"), ("POST", "/login/request-otp"),
    ("POST", "/superadmin/users/create"), ("GET", "/mcp/sse"),
    ("POST", "/mcp/oauth/token"), ("POST", "/webhook/telegram"),
    ("GET", "/authorize"), ("DELETE", "/signals/1"),
])
def test_recovery_blocks_before_dispatch(monkeypatch, method, path):
    calls, messages = invoke(monkeypatch, method, path)
    assert not calls
    assert messages[0]["status"] == 503


@pytest.mark.parametrize("method,path", [
    ("GET", "/login"), ("POST", "/login"), ("GET", "/positions"),
    ("GET", "/healthz"),
])
def test_recovery_allows_review_and_password_login(monkeypatch, method, path):
    calls, messages = invoke(monkeypatch, method, path)
    assert len(calls) == 1
    assert not messages


def test_disabled_recovery_preserves_dispatch(monkeypatch):
    calls, messages = invoke(monkeypatch, "POST", "/action/resume", "false")
    assert len(calls) == 1
    assert not messages
