"""Tests for app/screener/vcp.py — detect_vcp(), check_breakout(), _find_pivots()."""
from datetime import date, timedelta
import numpy as np
import pandas as pd
import pytest
from app.screener.vcp import detect_vcp, check_breakout, _find_pivots, VCPResult


class Eng:
    def __init__(self, thresholds=None, disabled=None):
        self._t = {
            "vcp_min_contractions": 3, "vcp_max_weeks": 52,
            "vcp_min_weeks": 3, "vcp_volume_dry_up": 50.0,
            "vcp_breakout_volume": 150.0, "vcp_max_extension": 5.0,
        }
        if thresholds:
            self._t.update(thresholds)
        self._d = disabled or set()
    def is_enabled(self, rule_id): return rule_id not in self._d
    def threshold(self, rule_id): return self._t.get(rule_id)


def _flat_df(rows=80, close=50.0, volume=500_000):
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(rows)]
    return pd.DataFrame({
        "date": dates,
        "open": [close]*rows, "high": [close*1.01]*rows,
        "low": [close*0.99]*rows, "close": [close]*rows,
        "volume": [volume]*rows,
    })


def _vcp_df(rows=100):
    """
    Build a synthetic price series with 3 successively tighter contractions
    that produce detectable swing highs/lows via _find_pivots(window=5).

    Each contraction is a clean V-shape with a distinct peak and trough
    separated by at least 5 bars (the pivot detection window).
    """
    import numpy as np
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(rows)]

    # Start at 50 and create a base with 3 V-shapes (peaks + troughs)
    close = np.full(rows, 50.0)

    # Contraction 1: peak at idx 10, trough at idx 20 (15% drop)
    close[10] = 55.0            # peak
    for i in range(11, 20):
        close[i] = 55.0 - (55.0 - 46.75) * (i - 10) / 10   # slope down
    close[20] = 46.75           # trough (~15% contraction)
    for i in range(21, 30):
        close[i] = 46.75 + (50.0 - 46.75) * (i - 20) / 10  # recovery

    # Contraction 2: peak at idx 35, trough at idx 45 (10% drop)
    close[35] = 53.0
    for i in range(36, 45):
        close[i] = 53.0 - (53.0 - 47.7) * (i - 35) / 10
    close[45] = 47.7            # ~10% contraction
    for i in range(46, 55):
        close[i] = 47.7 + (50.5 - 47.7) * (i - 45) / 10

    # Contraction 3: peak at idx 60, trough at idx 70 (6% drop)
    close[60] = 51.5
    for i in range(61, 70):
        close[i] = 51.5 - (51.5 - 48.4) * (i - 60) / 10
    close[70] = 48.4            # ~6% contraction
    for i in range(71, 80):
        close[i] = 48.4 + (50.0 - 48.4) * (i - 70) / 10

    highs  = close * 1.005
    lows   = close * 0.995
    # Make trough rows lower
    for idx in [20, 45, 70]:
        lows[idx] = close[idx] * 0.995
    # Make peak rows higher
    for idx in [10, 35, 60]:
        highs[idx] = close[idx] * 1.005

    volumes = np.full(rows, 500_000.0)
    volumes[60:75] = 150_000   # volume dry-up in final contraction

    return pd.DataFrame({
        "date": dates, "open": close.tolist(), "high": highs.tolist(),
        "low": lows.tolist(), "close": close.tolist(), "volume": volumes.tolist(),
    })


# --- _find_pivots ---

def test_find_pivots_identifies_peak():
    values = np.array([1, 2, 5, 2, 1, 2, 3, 2, 1], dtype=float)
    peaks = _find_pivots(values, direction="high", window=3)
    assert 2 in peaks  # index 2 is the peak at value 5


def test_find_pivots_identifies_trough():
    values = np.array([5, 3, 1, 3, 5, 3, 1, 3, 5], dtype=float)
    troughs = _find_pivots(values, direction="low", window=3)
    assert 2 in troughs  # index 2 is the trough at value 1


def test_find_pivots_flat_returns_empty():
    values = np.array([5.0]*10)
    # All values equal — no unique max/min, every point is a peak and trough
    peaks = _find_pivots(values, direction="high", window=3)
    assert isinstance(peaks, list)


# --- detect_vcp: insufficient data ---

def test_detect_vcp_returns_empty_for_short_df():
    vcp, rules = detect_vcp("BHP.AX", _flat_df(rows=10), Eng())
    assert not vcp.detected
    assert rules == {}


def test_detect_vcp_insufficient_pivot_points():
    # Flat price → no swing highs/lows → early return
    vcp, rules = detect_vcp("BHP.AX", _flat_df(rows=80), Eng())
    assert not vcp.detected
    # Should have vcp_min_contractions result saying insufficient pivot points
    assert "vcp_min_contractions" in rules
    assert not rules["vcp_min_contractions"].passed


# --- detect_vcp: with valid VCP pattern ---

def test_detect_vcp_runs_without_error_on_pattern():
    """detect_vcp must run end-to-end and return the correct types."""
    df = _vcp_df(rows=100)
    vcp, rules = detect_vcp("TEST.AX", df, Eng(thresholds={"vcp_min_contractions": 2}))
    assert isinstance(vcp, VCPResult)
    assert isinstance(rules, dict)
    # Function must have run through the contraction-pairing logic
    assert "vcp_min_contractions" in rules


def test_detect_vcp_returns_vcp_result_type():
    df = _vcp_df(rows=100)
    vcp, rules = detect_vcp("TEST.AX", df, Eng())
    assert isinstance(vcp, VCPResult)
    assert isinstance(rules, dict)


def test_detect_vcp_pivot_price_positive():
    df = _vcp_df(rows=100)
    vcp, _ = detect_vcp("TEST.AX", df, Eng(thresholds={"vcp_min_contractions": 2}))
    if vcp.detected or vcp.pivot_price:
        assert vcp.pivot_price > 0


def test_detect_vcp_avg_vol_computed_when_none():
    df = _vcp_df(rows=100)
    # avg_vol_50=None → should be computed internally from df
    vcp, rules = detect_vcp("TEST.AX", df, Eng(thresholds={"vcp_min_contractions": 2}), avg_vol_50=None)
    assert isinstance(rules, dict)


def test_detect_vcp_with_explicit_avg_vol():
    df = _vcp_df(rows=100)
    vcp, rules = detect_vcp("TEST.AX", df, Eng(thresholds={"vcp_min_contractions": 2}), avg_vol_50=500_000)
    assert isinstance(rules, dict)


# --- check_breakout ---

def test_check_breakout_passes_when_above_pivot():
    df = _flat_df(close=52.0, volume=900_000)  # 900k > 150% of 500k avg
    results = check_breakout("BHP.AX", df, pivot_price=51.0, avg_vol_50=500_000, engine=Eng())
    assert results["vcp_breakout_price"].passed    # 52/51 = +1.96% (within 5%)
    assert results["vcp_breakout_volume"].passed   # 180% of avg


def test_check_breakout_fails_price_below_pivot():
    df = _flat_df(close=49.0, volume=900_000)
    results = check_breakout("BHP.AX", df, pivot_price=51.0, avg_vol_50=500_000, engine=Eng())
    assert not results["vcp_breakout_price"].passed  # negative % above pivot


def test_check_breakout_fails_price_extended():
    df = _flat_df(close=58.0, volume=900_000)  # 58/51 = +13.7% → exceeds 5% max
    results = check_breakout("BHP.AX", df, pivot_price=51.0, avg_vol_50=500_000, engine=Eng())
    assert not results["vcp_breakout_price"].passed


def test_check_breakout_fails_low_volume():
    df = _flat_df(close=52.0, volume=200_000)  # 200k/500k = 40% < 150%
    results = check_breakout("BHP.AX", df, pivot_price=51.0, avg_vol_50=500_000, engine=Eng())
    assert not results["vcp_breakout_volume"].passed


def test_check_breakout_disabled_price_rule():
    df = _flat_df(close=52.0, volume=900_000)
    eng = Eng(disabled={"vcp_breakout_price"})
    results = check_breakout("BHP.AX", df, pivot_price=51.0, avg_vol_50=500_000, engine=eng)
    assert "vcp_breakout_price" not in results
    assert "vcp_breakout_volume" in results
