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
    """Every order sent to IBKR — entry, exit, stop, modification."""
    __tablename__ = "orders"

    id              = Column(Integer, primary_key=True)
    ibkr_order_id   = Column(Integer, nullable=True, index=True)  # IBKR perm ID
    ticker          = Column(String(16), nullable=False, index=True)
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    action          = Column(Enum(OrderAction), nullable=False)
    order_type      = Column(Enum(OrderType), nullable=False)
    status          = Column(Enum(OrderStatus), default=OrderStatus.PENDING)

    qty_ordered     = Column(Integer, nullable=False)
    qty_filled      = Column(Integer, default=0)
    limit_price     = Column(Numeric(12, 4), nullable=True)
    stop_price      = Column(Numeric(12, 4), nullable=True)
    avg_fill_price  = Column(Numeric(12, 4), nullable=True)

    commission_aud  = Column(Numeric(10, 4), default=0)
    slippage_aud    = Column(Numeric(10, 4), default=0)

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
    """Current open positions. One row per open stock holding."""
    __tablename__ = "positions"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(16), nullable=False, index=True)
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    entry_date      = Column(Date, nullable=False)
    entry_price     = Column(Numeric(12, 4), nullable=False)
    qty             = Column(Integer, nullable=False)
    current_price   = Column(Numeric(12, 4), nullable=True)

    # Stop management
    initial_stop    = Column(Numeric(12, 4), nullable=False)
    current_stop    = Column(Numeric(12, 4), nullable=False)   # Updated as trailing stop moves
    stop_type       = Column(String(32), default="HARD")        # HARD | TRAILING | THREE_WEEK

    # Profit targets
    target_1        = Column(Numeric(12, 4), nullable=True)
    target_2        = Column(Numeric(12, 4), nullable=True)
    target_1_hit    = Column(Boolean, default=False)

    # Pyramid tracking
    pyramid_count   = Column(Integer, default=0)   # Number of add-on positions
    avg_cost        = Column(Numeric(12, 4), nullable=True)   # Blended entry price

    # P&L
    unrealised_pnl  = Column(Numeric(12, 2), nullable=True)
    unrealised_pct  = Column(Numeric(8, 4), nullable=True)

    # Risk
    risk_aud        = Column(Numeric(10, 2), nullable=True)    # (entry - stop) * qty
    portfolio_pct   = Column(Numeric(6, 4), nullable=True)     # % of total capital

    is_paper        = Column(Boolean, default=True)
    status          = Column(Enum(TradeStatus), default=TradeStatus.OPEN)
    last_updated    = Column(DateTime, default=datetime.utcnow)
    created_at      = Column(DateTime, default=datetime.utcnow)

    account         = relationship("Account", back_populates="positions")

    def __repr__(self):
        return f"<Position {self.ticker} qty={self.qty} entry={self.entry_price}>"


class Trade(Base):
    """
    Closed trade record — full history for performance analysis and CGT reporting.
    Created when a position is fully closed.
    """
    __tablename__ = "trades"

    id              = Column(Integer, primary_key=True)
    ticker          = Column(String(16), nullable=False, index=True)
    account_id      = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    entry_date      = Column(Date, nullable=False)
    exit_date       = Column(Date, nullable=False)
    hold_days       = Column(Integer)

    entry_price     = Column(Numeric(12, 4), nullable=False)
    exit_price      = Column(Numeric(12, 4), nullable=False)
    qty             = Column(Integer, nullable=False)

    gross_pnl_aud   = Column(Numeric(12, 2))
    commission_aud  = Column(Numeric(10, 4), default=0)
    net_pnl_aud     = Column(Numeric(12, 2))
    pnl_pct         = Column(Numeric(8, 4))

    initial_stop    = Column(Numeric(12, 4))
    max_adverse_excursion = Column(Numeric(12, 4), nullable=True)   # MAE
    max_favourable_excursion = Column(Numeric(12, 4), nullable=True) # MFE

    exit_reason     = Column(Enum(ExitReason), nullable=False)
    is_paper        = Column(Boolean, default=True)

    # CGT fields (Australian tax)
    cgt_eligible_discount = Column(Boolean, default=False)  # Held > 12 months
    cgt_gain_aud    = Column(Numeric(12, 2), nullable=True)

    trade_thesis    = Column(Text, nullable=True)   # Pre-trade notes from signal
    post_trade_notes= Column(Text, nullable=True)   # Review notes

    created_at      = Column(DateTime, default=datetime.utcnow)

    account         = relationship("Account", back_populates="trades")

    def __repr__(self):
        return f"<Trade {self.ticker} {self.entry_date}→{self.exit_date} pnl={self.net_pnl_aud}>"
