"""
Trade, Position, and Order models.
Full audit trail: every order, fill, modification, and exit is recorded.
"""
import enum
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    Enum, Numeric, Text, JSON, ForeignKey
)
from sqlalchemy.orm import relationship
from app.database import Base


class OrderAction(str, enum.Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    LIMIT   = "LIMIT"
    MARKET  = "MARKET"
    STOP    = "STOP"
    BRACKET = "BRACKET"   # Entry + stop + target in one


class OrderStatus(str, enum.Enum):
    PENDING   = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED    = "FILLED"
    PARTIAL   = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"


class TradeStatus(str, enum.Enum):
    OPEN    = "OPEN"
    CLOSED  = "CLOSED"


class ExitReason(str, enum.Enum):
    STOP_LOSS        = "STOP_LOSS"
    TRAILING_STOP    = "TRAILING_STOP"
    PROFIT_TARGET_1  = "PROFIT_TARGET_1"
    PROFIT_TARGET_2  = "PROFIT_TARGET_2"
    TIME_STOP        = "TIME_STOP"            # Not moving after N weeks
    MARKET_REGIME    = "MARKET_REGIME"        # Market went into correction
    EARNINGS_AVOID   = "EARNINGS_AVOID"       # Exited before earnings
    CLIMAX_TOP       = "CLIMAX_TOP"           # Exhaustion signal
    MANUAL           = "MANUAL"              # Admin / WhatsApp override
    THREE_WEEKS_TIGHT= "THREE_WEEKS_TIGHT"   # 3-weeks-tight trailing stop


class Order(Base):
    """Every order sent to IBKR or crypto exchange — entry, exit, stop, modification."""
    __tablename__ = "orders"

    id              = Column(Integer, primary_key=True)
    ibkr_order_id   = Column(Integer, nullable=True, index=True)   # IBKR perm ID
    external_order_id = Column(String(128), nullable=True)         # ccxt order ID for crypto
    ticker          = Column(String(32), nullable=False, index=True)
    exchange_key    = Column(String(32), nullable=False, default="ASX")
                                            # Which exchange this order was sent to
    asset_type      = Column(String(16), nullable=False, default="EQUITY")
    currency        = Column(String(8),  nullable=False, default="AUD")
                                            # Order currency (native: AUD, USD, USDT)
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    # Relationships
    organization    = relationship("Organization")

    action          = Column(Enum(OrderAction), nullable=False)
    order_type      = Column(Enum(OrderType), nullable=False)
    status          = Column(Enum(OrderStatus), default=OrderStatus.PENDING)

    qty_ordered     = Column(Numeric(20, 8), nullable=False)   # Numeric for crypto fractional qty
    qty_filled      = Column(Numeric(20, 8), default=0)
    limit_price     = Column(Numeric(14, 4), nullable=True)
    stop_price      = Column(Numeric(14, 4), nullable=True)
    avg_fill_price  = Column(Numeric(14, 4), nullable=True)

    commission_local= Column(Numeric(12, 4), default=0)        # Commission in native currency
    commission_aud  = Column(Numeric(12, 4), default=0)        # AUD equivalent at fill time
    slippage_aud    = Column(Numeric(12, 4), default=0)
    fx_rate_aud     = Column(Numeric(10, 6), nullable=True)    # AUD/native rate at fill time

    is_paper        = Column(Boolean, default=True)
    submitted_at    = Column(DateTime, nullable=True)
    filled_at       = Column(DateTime, nullable=True)
    cancelled_at    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    raw_ibkr_response = Column(JSON, nullable=True)  # Full IBKR response for debugging

    def __repr__(self):
        return f"<Order {self.action} {self.ticker} qty={self.qty_ordered} {self.status}>"


class Position(Base):
    """
    Current open positions. One row per open holding (equity or crypto).
    All AUD-equivalent fields allow cross-market portfolio heat aggregation.
    """
    __tablename__ = "positions"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(32), nullable=False, index=True)
    exchange_key    = Column(String(32), nullable=False, default="ASX")
    asset_type      = Column(String(16), nullable=False, default="EQUITY")
    currency        = Column(String(8),  nullable=False, default="AUD")
                                            # Native price currency for this position
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    # Relationships
    organization    = relationship("Organization")

    entry_date      = Column(Date, nullable=False)
    entry_price     = Column(Numeric(14, 4), nullable=False)   # In native currency
    entry_fx_rate   = Column(Numeric(10, 6), nullable=True)    # AUD/native at entry
    qty             = Column(Numeric(20, 8), nullable=False)   # Numeric for crypto fractional
    current_price   = Column(Numeric(14, 4), nullable=True)
    current_fx_rate = Column(Numeric(10, 6), nullable=True)    # Latest AUD/native rate

    # Stop management
    initial_stop    = Column(Numeric(14, 4), nullable=False)
    current_stop    = Column(Numeric(14, 4), nullable=False)
    stop_type       = Column(String(32), default="HARD")

    # Profit targets (in native currency)
    target_1        = Column(Numeric(14, 4), nullable=True)
    target_2        = Column(Numeric(14, 4), nullable=True)
    target_1_hit    = Column(Boolean, default=False)

    # Pyramid tracking
    pyramid_count   = Column(Integer, default=0)
    avg_cost        = Column(Numeric(14, 4), nullable=True)

    # P&L — local currency + AUD equivalent
    unrealised_pnl_local = Column(Numeric(14, 2), nullable=True)  # In native currency
    unrealised_pnl  = Column(Numeric(14, 2), nullable=True)    # AUD equivalent
    unrealised_pct  = Column(Numeric(8, 4), nullable=True)

    # Risk — always AUD for cross-market portfolio heat
    risk_aud        = Column(Numeric(12, 2), nullable=True)    # (entry - stop) * qty in AUD
    portfolio_pct   = Column(Numeric(6, 4), nullable=True)     # % of total AUD capital

    is_paper        = Column(Boolean, default=True)
    status          = Column(Enum(TradeStatus), default=TradeStatus.OPEN)
    last_updated    = Column(DateTime, default=datetime.utcnow)
    created_at      = Column(DateTime, default=datetime.utcnow)

    account         = relationship("Account", back_populates="positions")

    def __repr__(self):
        return f"<Position {self.ticker} qty={self.qty} entry={self.entry_price}>"


class Trade(Base):
    """
    Closed trade record — full history for performance analysis and CGT/tax reporting.
    Created when a position is fully closed.
    Supports ASX equities, US equities, and crypto assets.
    All P&L fields are stored in both native currency and AUD equivalent.
    """
    __tablename__ = "trades"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(32), nullable=False, index=True)
    exchange_key    = Column(String(32), nullable=False, default="ASX")
    asset_type      = Column(String(16), nullable=False, default="EQUITY")
    currency        = Column(String(8),  nullable=False, default="AUD")
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    # Relationships
    organization    = relationship("Organization")

    entry_date      = Column(Date, nullable=False)
    exit_date       = Column(Date, nullable=False)
    hold_days       = Column(Integer)

    entry_price     = Column(Numeric(14, 4), nullable=False)   # Native currency
    exit_price      = Column(Numeric(14, 4), nullable=False)
    qty             = Column(Numeric(20, 8), nullable=False)   # Numeric for crypto

    # FX rates at entry/exit for AUD conversion
    entry_fx_rate   = Column(Numeric(10, 6), nullable=True)
    exit_fx_rate    = Column(Numeric(10, 6), nullable=True)

    # P&L in native currency
    gross_pnl_local = Column(Numeric(14, 2), nullable=True)
    commission_local= Column(Numeric(12, 4), default=0)
    net_pnl_local   = Column(Numeric(14, 2), nullable=True)

    # P&L in AUD (for portfolio reporting and tax)
    gross_pnl_aud   = Column(Numeric(14, 2))
    commission_aud  = Column(Numeric(12, 4), default=0)
    net_pnl_aud     = Column(Numeric(14, 2))
    pnl_pct         = Column(Numeric(8, 4))

    initial_stop    = Column(Numeric(14, 4))
    max_adverse_excursion    = Column(Numeric(14, 4), nullable=True)
    max_favourable_excursion = Column(Numeric(14, 4), nullable=True)

    exit_reason     = Column(Enum(ExitReason), nullable=False)
    is_paper        = Column(Boolean, default=True)

    # CGT fields (Australian tax — applies to ASX and US equities held > 12 months)
    cgt_eligible_discount = Column(Boolean, default=False)
    cgt_gain_aud    = Column(Numeric(14, 2), nullable=True)

    trade_thesis    = Column(Text, nullable=True)   # Pre-trade notes from signal
    post_trade_notes= Column(Text, nullable=True)   # Review notes

    created_at      = Column(DateTime, default=datetime.utcnow)

    account         = relationship("Account", back_populates="trades")

    def __repr__(self):
        return f"<Trade {self.ticker} {self.entry_date}→{self.exit_date} pnl={self.net_pnl_aud}>"
