"""
MCP OAuth authentication helpers.

- create_access_token / decode_access_token  — JWT lifecycle (1-hour tokens)
- MCPContext / get_mcp_context               — async context var carrying org_id + scopes
- require_scope                              — scope-check decorator for tools
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

# ---------------------------------------------------------------------------
# Context variable (async-safe: one value per coroutine chain)
# ---------------------------------------------------------------------------

@dataclass
class MCPContext:
    org_id: int
    scopes: List[str]
    credential_id: int
    client_id: str

_mcp_ctx: ContextVar[Optional[MCPContext]] = ContextVar("_mcp_ctx", default=None)


def get_mcp_context() -> Optional[MCPContext]:
    return _mcp_ctx.get()


def set_mcp_context(ctx: MCPContext):
    _mcp_ctx.set(ctx)


def clear_mcp_context():
    _mcp_ctx.set(None)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

_JWT_SECRET    = os.getenv("APP_SECRET_KEY", "changeme-secret")
_JWT_ALGORITHM = "HS256"
_TOKEN_TTL_HOURS = 1


def create_access_token(org_id: int, scopes: List[str], credential_id: int, client_id: str) -> str:
    """Issue a signed JWT carrying org + scope claims."""
    import jwt as _jwt
    payload = {
        "sub":           client_id,
        "org_id":        org_id,
        "scopes":        scopes,
        "credential_id": credential_id,
        "iat":           datetime.utcnow(),
        "exp":           datetime.utcnow() + timedelta(hours=_TOKEN_TTL_HOURS),
        "type":          "mcp_access",
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode + validate a JWT.  Returns the payload dict or None on any error
    (expired, tampered, wrong algorithm, etc.).
    """
    import jwt as _jwt
    try:
        payload = _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        if payload.get("type") != "mcp_access":
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

def has_scope(scope: str) -> bool:
    """Check whether the current MCP context includes the given scope."""
    ctx = get_mcp_context()
    if ctx is None:
        return False
    return scope in ctx.scopes


def assert_scope(scope: str):
    """Raise PermissionError if the current context lacks the required scope."""
    if not has_scope(scope):
        raise PermissionError(f"Token missing required scope: {scope}")
