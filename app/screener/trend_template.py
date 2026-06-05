"""
Minervini Trend Template — 8 criteria (all must pass for Stage 2 confirmation).

Rules encoded here (loaded from DB for enable/disable and threshold tuning):
  1. price_above_200ma      — Price > 200-day MA
  2. price_above_150ma      — Price > 150-day MA
  3. ma150_above_ma200      — 150MA > 200MA
  4. ma200_trending_up      — 200MA is higher today than N days ago (default 21)
  5. ma50_above_ma150_200   — 50MA > 150MA AND 50MA > 200MA
  6. price_above_ma50       — Price > 50-day MA
  7. pct_above_52w_low      — Price ≥ 30% above 52-week low
  8. pct_below_52w_high     — Price ≤ 25% below 52-week high
  +  rs_rating_min          — Relative Strength ≥ 70 (not in template but mandatory)
"""
from __future__ import annotations
import pandas as pd
from loguru import logger
from app.screener.rules import RuleEngine, RuleResult


def evaluate_trend_template(ticker: str, df: pd.DataFrame, engine: RuleEngine) -> dict[str, RuleResult]:
    """
    Evaluate all trend template rules against the latest price bar.

    Args:
        ticker: ASX ticker (e.g. "BHP.AX")
        df:     DataFrame with columns: close, ma_50, ma_150, ma_200, high_52w, low_52w, rs_rating
                Sorted ascending by date; latest row = df.iloc[-1]
        engine: RuleEngine instance with loaded rules

    Returns:
        Dict of rule_id → RuleResult
    """
    results: dict[str, RuleResult] = {}
    row = df.iloc[-1]
    close = float(row["close"])

    # -------------------------------------------------------------------------
    # 1. Price > 200MA
    # -------------------------------------------------------------------------
    rule_id = "trend_price_above_200ma"
    if engine.is_enabled(rule_id):
        ma200 = float(row.get("ma_200", 0) or 0)
        passed = close > ma200 and ma200 > 0
        results[rule_id] = RuleResult(rule_id, passed, close, ma200,
            f"Close {close:.3f} {'>' if passed else '<='} 200MA {ma200:.3f}")

    # -------------------------------------------------------------------------
    # 2. Price > 150MA
    # -------------------------------------------------------------------------
    rule_id = "trend_price_above_150ma"
    if engine.is_enabled(rule_id):
        ma150 = float(row.get("ma_150", 0) or 0)
        passed = close > ma150 and ma150 > 0
        results[rule_id] = RuleResult(rule_id, passed, close, ma150,
            f"Close {close:.3f} {'>' if passed else '<='} 150MA {ma150:.3f}")

    # -------------------------------------------------------------------------
    # 3. 150MA > 200MA
    # -------------------------------------------------------------------------
    rule_id = "trend_ma150_above_ma200"
    if engine.is_enabled(rule_id):
        ma150 = float(row.get("ma_150", 0) or 0)
        ma200 = float(row.get("ma_200", 0) or 0)
        passed = ma150 > ma200 > 0
        results[rule_id] = RuleResult(rule_id, passed, ma150, ma200,
            f"150MA {ma150:.3f} {'>' if passed else '<='} 200MA {ma200:.3f}")

    # -------------------------------------------------------------------------
    # 4. 200MA trending up (slope check over N days)
    # -------------------------------------------------------------------------
    rule_id = "trend_ma200_trending_up"
    if engine.is_enabled(rule_id):
        lookback = int(engine.threshold(rule_id) or 21)  # configurable, default 21 days
        if len(df) >= lookback + 1:
            ma200_now  = float(df.iloc[-1].get("ma_200", 0) or 0)
            ma200_prev = float(df.iloc[-lookback].get("ma_200", 0) or 0)
            passed = ma200_now > ma200_prev > 0
            results[rule_id] = RuleResult(rule_id, passed, ma200_now, ma200_prev,
                f"200MA {ma200_now:.3f} {'>' if passed else '<='} {lookback}d ago {ma200_prev:.3f}")
        else:
            results[rule_id] = RuleResult(rule_id, False, None, None, "Insufficient data")

    # -------------------------------------------------------------------------
    # 5. 50MA > 150MA AND 50MA > 200MA
    # -------------------------------------------------------------------------
    rule_id = "trend_ma50_above_ma150_200"
    if engine.is_enabled(rule_id):
        ma50  = float(row.get("ma_50", 0) or 0)
        ma150 = float(row.get("ma_150", 0) or 0)
        ma200 = float(row.get("ma_200", 0) or 0)
        passed = ma50 > ma150 and ma50 > ma200 and ma50 > 0
        results[rule_id] = RuleResult(rule_id, passed, ma50, None,
            f"50MA {ma50:.3f} vs 150MA {ma150:.3f} / 200MA {ma200:.3f}")

    # -------------------------------------------------------------------------
    # 6. Price > 50MA
    # -------------------------------------------------------------------------
    rule_id = "trend_price_above_ma50"
    if engine.is_enabled(rule_id):
        ma50 = float(row.get("ma_50", 0) or 0)
        passed = close > ma50 > 0
        results[rule_id] = RuleResult(rule_id, passed, close, ma50,
            f"Close {close:.3f} {'>' if passed else '<='} 50MA {ma50:.3f}")

    # -------------------------------------------------------------------------
    # 7. Price ≥ 30% above 52-week low
    # -------------------------------------------------------------------------
    rule_id = "trend_pct_above_52w_low"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 30.0)
        low_52w = float(row.get("low_52w", 0) or 0)
        pct_above = ((close - low_52w) / low_52w * 100) if low_52w > 0 else 0
        passed = pct_above >= threshold
        results[rule_id] = RuleResult(rule_id, passed, round(pct_above, 2), threshold,
            f"{pct_above:.1f}% above 52w low (min {threshold}%)")

    # -------------------------------------------------------------------------
    # 8. Price within 25% of 52-week high
    # -------------------------------------------------------------------------
    rule_id = "trend_pct_below_52w_high"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 25.0)
        high_52w = float(row.get("high_52w", 0) or 0)
        pct_below = ((high_52w - close) / high_52w * 100) if high_52w > 0 else 100
        passed = pct_below <= threshold
        results[rule_id] = RuleResult(rule_id, passed, round(pct_below, 2), threshold,
            f"{pct_below:.1f}% below 52w high (max {threshold}%)")

    # -------------------------------------------------------------------------
    # RS Rating (Relative Strength vs ASX200)
    # -------------------------------------------------------------------------
    rule_id = "trend_rs_rating_min"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 70.0)
        rs = float(row.get("rs_rating", 0) or 0)
        if rs == 0:
            try:
                from app.database import get_db
                from app.models.market import PriceBar
                from sqlalchemy import desc
                with get_db() as db:
                    bar = db.query(PriceBar).filter(PriceBar.ticker == ticker).order_by(desc(PriceBar.date)).first()
                    if bar and bar.rs_rating:
                        rs = float(bar.rs_rating)
            except Exception as e:
                logger.warning(f"Failed to fetch rs_rating from DB for {ticker}: {e}")
        passed = rs >= threshold
        results[rule_id] = RuleResult(rule_id, passed, rs, threshold,
            f"RS {rs:.1f} {'≥' if passed else '<'} {threshold}")

    return results
