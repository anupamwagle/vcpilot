"""Tests for app/mcp/auth.py and app/models/mcp.py."""
import pytest


# --- app/models/mcp.py ---

def test_generate_client_id_format():
    from app.models.mcp import generate_client_id
    cid = generate_client_id()
    assert cid.startswith("vcpilot_")
    assert len(cid) == len("vcpilot_") + 20


def test_generate_client_secret_format():
    from app.models.mcp import generate_client_secret
    sec = generate_client_secret()
    assert sec.startswith("vcp_sk_")
    assert len(sec) > 20


def test_hash_secret_returns_string_with_colon():
    from app.models.mcp import hash_secret
    hashed = hash_secret("mysecret")
    assert ":" in hashed
    assert len(hashed) > 32


def test_hash_secret_is_different_each_time():
    from app.models.mcp import hash_secret
    h1 = hash_secret("same_password")
    h2 = hash_secret("same_password")
    # Salt is random so hashes differ
    assert h1 != h2


def test_mcp_all_scopes_list():
    from app.models.mcp import MCP_ALL_SCOPES
    assert "trading:read" in MCP_ALL_SCOPES
    assert "signals:read" in MCP_ALL_SCOPES
    assert "market:read" in MCP_ALL_SCOPES


def test_scope_descriptions_match_scopes():
    from app.models.mcp import MCP_ALL_SCOPES, SCOPE_DESCRIPTIONS
    for scope in MCP_ALL_SCOPES:
        assert scope in SCOPE_DESCRIPTIONS


# --- app/mcp/auth.py ---

def test_get_mcp_context_returns_none_by_default():
    from app.mcp.auth import get_mcp_context, clear_mcp_context
    clear_mcp_context()
    assert get_mcp_context() is None


def test_set_and_get_mcp_context():
    from app.mcp.auth import MCPContext, set_mcp_context, get_mcp_context, clear_mcp_context
    ctx = MCPContext(org_id=1, scopes=["trading:read"], credential_id=1, client_id="test")
    set_mcp_context(ctx)
    try:
        result = get_mcp_context()
        assert result is not None
        assert result.org_id == 1
        assert "trading:read" in result.scopes
    finally:
        clear_mcp_context()


def test_clear_mcp_context():
    from app.mcp.auth import MCPContext, set_mcp_context, get_mcp_context, clear_mcp_context
    ctx = MCPContext(org_id=5, scopes=["market:read"], credential_id=2, client_id="c2")
    set_mcp_context(ctx)
    clear_mcp_context()
    assert get_mcp_context() is None


def test_has_scope_returns_false_when_no_context():
    from app.mcp.auth import has_scope, clear_mcp_context
    clear_mcp_context()
    assert has_scope("trading:read") is False


def test_has_scope_returns_true_when_present():
    from app.mcp.auth import MCPContext, set_mcp_context, has_scope, clear_mcp_context
    ctx = MCPContext(org_id=1, scopes=["trading:read", "signals:read"], credential_id=1, client_id="x")
    set_mcp_context(ctx)
    try:
        assert has_scope("trading:read") is True
        assert has_scope("rules:write") is False
    finally:
        clear_mcp_context()


def test_assert_scope_raises_when_missing():
    from app.mcp.auth import MCPContext, set_mcp_context, assert_scope, clear_mcp_context
    ctx = MCPContext(org_id=1, scopes=["trading:read"], credential_id=1, client_id="x")
    set_mcp_context(ctx)
    try:
        with pytest.raises(PermissionError, match="rules:write"):
            assert_scope("rules:write")
    finally:
        clear_mcp_context()


def test_assert_scope_passes_when_present():
    from app.mcp.auth import MCPContext, set_mcp_context, assert_scope, clear_mcp_context
    ctx = MCPContext(org_id=1, scopes=["trading:read", "market:read"], credential_id=1, client_id="x")
    set_mcp_context(ctx)
    try:
        assert_scope("trading:read")  # Should not raise
    finally:
        clear_mcp_context()


def test_create_access_token_returns_string():
    from app.mcp.auth import create_access_token
    token = create_access_token(org_id=1, scopes=["trading:read"], credential_id=1, client_id="test")
    assert isinstance(token, str)
    assert len(token) > 50


def test_decode_access_token_roundtrip():
    from app.mcp.auth import create_access_token, decode_access_token
    token = create_access_token(org_id=3, scopes=["signals:read", "market:read"], credential_id=2, client_id="cli_abc")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["org_id"] == 3
    assert "signals:read" in payload["scopes"]
    assert payload["type"] == "mcp_access"


def test_decode_access_token_returns_none_for_invalid():
    from app.mcp.auth import decode_access_token
    result = decode_access_token("notavalidjwt.atall.nope")
    assert result is None


def test_decode_access_token_returns_none_for_wrong_type():
    """Tokens not of type 'mcp_access' are rejected."""
    import jwt
    import os
    secret = os.getenv("APP_SECRET_KEY", "changeme-secret")
    from datetime import datetime, timedelta
    payload = {"sub": "x", "org_id": 1, "scopes": [], "credential_id": 1,
               "iat": datetime.utcnow(), "exp": datetime.utcnow() + timedelta(hours=1),
               "type": "other_type"}
    token = jwt.encode(payload, secret, algorithm="HS256")
    result = __import__("app.mcp.auth", fromlist=["decode_access_token"]).decode_access_token(token)
    assert result is None


# --- MCPCredential model ---

def test_mcp_credential_model_exists():
    from app.models.mcp import MCPCredential
    assert MCPCredential is not None


def test_mcp_credential_can_be_instantiated(db_session, org_and_account):
    from app.models.mcp import MCPCredential, generate_client_id, generate_client_secret, hash_secret
    org, _ = org_and_account
    secret = generate_client_secret()
    from datetime import datetime, timedelta
    client_id = generate_client_id()
    cred = MCPCredential(
        organization_id=org.id,
        name="Test Cred",
        client_id=client_id,
        client_secret_hash=hash_secret(secret),
        client_secret_preview=secret[:8] + "...",
        scopes=["trading:read"],
        expires_at=datetime.utcnow() + timedelta(days=365),
        is_active=True,
    )
    db_session.add(cred)
    db_session.commit()
    assert cred.id is not None
    assert cred.client_id.startswith("vcpilot_")
