"""
AuditLog — Immutable record of every system action, config change, and trade event.
Never update or delete rows in this table.
"""
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, JSON
from app.database import Base


class AuditAction(str, enum.Enum):
    # Config changes
    CONFIG_CHANGED      = "CONFIG_CHANGED"
    RULE_TOGGLED        = "RULE_TOGGLED"
    RULE_THRESHOLD_SET  = "RULE_THRESHOLD_SET"
    TIER_OVERRIDE_SET   = "TIER_OVERRIDE_SET"

    # Trading events
    SIGNAL_GENERATED    = "SIGNAL_GENERATED"
    ORDER_SUBMITTED     = "ORDER_SUBMITTED"
    ORDER_FILLED        = "ORDER_FILLED"
    ORDER_CANCELLED     = "ORDER_CANCELLED"
    ORDER_REJECTED      = "ORDER_REJECTED"
    POSITION_OPENED     = "POSITION_OPENED"
    POSITION_UPDATED    = "POSITION_UPDATED"  # Stop moved, pyramid added
    POSITION_CLOSED     = "POSITION_CLOSED"
    STOP_UPDATED        = "STOP_UPDATED"

    # System events
    SCREENER_RUN        = "SCREENER_RUN"
    MARKET_REGIME_CHANGE= "MARKET_REGIME_CHANGE"
    TRADING_PAUSED      = "TRADING_PAUSED"
    TRADING_RESUMED     = "TRADING_RESUMED"
    SYSTEM_STARTED      = "SYSTEM_STARTED"
    HEALTH_CHECK        = "HEALTH_CHECK"

    # Agent / WhatsApp events
    AGENT_COMMAND       = "AGENT_COMMAND"
    MANUAL_OVERRIDE     = "MANUAL_OVERRIDE"


class AuditLog(Base):
    """
    Append-only audit log. Every significant system event lands here.
    This is your trade journal, compliance record, and debugging tool.
    """
    __tablename__ = "audit_logs"

    id          = Column(Integer, primary_key=True)
    action      = Column(Enum(AuditAction), nullable=False, index=True)
    actor       = Column(String(64), default="system")   # system | admin | agent | celery
    entity_type = Column(String(64), nullable=True)       # e.g. "RuleConfig", "Position"
    entity_id   = Column(String(64), nullable=True)       # e.g. rule_id or position id
    ticker      = Column(String(16), nullable=True, index=True)

    # Before/after for config changes
    before_value= Column(Text, nullable=True)
    after_value = Column(Text, nullable=True)

    message     = Column(Text, nullable=True)    # Human-readable description
    detail      = Column(JSON, nullable=True)    # Full structured context
    ip_address  = Column(String(45), nullable=True)

    created_at  = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<AuditLog {self.action} by={self.actor} at={self.created_at}>"
