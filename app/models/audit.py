"""
AuditLog — Immutable record of every system action, config change, and trade event.
Never update or delete rows in this table.
"""
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
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
    SCREENER_TICKER     = "SCREENER_TICKER"   # Per-ticker result row (verbose mode)
    MARKET_REGIME_CHANGE= "MARKET_REGIME_CHANGE"
    TRADING_PAUSED      = "TRADING_PAUSED"
    TRADING_RESUMED     = "TRADING_RESUMED"
    SYSTEM_STARTED      = "SYSTEM_STARTED"
    HEALTH_CHECK        = "HEALTH_CHECK"

    # Agent / Telegram events
    AGENT_COMMAND       = "AGENT_COMMAND"
    MANUAL_OVERRIDE     = "MANUAL_OVERRIDE"

    # User activity tracking (captured by the dashboard middleware)
    FEATURE_ACCESS      = "FEATURE_ACCESS"   # User opened a feature/page (GET)
    FEATURE_ACTION      = "FEATURE_ACTION"   # User submitted a change (POST/PUT/PATCH/DELETE)

    # Task execution tracking
    TASK_RUN            = "TASK_RUN"     # Periodic task fired + summary
    TASK_ERROR          = "TASK_ERROR"   # Error inside a task

    # Auth events (B13 — added 13 Jul 2026). Not yet used by any call site:
    # the Postgres enum type needs migrate_saas Migration 013 to run in prod
    # first (ALTER TYPE ... ADD VALUE), since these deploy via git-pull +
    # auto-reload with no guaranteed migration-before-code ordering. Login/
    # logout call sites keep writing CONFIG_CHANGED/TASK_ERROR until a
    # follow-up change switches them over post-migration.
    LOGIN                = "LOGIN"          # Successful login
    LOGIN_FAILED         = "LOGIN_FAILED"   # Failed login attempt


class AuditLog(Base):
    """
    Append-only audit log. Every significant system event lands here.
    This is your trade journal, compliance record, and debugging tool.
    """
    __tablename__ = "audit_logs"

    id              = Column(Integer, primary_key=True)
    action          = Column(Enum(AuditAction), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor           = Column(String(128), default="system")  # system | admin | agent | celery | user email
    entity_type     = Column(String(64), nullable=True)       # e.g. "RuleConfig", "Position"
    entity_id       = Column(String(64), nullable=True)       # e.g. rule_id or position id
    ticker          = Column(String(16), nullable=True, index=True)
    feature         = Column(String(64), nullable=True, index=True)  # Friendly feature label (e.g. "Org Config")
    http_method     = Column(String(8),  nullable=True)              # GET / POST / ... for activity rows

    # Relationships
    organization    = relationship("Organization")


    # Before/after for config changes
    before_value= Column(Text, nullable=True)
    after_value = Column(Text, nullable=True)

    message     = Column(Text, nullable=True)    # Human-readable description
    detail      = Column(JSON, nullable=True)    # Full structured context
    ip_address  = Column(String(45), nullable=True)

    created_at  = Column(DateTime, default=datetime.utcnow, index=True)

    @classmethod
    def safe(cls, db, **kwargs):
        """Write an audit log entry, silently ignoring DB errors (e.g. missing column)."""
        try:
            entry = cls(**kwargs)
            db.add(entry)
            db.flush()
        except Exception as e:
            from loguru import logger
            logger.warning(f"AuditLog write failed (non-fatal): {e}")
            try:
                db.rollback()
            except Exception:
                pass

    def __repr__(self):
        return f"<AuditLog {self.action} by={self.actor} at={self.created_at}>"
