"""
Signal and Watchlist models.
A Signal is a stock that passed all AstraTrade screener criteria on a given date.
"""
import enum
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Enum, Numeric, Text, JSON, ForeignKey
)
from sqlalchemy.orm import relationship
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
    Works for ASX equities, US equities, and crypto assets.
    """
    __tablename__ = "signals"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(32), nullable=False, index=True)
                                            # yfinance canonical: "BHP.AX", "AAPL", "BTC-USD"
    exchange_key    = Column(String(32), nullable=False, default="ASX", index=True)
                                            # "ASX", "NYSE", "NASDAQ", "CRYPTO_INDEPENDENTRESERVE", …
    asset_type      = Column(String(16), nullable=False, default="EQUITY")
                                            # "EQUITY" | "CRYPTO"
    currency        = Column(String(8),  nullable=False, default="AUD")
                                            # Native price currency: "AUD", "USD"
    signal_date     = Column(Date, nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    status          = Column(Enum(SignalStatus), default=SignalStatus.PENDING, nullable=False)

    # Relationships
    organization    = relationship("Organization")


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

    # Per-signal rule overrides set by the user via the dashboard
    # e.g. {"vcp_breakout_volume": false}  — disables volume check for this signal only
    # Mandatory rules and globally-disabled rules cannot be overridden.
    rule_overrides  = Column(JSON, default=dict)

    # Risk calculations (all AUD-equivalent for cross-market comparisons)
    suggested_size_shares = Column(Integer, nullable=True)
    suggested_size_aud    = Column(Numeric(14, 2), nullable=True)   # AUD equivalent
    suggested_size_local  = Column(Numeric(14, 2), nullable=True)   # Native currency amount
    risk_per_trade_aud    = Column(Numeric(12, 2), nullable=True)
    fx_rate_aud           = Column(Numeric(10, 6), nullable=True)   # AUD/native rate at signal time

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


# Preset colour palette for watchlist labels (Tailwind-compatible hex values)
LABEL_COLOUR_PALETTE = [
    "#f59e0b",  # amber    — default Favourites
    "#3b82f6",  # blue
    "#10b981",  # emerald
    "#8b5cf6",  # violet
    "#ef4444",  # red
    "#ec4899",  # pink
    "#06b6d4",  # cyan
    "#f97316",  # orange
]


class WatchlistLabel(Base):
    """
    User-defined label/tag for grouping watchlist items.
    Each org gets a default 'Favourites' label (amber) on first seed.
    """
    __tablename__ = "watchlist_labels"

    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name            = Column(String(64), nullable=False)
    color           = Column(String(16), default="#f59e0b")  # hex colour
    is_default      = Column(Boolean, default=False)          # 'Favourites' flag
    sort_order      = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow)

    # Relationships
    organization    = relationship("Organization")
    items           = relationship("Watchlist", back_populates="label", lazy="dynamic")

    def __repr__(self):
        return f"<WatchlistLabel {self.name} org={self.organization_id}>"


class Watchlist(Base):
    """
    Stocks that pass partial AstraTrade criteria — stage 2, trend template met —
    but not yet a full signal (e.g. VCP still forming, volume not confirmed).
    Admin can add/remove manually via UI for any supported exchange.

    Multi-market: exchange_key and asset_type allow US equities and crypto alongside ASX.
    On-demand data fetch is triggered when a non-ASX200 stock is added.
    """
    __tablename__ = "watchlist"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(32), nullable=False, index=True)
                                            # yfinance canonical: "BHP.AX", "AAPL", "BTC-USD"
    exchange_key    = Column(String(32), nullable=False, default="ASX", index=True)
                                            # Which exchange this stock trades on
    asset_type      = Column(String(16), nullable=False, default="EQUITY")
                                            # "EQUITY" | "CRYPTO"
    currency        = Column(String(8),  nullable=False, default="AUD")
                                            # Native price currency
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    added_date      = Column(Date, default=datetime.utcnow)

    # Relationships
    organization    = relationship("Organization")
    label           = relationship("WatchlistLabel", back_populates="items", foreign_keys="Watchlist.label_id")

    status      = Column(Enum(WatchlistStatus), default=WatchlistStatus.WATCHING)
    added_by    = Column(String(64), default="screener")  # screener | admin | agent
    notes       = Column(Text, nullable=True)
    label_id    = Column(Integer, ForeignKey("watchlist_labels.id", ondelete="SET NULL"), nullable=True)
    rule_results= Column(JSON, default=dict)
    removed_date= Column(Date, nullable=True)

    # ── Precomputed VCP geometry (performance) ────────────────────────────────
    # Filled by the screener (and lazily by the dashboard) so the watchlist page
    # reads these columns instead of re-running detect_vcp on every load. Freshness
    # is keyed by `vcp_computed_date` = the date of the last price bar used; if it
    # matches the latest bar, the cached geometry is reused, otherwise recomputed.
    pivot_price       = Column(Numeric(12, 4), nullable=True)  # VCP buy point (or 52w-high fallback)
    stop_price        = Column(Numeric(12, 4), nullable=True)  # initial stop
    target_price      = Column(Numeric(12, 4), nullable=True)  # pivot * 1.20
    vcp_contractions  = Column(Integer, nullable=True)
    vcp_base_weeks    = Column(Integer, nullable=True)
    vcp_computed_date = Column(Date, nullable=True)            # date of last bar used

    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Watchlist {self.ticker} {self.status}>"
