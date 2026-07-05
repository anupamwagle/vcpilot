"""
Minimum Liquidity Filter — avoids putting genuinely untradeable names on the
watchlist (R2 / CLAUDE.md #42).

Minervini trades institutional-quality liquidity only. The screener's only
other liquidity-adjacent proxy (entry_min_share_price) is disabled by default,
and price != liquidity — a cheap, high-volume stock can be perfectly liquid,
while an expensive, thin one isn't. Especially with asx_universe_scope=
ALL_LISTED (~2,200 tickers), the screener can otherwise put names on the
watchlist where a 2%-risk position would be multiple days of average volume.

Rule encoded (RuleConfig, category=ENTRY, asset_types=EQUITY):
  entry_min_avg_dollar_volume — 50-day avg daily $ volume (avg_vol_50 * close)
  must be >= threshold (skip if below). Enabled by default, org-tunable.
"""
from __future__ import annotations
from typing import Optional
from app.screener.rules import RuleEngine, RuleResult


def evaluate_liquidity(
    ticker: str,
    close_price: float,
    avg_vol_50: float,
    engine: RuleEngine,
    asset_type: str = "EQUITY",
) -> dict[str, RuleResult]:
    """
    Check a ticker's 50-day average dollar volume against the configured
    minimum. No-ops entirely for non-equity assets.

    Args:
        ticker:      Stock ticker (for logging/messages only)
        close_price: Latest close (native currency)
        avg_vol_50:  50-day average share volume
        engine:      RuleEngine instance (EQUITY-scoped, or caller must have
                     already excluded crypto tickers)
        asset_type:  "EQUITY" or "CRYPTO" — CRYPTO always short-circuits to {}

    Returns:
        Dict of rule_id -> RuleResult. Empty dict if asset_type is CRYPTO,
        the rule is disabled, or inputs are missing/invalid.
    """
    results: dict[str, RuleResult] = {}

    if asset_type == "CRYPTO":
        return results
    if close_price is None or close_price <= 0 or avg_vol_50 is None:
        return results

    rule_id = "entry_min_avg_dollar_volume"
    if engine.is_enabled(rule_id):
        min_dollar_vol = engine.threshold(rule_id)
        if min_dollar_vol is not None:
            min_dollar_vol = float(min_dollar_vol)
            avg_dollar_vol = float(avg_vol_50) * float(close_price)
            passed = avg_dollar_vol >= min_dollar_vol
            results[rule_id] = RuleResult(
                rule_id, passed, round(avg_dollar_vol, 0), min_dollar_vol,
                f"{ticker} avg $ volume ${avg_dollar_vol:,.0f}/day "
                f"{'>=' if passed else '<'} min ${min_dollar_vol:,.0f}/day",
            )

    return results


def liquidity_ok(
    ticker: str,
    close_price: float,
    avg_vol_50: float,
    engine: RuleEngine,
    asset_type: str = "EQUITY",
) -> tuple[bool, Optional[str]]:
    """
    Convenience wrapper for hard-gate call sites that just need a pass/fail +
    reason, without juggling a RuleResult dict.

    Returns:
        (ok, reason) — reason is None when ok is True or the filter doesn't
        apply (e.g. crypto, rule disabled).
    """
    results = evaluate_liquidity(ticker, close_price, avg_vol_50, engine, asset_type)
    for rule_id, result in results.items():
        if not result.passed:
            return False, result.message
    return True, None
