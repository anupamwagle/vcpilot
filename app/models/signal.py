"""
Signal and Watchlist models.
A Signal is a stock that passed all Minervini screener criteria on a given date.
"""
import enum
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Enum, Numeric, Text, JSON, ForeignKey
)
from app.database import Base


class SignalStatus(str, enum.Enum):
    PENDING   = "PENDING"    # Generated, awaiting intraday entry trigger
    TRIGGERED = "TRIGGERED"  # Entry condition met, order placed
    EXPIRED   = "EXPIRED"    # Not triggered within the session
    SKIPPED   = "SKIPPED"    # Manually skipped via WhatsApp command
    CANCELLED = "CANCELLED"  # Rule or regime filter cancelled after generation


class Signal(Base):
    """
    One row per stock per screener run that passes all enabled rules.
    Captures the full context at signal generation time for audit purposes.
    """
    __tablename__ = "signals"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(16), nullable=False, index=True)
    signal_date     = Column(Date, nullable=False, index=True)
    status          = Column(Enum(SignalStatus), default=SignalStatus.PENDING, nullable=False)

    # Price context at signal generation
    close_price     = Column(Numeric(12, 4))
    pivot_price     = Column(Numeric(12, 4))           # VCP pivot buy point
    stop_price      = Column(Numeric(12, 4))           # Initial stop loss price
    target_price_1  = Column(Numeric(12, 4))           # First profit target (20-25%)
    target_price_2  = Column(Numeric(12, 4))           # Second profit target

    # Scores at signal time
    rs_rating       = Column(Numeric(6, 2))
    trend_score     = Column(Integer)                  # 0-8 trend template criteria met
    fundamental_score = Column(Integer)               # 0-10 fundamental criteria met

    # Rule results (JSON snapshot of which rules passed/failed)
    rule_results    = Column(JSON, default=dict)
    # e.g. {"trend_price_above_200ma": true, "fundamental_eps_growth": true, ...}

    # Risk calculations
    suggested_size_shares = Column(Integer, nullable=True)   # Position size in shares
    suggested_size_aud    = Column(Numeric(12, 2), nullable=True)
    risk_per_trade_aud    = Column(Numeric(10, 2), nullable=True)

    # VCP context
    vcp_contractions = Column(Integer, nullable=True)  # Number of contractions detected
    vcp_weeks        = Column(Integer, nullable=True)  # Base length in weeks

    notes           = Column(Text, nullable=True)       # Analyst / agent notes
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Signal {self.ticker} {self.signal_date} {self.status}>"


class WatchlistStatus(str, enum.Enum):
    WATCHING  = "WATCHING"   # In watchlist, not yet a signal
    SIGNALLED = "SIGNALLED"  # Graduated to a signal
    REMOVED   = "REMOVED"    # Removed from watchlist


class Watchlist(Base):
    """
    Stocks that pass partial Minervini criteria — stage 2, trend template met —
    but not yet a full signal (e.g. VCP still forming, volume not confirmed).
    Admin can add/remove manually via UI.
    """
    __tablename__ = "watchlist"

    id          = Column(Integer, primary_key=True)
    ticker      = Column(String(16), nullable=False, index=True)
    added_date  = Column(Date, default=datetime.utcnow)
    status      = Column(Enum(WatchlistStatus), default=WatchlistStatus.WATCHING)
    added_by    = Column(String(64), default="screener")  # screener | admin | agent
    notes       = Column(Text, nullable=True)
    rule_results= Column(JSON, default=dict)
    removed_date= Column(Date, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Watchlist {self.ticker} {self.status}>"
