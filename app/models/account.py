"""
Account and Tier models.
Tier-based rule overrides: STARTER < STANDARD < ADVANCED < ADMIN
"""
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from app.database import Base


class TierLevel(str, enum.Enum):
    STARTER  = "STARTER"    # ASX200 only, max 3 positions, conservative
    STANDARD = "STANDARD"   # ASX300, max 5 positions, standard rules
    ADVANCED = "ADVANCED"   # Full ASX, max 10 positions, all features
    ADMIN    = "ADMIN"      # Full access, can override everything


class AccountTier(Base):
    """
    Defines capabilities and rule overrides per tier.
    Admin sets these; individual accounts are assigned a tier.
    """
    __tablename__ = "account_tiers"

    id                    = Column(Integer, primary_key=True)
    level                 = Column(Enum(TierLevel), unique=True, nullable=False)
    label                 = Column(String(64), nullable=False)
    universe              = Column(String(32), default="ASX200")   # ASX200 | ASX300 | ALLASX
    max_positions         = Column(Integer, default=3)
    max_risk_pct_per_trade= Column(Numeric(5, 2), default=1.0)     # % of capital at risk per trade
    max_portfolio_heat_pct= Column(Numeric(5, 2), default=10.0)    # % total portfolio at risk
    allow_pyramid         = Column(Boolean, default=False)
    allow_short           = Column(Boolean, default=False)          # future use
    allow_manual_override = Column(Boolean, default=False)
    created_at            = Column(DateTime, default=datetime.utcnow)
    updated_at            = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    accounts = relationship("Account", back_populates="tier")

    def __repr__(self):
        return f"<AccountTier {self.level}>"


class Account(Base):
    """
    Represents a trading account (maps to one IBKR account).
    A single account for now; multi-account is a SaaS tier feature.
    """
    __tablename__ = "accounts"

    id              = Column(Integer, primary_key=True)
    name            = Column(String(128), nullable=False, default="Primary")
    ibkr_account_id = Column(String(32), nullable=True)           # e.g. DU1234567
    tier_id         = Column(Integer, ForeignKey("account_tiers.id"), nullable=False)
    is_active       = Column(Boolean, default=True)
    is_paper        = Column(Boolean, default=True)                # Always paper until explicitly set live
    capital_aud     = Column(Numeric(12, 2), default=0.00)         # Current account capital
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tier      = relationship("AccountTier", back_populates="accounts")
    positions = relationship("Position", back_populates="account")
    trades    = relationship("Trade", back_populates="account")

    def __repr__(self):
        return f"<Account {self.name} [{self.ibkr_account_id}]>"
