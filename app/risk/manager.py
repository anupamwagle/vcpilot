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
    shares: float              # Number of shares/units (float for crypto fractional)
    capital_aud: float         # Trade value in AUD equivalent
    capital_local: float       # Trade value in native currency (AUD for ASX, USD for US)
    risk_aud: float            # Risk in AUD equivalent
    risk_pct: float            # Risk as % of total AUD capital
    portfolio_pct: float       # Position as % of total AUD capital
    stop_price: float          # Stop price in native currency
    entry_price: float         # Entry price in native currency
    currency: str              # Native currency ("AUD", "USD")
    fx_rate_aud: float         # AUD/native rate used (1.0 for AUD positions)
    message: str


# IBKR minimum commissions by currency
_IBKR_MIN_COMMISSION = {
    "AUD": 6.0,          # ASX: min $6 AUD
    "USD": 1.0,          # US equities: min $1 USD
    "USDT": 0.0,         # Crypto: no fixed minimum (% based)
}


def calculate_position_size(
    capital_aud: float,               # Total account capital in base_currency (kept name for kwargs backward compatibility)
    entry_price: float,               # Entry price in NATIVE currency
    stop_price: float,                # Stop price in NATIVE currency
    engine: RuleEngine,
    currency: str = "AUD",            # Native currency of the stock ("AUD", "USD")
    fx_rate_aud: float = None,        # kept for compatibility
    regime_multiplier: float = 1.0,   # 0.5 in CAUTION, 0 in BEAR
    base_currency: str = "AUD",       # Base currency of the account capital
    is_crypto: bool = False,          # If True, skip commission checks and allow fractional shares
) -> SizingResult:
    """
    Minervini position sizing: Risk-based, currency-aware.

    Position size = (Capital × Risk%) ÷ (Entry − Stop)
    All calculations normalised to AUD for portfolio heat comparison.

    For US stocks: entry/stop prices are USD; capital is AUD.
    The fx_rate_aud converts capital to USD for sizing, then result back to AUD.

    Args:
        capital_aud:      Total account capital in base_currency
        entry_price:      Entry price in native currency (AUD for ASX, USD for US)
        stop_price:       Stop price in native currency
        engine:           RuleEngine for threshold lookup
        currency:         "AUD" or "USD" or "USDT"
        fx_rate_aud:      Kept for compatibility (used if base_currency is AUD)
        regime_multiplier: Scale factor from market regime
        base_currency:     Base currency of the account capital
        is_crypto:         If True, skip commission checks and allow fractional shares

    Returns:
        SizingResult with both native and AUD-equivalent values
    """
    from app.data.fetcher import get_fx_rate, currency_to_aud

    capital_base = capital_aud

    # Rate from base currency to asset currency
    if base_currency == currency:
        fx_rate_base_asset = 1.0
    elif base_currency == "AUD" and fx_rate_aud is not None:
        fx_rate_base_asset = fx_rate_aud
    else:
        try:
            fx_rate_base_asset = get_fx_rate(base_currency, currency)
        except Exception:
            fx_rate_base_asset = 0.65 if (base_currency == "AUD" and currency == "USD") else 1.0

    if regime_multiplier <= 0:
        return SizingResult(0, 0, 0, 0, 0, 0, stop_price, entry_price, currency,
                            fx_rate_base_asset or 1.0, "Blocked: BEAR market regime")

    # Convert capital to native currency for sizing
    if fx_rate_base_asset <= 0:
        capital_local = capital_base
    else:
        capital_local = capital_base * fx_rate_base_asset

    # Use crypto-specific risk limit if crypto trade
    if is_crypto:
        max_risk_pct = float(engine.threshold("crypto_max_risk_pct") or 1.0) * regime_multiplier
    else:
        max_risk_pct = float(engine.threshold("risk_max_pct_per_trade") or 2.0) * regime_multiplier

    max_pos_pct  = float(engine.threshold("risk_max_position_pct") or 30.0)
    min_commission = 0.0 if is_crypto else _IBKR_MIN_COMMISSION.get(currency, 1.0)

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return SizingResult(0, 0, 0, 0, 0, 0, stop_price, entry_price, currency, fx_rate_base_asset or 1.0,
                            "Invalid: stop price ≥ entry price")

    # Base calculation in native currency
    max_risk_local = capital_local * (max_risk_pct / 100)
    raw_shares = max_risk_local / risk_per_share
    
    if is_crypto:
        shares = raw_shares
    else:
        shares = max(1.0, math.floor(raw_shares))  # floor for equities (whole shares)

    # Cap by max position % of capital
    max_capital_local = capital_local * (max_pos_pct / 100)
    if is_crypto:
        max_shares_by_capital = max_capital_local / entry_price
    else:
        max_shares_by_capital = max(1.0, math.floor(max_capital_local / entry_price))
    shares = min(shares, max_shares_by_capital)

    # Commission efficiency check (skip for crypto)
    if min_commission > 0:
        trade_value = shares * entry_price
        if trade_value > 0 and (min_commission / trade_value) > 0.01:
            min_trade_value = min_commission / 0.01
            min_shares = math.ceil(min_trade_value / entry_price)
            if min_shares * entry_price > max_capital_local:
                return SizingResult(0, 0, 0, 0, 0, 0, stop_price, entry_price, currency, fx_rate_base_asset or 1.0,
                                    f"Trade too small: min {currency} {min_trade_value:.0f} needed for commission efficiency")
            shares = max(shares, float(min_shares))

    # Final calculations
    trade_value_local = shares * entry_price
    risk_local        = shares * risk_per_share

    # Convert to AUD equivalent (always needed for system-wide reporting/heat limits)
    try:
        trade_value_aud = currency_to_aud(trade_value_local, currency)
        risk_aud_equiv  = currency_to_aud(risk_local, currency)
    except Exception:
        trade_value_aud = trade_value_local
        risk_aud_equiv  = risk_local

    # Risk and portfolio percentage calculations relative to the chosen base capital currency
    risk_pct      = (risk_local / capital_local) * 100 if capital_local > 0 else 0.0
    portfolio_pct = (trade_value_local / capital_local) * 100 if capital_local > 0 else 0.0

    # Format log message
    # NOTE: f-string format specs are literal strings — "if/else" inside a `:spec` is invalid
    # syntax and raises ValueError("Invalid format specifier ..."). Pick the spec strings first.
    risk_base = risk_local / fx_rate_base_asset if (fx_rate_base_asset and fx_rate_base_asset > 0) else risk_local
    shares_fmt = ".4f" if is_crypto else ".0f"
    risk_fmt = ".4f" if is_crypto else ".2f"
    msg = (f"{shares:{shares_fmt}} units @ {currency} {entry_price:.4f} = {currency} {trade_value_local:.2f} "
           f"(~AUD {trade_value_aud:.2f}, {portfolio_pct:.1f}% of capital) | "
           f"Risk {base_currency} {risk_base:{risk_fmt}} ({risk_pct:.2f}%)")
    logger.debug(msg)

    return SizingResult(
        shares=round(shares, 8) if is_crypto else float(int(shares)),
        capital_aud=round(trade_value_aud, 2),
        capital_local=round(trade_value_local, 2),
        risk_aud=round(risk_aud_equiv, 2),
        risk_pct=round(risk_pct, 3),
        portfolio_pct=round(portfolio_pct, 2),
        stop_price=stop_price,
        entry_price=entry_price,
        currency=currency,
        fx_rate_aud=fx_rate_base_asset or 1.0,
        message=msg,
    )


def calculate_portfolio_heat(positions: list[dict]) -> float:
    """
    Total portfolio at risk across ALL open positions, normalised to AUD.
    Works across multiple exchanges and currencies.

    positions: list of dicts with keys:
        risk_aud        — pre-computed AUD risk per position (preferred)
        OR:
        capital_aud     — position value in AUD
        entry_price     — entry price in native currency
        current_stop    — current stop price in native currency
        qty             — number of shares/units
        fx_rate_aud     — AUD/native rate (default 1.0)

    Returns: total AUD risk as % of total AUD capital
    """
    total_capital = sum(p.get("capital_aud", 0) for p in positions)
    if total_capital == 0:
        return 0.0

    total_risk_aud = 0.0
    for p in positions:
        if p.get("risk_aud") is not None:
            total_risk_aud += float(p["risk_aud"])
        else:
            # Compute from raw fields
            qty   = float(p.get("qty", 0))
            entry = float(p.get("entry_price", 0))
            stop  = float(p.get("current_stop", p.get("stop_price", 0)))
            fx    = float(p.get("fx_rate_aud", 1.0)) or 1.0
            risk_local = qty * (entry - stop)
            total_risk_aud += risk_local / fx  # Convert to AUD

    return round((total_risk_aud / total_capital) * 100, 2)


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
