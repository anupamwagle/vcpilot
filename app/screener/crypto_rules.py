"""
Crypto-specific Minervini rule evaluators.
These rules supplement (not replace) the standard trend template + VCP rules
for crypto assets. Fundamental rules are skipped entirely for crypto.

Rule applicability (asset_types="CRYPTO"):
  crypto_btc_regime         — BTC-USD above 50MA (market direction proxy)
  crypto_market_cap_min     — minimum $100M market cap
  crypto_volume_min_24h     — minimum $5M 24h volume
  crypto_stop_width_pct     — minimum stop distance from pivot (10%)
  crypto_max_risk_pct       — max risk per trade override (1% vs equity 2%)
  crypto_vcp_contraction_pct— minimum VCP contraction depth (15%)
"""
from __future__ import annotations
import pandas as pd
from typing import Optional
from loguru import logger

from app.screener.rules import RuleEngine, RuleResult


def evaluate_crypto_rules(
    ticker: str,
    df: pd.DataFrame,           # price history for the asset (sorted ascending)
    engine: RuleEngine,
    market_cap_usd: Optional[float] = None,
    volume_24h_usd: Optional[float] = None,
    btc_df: Optional[pd.DataFrame] = None,  # BTC-USD price history (optional)
) -> dict[str, RuleResult]:
    """
    Evaluate all enabled CRYPTO-specific rules for a given asset.

    Args:
        ticker:         yfinance ticker (e.g. "BTC-USD", "ETH-USD")
        df:             Price history DataFrame for this asset
        engine:         RuleEngine instance (with asset_type="CRYPTO")
        market_cap_usd: Market cap in USD (from yfinance info)
        volume_24h_usd: 24h trading volume in USD (from yfinance info)
        btc_df:         Optional BTC-USD price history for regime check

    Returns:
        dict of rule_id → RuleResult
    """
    results: dict[str, RuleResult] = {}

    # -------------------------------------------------------------------------
    # 1. BTC Regime — BTC above its 50MA
    # -------------------------------------------------------------------------
    rule_id = "crypto_btc_regime"
    if engine.is_enabled(rule_id):
        if btc_df is not None and not btc_df.empty and len(btc_df) >= 50:
            btc_close  = float(btc_df["close"].iloc[-1])
            btc_ma50   = float(btc_df["close"].tail(50).mean())
            passed = btc_close > btc_ma50
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(btc_close, 2),
                threshold=round(btc_ma50, 2),
                message=f"BTC {btc_close:,.0f} {'above' if passed else 'below'} 50MA {btc_ma50:,.0f}"
            )
        elif ticker == "BTC-USD":
            # Self-check: BTC checking its own regime
            if len(df) >= 50:
                close = float(df["close"].iloc[-1])
                ma50  = float(df["close"].tail(50).mean())
                passed = close > ma50
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(close, 2), threshold=round(ma50, 2),
                    message=f"BTC {close:,.0f} {'above' if passed else 'below'} 50MA {ma50:,.0f}"
                )
        else:
            # BTC data not available — skip with a neutral pass (don't block)
            results[rule_id] = RuleResult(
                rule_id, True,
                message="BTC regime data unavailable — skipped"
            )

    # -------------------------------------------------------------------------
    # 2. Minimum market cap
    # -------------------------------------------------------------------------
    rule_id = "crypto_market_cap_min"
    if engine.is_enabled(rule_id):
        min_cap = float(engine.threshold(rule_id) or 100_000_000)

        # BTC and ETH always pass — they're blue-chip crypto
        if ticker in ("BTC-USD", "ETH-USD"):
            results[rule_id] = RuleResult(
                rule_id, True,
                value=None, threshold=min_cap,
                message=f"{ticker} is a major asset — market cap check waived"
            )
        elif market_cap_usd is not None:
            passed = market_cap_usd >= min_cap
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(market_cap_usd),
                threshold=min_cap,
                message=f"Market cap ${market_cap_usd/1e6:.0f}M {'≥' if passed else '<'} min ${min_cap/1e6:.0f}M"
            )
        else:
            # Market cap unavailable — skip (don't block; logged as unknown)
            results[rule_id] = RuleResult(
                rule_id, True,
                message="Market cap data unavailable — skipped"
            )

    # -------------------------------------------------------------------------
    # 3. Minimum 24h volume
    # -------------------------------------------------------------------------
    rule_id = "crypto_volume_min_24h"
    if engine.is_enabled(rule_id):
        min_vol = float(engine.threshold(rule_id) or 5_000_000)

        if ticker in ("BTC-USD", "ETH-USD"):
            results[rule_id] = RuleResult(
                rule_id, True,
                message=f"{ticker} blue-chip — volume check waived"
            )
        elif volume_24h_usd is not None:
            passed = volume_24h_usd >= min_vol
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(volume_24h_usd),
                threshold=min_vol,
                message=f"24h vol ${volume_24h_usd/1e6:.1f}M {'≥' if passed else '<'} min ${min_vol/1e6:.1f}M"
            )
        else:
            # Estimate from price × daily volume if raw USD volume unavailable
            if not df.empty and "volume" in df.columns and "close" in df.columns:
                last = df.iloc[-1]
                approx_vol_usd = float(last.get("volume", 0)) * float(last.get("close", 0))
                passed = approx_vol_usd >= min_vol
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(approx_vol_usd),
                    threshold=min_vol,
                    message=f"Est. 24h vol ${approx_vol_usd/1e6:.1f}M (price × vol) {'≥' if passed else '<'} min ${min_vol/1e6:.1f}M"
                )
            else:
                results[rule_id] = RuleResult(
                    rule_id, True,
                    message="24h volume data unavailable — skipped"
                )

    # -------------------------------------------------------------------------
    # 4. Minimum stop distance from pivot
    # (Informational rule — not a hard pass/fail for screening, just a warning)
    # The actual stop distance is enforced in the risk manager.
    # We still evaluate it here so it shows up in the rule results panel.
    # -------------------------------------------------------------------------
    rule_id = "crypto_stop_width_pct"
    if engine.is_enabled(rule_id):
        min_stop_pct = float(engine.threshold(rule_id) or 10.0)
        # Check current ATR as % of price — proxy for whether stops will be wide enough
        if not df.empty and "atr_14" in df.columns and "close" in df.columns:
            close = float(df["close"].iloc[-1])
            atr   = float(df["atr_14"].iloc[-1] or 0)
            if close > 0 and atr > 0:
                atr_pct = (atr / close) * 100
                # ATR-based stop (1.5× ATR) should be ≥ min_stop_pct
                stop_estimate = atr_pct * 1.5
                passed = stop_estimate >= min_stop_pct
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(stop_estimate, 1),
                    threshold=min_stop_pct,
                    message=f"Est. stop {stop_estimate:.1f}% (1.5×ATR) {'≥' if passed else '<'} min {min_stop_pct:.0f}%"
                )
            else:
                results[rule_id] = RuleResult(rule_id, True, message="ATR not available — skipped")
        else:
            results[rule_id] = RuleResult(rule_id, True, message="ATR data unavailable — skipped")

    # -------------------------------------------------------------------------
    # 5. Max risk per trade (informational — used by risk manager)
    # Screener always passes this; risk manager applies the actual cap.
    # -------------------------------------------------------------------------
    rule_id = "crypto_max_risk_pct"
    if engine.is_enabled(rule_id):
        cap = float(engine.threshold(rule_id) or 1.0)
        results[rule_id] = RuleResult(
            rule_id, True,
            value=cap, threshold=cap,
            message=f"Crypto risk cap {cap}% applied at position sizing"
        )

    # -------------------------------------------------------------------------
    # 6. VCP contraction depth for crypto
    # -------------------------------------------------------------------------
    rule_id = "crypto_vcp_contraction_pct"
    if engine.is_enabled(rule_id):
        min_depth = float(engine.threshold(rule_id) or 15.0)
        # We check recent price range: if the last significant swing high-to-low
        # within the base is at least min_depth%, that's a meaningful contraction.
        if len(df) >= 20:
            recent = df.tail(40)  # Last 40 bars = ~6 weeks for daily data
            high  = float(recent["high"].max())
            low   = float(recent["low"].min())
            if high > 0:
                depth_pct = ((high - low) / high) * 100
                passed = depth_pct >= min_depth
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(depth_pct, 1),
                    threshold=min_depth,
                    message=f"Contraction depth {depth_pct:.1f}% {'≥' if passed else '<'} min {min_depth:.0f}%"
                )
            else:
                results[rule_id] = RuleResult(rule_id, True, message="Price data insufficient — skipped")
        else:
            results[rule_id] = RuleResult(rule_id, True, message="Insufficient history for contraction check")

    # -------------------------------------------------------------------------
    # 7. RSI(14) > 50 — momentum confirmation (price must be in upward momentum)
    # -------------------------------------------------------------------------
    rule_id = "crypto_rsi_momentum"
    if engine.is_enabled(rule_id):
        rsi_threshold = float(engine.threshold(rule_id) or 50.0)
        if len(df) >= 15:
            # Calculate RSI(14) from price series
            delta = df["close"].diff()
            gain  = delta.clip(lower=0)
            loss  = (-delta).clip(lower=0)
            avg_gain = gain.ewm(com=13, adjust=False).mean()
            avg_loss = loss.ewm(com=13, adjust=False).mean()
            rs   = avg_gain / avg_loss.replace(0, float("nan"))
            rsi  = 100 - (100 / (1 + rs))
            rsi_val = float(rsi.iloc[-1]) if not rsi.empty else None
            if rsi_val is not None and not pd.isna(rsi_val):
                passed = rsi_val >= rsi_threshold
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(rsi_val, 1),
                    threshold=rsi_threshold,
                    message=f"RSI(14) {rsi_val:.1f} {'≥' if passed else '<'} {rsi_threshold:.0f} (momentum {'✓' if passed else '✗'})"
                )
            else:
                results[rule_id] = RuleResult(rule_id, True, message="RSI could not be computed — skipped")
        else:
            results[rule_id] = RuleResult(rule_id, True, message="Insufficient history for RSI — skipped")

    # -------------------------------------------------------------------------
    # 8. MACD bullish cross — 12/26/9 EMA MACD above signal line
    # -------------------------------------------------------------------------
    rule_id = "crypto_macd_bullish"
    if engine.is_enabled(rule_id):
        if len(df) >= 35:
            ema12 = df["close"].ewm(span=12, adjust=False).mean()
            ema26 = df["close"].ewm(span=26, adjust=False).mean()
            macd  = ema12 - ema26
            signal_line = macd.ewm(span=9, adjust=False).mean()
            macd_val    = float(macd.iloc[-1])
            sig_val     = float(signal_line.iloc[-1])
            macd_prev   = float(macd.iloc[-2]) if len(macd) >= 2 else macd_val
            sig_prev    = float(signal_line.iloc[-2]) if len(signal_line) >= 2 else sig_val
            # Require MACD above signal line AND positive histogram
            above_signal = macd_val > sig_val
            histogram_pos = (macd_val - sig_val) > 0
            passed = above_signal and histogram_pos
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(macd_val - sig_val, 4),
                threshold=0,
                message=f"MACD histogram {macd_val-sig_val:+.4f} ({'bullish ✓' if passed else 'bearish ✗'})"
            )
        else:
            results[rule_id] = RuleResult(rule_id, True, message="Insufficient history for MACD — skipped")

    # -------------------------------------------------------------------------
    # 9. Volume surge — recent volume > N× the 20-day average (breakout confirmation)
    # -------------------------------------------------------------------------
    rule_id = "crypto_volume_surge"
    if engine.is_enabled(rule_id):
        surge_threshold = float(engine.threshold(rule_id) or 1.5)
        if len(df) >= 21 and "volume" in df.columns:
            vol_20d_avg = float(df["volume"].tail(21).iloc[:-1].mean())  # last 20 bars excl. today
            vol_today   = float(df["volume"].iloc[-1])
            if vol_20d_avg > 0:
                ratio = vol_today / vol_20d_avg
                passed = ratio >= surge_threshold
                results[rule_id] = RuleResult(
                    rule_id, passed,
                    value=round(ratio, 2),
                    threshold=surge_threshold,
                    message=f"Volume {ratio:.2f}× 20-day avg ({'surge ✓' if passed else 'below threshold ✗'})"
                )
            else:
                results[rule_id] = RuleResult(rule_id, True, message="20-day volume avg is zero — skipped")
        else:
            results[rule_id] = RuleResult(rule_id, True, message="Insufficient history for volume surge — skipped")

    # -------------------------------------------------------------------------
    # 10. Minimum risk/reward ratio — pivot to target must be ≥ N× stop distance
    # -------------------------------------------------------------------------
    rule_id = "crypto_min_rr_ratio"
    if engine.is_enabled(rule_id):
        min_rr = float(engine.threshold(rule_id) or 2.5)
        if not df.empty and len(df) >= 20:
            close = float(df["close"].iloc[-1])
            # Estimate stop: 1.5× ATR below current price (or 10% as minimum)
            atr   = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns and df["atr_14"].iloc[-1] else close * 0.10
            est_stop   = close - (1.5 * atr)
            risk_dist  = close - est_stop
            # Target: 20% above current price (Minervini first target)
            target_dist = close * 0.20
            rr_ratio = target_dist / risk_dist if risk_dist > 0 else 0
            passed = rr_ratio >= min_rr
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(rr_ratio, 2),
                threshold=min_rr,
                message=f"R/R {rr_ratio:.2f}:1 ({'meets' if passed else 'below'} min {min_rr:.1f}:1)"
            )
        else:
            results[rule_id] = RuleResult(rule_id, True, message="Insufficient data for R/R check — skipped")

    # -------------------------------------------------------------------------
    # 11. BTC relative strength — non-BTC assets should outperform BTC over 50 days
    # -------------------------------------------------------------------------
    rule_id = "crypto_btc_relative_strength"
    if engine.is_enabled(rule_id):
        if ticker not in ("BTC-AUD", "BTC-USD") and btc_df is not None and not btc_df.empty and len(btc_df) >= 50 and len(df) >= 50:
            # RS = (asset 50d return) / (BTC 50d return). Above 1.0 = outperforming BTC.
            asset_ret = float(df["close"].iloc[-1]) / float(df["close"].iloc[-50]) - 1
            btc_ret   = float(btc_df["close"].iloc[-1]) / float(btc_df["close"].iloc[-50]) - 1
            rs_vs_btc = (asset_ret - btc_ret) * 100  # percentage outperformance
            min_rs_pct = float(engine.threshold(rule_id) or 0.0)  # default: must at least match BTC
            passed = rs_vs_btc >= min_rs_pct
            results[rule_id] = RuleResult(
                rule_id, passed,
                value=round(rs_vs_btc, 1),
                threshold=min_rs_pct,
                message=f"50d RS vs BTC: {rs_vs_btc:+.1f}% ({'outperforming ✓' if passed else 'underperforming ✗'})"
            )
        else:
            results[rule_id] = RuleResult(rule_id, True, message="BTC RS check skipped (BTC, or no BTC data)")

    return results


def get_crypto_fundamental_data(ticker: str) -> dict:
    """
    Fetch crypto-specific market data from yfinance (market cap, 24h volume).
    Returns: {market_cap_usd, volume_24h_usd, coin_name}
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return {
            "market_cap_usd": info.get("marketCap"),
            "volume_24h_usd": info.get("volume24Hr") or info.get("regularMarketVolume"),
            "coin_name":      info.get("longName") or info.get("shortName") or "",
        }
    except Exception as e:
        logger.debug(f"Crypto data fetch failed for {ticker}: {e}")
        return {"market_cap_usd": None, "volume_24h_usd": None, "coin_name": ""}
