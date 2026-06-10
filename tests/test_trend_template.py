"""Tests for app/screener/trend_template.py — evaluate_trend_template()."""
from datetime import date, timedelta
import pandas as pd
import pytest
from app.screener.trend_template import evaluate_trend_template


class Eng:
    """Stub RuleEngine: all rules enabled, configurable thresholds."""
    def __init__(self, thresholds=None, disabled=None):
        self._t = thresholds or {}
        self._d = disabled or set()
    def is_enabled(self, rule_id): return rule_id not in self._d
    def threshold(self, rule_id): return self._t.get(rule_id)


def _df(close=50.0, ma_50=45.0, ma_150=42.0, ma_200=40.0,
        high_52w=55.0, low_52w=30.0, rs=80.0, rows=30):
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    return pd.DataFrame({
        "date": dates, "close": [close]*rows,
        "ma_50": [ma_50]*rows, "ma_150": [ma_150]*rows,
        "ma_200": [ma_200]*rows,
        "high_52w": [high_52w]*rows, "low_52w": [low_52w]*rows,
        "rs_rating": [rs]*rows,
    })


# --- price_above_200ma ---

def test_price_above_200ma_passes():
    r = evaluate_trend_template("BHP.AX", _df(close=50, ma_200=40), Eng())
    assert r["trend_price_above_200ma"].passed


def test_price_above_200ma_fails():
    r = evaluate_trend_template("BHP.AX", _df(close=35, ma_200=40), Eng())
    assert not r["trend_price_above_200ma"].passed


def test_price_above_200ma_skipped_when_disabled():
    r = evaluate_trend_template("BHP.AX", _df(), Eng(disabled={"trend_price_above_200ma"}))
    assert "trend_price_above_200ma" not in r


# --- price_above_150ma ---

def test_price_above_150ma_passes():
    r = evaluate_trend_template("BHP.AX", _df(close=50, ma_150=42), Eng())
    assert r["trend_price_above_150ma"].passed


def test_price_above_150ma_fails():
    r = evaluate_trend_template("BHP.AX", _df(close=40, ma_150=42), Eng())
    assert not r["trend_price_above_150ma"].passed


# --- ma150_above_ma200 ---

def test_ma150_above_ma200_passes():
    r = evaluate_trend_template("BHP.AX", _df(ma_150=42, ma_200=40), Eng())
    assert r["trend_ma150_above_ma200"].passed


def test_ma150_above_ma200_fails():
    r = evaluate_trend_template("BHP.AX", _df(ma_150=38, ma_200=40), Eng())
    assert not r["trend_ma150_above_ma200"].passed


# --- ma200_trending_up ---

def test_ma200_trending_up_passes():
    rows = 30
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    # ma_200 slopes up: starts at 38, ends at 40
    ma200_vals = [38.0 + i * (2.0 / rows) for i in range(rows)]
    df = pd.DataFrame({
        "date": dates, "close": [50.0]*rows,
        "ma_50": [45.0]*rows, "ma_150": [42.0]*rows,
        "ma_200": ma200_vals,
        "high_52w": [55.0]*rows, "low_52w": [30.0]*rows, "rs_rating": [80.0]*rows,
    })
    r = evaluate_trend_template("BHP.AX", df, Eng(thresholds={"trend_ma200_trending_up": 21}))
    assert r["trend_ma200_trending_up"].passed


def test_ma200_trending_up_insufficient_data():
    df = _df(rows=10)  # fewer than lookback + 1
    r = evaluate_trend_template("BHP.AX", df, Eng(thresholds={"trend_ma200_trending_up": 21}))
    assert not r["trend_ma200_trending_up"].passed


# --- ma50_above_ma150_200 ---

def test_ma50_above_ma150_200_passes():
    r = evaluate_trend_template("BHP.AX", _df(ma_50=50, ma_150=42, ma_200=40), Eng())
    assert r["trend_ma50_above_ma150_200"].passed


def test_ma50_above_ma150_200_fails_when_below_150():
    r = evaluate_trend_template("BHP.AX", _df(ma_50=41, ma_150=42, ma_200=40), Eng())
    assert not r["trend_ma50_above_ma150_200"].passed


# --- price_above_ma50 ---

def test_price_above_ma50_passes():
    r = evaluate_trend_template("BHP.AX", _df(close=50, ma_50=45), Eng())
    assert r["trend_price_above_ma50"].passed


def test_price_above_ma50_fails():
    r = evaluate_trend_template("BHP.AX", _df(close=40, ma_50=45), Eng())
    assert not r["trend_price_above_ma50"].passed


# --- pct_above_52w_low ---

def test_pct_above_52w_low_passes():
    # close=50, low=30 → 66.7% above → passes 30% threshold
    r = evaluate_trend_template("BHP.AX", _df(close=50, low_52w=30), Eng())
    assert r["trend_pct_above_52w_low"].passed


def test_pct_above_52w_low_fails():
    # close=31, low=30 → 3.3% above → fails 30% threshold
    r = evaluate_trend_template("BHP.AX", _df(close=31, low_52w=30), Eng())
    assert not r["trend_pct_above_52w_low"].passed


# --- pct_below_52w_high ---

def test_pct_below_52w_high_passes():
    # close=50, high=55 → 9% below → passes 25% threshold
    r = evaluate_trend_template("BHP.AX", _df(close=50, high_52w=55), Eng())
    assert r["trend_pct_below_52w_high"].passed


def test_pct_below_52w_high_fails():
    # close=30, high=55 → 45% below → fails 25% threshold
    r = evaluate_trend_template("BHP.AX", _df(close=30, high_52w=55), Eng())
    assert not r["trend_pct_below_52w_high"].passed


# --- rs_rating ---

def test_rs_rating_passes():
    r = evaluate_trend_template("BHP.AX", _df(rs=80), Eng(thresholds={"trend_rs_rating_min": 70}))
    assert r["trend_rs_rating_min"].passed


def test_rs_rating_fails():
    r = evaluate_trend_template("BHP.AX", _df(rs=50), Eng(thresholds={"trend_rs_rating_min": 70}))
    assert not r["trend_rs_rating_min"].passed


def test_all_rules_disabled_returns_empty():
    all_rules = {
        "trend_price_above_200ma", "trend_price_above_150ma", "trend_ma150_above_ma200",
        "trend_ma200_trending_up", "trend_ma50_above_ma150_200", "trend_price_above_ma50",
        "trend_pct_above_52w_low", "trend_pct_below_52w_high", "trend_rs_rating_min",
    }
    r = evaluate_trend_template("BHP.AX", _df(), Eng(disabled=all_rules))
    assert r == {}
