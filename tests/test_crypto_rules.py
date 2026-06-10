"""Tests for app/screener/crypto_rules.py — evaluate_crypto_rules()."""
from datetime import date, timedelta
import pandas as pd
import pytest
from app.screener.crypto_rules import evaluate_crypto_rules


class Eng:
    def __init__(self, thresholds=None, disabled=None):
        self._t = thresholds or {}
        self._d = disabled or set()
    def is_enabled(self, rule_id): return rule_id not in self._d
    def threshold(self, rule_id): return self._t.get(rule_id)


def _df(close=50000.0, rows=60, vol=1_000_000_000, atr=2000.0):
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(rows)]
    closes = [close]*rows
    return pd.DataFrame({
        "date": dates, "open": closes, "high": [c*1.02 for c in closes],
        "low": [c*0.98 for c in closes], "close": closes,
        "volume": [vol]*rows, "atr_14": [atr]*rows,
    })


# --- BTC regime ---

def test_btc_regime_passes_when_above_ma50():
    btc = _df(close=50000, rows=60)  # all same price → close == ma50 == 50000 → not > 50000
    # Use slightly rising prices so close > ma50
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [49000.0 + i * 40 for i in range(60)]  # rising; last is 49000+40*59=51360
    btc = pd.DataFrame({
        "date": dates, "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
    })
    r = evaluate_crypto_rules("ETH-USD", _df(), Eng(), btc_df=btc)
    assert r["crypto_btc_regime"].passed


def test_btc_regime_fails_when_below_ma50():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [52000.0 - i * 40 for i in range(60)]  # declining; last close < ma50
    btc = pd.DataFrame({
        "date": dates, "close": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
        "open": closes, "high": closes, "low": closes,
    })
    r = evaluate_crypto_rules("ETH-USD", _df(), Eng(), btc_df=btc)
    assert not r["crypto_btc_regime"].passed


def test_btc_regime_skips_with_no_btc_data():
    r = evaluate_crypto_rules("ETH-USD", _df(), Eng(), btc_df=None)
    # No BTC data → neutral pass
    assert r["crypto_btc_regime"].passed


def test_btc_self_check():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [49000.0 + i * 40 for i in range(60)]
    df = pd.DataFrame({
        "date": dates, "close": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
        "open": closes, "high": closes, "low": closes,
    })
    r = evaluate_crypto_rules("BTC-USD", df, Eng(), btc_df=None)
    assert r["crypto_btc_regime"].passed  # self-check, rising prices


# --- market cap ---

def test_market_cap_passes():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(), market_cap_usd=5e9)
    assert r["crypto_market_cap_min"].passed


def test_market_cap_fails():
    r = evaluate_crypto_rules("UNKNOWN-USD", _df(), Eng(), market_cap_usd=5e6)
    assert not r["crypto_market_cap_min"].passed


def test_market_cap_waived_for_btc():
    r = evaluate_crypto_rules("BTC-USD", _df(), Eng(), market_cap_usd=None)
    assert r["crypto_market_cap_min"].passed


def test_market_cap_waived_for_eth():
    r = evaluate_crypto_rules("ETH-USD", _df(), Eng(), market_cap_usd=None)
    assert r["crypto_market_cap_min"].passed


def test_market_cap_none_skips():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(), market_cap_usd=None)
    assert r["crypto_market_cap_min"].passed  # unknown → skip


# --- volume 24h ---

def test_volume_24h_passes():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(), volume_24h_usd=100e6)
    assert r["crypto_volume_min_24h"].passed


def test_volume_24h_fails():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(), volume_24h_usd=1e6)
    assert not r["crypto_volume_min_24h"].passed


def test_volume_24h_estimated_from_df():
    # volume_24h_usd=None → estimate from df close × volume
    # close=50000, volume=1e9 → 50000 * 1e9 = 5e13 → passes min $5M
    r = evaluate_crypto_rules("SOL-USD", _df(close=50000, vol=int(1e6)), Eng(), volume_24h_usd=None)
    assert r["crypto_volume_min_24h"].passed  # 50000 * 1e6 = 5e10 >> 5e6


def test_volume_24h_waived_for_btc():
    r = evaluate_crypto_rules("BTC-USD", _df(), Eng(), volume_24h_usd=None)
    assert r["crypto_volume_min_24h"].passed


# --- stop width ---

def test_stop_width_passes_wide_atr():
    # atr=5000 on close=50000 → atr_pct=10%, stop_est=15% ≥ min 10%
    r = evaluate_crypto_rules("BTC-USD", _df(close=50000, atr=5000), Eng())
    assert r["crypto_stop_width_pct"].passed


def test_stop_width_fails_narrow_atr():
    # atr=100 on close=50000 → atr_pct=0.2%, stop_est=0.3% < min 10%
    r = evaluate_crypto_rules("SOL-USD", _df(close=50000, atr=100), Eng())
    assert not r["crypto_stop_width_pct"].passed


# --- max risk pct (always passes) ---

def test_max_risk_pct_always_passes():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(thresholds={"crypto_max_risk_pct": 1.0}))
    assert r["crypto_max_risk_pct"].passed


# --- VCP contraction depth ---

def test_vcp_contraction_depth_passes():
    # Build df with significant high-low range
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [50000.0]*60
    highs  = [60000.0]*60  # 20% range
    lows   = [40000.0]*60
    df = pd.DataFrame({
        "date": dates, "close": closes, "open": closes,
        "high": highs, "low": lows, "volume": [1e9]*60, "atr_14": [2000]*60,
    })
    r = evaluate_crypto_rules("SOL-USD", df, Eng(thresholds={"crypto_vcp_contraction_pct": 15.0}))
    assert r["crypto_vcp_contraction_pct"].passed


def test_vcp_contraction_depth_fails():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(thresholds={"crypto_vcp_contraction_pct": 50.0}))
    assert not r["crypto_vcp_contraction_pct"].passed


def test_vcp_contraction_depth_insufficient_history():
    df = _df(rows=10)
    r = evaluate_crypto_rules("SOL-USD", df, Eng())
    assert r["crypto_vcp_contraction_pct"].passed  # skip → neutral pass


# --- RSI momentum ---

def test_rsi_momentum_passes_bullish():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [49000.0 + i * 50 for i in range(60)]  # steadily rising → high RSI
    df = pd.DataFrame({
        "date": dates, "close": closes, "open": closes, "high": closes,
        "low": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
    })
    r = evaluate_crypto_rules("SOL-USD", df, Eng(thresholds={"crypto_rsi_momentum": 50.0}))
    assert r["crypto_rsi_momentum"].passed


def test_rsi_momentum_fails_bearish():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [51000.0 - i * 50 for i in range(60)]  # declining → low RSI
    df = pd.DataFrame({
        "date": dates, "close": closes, "open": closes, "high": closes,
        "low": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
    })
    r = evaluate_crypto_rules("SOL-USD", df, Eng(thresholds={"crypto_rsi_momentum": 50.0}))
    assert not r["crypto_rsi_momentum"].passed


def test_rsi_insufficient_data():
    df = _df(rows=10)
    r = evaluate_crypto_rules("SOL-USD", df, Eng())
    assert r["crypto_rsi_momentum"].passed  # skipped → neutral


# --- MACD bullish ---

def test_macd_bullish_passes():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    closes = [49000.0 + i * 100 for i in range(60)]  # rising → MACD positive
    df = pd.DataFrame({
        "date": dates, "close": closes, "open": closes, "high": closes,
        "low": closes, "volume": [1e9]*60, "atr_14": [2000]*60,
    })
    r = evaluate_crypto_rules("SOL-USD", df, Eng())
    assert r["crypto_macd_bullish"].passed


def test_macd_insufficient_data():
    df = _df(rows=20)
    r = evaluate_crypto_rules("SOL-USD", df, Eng())
    assert r["crypto_macd_bullish"].passed  # skipped


# --- Volume surge ---

def test_volume_surge_passes():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(25)]
    vols = [1_000_000]*24 + [3_000_000]  # last bar is 3× avg
    closes = [50000.0]*25
    df = pd.DataFrame({
        "date": dates, "close": closes, "open": closes, "high": closes,
        "low": closes, "volume": vols, "atr_14": [2000]*25,
    })
    r = evaluate_crypto_rules("SOL-USD", df, Eng(thresholds={"crypto_volume_surge": 1.5}))
    assert r["crypto_volume_surge"].passed


def test_volume_surge_fails():
    r = evaluate_crypto_rules("SOL-USD", _df(vol=500_000), Eng(thresholds={"crypto_volume_surge": 2.0}))
    # All same volume → ratio = 1.0 < 2.0
    assert not r["crypto_volume_surge"].passed


# --- BTC relative strength ---

def test_btc_rs_passes_outperforming():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    btc_closes   = [49000 + i * 50 for i in range(60)]  # +6%
    asset_closes = [10 + i * 0.30 for i in range(60)]   # +180% → massively outperforms
    btc = pd.DataFrame({"close": btc_closes, "date": dates, "volume": [1e9]*60, "atr_14": [2000]*60,
                        "open": btc_closes, "high": btc_closes, "low": btc_closes})
    asset = pd.DataFrame({"close": asset_closes, "date": dates, "volume": [1e6]*60, "atr_14": [0.5]*60,
                          "open": asset_closes, "high": asset_closes, "low": asset_closes})
    r = evaluate_crypto_rules("SOL-USD", asset, Eng(), btc_df=btc)
    assert r["crypto_btc_relative_strength"].passed


def test_btc_rs_skipped_for_btc_self():
    df = _df(rows=60)
    r = evaluate_crypto_rules("BTC-AUD", df, Eng(), btc_df=df)
    assert r["crypto_btc_relative_strength"].passed  # skipped for BTC itself


def test_disabled_rule_absent():
    r = evaluate_crypto_rules("SOL-USD", _df(), Eng(disabled={"crypto_market_cap_min"}))
    assert "crypto_market_cap_min" not in r
