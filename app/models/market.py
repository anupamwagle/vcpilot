"""
Market data models — Stock universe and daily OHLCV price bars.
PriceBar is a TimescaleDB hypertable (partitioned by date).
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Numeric, BigInteger, Text, UniqueConstraint, Index
)
from app.database import Base


class Stock(Base):
    """
    Master list of ASX-listed stocks in scope for the screener.
    Updated weekly from the configured universe (ASX200 / ASX300 / ALLASX).
    """
    __tablename__ = "stocks"

    id          = Column(Integer, primary_key=True)
    ticker      = Column(String(16), unique=True, nullable=False, index=True)
                                             # e.g. "BHP.AX" (yfinance format)
    asx_code    = Column(String(10), nullable=False)  # e.g. "BHP"
    name        = Column(String(256))
    sector      = Column(String(128))
    industry    = Column(String(128))
    gics_sector = Column(String(128))
    market_cap  = Column(BigInteger, nullable=True)   # AUD cents
    float_shares= Column(BigInteger, nullable=True)
    in_asx200   = Column(Boolean, default=False)
    in_asx300   = Column(Boolean, default=False)
    is_active   = Column(Boolean, default=True)        # False = delisted / excluded
    blacklisted = Column(Boolean, default=False)       # Admin can exclude specific stocks
    blacklist_reason = Column(Text, nullable=True)
    last_price  = Column(Numeric(12, 4), nullable=True)
    last_updated= Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Stock {self.asx_code}>"


class PriceBar(Base):
    """
    Daily OHLCV + derived fields for each stock.
    Designed as a TimescaleDB hypertable on (ticker, date).
    """
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_pricebar_ticker_date"),
        Index("ix_pricebar_ticker_date", "ticker", "date"),
    )

    id          = Column(Integer, primary_key=True)
    ticker      = Column(String(16), nullable=False, index=True)
    date        = Column(Date, nullable=False, index=True)

    # OHLCV
    open        = Column(Numeric(12, 4))
    high        = Column(Numeric(12, 4))
    low         = Column(Numeric(12, 4))
    close       = Column(Numeric(12, 4))
    adj_close   = Column(Numeric(12, 4))
    volume      = Column(BigInteger)

    # Moving averages (computed on ingest)
    ma_10       = Column(Numeric(12, 4), nullable=True)
    ma_21       = Column(Numeric(12, 4), nullable=True)
    ma_50       = Column(Numeric(12, 4), nullable=True)
    ma_150      = Column(Numeric(12, 4), nullable=True)
    ma_200      = Column(Numeric(12, 4), nullable=True)
    ma_200_prev = Column(Numeric(12, 4), nullable=True)  # Prior day 200MA (slope check)

    # Volume metrics
    avg_vol_50  = Column(Numeric(18, 2), nullable=True)  # 50-day avg volume
    vol_ratio   = Column(Numeric(8, 4), nullable=True)   # Today vol / avg_vol_50

    # 52-week range
    high_52w    = Column(Numeric(12, 4), nullable=True)
    low_52w     = Column(Numeric(12, 4), nullable=True)
    pct_from_52w_high = Column(Numeric(8, 4), nullable=True)  # negative = below high
    pct_from_52w_low  = Column(Numeric(8, 4), nullable=True)  # positive = above low

    # Relative Strength vs ASX200 (percentile rank, 0–100)
    rs_rating   = Column(Numeric(6, 2), nullable=True)

    # ATR (Average True Range) — used for stop loss calculation
    atr_14      = Column(Numeric(12, 4), nullable=True)

    created_at  = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PriceBar {self.ticker} {self.date} close={self.close}>"
