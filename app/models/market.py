"""
Market data models — Stock universe, daily OHLCV price bars, and intraday entry check logs.
PriceBar is a TimescaleDB hypertable (partitioned by date).

Multi-market support:
  - Stock.exchange_key  maps to ExchangeConfig.exchange_key ("ASX", "NYSE", "CRYPTO_INDEPENDENTRESERVE", …)
  - Stock.asset_type    distinguishes EQUITY vs CRYPTO
  - Stock.currency      is the native trading currency ("AUD", "USD", "USDT")
  - Ticker is always the yfinance canonical format: "BHP.AX", "AAPL", "BTC-USD"
  - stock.exchange_code is the clean display code: "BHP", "AAPL", "BTC"
  - Price data (price_bars) is shared across all orgs — no org_id on these tables.
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Numeric, BigInteger, Text, UniqueConstraint, Index, JSON
)
from app.database import Base


class Stock(Base):
    """
    Master list of all tradeable instruments — ASX equities, US equities, and crypto assets.
    Global table (no org_id). Price data is central and shared across all tenant organisations.

    Universe membership:
      - ASX200 stocks: populated by weekly refresh_universe task
      - Other stocks:  added on-demand when any org adds them to their watchlist
    """
    __tablename__ = "stocks"

    id           = Column(Integer, primary_key=True)
    ticker       = Column(String(32), unique=True, nullable=False, index=True)
                                            # yfinance canonical: "BHP.AX", "AAPL", "BTC-USD"
    exchange_code= Column(String(16), nullable=False)
                                            # Display code: "BHP", "AAPL", "BTC"
    exchange_key = Column(String(32), nullable=False, default="ASX", index=True)
                                            # ExchangeConfig.exchange_key: "ASX", "NYSE", "CRYPTO_INDEPENDENTRESERVE"
    asset_type   = Column(String(16), nullable=False, default="EQUITY")
                                            # "EQUITY" | "CRYPTO"
    currency     = Column(String(8),  nullable=False, default="AUD")
                                            # Native trading currency: "AUD", "USD", "USDT"

    # Metadata
    name         = Column(String(256))
    sector       = Column(String(128))
    industry     = Column(String(128))
    gics_sector  = Column(String(128))
    market_cap   = Column(BigInteger, nullable=True)   # in native currency cents
    float_shares = Column(BigInteger, nullable=True)

    # Index membership flags (ASX-specific kept for backward compat)
    asx_code     = Column(String(10),  nullable=True)  # "BHP" — kept for ASX compat
    in_asx200    = Column(Boolean, default=False)
    in_asx300    = Column(Boolean, default=False)
    in_index     = Column(Boolean, default=False)       # In primary index for the exchange
    index_name   = Column(String(32), nullable=True)    # "ASX200", "SP500", "NASDAQ100"

    # Status flags
    is_active    = Column(Boolean, default=True)        # False = delisted / excluded
    blacklisted  = Column(Boolean, default=False)
    blacklist_reason = Column(Text, nullable=True)

    last_price   = Column(Numeric(14, 4), nullable=True)
    last_updated = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Stock {self.exchange_code} [{self.exchange_key}]>"


class PriceBar(Base):
    """
    Daily OHLCV + derived fields for each stock/crypto asset.
    Designed as a TimescaleDB hypertable on (ticker, date).
    All prices are in the stock's native currency (see Stock.currency).
    """
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_pricebar_ticker_date"),
        Index("ix_pricebar_ticker_date", "ticker", "date"),
        Index("ix_pricebar_exchange_date", "exchange_key", "date"),
    )

    id           = Column(Integer, primary_key=True)
    ticker       = Column(String(32), nullable=False, index=True)
    exchange_key = Column(String(32), nullable=False, default="ASX", index=True)
    date         = Column(Date, nullable=False, index=True)

    # OHLCV
    open         = Column(Numeric(14, 4))
    high         = Column(Numeric(14, 4))
    low          = Column(Numeric(14, 4))
    close        = Column(Numeric(14, 4))
    adj_close    = Column(Numeric(14, 4))
    volume       = Column(BigInteger)

    # Moving averages (computed on ingest)
    ma_10        = Column(Numeric(14, 4), nullable=True)
    ma_21        = Column(Numeric(14, 4), nullable=True)
    ma_50        = Column(Numeric(14, 4), nullable=True)
    ma_150       = Column(Numeric(14, 4), nullable=True)
    ma_200       = Column(Numeric(14, 4), nullable=True)
    ma_200_prev  = Column(Numeric(14, 4), nullable=True)  # Prior day (slope check)

    # Volume metrics
    avg_vol_50   = Column(Numeric(20, 2), nullable=True)  # 50-day avg volume
    vol_ratio    = Column(Numeric(8, 4),  nullable=True)  # Today vol / avg_vol_50

    # 52-week range
    high_52w     = Column(Numeric(14, 4), nullable=True)
    low_52w      = Column(Numeric(14, 4), nullable=True)
    pct_from_52w_high = Column(Numeric(8, 4), nullable=True)
    pct_from_52w_low  = Column(Numeric(8, 4), nullable=True)

    # Relative Strength (percentile rank within same exchange universe, 0–100)
    rs_rating    = Column(Numeric(6, 2), nullable=True)

    # ATR (Average True Range) — stop loss calculation
    atr_14       = Column(Numeric(14, 4), nullable=True)

    created_at   = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PriceBar {self.ticker} {self.date} close={self.close}>"


class EntryCheckLog(Base):
    """
    Per-org, per-signal intraday metric snapshot captured every 5-15 minutes
    during market hours. Powers the Admin Data Log page.
    """
    __tablename__ = "entry_check_logs"
    __table_args__ = (
        Index("ix_ecl_org_checked", "organization_id", "checked_at"),
        Index("ix_ecl_ticker", "ticker"),
    )

    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    signal_id       = Column(Integer, nullable=True, index=True)
    ticker          = Column(String(32), nullable=False)
    exchange_key    = Column(String(32), nullable=True, default="ASX")
    checked_at      = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Price
    price_current   = Column(Numeric(14, 4), nullable=True)
    price_pivot     = Column(Numeric(14, 4), nullable=True)
    price_stop      = Column(Numeric(14, 4), nullable=True)
    price_vs_pivot  = Column(Numeric(8, 4),  nullable=True)

    # Volume
    vol_current     = Column(BigInteger, nullable=True)
    vol_avg_50      = Column(Numeric(20, 2), nullable=True)
    vol_ratio       = Column(Numeric(8, 4),  nullable=True)

    # Moving averages
    ma_10           = Column(Numeric(14, 4), nullable=True)
    ma_50           = Column(Numeric(14, 4), nullable=True)
    ma_150          = Column(Numeric(14, 4), nullable=True)
    ma_200          = Column(Numeric(14, 4), nullable=True)

    # 52-week range
    high_52w        = Column(Numeric(14, 4), nullable=True)
    low_52w         = Column(Numeric(14, 4), nullable=True)
    pct_from_52w_high = Column(Numeric(8, 4), nullable=True)

    rs_rating       = Column(Numeric(6, 2), nullable=True)
    breakout_confirmed = Column(Boolean, default=False)
    rule_results    = Column(JSON, default=dict)

    data_source     = Column(String(32), default="yfinance")
    data_delay_mins = Column(Integer, default=20)
    bar_timestamp   = Column(DateTime, nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<EntryCheckLog {self.ticker} @ {self.checked_at} confirmed={self.breakout_confirmed}>"


class StockFundamentals(Base):
    """
    Persisted "Stock Story" payload for a single instrument (CommSec-style).

    Global table (no org_id) — keyed by yfinance canonical ticker, shared across
    all tenant organisations exactly like `stocks` / `price_bars`.

    The full narrative payload (business summary, key stats, EPS actuals vs
    estimates, dividends, net-income history, growth rates, debt vs equity,
    analyst ratings, etc.) is stored as a single JSON blob in `data` so the
    schema never needs to change as the story grows. Price-derived figures
    (last price, 1Y sparkline) are merged in at read-time from `price_bars`
    — they are NOT stored here, so this table only needs refreshing when the
    underlying fundamentals change (weekly cadence, staleness-gated).

    `fetched_at` drives the staleness gate in `refresh_stock_fundamentals` and
    the on-demand fetch in the `/stock-story/{ticker}` route.
    """
    __tablename__ = "stock_fundamentals"
    __table_args__ = (
        Index("ix_stockfund_fetched", "fetched_at"),
    )

    id           = Column(Integer, primary_key=True)
    ticker       = Column(String(32), unique=True, nullable=False, index=True)
    exchange_key = Column(String(32), nullable=False, default="ASX", index=True)
    asset_type   = Column(String(16), nullable=False, default="EQUITY")
    currency     = Column(String(8),  nullable=False, default="AUD")

    company_name = Column(String(256), nullable=True)
    # Full CommSec-style story payload (see app.data.fetcher.get_stock_story)
    data         = Column(JSON, default=dict)

    # Staleness / housekeeping
    fetch_ok     = Column(Boolean, default=True)   # False = last fetch returned no usable data
    fetch_error  = Column(Text, nullable=True)
    fetched_at   = Column(DateTime, nullable=True, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<StockFundamentals {self.ticker} fetched={self.fetched_at}>"
