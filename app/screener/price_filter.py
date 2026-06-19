"""
Share Price Range Filter — equity-only portfolio-construction preference.

Lets an org restrict which equities it will screen/trade to a configured
price band, e.g. "only stocks between $0.10 and $1.00". This is NOT a
Minervini criterion — it's a capital-allocation preference (avoid
ultra-illiquid penny stocks, or conversely avoid high-priced stocks that
eat too much of the position-sizing budget per share).

Rules encoded (RuleConfig, category=ENTRY, asset_types=EQUITY):
  entry_min_share_price — price must be >= threshold (skip if below)
  entry_max_share_price — price must be <= threshold (skip if above)

Both rules are disabled by default (enabled_globally=False) — the filter
has zero effect until an org admin explicitly turns it on via /admin/rules
and sets thresholds. CRYPTO signals/tickers are never subject to this
filter regardless of rule state — callers must gate on asset_type
themselves (see callers in app/tasks/screening.py, app/tasks/trading.py,
app/trading/order_executor.py) since a single RuleEngine instance may be
shared across equity and crypto tickers in some call sites.
"""
from __future__ import annotations
from typing import Optional
from app.screener.rules import RuleEngine, RuleResult


def evaluate_price_range(
    ticker: str,
    price: float,
    engine: RuleEngine,
    asset_type: str = "EQUITY",
) -> dict[str, RuleResult]:
    """
    Check a ticker's current price against the configured min/max share
    price band. No-ops entirely for non-equity assets.

    Args:
        ticker:     Stock ticker (for logging/messages only)
        price:      Current price to check (close, intraday, or entry price)
        engine:     RuleEngine instance (asset_type should be EQUITY-scoped,
                    or the caller must have already excluded crypto tickers)
        asset_type: "EQUITY" or "CRYPTO" — CRYPTO always short-circuits to {}

    Returns:
        Dict of rule_id -> RuleResult. Empty dict if asset_type is CRYPTO,
        if neither rule is enabled, or if price is falsy/invalid.
    """
    results: dict[str, RuleResult] = {}

    if asset_type == "CRYPTO":
        return results

    if price is None or price <= 0:
        return results

    price = float(price)

    # -------------------------------------------------------------------------
    # Minimum share price
    # -------------------------------------------------------------------------
    rule_id = "entry_min_share_price"
    if engine.is_enabled(rule_id):
        min_price = engine.threshold(rule_id)
        if min_price is not None:
            min_price = float(min_price)
            passed = price >= min_price
            results[rule_id] = RuleResult(
                rule_id, passed, round(price, 4), min_price,
                f"{ticker} price {price:.4f} {'>=' if passed else '<'} min {min_price:.4f}",
            )

    # -------------------------------------------------------------------------
    # Maximum share price
    # -------------------------------------------------------------------------
    rule_id = "entry_max_share_price"
    if engine.is_enabled(rule_id):
        max_price = engine.threshold(rule_id)
        if max_price is not None:
            max_price = float(max_price)
            passed = price <= max_price
            results[rule_id] = RuleResult(
                rule_id, passed, round(price, 4), max_price,
                f"{ticker} price {price:.4f} {'<=' if passed else '>'} max {max_price:.4f}",
            )

    return results


def price_in_range(
    ticker: str,
    price: float,
    engine: RuleEngine,
    asset_type: str = "EQUITY",
) -> tuple[bool, Optional[str]]:
    """
    Convenience wrapper for hard-gate call sites that just need a
    pass/fail + reason, without juggling a RuleResult dict.

    Returns:
        (in_range, reason) — reason is None when in_range is True or the
        filter doesn't apply (e.g. crypto, both rules disabled).
    """
    results = evaluate_price_range(ticker, price, engine, asset_type)
    for rule_id, result in results.items():
        if not result.passed:
            return False, result.message
    return True, None
