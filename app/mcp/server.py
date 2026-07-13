"""
AstraTrade MCP Server

Creates a FastMCP server and wraps it with an auth middleware so every
request to /mcp requires a valid Bearer JWT from POST /mcp/oauth/token.

The org context (org_id, scopes) is injected into the async ContextVar
before FastMCP dispatches to the tool functions, making org isolation
seamless without passing org_id through every tool signature.

Architecture:
  FastAPI app
    ├─ POST /mcp/oauth/token  (handled by web/main.py router)
    └─ /mcp                   (mounted Starlette app — this module)
          ├─ AuthMiddleware    → validates Bearer JWT → sets ContextVar
          └─ FastMCP SSE app  → GET /sse, POST /messages
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from loguru import logger
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from app.mcp.auth import (
    MCPContext,
    decode_access_token,
    set_mcp_context,
    clear_mcp_context,
)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class MCPAuthMiddleware:
    """
    Validates Bearer JWT on every request to the MCP sub-app.
    On success, sets the org context ContextVar for the duration of the request.

    Pure-ASGI middleware (not BaseHTTPMiddleware) — BaseHTTPMiddleware buffers
    the whole response before forwarding it to the client, which is known to
    interfere with long-lived streaming responses (GET /sse). This
    implementation passes the original scope/receive/send straight through to
    the wrapped app once auth succeeds, so the SSE stream is untouched.
    """

    # Paths that don't need a token (none under /mcp, but left as extension point)
    _PUBLIC_PATHS: set = set()

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in self._PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        auth_header = headers.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="AstraTrade MCP"'},
            )
            return await response(scope, receive, send)

        token = auth_header[len("Bearer "):]
        payload = decode_access_token(token)
        if payload is None:
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Invalid or expired token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="AstraTrade MCP"'},
            )
            return await response(scope, receive, send)

        # Validate credential is still active in DB (catches revocations)
        credential_id = payload.get("credential_id")
        org_id        = payload.get("org_id")
        if credential_id:
            try:
                from app.database import get_db
                from app.models.mcp import MCPCredential
                with get_db() as db:
                    cred = db.query(MCPCredential).filter(
                        MCPCredential.id == credential_id,
                        MCPCredential.organization_id == org_id,
                    ).first()
                    if not cred or not cred.is_valid:
                        response = JSONResponse(
                            {"error": "unauthorized", "detail": "Credential has been revoked or expired"},
                            status_code=401,
                        )
                        return await response(scope, receive, send)
                    # Update last_used_at (best-effort)
                    try:
                        from datetime import datetime
                        cred.last_used_at = datetime.utcnow()
                        db.commit()
                    except Exception as e:
                        logger.debug(f"MCPAuthMiddleware: last_used_at update failed (non-fatal): {e}")
            except Exception as e:
                logger.warning(f"MCPAuthMiddleware DB check failed (non-fatal): {e}")

        ctx = MCPContext(
            org_id=org_id,
            scopes=payload.get("scopes", []),
            credential_id=credential_id,
            client_id=payload.get("sub", ""),
        )
        set_mcp_context(ctx)

        try:
            await self.app(scope, receive, send)
        finally:
            clear_mcp_context()


# ---------------------------------------------------------------------------
# FastMCP server setup
# ---------------------------------------------------------------------------

def _build_mcp_server():
    """Construct and register all AstraTrade MCP tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise ImportError(
            "The 'mcp' package is not installed. "
            "Add 'mcp[server]>=1.0' to requirements.txt and rebuild the Docker image."
        )

    mcp = FastMCP(
        name="AstraTrade",
        instructions=(
            "AstraTrade is an automated stock trading system using the VCP / algorithmic trading methodology. Use these tools to read "
            "market signals, manage watchlists, monitor positions, and execute or close "
            "trades on behalf of the authenticated organisation. "
            "All actions are org-scoped — you can only see and affect the organisation "
            "associated with the OAuth credential used to authenticate this session."
        ),
    )

    # Import tool functions
    from app.mcp.tools import (
        # Market
        get_market_regime,
        evaluate_market_regime,
        # Signals
        get_signals,
        run_screener,
        skip_signal,
        unskip_signal,
        # Watchlist
        get_watchlist,
        add_to_watchlist,
        remove_from_watchlist,
        # Positions / Trading
        get_positions,
        get_portfolio_stats,
        place_order,
        close_position,
        pause_trading,
        resume_trading,
        # Rules / Config
        get_rules,
        update_rule,
        get_config,
    )

    # Register all tools
    for fn in [
        get_market_regime,
        evaluate_market_regime,
        get_signals,
        run_screener,
        skip_signal,
        unskip_signal,
        get_watchlist,
        add_to_watchlist,
        remove_from_watchlist,
        get_positions,
        get_portfolio_stats,
        place_order,
        close_position,
        pause_trading,
        resume_trading,
        get_rules,
        update_rule,
        get_config,
    ]:
        mcp.tool()(fn)

    return mcp


def create_mcp_app() -> Starlette:
    """
    Build the full MCP ASGI app (auth middleware wrapping the FastMCP SSE app).
    Mount this at /mcp in the FastAPI application.
    """
    mcp_server = _build_mcp_server()

    # sse_app() returns an ASGI app implementing GET /sse + POST /messages
    sse_asgi = mcp_server.sse_app()

    app = Starlette(
        middleware=[Middleware(MCPAuthMiddleware)],
        # Route everything to the FastMCP SSE handler
        routes=[],
    )
    # Wrap: apply middleware then delegate to FastMCP
    from starlette.routing import Mount
    app = Starlette(
        middleware=[Middleware(MCPAuthMiddleware)],
        routes=[Mount("/", app=sse_asgi)],
    )
    return app
