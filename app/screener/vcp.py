"""
VCP — Volatility Contraction Pattern Detection.

Minervini's VCP characteristics:
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

    if len(df) < 60:
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
    pivot_highs = _find_pivots(highs, direction="high", window=5)
    pivot_lows  = _find_pivots(lows,  direction="low",  window=5)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        rule_results["vcp_min_contractions"] = RuleResult(
            "vcp_min_contractions", False, 0, min_contractions, "Insufficient pivot points"
        )
        return vcp, rule_results

    # Calculate % contraction between each successive high and low pair
    contractions: list[dict] = []
    for i in range(min(len(pivot_highs) - 1, len(pivot_lows) - 1)):
        high_val = highs[pivot_highs[i]]
        low_val  = lows[pivot_lows[i]]
        contraction_pct = ((high_val - low_val) / high_val) * 100 if high_val > 0 else 0
        contractions.append({
            "high_idx": pivot_highs[i],
            "low_idx": pivot_lows[i],
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
    vcp.detail = {
        "contractions": [
            {"pct": round(c["contraction_pct"], 2), "high": c["high_val"], "low": c["low_val"]}
            for c in valid_contractions
        ]
    }

    return vcp, rule_results


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
        threshold = float(engine.threshold(rule_id) or 150.0)
        vol_ratio = (today_vol / avg_vol_50 * 100) if avg_vol_50 > 0 else 0
        passed = vol_ratio >= threshold
        rule_results[rule_id] = RuleResult(rule_id, passed, round(vol_ratio, 1), threshold,
            f"Volume {vol_ratio:.0f}% of avg (min {threshold}%)")

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
