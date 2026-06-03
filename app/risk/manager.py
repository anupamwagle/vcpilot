"""
Risk Manager — Position sizing, portfolio heat, and pyramid rules.
All parameters loaded from DB RuleConfig / SystemConfig.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math
from loguru import logger

from app.screener.rules import RuleEngine


@dataclass
class SizingResult:
    shares: int
    capital_aud: float
    risk_aud: float
    risk_pct: float
    portfolio_pct: float
    stop_price: float
    entry_price: float
    message: str


def calculate_position_size(
    capital_aud: float,           # Total account capital
    entry_price: float,
    stop_price: float,
    engine: RuleEngine,
    ibkr_min_commission: float = 6.0,   # IBKR ASX minimum commission
    regime_multiplier: float = 1.0,      # 0.5 in CAUTION, 0 in BEAR
) -> SizingResult:
    """
    Minervini position sizing: Risk-based.
    Position size = (Capital × Risk%) ÷ (Entry − Stop)

    Constraints applied:
    - Max risk per trade (default 2% of capital)
    - Max position size as % of capital (default 30%)
    - Commission must be ≤ 1% of trade value
    - Minimum 1 share
    """
    max_risk_pct  = float(engine.threshold("risk_max_pct_per_trade") or 2.0) * regime_multiplier
    max_pos_pct   = float(engine.threshold("risk_max_position_pct") or 30.0)

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return SizingResult(0, 0, 0, 0, 0, stop_price, entry_price,
                            "Invalid: stop price ≥ entry price")

    # Base calculation
    max_risk_aud = capital_aud * (max_risk_pct / 100)
    raw_shares = max_risk_aud / risk_per_share
    shares = max(1, math.floor(raw_shares))

    # Cap by max position % of capital
    max_capital_aud = capital_aud * (max_pos_pct / 100)
    max_shares_by_capital = max(1, math.floor(max_capital_aud / entry_price))
    shares = min(shares, max_shares_by_capital)

    # Ensure commission is ≤ 1% of trade value
    trade_value = shares * entry_price
    if trade_value > 0 and (ibkr_min_commission / trade_value) > 0.01:
        # Trade too small — find minimum shares to make commission ≤ 1%
        min_trade_value = ibkr_min_commission / 0.01
        min_shares = math.ceil(min_trade_value / entry_price)
        if min_shares * entry_price > max_capital_aud:
            return SizingResult(0, 0, 0, 0, 0, stop_price, entry_price,
                                f"Trade too small: min ${min_trade_value:.0f} needed for commission efficiency")
        shares = max(shares, min_shares)

    # Final calcs
    trade_value  = shares * entry_price
    risk_aud     = shares * risk_per_share
    risk_pct     = (risk_aud / capital_aud) * 100
    portfolio_pct= (trade_value / capital_aud) * 100

    msg = (f"{shares} shares @ ${entry_price:.3f} = ${trade_value:.0f} "
           f"({portfolio_pct:.1f}% of capital) | Risk ${risk_aud:.0f} ({risk_pct:.2f}%)")
    logger.debug(msg)

    return SizingResult(shares, round(trade_value, 2), round(risk_aud, 2),
                        round(risk_pct, 3), round(portfolio_pct, 2),
                        stop_price, entry_price, msg)


def calculate_portfolio_heat(positions: list[dict]) -> float:
    """
    Total portfolio at risk across all open positions.
    positions: list of dicts with keys: capital_aud, entry_price, stop_price, qty
    Returns: total risk as % of capital sum.
    """
    total_capital = sum(p.get("capital_aud", 0) for p in positions)
    if total_capital == 0:
        return 0.0
    total_risk = sum(
        p.get("qty", 0) * (p.get("entry_price", 0) - p.get("current_stop", p.get("stop_price", 0)))
        for p in positions
    )
    return round((total_risk / total_capital) * 100, 2)


def check_portfolio_heat(
    current_heat: float,
    engine: RuleEngine,
) -> tuple[bool, str]:
    """
    Returns (can_add_position, message).
    Blocks new trades if portfolio heat exceeds configured maximum.
    """
    max_heat = float(engine.threshold("portfolio_max_heat_pct") or 15.0)
    if current_heat >= max_heat:
        return False, f"Portfolio heat {current_heat:.1f}% ≥ max {max_heat:.1f}% — no new entries"
    return True, f"Portfolio heat {current_heat:.1f}% / {max_heat:.1f}% — entry allowed"


def calculate_pyramid_size(
    original_size: SizingResult,
    current_profit_pct: float,
    pyramid_number: int,        # 1 = first add-on, 2 = second add-on
    engine: RuleEngine,
) -> Optional[SizingResult]:
    """
    Pyramid add-on sizing (Minervini: only add to winning positions).
    Add-on size = 50% of initial for first pyramid, 25% for second.
    Only allowed if position is up at least 2-3%.
    """
    min_profit_to_pyramid = float(engine.threshold("pyramid_min_profit_pct") or 2.0)
    max_pyramids = int(engine.threshold("pyramid_max_count") or 2)

    if pyramid_number > max_pyramids:
        return None
    if current_profit_pct < min_profit_to_pyramid:
        return None

    multipliers = {1: 0.5, 2: 0.25}
    mult = multipliers.get(pyramid_number, 0.25)
    add_shares = max(1, math.floor(original_size.shares * mult))

    return SizingResult(
        shares=add_shares,
        capital_aud=round(add_shares * original_size.entry_price, 2),
        risk_aud=round(original_size.risk_aud * mult, 2),
        risk_pct=round(original_size.risk_pct * mult, 3),
        portfolio_pct=round(original_size.portfolio_pct * mult, 2),
        stop_price=original_size.stop_price,
        entry_price=original_size.entry_price,
        message=f"Pyramid #{pyramid_number}: {add_shares} shares ({mult*100:.0f}% of initial)",
    )
