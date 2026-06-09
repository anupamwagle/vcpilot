"""
Exchange and Market Regime models.

ExchangeConfig:     Super admin managed — defines available trading venues (equities + crypto).
MarketRegimeRecord: Per-exchange market regime history, replacing single global SystemConfig key.

Design principles:
  - ExchangeConfig rows are global (no org_id) — super admin enables/disables at platform level.
  - Each org selects active exchanges via SystemConfig key 'active_exchanges' (comma-separated keys).
  - Crypto exchanges use ccxt under the hood; equities use IBKR.
  - MarketRegimeRecord stores one row per evaluation per exchange (+ optional org scope).
"""
import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Numeric, Text, JSON,
    UniqueConstraint, Index
)
from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AssetType(str, enum.Enum):
    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"


class BrokerType(str, enum.Enum):
    IBKR  = "IBKR"   # Interactive Brokers — ASX + US equities
    CCXT  = "CCXT"   # ccxt unified crypto API


class ExchangeKey(str, enum.Enum):
    """
    Canonical exchange identifiers used throughout the app.
    Equities use pandas_market_calendars names where possible.
    Crypto keys are prefixed CRYPTO_ to avoid collisions.
    """
    ASX            = "ASX"
    NYSE           = "NYSE"
    NASDAQ         = "NASDAQ"
    CRYPTO_BINANCE           = "CRYPTO_BINANCE"
    CRYPTO_COINBASE          = "CRYPTO_COINBASE"
    CRYPTO_KRAKEN            = "CRYPTO_KRAKEN"
    CRYPTO_MEXC              = "CRYPTO_MEXC"
    CRYPTO_INDEPENDENTRESERVE= "CRYPTO_INDEPENDENTRESERVE"


# ---------------------------------------------------------------------------
# ExchangeConfig — global, managed by super admin
# ---------------------------------------------------------------------------

class ExchangeConfig(Base):
    """
    One row per supported trading venue.
    Seeded on first startup by seed_config.py.
    Super admin can enable/disable exchanges and configure crypto providers.

    Equity exchanges route through IBKR; crypto exchanges route through ccxt.
    """
    __tablename__ = "exchange_configs"

    id               = Column(Integer, primary_key=True)
    exchange_key     = Column(String(32), unique=True, nullable=False, index=True)
                                                # ExchangeKey enum value, e.g. "ASX", "CRYPTO_BINANCE"
    display_name     = Column(String(128), nullable=False)
                                                # "Australian Securities Exchange"
    asset_type       = Column(String(16), nullable=False, default="EQUITY")
                                                # "EQUITY" | "CRYPTO"
    broker_type      = Column(String(16), nullable=False, default="IBKR")
                                                # "IBKR" | "CCXT"
    is_enabled       = Column(Boolean, default=True, nullable=False)
                                                # Super admin can disable globally
    trading_currency = Column(String(8),  nullable=False, default="USD")
                                                # Settlement currency: AUD, USD, USDT
    flag_emoji       = Column(String(8),  nullable=True)
                                                # 🇦🇺 🇺🇸 ₿ — shown on stock badges
    # Calendar & session info
    calendar_key     = Column(String(32), nullable=True)
                                                # pandas_market_calendars key: "ASX", "NYSE", "NASDAQ"
    timezone         = Column(String(64), nullable=False, default="UTC")
                                                # "Australia/Sydney", "America/New_York"
    market_open_utc  = Column(String(8),  nullable=True)
                                                # "23:30" — approximate UTC open (for schedule hints)
    market_close_utc = Column(String(8),  nullable=True)
                                                # "06:00" — approximate UTC close

    # Market regime index (for BULL/CAUTION/BEAR evaluation)
    index_ticker     = Column(String(32), nullable=True)
                                                # "^AXJO", "^GSPC", "^IXIC", "BTC-USD"

    # IBKR routing (equity only)
    ibkr_exchange    = Column(String(32), nullable=True)
                                                # IBKR exchange param: "ASX", "SMART"
    ibkr_currency    = Column(String(8),  nullable=True)
                                                # IBKR currency: "AUD", "USD"

    # ccxt provider (crypto only)
    ccxt_provider    = Column(String(64), nullable=True)
                                                # "binance", "coinbase", "kraken"
    ccxt_sandbox     = Column(Boolean, default=False)
                                                # Use ccxt sandbox/testnet by default

    # Ticker format hint (for normalize_ticker helper)
    ticker_suffix    = Column(String(8),  nullable=True)
                                                # ".AX" for ASX, None for US, "-USD" for crypto
    yfinance_suffix  = Column(String(8),  nullable=True)
                                                # yfinance append format (may differ from display)

    sort_order       = Column(Integer, default=0)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ExchangeConfig {self.exchange_key} enabled={self.is_enabled}>"

    @property
    def is_crypto(self) -> bool:
        return str(self.asset_type) == "CRYPTO"

    @property
    def is_equity(self) -> bool:
        return str(self.asset_type) == "EQUITY"


# ---------------------------------------------------------------------------
# MarketRegimeRecord — per-exchange regime history
# ---------------------------------------------------------------------------

class MarketRegimeRecord(Base):
    """
    One row per market regime evaluation, per exchange.
    Replaces the single 'last_market_regime' SystemConfig key.

    The most recent row per exchange_key (+ org scope) is the current regime.
    Historical rows give trend and context for audit.
    """
    __tablename__ = "market_regimes"
    __table_args__ = (
        Index("ix_market_regimes_exchange_org", "exchange_key", "organization_id"),
    )

    id              = Column(Integer, primary_key=True)
    exchange_key    = Column(String(32), nullable=False, index=True)
                                            # ExchangeKey value, e.g. "ASX", "NYSE"
    organization_id = Column(Integer, nullable=True)
                                            # NULL = global evaluation (shared across orgs)
    regime          = Column(String(16), nullable=False)
                                            # "BULL", "CAUTION", "BEAR"
    evaluated_at    = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Key metrics at evaluation time
    index_close     = Column(Numeric(14, 4), nullable=True)
    index_ma200     = Column(Numeric(14, 4), nullable=True)
    breadth_pct     = Column(Numeric(6, 2), nullable=True)  # % stocks above 200MA
    distribution_days = Column(Integer, nullable=True)

    # Full rule results snapshot
    rule_results    = Column(JSON, default=dict)

    created_at      = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MarketRegimeRecord {self.exchange_key} {self.regime} @ {self.evaluated_at}>"
