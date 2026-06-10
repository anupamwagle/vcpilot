"""
Market Regime Filter — AstraTrade only trades in confirmed uptrending markets.

Rules:
  regime_index_above_200ma      — ASX200 price > 200-day MA
  regime_pct_stocks_above_200ma — % of ASX200 stocks above their 200MA ≥ 60%
  regime_distribution_days      — ≤ 4 distribution days in last 25 sessions
  regime_follow_through_day     — After correction: require a follow-through day

Regime states:
  BULL     — All criteria green, full trading allowed
  CAUTION  — Mixed signals, reduce size, no new entries
  BEAR     — Market in correction, close out / hold only, NO new entries
"""
from __future__ import annotations
import enum
from datetime import date, datetime
from typing import Optional
import pandas as pd
from loguru import logger

from app.screener.rules import RuleEngine, RuleResult
from app.database import get_db
from app.models.audit import AuditLog, AuditAction


class MarketRegime(str, enum.Enum):
    BULL    = "BULL"
    CAUTION = "CAUTION"
    BEAR    = "BEAR"


def evaluate_market_regime(
    index_df: pd.DataFrame,          # Exchange index daily OHLCV (^AXJO / ^GSPC / ^IXIC / BTC-USD)
    universe_df: pd.DataFrame,       # Stocks in same-exchange universe with close + ma_200
    engine: RuleEngine,
    exchange_key: str = "ASX",       # Which exchange this evaluation is for
) -> tuple[MarketRegime, dict[str, RuleResult]]:
    """
    Evaluate market health and determine trading regime for a specific exchange.
    Called once per exchange per evaluation cycle.

    Args:
        index_df:     DataFrame for the exchange's benchmark index, ascending date
        universe_df:  DataFrame with columns [ticker, close, ma_200] — same-exchange stocks
        engine:       RuleEngine instance
        exchange_key: "ASX", "NYSE", "NASDAQ", or "CRYPTO_*"

    Returns:
        (MarketRegime, dict of rule_id → RuleResult)

    Crypto-specific behaviour:
        Crypto has no breadth or distribution day concept. Only the index-above-200MA
        rule is evaluated (using BTC-USD as the regime proxy).
    """
    rule_results: dict[str, RuleResult] = {}
    criteria_passed = 0
    total_enabled = 0

    is_crypto = exchange_key.startswith("CRYPTO") if exchange_key else False
    index_label = {
        "ASX": "ASX200", "NYSE": "S&P500", "NASDAQ": "NASDAQ",
        "CRYPTO_BINANCE":            "BTC (USD)",
        "CRYPTO_KRAKEN":             "BTC (USD)",
        "CRYPTO_COINBASE":           "BTC (USD)",
        "CRYPTO_INDEPENDENTRESERVE": "BTC (AUD)",
    }.get(exchange_key or "ASX", exchange_key or "Index")

    # -------------------------------------------------------------------------
    # 1. Index price above 200MA
    # -------------------------------------------------------------------------
    rule_id = "regime_index_above_200ma"
    if engine.is_enabled(rule_id):
        total_enabled += 1
        latest = index_df.iloc[-1]
        close = float(latest.get("close", 0))
        ma200 = index_df["close"].tail(200).mean() if len(index_df) >= 200 else 0
        passed = close > ma200 > 0
        if passed:
            criteria_passed += 1
        rule_results[rule_id] = RuleResult(rule_id, passed, round(close, 2), round(ma200, 2),
            f"{index_label} {close:.2f} {'>' if passed else '<='} 200MA {ma200:.2f}")

    # -------------------------------------------------------------------------
    # 2. % of universe stocks above their 200MA (equity only — not crypto)
    # -------------------------------------------------------------------------
    rule_id = "regime_pct_stocks_above_200ma"
    if engine.is_enabled(rule_id) and not is_crypto:
        total_enabled += 1
        threshold = float(engine.threshold(rule_id) or 60.0)
        valid = universe_df.dropna(subset=["close", "ma_200"])
        if len(valid) > 0:
            above = (valid["close"] > valid["ma_200"]).sum()
            pct = (above / len(valid)) * 100
        else:
            pct = 0
        passed = pct >= threshold
        if passed:
            criteria_passed += 1
        rule_results[rule_id] = RuleResult(rule_id, passed, round(pct, 1), threshold,
            f"{pct:.1f}% of stocks above 200MA (min {threshold}%)")

    # -------------------------------------------------------------------------
    # 3. Distribution days check (equity only — crypto doesn't close down predictably)
    # -------------------------------------------------------------------------
    rule_id = "regime_distribution_days"
    if engine.is_enabled(rule_id) and not is_crypto:
        total_enabled += 1
        max_dist_days = int(engine.threshold(rule_id) or 4)
        lookback = 25
        if len(index_df) >= lookback + 1:
            recent = index_df.tail(lookback + 1).copy()
            recent["prev_close"] = recent["close"].shift(1)
            recent["prev_vol"] = recent["volume"].shift(1)
            # Distribution day: index closes down >0.2% on higher volume
            dist = recent[
                (recent["close"] < recent["prev_close"] * 0.998) &
                (recent["volume"] > recent["prev_vol"])
            ]
            dist_count = len(dist) - 1  # Exclude first row (NaN shift)
            dist_count = max(0, dist_count)
        else:
            dist_count = 0
        passed = dist_count <= max_dist_days
        if passed:
            criteria_passed += 1
        rule_results[rule_id] = RuleResult(rule_id, passed, dist_count, max_dist_days,
            f"{dist_count} distribution days in {lookback} sessions (max {max_dist_days})")

    # -------------------------------------------------------------------------
    # Determine regime
    # -------------------------------------------------------------------------
    if total_enabled == 0:
        regime = MarketRegime.BULL  # No rules = no filter
    elif criteria_passed == total_enabled:
        regime = MarketRegime.BULL
    elif criteria_passed >= total_enabled - 1:
        regime = MarketRegime.CAUTION
    else:
        regime = MarketRegime.BEAR

    logger.info(f"[{exchange_key}] Market regime: {regime} ({criteria_passed}/{total_enabled} criteria passed)")
    return regime, rule_results


def is_trading_allowed(regime: MarketRegime) -> bool:
    """New entries only allowed in BULL regime."""
    return regime == MarketRegime.BULL


def get_size_multiplier(regime: MarketRegime) -> float:
    """Scale position size based on market regime.
    BULL=100%, CAUTION=50%, BEAR=0%
    """
    if regime == MarketRegime.BULL:
        return 1.0
    if regime == MarketRegime.CAUTION:
        return 0.5
    return 0.0
