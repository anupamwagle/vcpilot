"""Restricted web access while a production database migration is verified."""
import os

from starlette.responses import JSONResponse


def recovery_enabled():
    return os.environ.get("ASTRA_RECOVERY_MODE", "false").lower() == "true"


class RecoveryMiddleware:
    """Keep broker/agent and mutation endpoints unavailable during recovery.

    Deployment must also omit workers/gateways and deny application egress.
    Password sign-in remains available; OTP delivery is intentionally disabled.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and recovery_enabled():
            path = scope.get("path", "").rstrip("/") or "/"
            blocked_prefixes = ("/mcp", "/webhook", "/action", "/authorize")
            blocked = any(path == p or path.startswith(p + "/") for p in blocked_prefixes)
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                blocked = blocked or not (scope["method"] == "POST" and path == "/login")
            # Read-only MCP remains protected by credential/scope checks in the
            # MCP middleware. No other recovery mutations are permitted.
            if (os.environ.get("ASTRA_RECOVERY_MCP_READ_ONLY", "false").lower() == "true"
                    and path in {"/mcp", "/mcp/oauth/token"}):
                blocked = False
            if blocked:
                response = JSONResponse(
                    {"detail": "Migration review mode: trading, changes, email and agent actions are paused."},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
