"""
MCP Credentials — per-org OAuth 2.0 client_credentials for MCP server access.

Each organisation can have multiple named credentials (e.g. one per tool/platform).
The client_secret is shown once at creation and stored hashed (PBKDF2-SHA256).
Credentials expire after 12 months by default; super admin can revoke at any time.
"""
import uuid
import hashlib
import os
from datetime import datetime, timedelta
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, JSON, Text
)
from sqlalchemy.orm import relationship
from app.database import Base

# Default validity window (days)
MCP_CREDENTIAL_VALIDITY_DAYS = 365

# All recognised scopes
MCP_ALL_SCOPES = [
    "trading:read",    # view positions, portfolio stats
    "trading:write",   # place/close orders, pause/resume trading
    "signals:read",    # view signals
    "signals:write",   # skip/unskip signals, run screener
    "watchlist:read",  # view watchlist
    "watchlist:write", # add/remove watchlist items
    "rules:read",      # view AstraTrade rules
    "rules:write",     # update rule thresholds / toggles
    "config:read",     # view system config (non-secret keys)
    "market:read",     # market regime, price data
]

SCOPE_DESCRIPTIONS = {
    "trading:read":    "View open positions, closed trades, and portfolio statistics",
    "trading:write":   "Place bracket orders, close positions, pause/resume automated trading",
    "signals:read":    "View generated AstraTrade signals and their status",
    "signals:write":   "Skip/unskip signals, trigger the screener manually",
    "watchlist:read":  "View the organisation's watchlist and labels",
    "watchlist:write": "Add tickers to the watchlist, remove items",
    "rules:read":      "View AstraTrade rule configurations and thresholds",
    "rules:write":     "Enable/disable rules, adjust thresholds",
    "config:read":     "Read non-secret system configuration values",
    "market:read":     "Read market regime (BULL/CAUTION/BEAR) and price data",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_client_id() -> str:
    """Generate a prefixed, URL-safe client ID."""
    return f"vcpilot_{uuid.uuid4().hex[:20]}"


def generate_client_secret() -> str:
    """Generate a high-entropy client secret."""
    return f"vcp_sk_{os.urandom(32).hex()}"


def hash_secret(secret: str) -> str:
    """Hash a client secret with PBKDF2-SHA256 + random salt."""
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}:{h.hex()}"


def verify_secret(secret: str, hashed: str) -> bool:
    """Verify a plain secret against its stored hash."""
    if not hashed or ":" not in hashed:
        return False
    try:
        salt_hex, hash_hex = hashed.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        stored_h = bytes.fromhex(hash_hex)
        check_h = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000)
        return check_h == stored_h
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class MCPCredential(Base):
    """
    OAuth 2.0 client_credentials credential scoped to one organisation.

    Workflow:
    1. Super admin clicks "Generate Credentials" for an org.
    2. A client_id + client_secret pair is created. The plain secret is returned
       once and shown in the UI — it is never stored again.
    3. The org administrator configures their MCP client (e.g. Claude Desktop)
       with the client_id + client_secret and the token URL.
    4. The MCP client calls POST /mcp/oauth/token with client_credentials grant
       to receive a short-lived JWT access token.
    5. All MCP requests carry that token as a Bearer header.
    """
    __tablename__ = "mcp_credentials"

    id               = Column(Integer, primary_key=True)
    organization_id  = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name             = Column(String(128), nullable=False, default="Default")
    client_id        = Column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash    = Column(String(256), nullable=False)
    # Last 8 chars shown in UI so the admin can identify which secret was issued
    client_secret_preview = Column(String(16), nullable=False)
    scopes           = Column(JSON, nullable=False, default=list)
    expires_at       = Column(DateTime, nullable=False)
    is_active        = Column(Boolean, default=True, nullable=False)
    created_at       = Column(DateTime, default=datetime.utcnow)
    created_by       = Column(String(128), nullable=True)  # super admin email
    last_used_at     = Column(DateTime, nullable=True)
    revoked_at       = Column(DateTime, nullable=True)
    revoked_by       = Column(String(128), nullable=True)
    notes            = Column(Text, nullable=True)  # optional description / usage note

    organization = relationship("Organization")

    # ------------------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.is_active and not self.is_expired

    @property
    def days_remaining(self) -> int:
        delta = self.expires_at - datetime.utcnow()
        return max(0, delta.days)

    @property
    def status_label(self) -> str:
        if not self.is_active:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.days_remaining <= 14:
            return "expiring_soon"
        return "active"

    def __repr__(self):
        return f"<MCPCredential {self.client_id} org={self.organization_id}>"
