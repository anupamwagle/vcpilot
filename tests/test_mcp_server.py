"""Tests for app/mcp/server.py — MCPAuthMiddleware and MCP app setup."""
import asyncio
import pytest
from unittest.mock import AsyncMock


# ---- MCPAuthMiddleware ASGI __call__ tests -----------------------------------
# MCPAuthMiddleware is pure-ASGI (not BaseHTTPMiddleware — see the class
# docstring in app/mcp/server.py for why: BaseHTTPMiddleware buffers the whole
# response, which breaks long-lived SSE streaming). These tests drive it via
# a raw scope/receive/send trio rather than Starlette's TestClient, and use
# asyncio.run() directly rather than @pytest.mark.asyncio, matching this
# project's convention elsewhere (pytest-asyncio isn't installed — a bare
# @pytest.mark.asyncio async def test silently SKIPS instead of running).

def _make_scope(auth_header=None, path="/mcp/sse"):
    headers = []
    if auth_header:
        headers.append((b"authorization", auth_header.encode()))
    return {"type": "http", "path": path, "headers": headers}


async def _run_middleware(scope):
    """Drive MCPAuthMiddleware against a mock inner app, capturing any ASGI
    messages the middleware itself sends (i.e. the request was rejected
    before reaching the inner app)."""
    from app.mcp.server import MCPAuthMiddleware

    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    inner_app = AsyncMock()
    middleware = MCPAuthMiddleware(app=inner_app)
    await middleware(scope, receive, send)
    return messages, inner_app


def _status_of(messages):
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"]


def test_middleware_missing_bearer_returns_401():
    messages, inner_app = asyncio.run(_run_middleware(_make_scope(auth_header=None)))
    assert _status_of(messages) == 401
    inner_app.assert_not_called()


def test_middleware_invalid_token_returns_401():
    messages, inner_app = asyncio.run(_run_middleware(_make_scope(auth_header="Bearer notvalidtoken")))
    assert _status_of(messages) == 401
    inner_app.assert_not_called()


def test_middleware_valid_token_calls_next(db_session, org_and_account):
    from app.mcp.auth import create_access_token
    from app.models.mcp import MCPCredential, generate_client_id, generate_client_secret, hash_secret
    from datetime import datetime, timedelta

    org, _ = org_and_account
    secret = generate_client_secret()
    client_id = generate_client_id()
    cred = MCPCredential(
        organization_id=org.id,
        name="Test",
        client_id=client_id,
        client_secret_hash=hash_secret(secret),
        client_secret_preview=secret[:8] + "...",
        scopes=["trading:read"],
        expires_at=datetime.utcnow() + timedelta(days=365),
        is_active=True,
    )
    db_session.add(cred)
    db_session.commit()

    token = create_access_token(
        org_id=org.id, scopes=["trading:read"],
        credential_id=cred.id, client_id=cred.client_id
    )

    messages, inner_app = asyncio.run(_run_middleware(_make_scope(auth_header=f"Bearer {token}")))
    inner_app.assert_called_once()
    # Auth succeeded — the middleware delegates straight through without
    # writing any response of its own.
    assert messages == []


def test_middleware_valid_token_but_revoked_cred_returns_401(db_session, org_and_account):
    from app.mcp.auth import create_access_token
    from app.models.mcp import MCPCredential, generate_client_id, generate_client_secret, hash_secret
    from datetime import datetime, timedelta

    org, _ = org_and_account
    secret = generate_client_secret()
    client_id2 = generate_client_id()
    cred = MCPCredential(
        organization_id=org.id,
        name="Revoked",
        client_id=client_id2,
        client_secret_hash=hash_secret(secret),
        client_secret_preview=secret[:8] + "...",
        scopes=["trading:read"],
        expires_at=datetime.utcnow() + timedelta(days=365),
        is_active=False,  # Revoked
    )
    db_session.add(cred)
    db_session.commit()

    token = create_access_token(
        org_id=org.id, scopes=["trading:read"],
        credential_id=cred.id, client_id=cred.client_id
    )

    messages, inner_app = asyncio.run(_run_middleware(_make_scope(auth_header=f"Bearer {token}")))
    assert _status_of(messages) == 401
    inner_app.assert_not_called()


def test_middleware_non_http_scope_passes_through():
    """Non-HTTP scopes (e.g. lifespan) must bypass auth entirely."""
    from app.mcp.server import MCPAuthMiddleware

    inner_app = AsyncMock()
    middleware = MCPAuthMiddleware(app=inner_app)
    scope = {"type": "lifespan"}
    asyncio.run(middleware(scope, AsyncMock(), AsyncMock()))
    inner_app.assert_called_once()


# ---- _build_mcp_server -------------------------------------------------------

def test_build_mcp_server_returns_fastmcp():
    """_build_mcp_server() should construct a FastMCP instance with tools registered."""
    try:
        from app.mcp.server import _build_mcp_server
        mcp = _build_mcp_server()
        assert mcp is not None
        assert hasattr(mcp, "tool")
    except ImportError:
        pytest.skip("mcp package not installed")


def test_create_mcp_app_returns_starlette():
    """create_mcp_app() returns a Starlette ASGI application."""
    try:
        from app.mcp.server import create_mcp_app
        app = create_mcp_app()
        from starlette.applications import Starlette
        assert isinstance(app, Starlette)
    except ImportError:
        pytest.skip("mcp package not installed")
