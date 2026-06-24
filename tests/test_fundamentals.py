"""Tests for app/screener/fundamentals.py — evaluate_fundamentals()."""
import pytest
from app.screener.fundamentals import evaluate_fundamentals


class Eng:
    def __init__(self, thresholds=None, disabled=None):
        self._t = thresholds or {}
        self._d = disabled or set()
    def is_enabled(self, rule_id): return rule_id not in self._d
    def threshold(self, rule_id): return self._t.get(rule_id)


BASE = {
    "eps_quarterly": [1.5, 1.3, 1.1, 0.9, 0.8, 0.7, 0.6, 0.5],  # latest first, 8 quarters
    "revenue_quarterly": [1000, 900, 800, 700, 600, 0, 0, 0],
    "roe": 0.22,               # 22%
    "net_margin": 0.15,        # 15%
    "net_margin_prev": 0.12,   # improving
    "inst_ownership_pct": 0.35,  # 35%
}


# --- EPS growth recent ---

def test_eps_growth_recent_passes():
    # eps[0]=1.5, eps[4]=0.8 → (1.5-0.8)/0.8*100=87.5% > 25%
    r = evaluate_fundamentals("BHP.AX", BASE, Eng())
    assert r["fundamental_eps_growth_recent"].passed


def test_eps_growth_recent_fails():
    data = dict(BASE, eps_quarterly=[0.81, 1.3, 1.1, 0.9, 0.8, 0.7, 0.6, 0.5])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_eps_growth_recent"].passed  # only 1.25% growth


def test_eps_growth_loss_to_profit():
    data = dict(BASE, eps_quarterly=[0.5, 0.3, 0.1, -0.1, -0.5, -0.3, -0.2, -0.1])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # eps[4]=-0.5 (loss), eps[0]=0.5 (profit) → turned profitable = pass
    assert r["fundamental_eps_growth_recent"].passed


def test_eps_growth_insufficient_data():
    data = dict(BASE, eps_quarterly=[1.5, 1.3])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Data-availability policy: insufficient data -> pass (no penalty)
    assert r["fundamental_eps_growth_recent"].passed
    assert "unavailable" in r["fundamental_eps_growth_recent"].message.lower()


# --- EPS acceleration ---

def test_eps_accel_passes():
    # q1 growth > q2 growth
    data = dict(BASE, eps_quarterly=[2.0, 1.5, 1.1, 0.9, 0.8, 0.7, 0.6, 0.5])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert r["fundamental_eps_growth_accel"].passed


def test_eps_accel_fails():
    # Make q1 growth lower than q2
    data = dict(BASE, eps_quarterly=[0.85, 1.5, 1.1, 0.9, 0.8, 0.7, 0.6, 0.5])
    # q1: (0.85-0.8)/0.8=6.25%, q2: (1.5-0.7)/0.7=114% → not accelerating
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_eps_growth_accel"].passed


def test_eps_accel_insufficient_data():
    data = dict(BASE, eps_quarterly=[1.5, 1.3, 1.1])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Data-availability policy: insufficient data -> pass (no penalty)
    assert r["fundamental_eps_growth_accel"].passed
    assert "unavailable" in r["fundamental_eps_growth_accel"].message.lower()


# --- Annual EPS growth ---

def test_annual_eps_growth_passes():
    # ttm=1.5+1.3+1.1+0.9=4.8, prior=0.8+0.7+0.6+0.5=2.6 → 84.6% > 25%
    r = evaluate_fundamentals("BHP.AX", BASE, Eng())
    assert r["fundamental_eps_growth_annual"].passed


def test_annual_eps_growth_fails():
    data = dict(BASE, eps_quarterly=[0.82, 0.81, 0.80, 0.79, 0.80, 0.79, 0.78, 0.77])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_eps_growth_annual"].passed


def test_annual_eps_prior_zero():
    data = dict(BASE, eps_quarterly=[1.5, 1.3, 1.1, 0.9, -0.1, -0.2, -0.3, -0.4])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Prior-year TTM negative, current positive -> turned profitable annually -> pass
    assert r["fundamental_eps_growth_annual"].passed
    assert "profitable" in r["fundamental_eps_growth_annual"].message.lower()


# --- Sales growth ---

def test_sales_growth_passes():
    # rev[0]=1000, rev[4]=600 → 66.7% > 25%
    r = evaluate_fundamentals("BHP.AX", BASE, Eng())
    assert r["fundamental_sales_growth"].passed


def test_sales_growth_fails():
    data = dict(BASE, revenue_quarterly=[605, 900, 800, 700, 600, 0, 0, 0])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_sales_growth"].passed


def test_sales_growth_insufficient_data():
    data = dict(BASE, revenue_quarterly=[1000, 900])
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Data-availability policy: insufficient data -> pass (no penalty)
    assert r["fundamental_sales_growth"].passed
    assert "unavailable" in r["fundamental_sales_growth"].message.lower()


# --- ROE ---

def test_roe_passes_decimal_form():
    data = dict(BASE, roe=0.22)  # 22% decimal
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert r["fundamental_roe"].passed


def test_roe_passes_pct_form():
    data = dict(BASE, roe=22.0)  # already in % form
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert r["fundamental_roe"].passed


def test_roe_fails():
    data = dict(BASE, roe=0.10)  # 10% < 17%
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_roe"].passed


def test_roe_not_available():
    data = dict(BASE, roe=None)
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Data-availability policy: missing ROE -> pass (no penalty)
    assert r["fundamental_roe"].passed
    assert "unavailable" in r["fundamental_roe"].message.lower()


# --- Profit margin ---

def test_profit_margin_passes():
    r = evaluate_fundamentals("BHP.AX", BASE, Eng())
    assert r["fundamental_profit_margin"].passed


def test_profit_margin_fails_negative():
    data = dict(BASE, net_margin=-0.05)
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_profit_margin"].passed


def test_profit_margin_fails_declining():
    data = dict(BASE, net_margin=0.10, net_margin_prev=0.15)  # declining
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_profit_margin"].passed


def test_profit_margin_not_available():
    data = dict(BASE, net_margin=None)
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    # Data-availability policy: missing margin -> pass (no penalty)
    assert r["fundamental_profit_margin"].passed
    assert "unavailable" in r["fundamental_profit_margin"].message.lower()


# --- Institutional ownership ---

def test_inst_own_passes():
    r = evaluate_fundamentals("BHP.AX", BASE, Eng())
    assert r["fundamental_institutional_own"].passed  # 35%


def test_inst_own_too_low():
    data = dict(BASE, inst_ownership_pct=0.02)  # 2% < min 5%
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_institutional_own"].passed


def test_inst_own_too_high():
    data = dict(BASE, inst_ownership_pct=0.85)  # 85% > max 80%
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert not r["fundamental_institutional_own"].passed


def test_inst_own_not_available_passes():
    data = dict(BASE, inst_ownership_pct=None)
    r = evaluate_fundamentals("BHP.AX", data, Eng())
    assert r["fundamental_institutional_own"].passed  # pass when unknown


def test_disabled_rule_not_in_results():
    r = evaluate_fundamentals("BHP.AX", BASE, Eng(disabled={"fundamental_roe"}))
    assert "fundamental_roe" not in r
