"""Tests for utility helpers in app/tasks/screening.py."""
import pytest
from app.tasks.screening import serialize_rule_results, _safe_float, _safe_int


# --- serialize_rule_results ---

class _R:
    """Minimal RuleResult-like object."""
    def __init__(self, passed, value=None, message=None):
        self.passed = passed
        self.value = value
        self.message = message or ""


def test_serialize_rule_results_with_ruleresult_objects():
    results = {
        "trend_200ma": _R(passed=True, value=50.0),
        "trend_50ma":  _R(passed=False, value=30.0, message="below MA"),
    }
    out = serialize_rule_results(results)
    assert out["trend_200ma"]["passed"] is True
    assert out["trend_200ma"]["value"] == 50.0
    assert out["trend_50ma"]["passed"] is False
    assert out["trend_50ma"]["message"] == "below MA"


def test_serialize_rule_results_with_dict_values():
    results = {
        "my_rule": {"passed": True, "value": 99.9, "message": "ok"},
    }
    out = serialize_rule_results(results)
    assert out["my_rule"]["passed"] is True
    assert out["my_rule"]["value"] == 99.9


def test_serialize_rule_results_with_bool_values():
    results = {"simple_rule": True, "failing_rule": False}
    out = serialize_rule_results(results)
    assert out["simple_rule"]["passed"] is True
    assert out["failing_rule"]["passed"] is False
    assert out["simple_rule"]["value"] is None


def test_serialize_rule_results_empty():
    assert serialize_rule_results({}) == {}


def test_serialize_rule_results_numpy_value():
    import numpy as np
    results = {"numpy_rule": _R(passed=True, value=np.float64(3.14))}
    out = serialize_rule_results(results)
    assert abs(out["numpy_rule"]["value"] - 3.14) < 0.01
    # Must be a plain Python type, not numpy
    assert not hasattr(out["numpy_rule"]["value"], "item")


# --- _safe_float ---

def test_safe_float_normal():
    assert _safe_float(3.14) == 3.14


def test_safe_float_int():
    assert _safe_float(10) == 10.0


def test_safe_float_string_number():
    assert _safe_float("2.5") == 2.5


def test_safe_float_none_returns_none():
    assert _safe_float(None) is None


def test_safe_float_nan_returns_none():
    import math
    assert _safe_float(float("nan")) is None


def test_safe_float_inf_returns_none():
    assert _safe_float(float("inf")) is None


def test_safe_float_invalid_string_returns_none():
    assert _safe_float("not_a_number") is None


# --- _safe_int ---

def test_safe_int_normal():
    assert _safe_int(7.9) == 7


def test_safe_int_string():
    assert _safe_int("42") == 42


def test_safe_int_none_returns_none():
    assert _safe_int(None) is None


def test_safe_int_nan_returns_none():
    import math
    assert _safe_int(float("nan")) is None


def test_safe_int_invalid_returns_none():
    assert _safe_int("abc") is None
