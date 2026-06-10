"""Extended tests for app/screener/vcp.py — covering contraction detection paths."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock


def _make_engine(thresholds=None):
    engine = MagicMock()
    defaults = {
        "vcp_min_contractions": 2,
        "vcp_base_weeks": None,  # handled specially
        "vcp_max_weeks": 52,
        "vcp_volume_dry_up": 60,
        "vcp_breakout_volume": 1.5,
        "vcp_max_extension": 5.0,
    }
    if thresholds:
        defaults.update(thresholds)
    engine.is_enabled.return_value = True
    engine.threshold.side_effect = lambda k: defaults.get(k)
    return engine


def _make_vcp_df(n=100, base_close=50.0):
    """Create a price df that mimics a VCP pattern with 3 contractions."""
    import random
    random.seed(42)
    dates = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")

    # Build a simple VCP-like pattern: declining highs, higher lows
    closes = []
    highs = []
    lows = []
    volumes = []

    for i in range(n):
        # Create a price that forms contractions
        phase = i / n
        noise = random.uniform(-0.5, 0.5)
        if phase < 0.3:  # First contraction
            c = base_close + 5 - phase * 10 + noise
        elif phase < 0.6:  # Second contraction
            c = base_close + 2 - (phase - 0.3) * 8 + noise
        else:  # Third contraction (tight)
            c = base_close - 1 - (phase - 0.6) * 4 + noise

        closes.append(max(c, base_close - 10))
        highs.append(closes[-1] + abs(noise) + 0.5)
        lows.append(closes[-1] - abs(noise) - 0.3)
        volumes.append(int(500000 + random.uniform(-100000, 100000)))

    df = pd.DataFrame({
        "date": dates,
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes,
        "open": closes,
        "avg_vol_50": [500000] * n,
        "atr_14": [1.5] * n,
    })
    df = df.set_index("date")
    return df


# ────────────────────────────────────────────────────────────
# detect_vcp — real DataFrame with VCP-like pattern
# ────────────────────────────────────────────────────────────

def test_detect_vcp_with_price_data():
    from app.screener.vcp import detect_vcp
    df = _make_vcp_df(n=80)
    engine = _make_engine()

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    assert hasattr(vcp, "detected")
    assert hasattr(vcp, "contraction_count")
    assert isinstance(rules, dict)


def test_detect_vcp_insufficient_data_returns_false():
    from app.screener.vcp import detect_vcp
    # Only 5 bars — not enough for VCP
    df = pd.DataFrame({
        "close": [50.0, 51.0, 49.0, 50.0, 48.0],
        "high":  [51.0, 52.0, 50.0, 51.0, 49.0],
        "low":   [49.0, 50.0, 48.0, 49.0, 47.0],
        "volume": [100000] * 5,
        "avg_vol_50": [100000] * 5,
        "atr_14": [1.0] * 5,
    })
    engine = _make_engine()

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    assert vcp.detected is False


def test_detect_vcp_empty_df_returns_false():
    from app.screener.vcp import detect_vcp
    df = pd.DataFrame()
    engine = _make_engine()

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    assert vcp.detected is False


def test_detect_vcp_contraction_count_populated():
    from app.screener.vcp import detect_vcp
    df = _make_vcp_df(n=120)
    engine = _make_engine()

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    # Even if not detected, contraction_count should be a non-negative int
    assert isinstance(vcp.contraction_count, int)
    assert vcp.contraction_count >= 0


def test_detect_vcp_all_rules_disabled():
    """When all rules disabled, VCP is not blocked by rule checks."""
    from app.screener.vcp import detect_vcp
    df = _make_vcp_df(n=80)

    engine = MagicMock()
    engine.is_enabled.return_value = False
    engine.threshold.return_value = None

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    # With no rules enabled, should return gracefully
    assert vcp is not None


def test_detect_vcp_min_contractions_not_met():
    """When contraction count < min, VCP not detected."""
    from app.screener.vcp import detect_vcp
    # 40-bar flat price — no meaningful contractions
    df = pd.DataFrame({
        "close": [50.0] * 40,
        "high":  [50.5] * 40,
        "low":   [49.5] * 40,
        "volume": [500000] * 40,
        "avg_vol_50": [500000] * 40,
        "atr_14": [0.5] * 40,
    })
    engine = _make_engine({"vcp_min_contractions": 3})

    vcp, rules = detect_vcp("BHP.AX", df, engine)
    assert vcp.detected is False
