"""
tests/test_central_operations.py
=================================
Tests for the Central Operations refactor:

1. Template source inspection -- global-only buttons gone from org admin pages.
2. Auth guards -- all /superadmin/action/* routes reject non-superadmin callers.
3. Route logic -- sa_action_refresh_universe passes organization_id=None.
4. evaluate_market_regime_task CRYPTO key fix.
5. Exchange forwarding for breakout/exit check routes.
6. Session key regression guard.
"""

import asyncio
import pathlib
import re
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).parent.parent
TEMPLATES_DIR = _ROOT / "web" / "templates"
MAIN_PY_PATH = _ROOT / "web" / "main.py"
SCREENING_PY_PATH = _ROOT / "app" / "tasks" / "screening.py"


def _tpl(rel):
    return (TEMPLATES_DIR / rel).read_text(encoding="utf-8")


def _mock_request(session=None):
    req = MagicMock()
    req.session = session or {}
    req.url.path = "/"
    return req


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Template: admin/health.html
# ---------------------------------------------------------------------------

class TestAdminHealthTemplate:
    HTML = _tpl("admin/health.html")

    def test_no_full_setup(self):
        assert "/action/full-setup" not in self.HTML

    def test_no_refresh_data(self):
        assert "/action/refresh-data" not in self.HTML

    def test_no_evaluate_regime(self):
        assert "/action/evaluate-regime" not in self.HTML

    def test_no_refresh_universe(self):
        assert "/action/refresh-universe" not in self.HTML

    def test_no_seed_crypto(self):
        assert "/action/seed-crypto" not in self.HTML

    def test_has_ping_worker(self):
        assert "/action/ping-worker" in self.HTML

    def test_has_force_screen(self):
        assert "/action/run-screener" in self.HTML or "/action/force-screen" in self.HTML

    def test_has_send_report(self):
        assert "/action/send-report" in self.HTML

    def test_has_recategorise_labels(self):
        assert "/action/recategorise-labels" in self.HTML

    def test_links_to_central_ops(self):
        assert "/superadmin/operations" in self.HTML


# ---------------------------------------------------------------------------
# 2. Template: admin/tasks.html
# ---------------------------------------------------------------------------

class TestAdminTasksTemplate:
    HTML = _tpl("admin/tasks.html")

    def test_no_full_setup(self):
        assert "/action/full-setup" not in self.HTML

    def test_no_evaluate_regime(self):
        assert "/action/evaluate-regime" not in self.HTML

    def test_no_refresh_data(self):
        assert "/action/refresh-data" not in self.HTML

    def test_has_force_screen(self):
        assert "/action/force-screen" in self.HTML or "/action/run-screener" in self.HTML

    def test_has_breakout_check(self):
        assert "/action/force-breakout-check" in self.HTML

    def test_has_exit_check(self):
        assert "/action/force-exit-check" in self.HTML

    def test_has_position_sync(self):
        assert "/action/force-position-sync" in self.HTML

    def test_has_stop_sync(self):
        assert "/action/force-stop-sync" in self.HTML

    def test_links_to_central_ops(self):
        assert "/superadmin/operations" in self.HTML


# ---------------------------------------------------------------------------
# 3. Template: superadmin/operations.html
# ---------------------------------------------------------------------------

class TestOperationsTemplate:
    HTML = _tpl("superadmin/operations.html")

    def test_has_refresh_data(self):
        assert "/superadmin/action/refresh-data" in self.HTML

    def test_has_refresh_universe(self):
        assert "/superadmin/action/refresh-universe" in self.HTML

    def test_has_evaluate_regime(self):
        assert "/superadmin/action/evaluate-regime" in self.HTML

    def test_has_seed_crypto(self):
        assert "/superadmin/action/seed-crypto" in self.HTML

    def test_has_full_setup(self):
        assert "/superadmin/action/full-setup" in self.HTML

    def test_has_ping_worker(self):
        assert "/superadmin/action/ping-worker" in self.HTML

    def test_has_scope_options(self):
        assert "ASX200" in self.HTML
        assert "ASX300" in self.HTML
        assert "ALL_LISTED" in self.HTML

    def test_mentions_custom_stocks(self):
        assert "custom" in self.HTML.lower()

    def test_has_regime_section(self):
        assert "regime" in self.HTML.lower()


# ---------------------------------------------------------------------------
# 4. Template: base.html sidebar
# ---------------------------------------------------------------------------

class TestBaseSidebar:
    HTML = _tpl("base.html")

    def test_central_ops_link(self):
        assert "/superadmin/operations" in self.HTML

    def test_central_ops_label(self):
        assert "Central Ops" in self.HTML


# ---------------------------------------------------------------------------
# 5. Source-code inspection (no imports required)
# ---------------------------------------------------------------------------

class TestSourceInspection:
    MAIN = MAIN_PY_PATH.read_text(encoding="utf-8")

    def _fn(self, name):
        m = re.search(r"def " + name + r".*?(?=\n@app|\Z)", self.MAIN, re.DOTALL)
        assert m, f"Could not find {name} in main.py"
        return m.group(0)

    def test_force_breakout_check_has_form_exchange_param(self):
        assert re.search(
            r"def action_force_breakout_check\([^)]*exchange\s*:\s*str\s*=\s*Form",
            self.MAIN, re.DOTALL)

    def test_force_breakout_check_passes_exchange_key(self):
        assert "exchange_key" in self._fn("action_force_breakout_check")

    def test_force_exit_check_has_form_exchange_param(self):
        assert re.search(
            r"def action_force_exit_check\([^)]*exchange\s*:\s*str\s*=\s*Form",
            self.MAIN, re.DOTALL)

    def test_force_exit_check_passes_exchange_key(self):
        assert "exchange_key" in self._fn("action_force_exit_check")

    def test_sa_refresh_universe_passes_none_org_id(self):
        assert "organization_id=None" in self._fn("sa_action_refresh_universe")

    def test_all_sa_routes_have_superadmin_guard(self):
        routes = [
            "sa_action_refresh_data",
            "sa_action_refresh_universe",
            "sa_action_evaluate_regime",
            "sa_action_seed_crypto",
            "sa_action_full_setup",
            "sa_action_ping_worker",
        ]
        for route in routes:
            body = self._fn(route)
            assert "_is_superadmin" in body, (
                f"{route} is missing _is_superadmin() guard"
            )

    def test_screening_has_cfg_keys_to_write(self):
        src = SCREENING_PY_PATH.read_text(encoding="utf-8")
        assert "cfg_keys_to_write" in src
        assert 'exchange_key == "CRYPTO"' in src
        assert 'startswith("CRYPTO_")' in src or "startswith('CRYPTO_')" in src


# ---------------------------------------------------------------------------
# 6. Auth guards on superadmin action routes
# ---------------------------------------------------------------------------

SUPERADMIN_ROUTES = [
    ("sa_action_refresh_data",     {"exchange": "ASX"}),
    ("sa_action_refresh_universe", {"scope": "ASX200"}),
    ("sa_action_evaluate_regime",  {"exchange": "ASX"}),
    ("sa_action_seed_crypto",      {"exchange": "CRYPTO_INDEPENDENTRESERVE"}),
    ("sa_action_full_setup",       {}),
    ("sa_action_ping_worker",      {}),
]


def _call_sa_route(fn_name, session, form_fields):
    import web.main as m
    fn = getattr(m, fn_name)
    req = _mock_request(session)
    kwargs = {"request": req}
    kwargs.update(form_fields)
    return _run(fn(**kwargs))


@pytest.mark.parametrize("fn_name,form_fields", SUPERADMIN_ROUTES)
def test_superadmin_route_blocks_unauthenticated(fn_name, form_fields):
    resp = _call_sa_route(fn_name, session={}, form_fields=form_fields)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


@pytest.mark.parametrize("fn_name,form_fields", SUPERADMIN_ROUTES)
def test_superadmin_route_blocks_org_admin(fn_name, form_fields):
    session = {"authenticated": True, "user_role": "org_admin", "organization_id": 1}
    resp = _call_sa_route(fn_name, session=session, form_fields=form_fields)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 7. sa_action_refresh_universe always passes organization_id=None
# ---------------------------------------------------------------------------

def test_sa_refresh_universe_org_id_is_none():
    import web.main as m
    req = _mock_request({"authenticated": True, "user_role": "superadmin",
                         "organization_id": 42})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.screening.refresh_universe", mock_task):
        _run(m.sa_action_refresh_universe(request=req, scope="ASX200"))

    assert captured.get("organization_id") is None, (
        f"sa_action_refresh_universe must pass organization_id=None; got {captured!r}"
    )


def test_sa_refresh_universe_scope_forwarded():
    import web.main as m
    req = _mock_request({"authenticated": True, "user_role": "superadmin"})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.screening.refresh_universe", mock_task):
        _run(m.sa_action_refresh_universe(request=req, scope="ALL_LISTED"))

    assert captured.get("scope") == "ALL_LISTED"


# ---------------------------------------------------------------------------
# 8. Exchange forwarding: breakout check
# ---------------------------------------------------------------------------

def test_force_breakout_check_forwards_crypto_exchange():
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 1})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.trading.check_entry_triggers", mock_task):
        _run(m.action_force_breakout_check(
            request=req, exchange="CRYPTO_INDEPENDENTRESERVE"))

    assert captured.get("exchange_key") == "CRYPTO_INDEPENDENTRESERVE", (
        f"exchange_key not forwarded; got {captured!r}"
    )


def test_force_breakout_check_forwards_asx():
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 1})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.trading.check_entry_triggers", mock_task):
        _run(m.action_force_breakout_check(request=req, exchange="ASX"))

    assert captured.get("exchange_key") == "ASX"


def test_force_breakout_check_requires_auth():
    import web.main as m
    resp = _run(m.action_force_breakout_check(request=_mock_request(), exchange="ASX"))
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 9. Exchange forwarding: exit check
# ---------------------------------------------------------------------------

def test_force_exit_check_forwards_exchange():
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 1})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.trading.check_exit_rules_task", mock_task):
        _run(m.action_force_exit_check(request=req, exchange="NYSE"))

    assert captured.get("exchange_key") == "NYSE", (
        f"exchange_key not forwarded; got {captured!r}"
    )


def test_force_exit_check_forwards_crypto_exchange():
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 1})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.trading.check_exit_rules_task", mock_task):
        _run(m.action_force_exit_check(
            request=req, exchange="CRYPTO_INDEPENDENTRESERVE"))

    assert captured.get("exchange_key") == "CRYPTO_INDEPENDENTRESERVE"


# ---------------------------------------------------------------------------
# 10. Session key regression
# ---------------------------------------------------------------------------

def test_action_refresh_universe_reads_organization_id_from_session():
    """Route must read session['organization_id'], not session['org_id']."""
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 99})
    captured = {}
    mock_task = MagicMock()
    mock_task.delay = lambda **kw: captured.update(kw)

    with patch("app.tasks.screening.refresh_universe", mock_task):
        _run(m.action_refresh_universe(request=req, scope="ASX200"))

    assert captured.get("organization_id") == 99, (
        f"action_refresh_universe must pass org_id=99; got {captured!r} -- "
        "probably reading session['org_id'] instead of session['organization_id']"
    )


def test_action_recategorise_labels_reads_organization_id_from_session():
    import web.main as m
    req = _mock_request({"authenticated": True, "organization_id": 77})
    captured = {}
    mock_recat = MagicMock()
    mock_recat.si = lambda **kw: captured.update(kw) or MagicMock()
    mock_refresh = MagicMock()
    mock_refresh.si = lambda **kw: MagicMock()

    # Route chains refresh_asx_sector_data -> recategorise_watchlist_labels via
    # celery.chain, invoking each task's .si(); assert org_id flows through .si().
    with patch("app.tasks.screening.recategorise_watchlist_labels", mock_recat), \
         patch("app.tasks.screening.refresh_asx_sector_data", mock_refresh):
        _run(m.action_recategorise_labels(request=req, force="0"))

    assert captured.get("organization_id") == 77, (
        f"action_recategorise_labels must pass org_id=77 via chain .si(); got {captured!r}"
    )
    assert captured.get("force") is False


# ---------------------------------------------------------------------------
# 11. evaluate_market_regime_task CRYPTO key fix (DB-level)
# ---------------------------------------------------------------------------

def _make_index_df():
    import numpy as np
    import pandas as pd
    dates = pd.date_range("2025-01-01", periods=220, freq="B")
    closes = np.linspace(80000.0, 89000.0, 220)
    return pd.DataFrame({
        "date": dates,
        "close": closes,
        "open": closes,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "volume": np.ones(220) * 1e9,
    })


def test_crypto_regime_writes_specific_key_for_active_org(db_session, org_and_account):
    """
    When exchange_key='CRYPTO', the task must write both last_market_regime_CRYPTO
    AND last_market_regime_CRYPTO_INDEPENDENTRESERVE for an org that has IR active.
    """
    from app.models.config import SystemConfig
    from app.screener.market_regime import MarketRegime

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="active_exchanges",
        value="ASX,CRYPTO_INDEPENDENTRESERVE",
        organization_id=org.id,
        group="system",
        label="Active Exchanges",
    ))
    db_session.commit()

    with patch("app.tasks.screening.get_price_history", return_value=_make_index_df()), \
         patch("app.tasks.screening.evaluate_market_regime",
               return_value=(MarketRegime.CAUTION, {})):
        from app.tasks.screening import evaluate_market_regime_task
        evaluate_market_regime_task.run(exchange_key="CRYPTO")

    db_session.expire_all()

    generic = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_CRYPTO",
        SystemConfig.organization_id == org.id,
    ).first()
    specific = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_CRYPTO_INDEPENDENTRESERVE",
        SystemConfig.organization_id == org.id,
    ).first()

    assert generic is not None, "last_market_regime_CRYPTO not written"
    assert specific is not None, (
        "last_market_regime_CRYPTO_INDEPENDENTRESERVE not written -- "
        "health page will show stale regime for IR org"
    )
    assert generic.value in ("BULL", "CAUTION", "BEAR")
    assert specific.value == generic.value


def test_crypto_regime_does_not_write_specific_key_for_asx_only_org(
        db_session, org_and_account):
    """Org with only ASX active must not receive a CRYPTO_IR regime key."""
    from app.models.config import SystemConfig
    from app.screener.market_regime import MarketRegime

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="active_exchanges", value="ASX",
        organization_id=org.id, group="system", label="Active Exchanges",
    ))
    db_session.commit()

    with patch("app.tasks.screening.get_price_history", return_value=_make_index_df()), \
         patch("app.tasks.screening.evaluate_market_regime",
               return_value=(MarketRegime.BULL, {})):
        from app.tasks.screening import evaluate_market_regime_task
        evaluate_market_regime_task.run(exchange_key="CRYPTO")

    db_session.expire_all()

    specific = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_CRYPTO_INDEPENDENTRESERVE",
        SystemConfig.organization_id == org.id,
    ).first()
    assert specific is None, (
        "ASX-only org should not receive a CRYPTO_INDEPENDENTRESERVE regime key"
    )


def test_crypto_regime_writes_generic_key_when_no_active_exchanges(
        db_session, org_and_account):
    """Generic last_market_regime_CRYPTO is always written for every active org."""
    from app.models.config import SystemConfig
    from app.screener.market_regime import MarketRegime

    org, _ = org_and_account
    # No active_exchanges row

    with patch("app.tasks.screening.get_price_history", return_value=_make_index_df()), \
         patch("app.tasks.screening.evaluate_market_regime",
               return_value=(MarketRegime.BEAR, {})):
        from app.tasks.screening import evaluate_market_regime_task
        evaluate_market_regime_task.run(exchange_key="CRYPTO")

    db_session.expire_all()

    generic = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_CRYPTO",
        SystemConfig.organization_id == org.id,
    ).first()
    assert generic is not None, "last_market_regime_CRYPTO must be written for all orgs"
    assert generic.value == "BEAR"
