"""Tests for app/screener/rules.py — RuleEngine, RuleResult, ScreenResult."""
import pytest
from app.screener.rules import RuleResult, ScreenResult


# --- RuleResult ---

def test_rule_result_bool_true():
    r = RuleResult("trend_price_above_200ma", passed=True, value=50.0, threshold=40.0)
    assert bool(r) is True


def test_rule_result_bool_false():
    r = RuleResult("trend_price_above_200ma", passed=False)
    assert bool(r) is False


def test_rule_result_message_defaults_empty():
    r = RuleResult("some_rule", passed=True)
    assert r.message == ""


# --- ScreenResult ---

def test_screen_result_summary():
    sr = ScreenResult(ticker="BHP.AX", passed=True, trend_score=7)
    sr.rule_results = {
        "trend_price_above_200ma": RuleResult("trend_price_above_200ma", passed=True, value=50, threshold=40),
        "trend_price_above_150ma": RuleResult("trend_price_above_150ma", passed=False, value=35, threshold=42),
    }
    summary = sr.summary
    assert summary["trend_price_above_200ma"]["passed"] is True
    assert summary["trend_price_above_150ma"]["passed"] is False
    assert summary["trend_price_above_200ma"]["value"] == 50


def test_screen_result_defaults():
    sr = ScreenResult(ticker="WOW.AX")
    assert sr.passed is False
    assert sr.trend_score == 0
    assert sr.fund_score == 0
    assert sr.rs_rating == 0.0
    assert sr.rule_results == {}


# --- RuleEngine (DB-backed, loaded via monkeypatched SessionLocal) ---

def test_rule_engine_loads_rules_from_db(db_session, org_and_account):
    """RuleEngine.is_enabled() returns False for unknown rules."""
    from app.screener.rules import RuleEngine
    org, _ = org_and_account
    engine = RuleEngine(organization_id=org.id, tier="GOLD")
    # An unknown rule always returns False
    assert engine.is_enabled("definitely_not_a_real_rule_xyz") is False


def test_rule_engine_threshold_returns_none_for_unknown(db_session, org_and_account):
    from app.screener.rules import RuleEngine
    org, _ = org_and_account
    engine = RuleEngine(organization_id=org.id, tier="GOLD")
    assert engine.threshold("definitely_not_a_real_rule_xyz") is None


def test_rule_engine_signal_overrides(db_session, org_and_account):
    """apply_signal_overrides and clear_signal_overrides work without errors."""
    from app.screener.rules import RuleEngine
    from app.models.config import RuleConfig, RuleCategory
    from decimal import Decimal
    org, _ = org_and_account
    # Seed a real rule so override has something to act on
    rc = RuleConfig(
        rule_id="test_override_rule", label="Test", category=RuleCategory.ENTRY,
        enabled_globally=True, is_mandatory=False, organization_id=org.id,
        threshold=Decimal("1.0"),
    )
    db_session.add(rc)
    db_session.commit()
    engine = RuleEngine(organization_id=org.id, tier="GOLD")
    engine.apply_signal_overrides({"test_override_rule": False})
    engine.clear_signal_overrides()
    # After clear, no errors
    assert engine._signal_overrides == {}


def test_rule_engine_get_rule_meta_unknown(db_session, org_and_account):
    from app.screener.rules import RuleEngine
    org, _ = org_and_account
    engine = RuleEngine(organization_id=org.id)
    assert engine.get_rule_meta("nonexistent_rule") is None


def test_rule_engine_all_rules_meta_returns_list(db_session, org_and_account):
    from app.screener.rules import RuleEngine
    org, _ = org_and_account
    engine = RuleEngine(organization_id=org.id)
    meta = engine.all_rules_meta()
    assert isinstance(meta, list)
