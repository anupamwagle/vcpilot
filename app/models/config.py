"""
SystemConfig and RuleConfig models.

SystemConfig: key-value store for global operational parameters.
RuleConfig:   each Minervini rule as a row — enabled/disabled, threshold values,
              tier-level applicability. Admin can toggle any rule globally or per tier.
"""
import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    Enum, ForeignKey, Numeric, Text, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base


class ConfigValueType(str, enum.Enum):
    STRING  = "STRING"
    INTEGER = "INTEGER"
    FLOAT   = "FLOAT"
    BOOLEAN = "BOOLEAN"
    JSON    = "JSON"


class RuleCategory(str, enum.Enum):
    TREND_TEMPLATE  = "TREND_TEMPLATE"    # 8 Minervini trend conditions
    FUNDAMENTAL     = "FUNDAMENTAL"       # EPS, sales, ROE, margins (equity only)
    VCP             = "VCP"               # Volatility Contraction Pattern
    MARKET_REGIME   = "MARKET_REGIME"     # Market direction / health filter
    ENTRY           = "ENTRY"             # Entry conditions
    EXIT_DEFENSIVE  = "EXIT_DEFENSIVE"    # Stop loss, time stop
    EXIT_OFFENSIVE  = "EXIT_OFFENSIVE"    # Profit targets, climax top
    POSITION_SIZING = "POSITION_SIZING"   # Risk %, pyramid rules
    PORTFOLIO       = "PORTFOLIO"         # Max positions, heat limits
    EARNINGS        = "EARNINGS"          # Earnings avoidance rules
    CRYPTO          = "CRYPTO"            # Crypto-specific rules (market cap, dominance, etc.)


class SystemConfig(Base):
    """
    Global operational settings editable from the admin UI.
    All values stored as strings; cast using value_type on read.
    """
    __tablename__ = "system_configs"

    id              = Column(Integer, primary_key=True)
    key             = Column(String(128), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    value           = Column(Text, nullable=False)
    value_type      = Column(Enum(ConfigValueType), default=ConfigValueType.STRING)
    label           = Column(String(256))                  # Human-readable name for UI
    description     = Column(Text)                         # Shown as tooltip in UI
    group           = Column(String(64), default="general")# Groups settings in UI
    is_secret       = Column(Boolean, default=False)       # Hides value in UI
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by      = Column(String(64), default="system")

    # Relationships
    organization    = relationship("Organization")

    __table_args__ = (
        UniqueConstraint("key", "organization_id", name="uq_system_config_key_org"),
    )


    def typed_value(self):
        """Return value cast to its declared type."""
        if self.value_type == ConfigValueType.INTEGER:
            return int(self.value)
        elif self.value_type == ConfigValueType.FLOAT:
            return float(self.value)
        elif self.value_type == ConfigValueType.BOOLEAN:
            return self.value.lower() in ("true", "1", "yes")
        elif self.value_type == ConfigValueType.JSON:
            import json
            return json.loads(self.value)
        return self.value

    def __repr__(self):
        return f"<SystemConfig {self.key}={self.value}>"


class RuleConfig(Base):
    """
    One row per Minervini rule. Each rule can be:
    - Enabled/disabled globally
    - Overridden per account tier (stored in tier_overrides JSON)
    - Have its threshold(s) adjusted without code changes

    Structure of tier_overrides (JSON):
    {
        "STARTER":  {"enabled": true, "threshold": 70},
        "STANDARD": {"enabled": true, "threshold": 70},
        "ADVANCED": {"enabled": true, "threshold": 65},
        "ADMIN":    {"enabled": true, "threshold": 60}
    }
    """
    __tablename__ = "rule_configs"
    __table_args__ = (
        UniqueConstraint('rule_id', 'organization_id', name='uq_rule_config_rule_org'),
    )

    id              = Column(Integer, primary_key=True)
    rule_id         = Column(String(64), nullable=False, index=True)
                                                       # e.g. "trend_price_above_200ma"
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    category        = Column(Enum(RuleCategory), nullable=False)
    label           = Column(String(256), nullable=False)  # Display name
    description     = Column(Text)                        # Full rule explanation
    minervini_ref   = Column(Text)                        # Direct quote / book reference

    # Global toggle — overrides everything when False
    enabled_globally= Column(Boolean, default=True, nullable=False)

    # Default threshold (numeric rules). Non-numeric rules leave this null.
    threshold       = Column(Numeric(20, 4), nullable=True)
    threshold_label = Column(String(128), nullable=True)   # e.g. "Min RS percentile"
    threshold_min   = Column(Numeric(20, 4), nullable=True)
    threshold_max   = Column(Numeric(20, 4), nullable=True)

    # Tier-level overrides (JSON object, see docstring above)
    tier_overrides  = Column(JSON, default=dict)

    # Asset type applicability — which markets this rule applies to
    # "BOTH"   = equities + crypto (default — backward compatible)
    # "EQUITY" = ASX, NYSE, NASDAQ only (not evaluated for crypto assets)
    # "CRYPTO" = crypto exchanges only (not evaluated for equities)
    asset_types     = Column(String(16), nullable=False, default="BOTH")

    # Metadata
    is_mandatory    = Column(Boolean, default=False)      # Cannot be disabled (e.g. stop loss)
    sort_order      = Column(Integer, default=0)          # Display order in UI
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by      = Column(String(64), default="system")

    # Relationships
    organization    = relationship("Organization")

    def is_enabled_for_tier(self, tier_level: str) -> bool:
        """Check if rule is enabled for a given tier level."""
        if not self.enabled_globally:
            return False
        overrides = self.tier_overrides or {}
        tier_override = overrides.get(tier_level, {})
        return tier_override.get("enabled", True)

    def threshold_for_tier(self, tier_level: str):
        """Get threshold value for a given tier (falls back to global threshold)."""
        overrides = self.tier_overrides or {}
        tier_override = overrides.get(tier_level, {})
        return tier_override.get("threshold", float(self.threshold) if self.threshold else None)

    def __repr__(self):
        return f"<RuleConfig {self.rule_id} enabled={self.enabled_globally}>"
