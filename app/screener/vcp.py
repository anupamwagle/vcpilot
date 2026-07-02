"""
VCP — Volatility Contraction Pattern Detection.

VCP characteristics (AstraTrade methodology):
  - 3–4 successive price contractions (each tighter than the last)
  - Volume decreases on each contraction (dries up to lowest point)
  - Final contraction is the tightest (pivot area)
  - Pivot buy point = high of the final tight contraction
  - Entry on breakout above pivot with volume ≥ 150% of avg

Rules encoded:
  vcp_min_contractions  — Minimum contraction count (default 3)
  vcp_max_weeks         — Max base length in weeks (default 52)
  vcp_min_weeks         — Min base length in weeks (default 3)
  vcp_volume_dry_up     — Volume on final contraction < 50% of avg
  vcp_breakout_volume   — Breakout volume ≥ 150% of 50-day avg
  vcp_max_extension     — Price must be within N% of pivot (default 5%)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd
from loguru import logger
from app.screener.rules import RuleEngine, RuleResult


@dataclass
class VCPResult:
    detected: bool = False
    pivot_price: Optional[float] = None
    stop_price: Optional[float] = None       # Low of final contraction
    contraction_count: int = 0
    base_weeks: int = 0
    volume_dried_up: bool = False
    final_contraction_pct: Optional[float] = None
    detail: dict = field(default_factory=dict)


def detect_vcp(
    ticker: str,
    df: pd.DataFrame,
    engine: RuleEngine,
    avg_vol_50: Optional[float] = None,
) -> tuple[VCPResult, dict[str, RuleResult]]:
    """
    Detect VCP pattern in the provided DataFrame.

    Args:
        ticker:      Stock ticker
        df:          OHLCV DataFrame, ascending date, minimum 60 rows
        engine:      RuleEngine instance
        avg_vol_50:  50-day average volume (if None, computed from df)

    Returns:
        (VCPResult, dict of rule_id → RuleResult)
    """
    rule_results: dict[str, RuleResult] = {}
    vcp = VCPResult()

    if len(df) < 15:
        return vcp, rule_results

    # Thresholds from config
    min_contractions = int(engine.threshold("vcp_min_contractions") or 3)
    max_weeks = int(engine.threshold("vcp_max_weeks") or 52)
    min_weeks = int(engine.threshold("vcp_min_weeks") or 3)
    vol_dry_up_pct = float(engine.threshold("vcp_volume_dry_up") or 50.0)
    breakout_vol_pct = float(engine.threshold("vcp_breakout_volume") or 150.0)

    if avg_vol_50 is None:
        avg_vol_50 = df["volume"].tail(50).mean()

    # Use last max_weeks * 5 trading days as the analysis window
    lookback_bars = min(max_weeks * 5, len(df))
    window = df.tail(lookback_bars).copy()

    # Find pivot highs and lows using a simple swing detection
    highs = window["high"].values
    lows  = window["low"].values

    # Identify local swing highs (peaks) and lows (troughs)
    win = 3 if len(df) < 60 else 5
    pivot_highs = _find_pivots(highs, direction="high", window=win)
    pivot_lows  = _find_pivots(lows,  direction="low",  window=win)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        rule_results["vcp_min_contractions"] = RuleResult(
            "vcp_min_contractions", False, 0, min_contractions, "Insufficient pivot points"
        )
        return vcp, rule_results

    # Calculate % contraction between each successive high and low pair.
    # IMPORTANT: pivot_highs and pivot_lows are detected independently (separate
    # scans over the highs/lows series), so they are NOT guaranteed to line up
    # index-for-index as the same physical swing. Zipping them by raw list
    # position (the old approach) could pair a high from one swing with a low
    # from an unrelated, later swing — and since price generally trends upward
    # over the life of a base, that later low can sit ABOVE the mismatched
    # high, producing a "contraction" with stop_price > pivot_price (the VMC
    # bug). Instead, pair each swing high with the next swing low that
    # actually follows it in time, which is what a real peak→trough
    # contraction means.
    contractions: list[dict] = []
    for h_idx in pivot_highs:
        candidate_lows = [l_idx for l_idx in pivot_lows if l_idx > h_idx]
        if not candidate_lows:
            continue
        low_idx = min(candidate_lows)
        high_val = highs[h_idx]
        low_val  = lows[low_idx]
        if high_val <= low_val:
            # Defensive guard — a valid contraction must have the high above
            # the low. Skip anything that doesn't (shouldn't happen once
            # pairing is chronological, but never trust it blindly).
            continue
        contraction_pct = ((high_val - low_val) / high_val) * 100 if high_val > 0 else 0
        contractions.append({
            "high_idx": h_idx,
            "low_idx": low_idx,
            "high_val": high_val,
            "low_val": low_val,
            "contraction_pct": contraction_pct,
        })

    # Filter: each contraction must be tighter than the previous
    valid_contractions = [contractions[0]] if contractions else []
    for i in range(1, len(contractions)):
        if contractions[i]["contraction_pct"] < valid_contractions[-1]["contraction_pct"]:
            valid_contractions.append(contractions[i])
        else:
            # Break in tightening — reset
            if len(contractions[i:]) >= min_contractions:
                valid_contractions = [contractions[i]]
            else:
                break

    contraction_count = len(valid_contractions)

    # -------------------------------------------------------------------------
    # Rule: Minimum contractions
    # -------------------------------------------------------------------------
    rule_id = "vcp_min_contractions"
    if engine.is_enabled(rule_id):
        passed = contraction_count >= min_contractions
        rule_results[rule_id] = RuleResult(rule_id, passed, contraction_count, min_contractions,
            f"{contraction_count} contractions detected (min {min_contractions})")

    if contraction_count < min_contractions:
        return vcp, rule_results

    # Use the last valid contraction as the current pivot area
    last_c = valid_contractions[-1]
    pivot_price = last_c["high_val"]
    stop_price  = last_c["low_val"]
    final_contraction_pct = last_c["contraction_pct"]

    # -------------------------------------------------------------------------
    # Rule: Base length in weeks
    # -------------------------------------------------------------------------
    rule_id = "vcp_base_weeks"
    if engine.is_enabled(rule_id):
        base_bars = last_c["high_idx"] - valid_contractions[0]["high_idx"]
        base_weeks = base_bars // 5
        passed = min_weeks <= base_weeks <= max_weeks
        rule_results[rule_id] = RuleResult(rule_id, passed, base_weeks, f"{min_weeks}–{max_weeks}",
            f"Base {base_weeks} weeks (range {min_weeks}–{max_weeks})")
        vcp.base_weeks = base_weeks

    # -------------------------------------------------------------------------
    # Rule: Volume dry-up on final contraction
    # -------------------------------------------------------------------------
    rule_id = "vcp_volume_dry_up"
    if engine.is_enabled(rule_id):
        final_area_vol = window["volume"].iloc[last_c["low_idx"]:last_c["low_idx"]+5].mean()
        dry_up_ratio = (final_area_vol / avg_vol_50 * 100) if avg_vol_50 > 0 else 100
        passed = dry_up_ratio <= vol_dry_up_pct
        rule_results[rule_id] = RuleResult(rule_id, passed, round(dry_up_ratio, 1), vol_dry_up_pct,
            f"Final vol {dry_up_ratio:.0f}% of avg (max {vol_dry_up_pct}%)")
        vcp.volume_dried_up = passed

    # Populate result
    vcp.detected = all(r.passed for r in rule_results.values())
    vcp.pivot_price = pivot_price
    vcp.stop_price = stop_price
    vcp.contraction_count = contraction_count
    vcp.final_contraction_pct = final_contraction_pct
    # Map pivot indices back to calendar dates when the window carries a "date"
    # column (it does when the df is built from PriceBar rows). This lets the
    # Stock Story plot each contraction leg on the price timeline. Additive —
    # the pct/high/low keys are preserved for existing callers/tests.
    _dates = window["date"].tolist() if "date" in window.columns else None
    def _leg_date(idx):
        if _dates is None:
            return None
        try:
            return str(_dates[idx])[:10]
        except Exception:
            return None

    vcp.detail = {
        "contractions": [
            {"pct": round(c["contraction_pct"], 2), "high": c["high_val"], "low": c["low_val"],
             "high_date": _leg_date(c["high_idx"]), "low_date": _leg_date(c["low_idx"])}
            for c in valid_contractions
        ]
    }

    return vcp, rule_results


def resolve_watchlist_geometry(vcp_result, *, close=0.0, high_52w=0.0, atr_14=0.0) -> dict:
    """
    Single source of truth for a watchlist item's displayed VCP geometry
    (pivot / stop / target / contractions / base weeks).

    When `detect_vcp` produced a real pivot we use it directly; otherwise we apply
    the same fallback the dashboard historically computed inline: pivot = 52-week
    high (or last close), stop = pivot − 2·ATR (or −8%), target = pivot · 1.20.

    Pure function — no DB / network — so it can run in the screener (to persist the
    result on the Watchlist row) and in the dashboard (lazy fallback) identically.
    Returns a dict with the 5 geometry keys (values may be None when no price data).
    """
    pivot = float(vcp_result.pivot_price) if (vcp_result and vcp_result.pivot_price) else 0.0
    stop = float(vcp_result.stop_price) if (vcp_result and vcp_result.stop_price) else 0.0
    contractions = int(vcp_result.contraction_count) if vcp_result else 0
    base_weeks = int(vcp_result.base_weeks) if vcp_result else 0

    if not pivot:
        high_52w = float(high_52w or 0)
        close = float(close or 0)
        pivot = high_52w if high_52w > 0 else close
        atr = float(atr_14 or 0)
        stop = (pivot - 2 * atr) if atr > 0 else pivot * 0.92
        if stop <= 0 or stop >= pivot:
            stop = pivot * 0.92
        contractions = 0
        base_weeks = 0

    target = pivot * 1.20 if pivot else None
    return {
        "pivot_price": pivot or None,
        "stop_price": stop or None,
        "target_price": target,
        "vcp_contractions": contractions,
        "vcp_base_weeks": base_weeks,
    }


def check_breakout(
    ticker: str,
    df: pd.DataFrame,
    pivot_price: float,
    avg_vol_50: float,
    engine: RuleEngine,
) -> dict[str, RuleResult]:
    """
    Check if today's bar is a valid breakout from the VCP pivot.
    Called intraday (or on latest EOD bar) when we have an active signal.
    """
    rule_results: dict[str, RuleResult] = {}
    latest = df.iloc[-1]
    close = float(latest["close"])
    today_vol = float(latest["volume"])

    # -------------------------------------------------------------------------
    # Price at/above pivot
    # -------------------------------------------------------------------------
    rule_id = "vcp_breakout_price"
    if engine.is_enabled(rule_id):
        max_extension = float(engine.threshold("vcp_max_extension") or 5.0)
        pct_above_pivot = ((close - pivot_price) / pivot_price * 100) if pivot_price > 0 else -100
        passed = 0 <= pct_above_pivot <= max_extension
        rule_results[rule_id] = RuleResult(rule_id, passed, round(pct_above_pivot, 2), max_extension,
            f"Close {pct_above_pivot:.1f}% above pivot {pivot_price:.3f} (max {max_extension}%)")

    # -------------------------------------------------------------------------
    # Breakout volume ≥ 150% of avg
    # -------------------------------------------------------------------------
    rule_id = "vcp_breakout_volume"
    if engine.is_enabled(rule_id):
        projected_vol = today_vol
        try:
            from datetime import datetime
            import pytz
            
            is_crypto = ticker.endswith("-USD") or ticker.endswith("-USDT") or ticker.endswith("-AUD")
            if not is_crypto:
                if ticker.endswith(".AX"):
                    tz = pytz.timezone("Australia/Sydney")
                    open_h, open_m = 10, 0
                    close_h, close_m = 16, 12
                else:
                    tz = pytz.timezone("America/New_York")
                    open_h, open_m = 9, 30
                    close_h, close_m = 16, 0

                now = datetime.now(tz)
                open_mins = open_h * 60 + open_m
                close_mins = close_h * 60 + close_m
                current_mins = now.hour * 60 + now.minute
                
                if open_mins < current_mins < close_mins:
                    elapsed_mins = current_mins - open_mins
                    total_mins = close_mins - open_mins
                    # linearly project volume for the remainder of the day
                    projected_vol = today_vol * (total_mins / max(1, elapsed_mins))
        except Exception:
            pass

        threshold = float(engine.threshold(rule_id) or 150.0)
        vol_ratio = (projected_vol / avg_vol_50 * 100) if avg_vol_50 > 0 else 0
        passed = vol_ratio >= threshold
        
        lbl = "Projected Vol" if projected_vol > today_vol else "Volume"
        rule_results[rule_id] = RuleResult(rule_id, passed, round(vol_ratio, 1), threshold,
            f"{lbl} {vol_ratio:.0f}% of avg (min {threshold}%)")

    return rule_results


def _find_pivots(values: np.ndarray, direction: str = "high", window: int = 5) -> list[int]:
    """Find indices of local pivot highs or lows."""
    pivots = []
    half = window // 2
    for i in range(half, len(values) - half):
        neighbourhood = values[i - half: i + half + 1]
        if direction == "high" and values[i] == max(neighbourhood):
            pivots.append(i)
        elif direction == "low" and values[i] == min(neighbourhood):
            pivots.append(i)
    return pivots
