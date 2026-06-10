"""Tests for app/mcp/server.py — MCPAuthMiddleware and MCP app setup."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---- MCPAuthMiddleware dispatch tests ----------------------------------------

def _make_request(auth_header=None, path="/mcp/sse"):
    """Build a mock Starlette request."""
    request = MagicMock()
    request.url.path = path
    request.headers = {}
    if auth_header:
        request.headers = {"Authorization": auth_header}
    return request


async def _call_next_ok(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True}, status_code=200)


@pytest.mark.asyncio
async def test_middleware_missing_bearer_returns_401():
    from app.mcp.server import MCPAuthMiddleware
    middleware = MCPAuthMiddleware(app=MagicMock())
    request = _make_request(auth_header=None)
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_middleware_invalid_token_returns_401():
    from app.mcp.server import MCPAuthMiddleware
    middleware = MCPAuthMiddleware(app=MagicMock())
    request = _make_request(auth_header="Bearer notvalidtoken")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_middleware_valid_token_calls_next(db_session, org_and_account):
    from app.mcp.server import MCPAuthMiddleware
    from app.mcp.auth import create_access_token
    from app.models.mcp import MCPCredential, generate_client_id, generate_client_secret, hash_secret

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
        is_active=True,
    )
    db_session.add(cred)
    db_session.commit()

    token = create_access_token(
        org_id=org.id, scopes=["trading:read"],
        credential_id=cred.id, client_id=cred.client_id
    )

    middleware = MCPAuthMiddleware(app=MagicMock())
    request = _make_request(auth_header=f"Bearer {token}")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_valid_token_but_revoked_cred_returns_401(db_session, org_and_account):
    from app.mcp.server import MCPAuthMiddleware
    from app.mcp.auth import create_access_token
    from app.models.mcp import MCPCredential, generate_client_id, generate_client_secret, hash_secret

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
        is_active=False,  # Revoked
    )
    db_session.add(cred)
    db_session.commit()

    token = create_access_token(
        org_id=org.id, scopes=["trading:read"],
        credential_id=cred.id, client_id=cred.client_id
    )

    middleware = MCPAuthMiddleware(app=MagicMock())
    request = _make_request(auth_header=f"Bearer {token}")
    response = await middleware.dispatch(request, _call_next_ok)
    assert response.status_code == 401


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
