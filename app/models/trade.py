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


# ---------------------------------------------------------------------------
# Plain-English rationale for every exit reason.
# Surfaced in the Closed Trades view so a non-expert user understands *why* a
# position was closed and the Minervini idea behind it. Each entry has a short
# `summary` (one line) and a fuller `detail` (a few sentences).
# IMPORTANT: every ExitReason member MUST have an entry here — a regression test
# (tests/test_exit_rationale.py) enforces this so new reasons can't ship without
# a user-facing explanation.
# ---------------------------------------------------------------------------
EXIT_REASON_RATIONALE: dict[str, dict[str, str]] = {
    "STOP_LOSS": {
        "summary": "Price hit your protective stop — the loss was capped automatically.",
        "detail": (
            "The price fell to the pre-set stop level, so the position was sold to stop the loss "
            "growing any further. This is the single most important discipline in the Minervini "
            "method: cut losses short and keep every one small, because one big loss can erase many "
            "good gains. As the saying goes, your first loss is usually your best loss."
        ),
    },
    "TRAILING_STOP": {
        "summary": "A profit-protecting stop was trailed up under the trend, and price broke it.",
        "detail": (
            "As the trade moved in your favour the stop was raised behind the price (often just under "
            "the 50-day moving average). When the price closed back below that level the position was "
            "sold to bank the gain. The idea: ride a winner for as long as the uptrend holds, but give "
            "very little back once that trend clearly breaks."
        ),
    },
    "PROFIT_TARGET_1": {
        "summary": "Part of the position was sold into strength to lock in an early gain.",
        "detail": (
            "After a solid advance, a portion of the position was sold while the stock was still strong "
            "— taking money off the table and reducing risk while letting the remainder run. Minervini "
            "calls this 'selling into strength': bank some profit while buyers are eager, rather than "
            "waiting until the move is already over."
        ),
    },
    "PROFIT_TARGET_2": {
        "summary": "A large winner gave back enough from its peak, so the remainder was sold.",
        "detail": (
            "Once the trade reached a big gain it was managed with a trailing stop instead of a fixed "
            "target, so it could keep running. This exit means the price pulled back far enough from its "
            "high to take the rest off the table. The principle: let your winners run, then protect the "
            "profit when the move finally runs out of steam."
        ),
    },
    "TIME_STOP": {
        "summary": "The trade hadn't made enough progress in the allotted time, so the capital was freed up.",
        "detail": (
            "A correct breakout should generally start working fairly soon. This position hadn't reached "
            "its expected gain within the time window, so it was closed — for a loss, breakeven, or even a "
            "small profit — to move the money into a faster-acting leader. Minervini calls this "
            "'opportunity cost': money sitting in a stock that isn't moving is money that isn't compounding "
            "somewhere better. Note: this can close a position that is modestly in profit simply because it "
            "is rising too slowly."
        ),
    },
    "MARKET_REGIME": {
        "summary": "The broad market turned hostile, so exposure was reduced.",
        "detail": (
            "The overall market shifted into a downtrend (for example, the index fell below its 200-day "
            "moving average). Around three out of four breakouts fail in a weak market, so the position was "
            "trimmed or closed to protect your capital. Minervini's rule: never fight the tape — trade "
            "aggressively in healthy markets and defensively in poor ones."
        ),
    },
    "EARNINGS_AVOID": {
        "summary": "Closed ahead of an earnings report to avoid a binary overnight gap.",
        "detail": (
            "Earnings announcements can gap a stock sharply up or down overnight — a coin-flip that a stop "
            "can't protect you from. The position was exited (or trimmed) before the report. Minervini only "
            "holds through earnings when there is already a comfortable profit cushion to absorb a bad reaction."
        ),
    },
    "CLIMAX_TOP": {
        "summary": "An exhaustion spike on heavy volume suggested a top, so it was sold into strength.",
        "detail": (
            "After a long run, a sharp surge on unusually high volume often marks the end of a move as the "
            "last eager buyers pile in. The position was sold into that euphoria rather than waiting for the "
            "pullback that usually follows. Minervini: sell into strength when a stock goes near-vertical — "
            "that's when demand is highest and it's easiest to get a good price."
        ),
    },
    "THREE_WEEKS_TIGHT": {
        "summary": "A tight, calm consolidation pattern — normally a reason to hold, not sell.",
        "detail": (
            "Three weekly closes within a very narrow range ('three weeks tight') shows the stock is quietly "
            "coiling for its next move, and is usually a signal to HOLD. If it appears here as a close reason, "
            "treat it as a managed continuation exit. Minervini views this pattern as a sign of strength and "
            "often a place to add, not exit."
        ),
    },
    "MANUAL": {
        "summary": "Closed manually by you (via the dashboard or a WhatsApp/Telegram command).",
        "detail": (
            "This position was closed by hand rather than by an automatic rule — for example from the "
            "Positions page or a remote command. The system simply recorded and executed your decision."
        ),
    },
}


def exit_reason_rationale(reason) -> dict[str, str]:
    """
    Return {'summary','detail'} plain-English rationale for an ExitReason.
    Accepts an ExitReason, its .value, or a raw string like 'ExitReason.TIME_STOP'.
    Always returns a dict (empty strings if somehow unknown) so callers/templates
    never break.
    """
    if reason is None:
        return {"summary": "", "detail": ""}
    if isinstance(reason, ExitReason):
        key = reason.value
    else:
        key = str(reason).replace("ExitReason.", "").strip()
    return EXIT_REASON_RATIONALE.get(key, {"summary": "", "detail": ""})


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
