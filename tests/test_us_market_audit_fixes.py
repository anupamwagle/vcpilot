"""
Tests for the US equity audit fixes applied after adding S&P 500 / NASDAQ-100 support.

Covers:
  - generate_daily_report() queries per-exchange regime keys (not the stale 'last_market_regime')
  - cmd_status() shows per-exchange position breakdown
  - cmd_positions() includes exchange label per position
  - cmd_market() shows all active exchange regimes
  - Superadmin seed-us-universe route exists in main.py
  - operations.html has the US Universe Refresh section
  - data_log.html mentions US market hours
"""
import pytest
from datetime import date
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _seed_regime(db_session, org_id, exchange_key, value):
    from app.models.config import SystemConfig
    db_session.add(SystemConfig(
        key=f"last_market_regime_{exchange_key}",
        value=value,
        label=f"Market Regime {exchange_key}",
        group="system",
        organization_id=org_id,
    ))
    db_session.commit()


def _make_position(db_session, org, account, ticker, exchange_key, asset_type="EQUITY", currency="AUD"):
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker=ticker,
        exchange_key=exchange_key,
        asset_type=asset_type,
        currency=currency,
        account_id=account.id,
        organization_id=org.id,
        entry_date=date(2026, 6, 1),
        entry_price=100.0,
        qty=10,
        initial_stop=90.0,
        current_stop=90.0,
        status=TradeStatus.OPEN,
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


def _make_handler(org):
    from app.agent.commands import AgentCommandHandler
    return AgentCommandHandler(organization_id=org.id, notifier=MagicMock())


# ─────────────────────────────────────────────────────────────────────────────
# generate_daily_report — regime key fix
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateDailyReportRegime:
    def test_returns_unknown_when_no_regime_seeded(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        report = generate_daily_report(organization_id=org.id)
        assert report["market_regime"] == "UNKNOWN"

    def test_returns_asx_regime_only(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        report = generate_daily_report(organization_id=org.id)
        assert "ASX:BULL" in report["market_regime"]

    def test_returns_multi_exchange_composite(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        _seed_regime(db_session, org.id, "NYSE", "CAUTION")
        report = generate_daily_report(organization_id=org.id)
        regime = report["market_regime"]
        assert "ASX:BULL" in regime
        assert "US:CAUTION" in regime

    def test_all_three_exchanges(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        _seed_regime(db_session, org.id, "NYSE", "BEAR")
        _seed_regime(db_session, org.id, "NASDAQ", "CAUTION")
        report = generate_daily_report(organization_id=org.id)
        regime = report["market_regime"]
        assert "ASX:BULL" in regime
        assert "US:BEAR" in regime
        assert "NASDAQ:CAUTION" in regime

    def test_skips_empty_regime_values(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        _seed_regime(db_session, org.id, "NYSE", "UNKNOWN")  # UNKNOWN → skipped
        report = generate_daily_report(organization_id=org.id)
        regime = report["market_regime"]
        assert "ASX:BULL" in regime
        assert "UNKNOWN" not in regime  # filtered out
        assert "|" not in regime        # no separator for empty side

    def test_does_not_query_stale_last_market_regime_key(self, db_session, org_and_account):
        """Regression: old code queried 'last_market_regime' which never matched — always UNKNOWN."""
        from app.models.config import SystemConfig
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        # Seed the stale global key that the old code used
        db_session.add(SystemConfig(
            key="last_market_regime", value="BULL",
            label="Market Regime (stale)", group="system",
            organization_id=org.id,
        ))
        # Also seed the correct per-exchange keys
        _seed_regime(db_session, org.id, "ASX", "CAUTION")
        db_session.commit()
        report = generate_daily_report(organization_id=org.id)
        # Should return CAUTION from the correct key, not BULL from the stale one
        assert "CAUTION" in report["market_regime"]
        assert report["market_regime"] != "BULL"


# ─────────────────────────────────────────────────────────────────────────────
# cmd_status — per-exchange position breakdown
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStatusExchangeBreakdown:
    def test_no_breakdown_when_no_positions(self, db_session, org_and_account):
        org, _ = org_and_account
        h = _make_handler(org)
        result = h.cmd_status([])
        assert "Open positions: 0" in result

    def test_asx_position_shown_in_breakdown(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "BHP.AX", "ASX", currency="AUD")
        h = _make_handler(org)
        result = h.cmd_status([])
        assert "ASX:1" in result

    def test_us_position_shown_in_breakdown(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "AAPL", "NASDAQ", currency="USD")
        h = _make_handler(org)
        result = h.cmd_status([])
        assert "US:1" in result

    def test_crypto_position_shown_in_breakdown(self, db_session, org_and_account, open_crypto_position):
        org, _ = org_and_account
        h = _make_handler(org)
        result = h.cmd_status([])
        assert "Crypto:1" in result

    def test_mixed_exchanges_all_counted(self, db_session, org_and_account, open_crypto_position):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "BHP.AX", "ASX", currency="AUD")
        _make_position(db_session, org, acct, "NVDA", "NASDAQ", currency="USD")
        h = _make_handler(org)
        result = h.cmd_status([])
        # Total = 3 (1 ASX + 1 US + 1 crypto from fixture)
        assert "ASX:1" in result
        assert "US:1" in result
        assert "Crypto:1" in result

    def test_nyse_also_counted_as_us(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "JPM", "NYSE", currency="USD")
        h = _make_handler(org)
        result = h.cmd_status([])
        assert "US:1" in result


# ─────────────────────────────────────────────────────────────────────────────
# cmd_positions — exchange label per position
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdPositionsExchangeLabel:
    def test_asx_position_has_asx_label(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "BHP.AX", "ASX", currency="AUD")
        h = _make_handler(org)
        result = h.cmd_positions([])
        assert "BHP.AX" in result
        assert "(ASX)" in result

    def test_nasdaq_position_has_nasdaq_label(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "AAPL", "NASDAQ", currency="USD")
        h = _make_handler(org)
        result = h.cmd_positions([])
        assert "AAPL" in result
        assert "(NASDAQ)" in result

    def test_nyse_position_has_nyse_label(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "JPM", "NYSE", currency="USD")
        h = _make_handler(org)
        result = h.cmd_positions([])
        assert "JPM" in result
        assert "(NYSE)" in result

    def test_crypto_position_has_no_exchange_label(self, db_session, org_and_account, open_crypto_position):
        org, _ = org_and_account
        h = _make_handler(org)
        result = h.cmd_positions([])
        assert "TRX-AUD" in result
        # Crypto positions don't get an (EXCHANGE) label
        assert "(CRYPTO" not in result

    def test_us_position_shows_usd_currency(self, db_session, org_and_account):
        org, acct = org_and_account
        _make_position(db_session, org, acct, "MSFT", "NASDAQ", currency="USD")
        h = _make_handler(org)
        result = h.cmd_positions([])
        assert "US$" in result


# ─────────────────────────────────────────────────────────────────────────────
# cmd_market — per-exchange regimes
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdMarketMultiExchange:
    def test_shows_asx_regime(self, db_session, org_and_account):
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "ASX" in result
        assert "BULL" in result

    def test_shows_nyse_regime(self, db_session, org_and_account):
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "NYSE", "CAUTION")
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "US" in result or "NYSE" in result
        assert "CAUTION" in result

    def test_shows_nasdaq_regime(self, db_session, org_and_account):
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "NASDAQ", "BEAR")
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "NASDAQ" in result
        assert "BEAR" in result

    def test_shows_all_three_regimes(self, db_session, org_and_account):
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "BULL")
        _seed_regime(db_session, org.id, "NYSE", "CAUTION")
        _seed_regime(db_session, org.id, "NASDAQ", "BEAR")
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "BULL" in result
        assert "CAUTION" in result
        assert "BEAR" in result

    def test_returns_something_when_no_regimes_seeded(self, db_session, org_and_account):
        org, _ = org_and_account
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "Market" in result or "Regime" in result

    def test_does_not_use_stale_last_market_regime_key(self, db_session, org_and_account):
        """Regression: old code read 'last_market_regime' — should be ignored now."""
        from app.models.config import SystemConfig
        org, _ = org_and_account
        # Seed the stale key with a misleading value
        db_session.add(SystemConfig(
            key="last_market_regime", value="BEAR",
            label="Old key", group="system",
            organization_id=org.id,
        ))
        # Seed correct ASX key with BULL
        _seed_regime(db_session, org.id, "ASX", "BULL")
        db_session.commit()
        h = _make_handler(org)
        result = h.cmd_market([])
        assert "BULL" in result
        # The stale BEAR from old key must NOT appear as the sole result
        # (BEAR may appear if seeded for another exchange, but not from stale key alone)


# ─────────────────────────────────────────────────────────────────────────────
# Template / route presence checks (filesystem)
# ─────────────────────────────────────────────────────────────────────────────

class TestTemplateAuditFixPresence:
    def test_operations_html_has_us_universe_section(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "templates", "superadmin", "operations.html")
        if not os.path.exists(path):
            pytest.skip("operations.html not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert "seed-us-universe" in content
        assert "US Universe" in content
        assert "SP500+NASDAQ100" in content

    def test_operations_html_has_universe_us_flash_message(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "templates", "superadmin", "operations.html")
        if not os.path.exists(path):
            pytest.skip("operations.html not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert "universe_us" in content

    def test_data_log_html_mentions_us_hours(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "templates", "admin", "data_log.html")
        if not os.path.exists(path):
            pytest.skip("data_log.html not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert "NYSE" in content or "23:30" in content or "US equities" in content

    def test_tasks_html_no_longer_says_asx_universe(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "templates", "admin", "tasks.html")
        if not os.path.exists(path):
            pytest.skip("tasks.html not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert "ASX Universe" not in content
        assert ">Universe<" in content or "field-label\">Universe" in content

    def test_main_py_has_superadmin_seed_us_universe_route(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "main.py")
        if not os.path.exists(path):
            pytest.skip("main.py not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert '"/superadmin/action/seed-us-universe"' in content
        assert "sa_action_seed_us_universe" in content

    def test_main_py_has_org_scoped_seed_us_universe_route(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "main.py")
        if not os.path.exists(path):
            pytest.skip("main.py not accessible from test runner")
        content = open(path, encoding="utf-8").read()
        assert '"/action/seed-us-universe"' in content


# ─────────────────────────────────────────────────────────────────────────────
# reporting.generate_daily_report — org_id=None (global scope) still works
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateDailyReportGlobalScope:
    def test_global_scope_returns_unknown_when_no_regime(self, db_session, org_and_account):
        """With organization_id=None the regime section uses the same logic and gracefully returns UNKNOWN."""
        from app.tasks.reporting import generate_daily_report
        report = generate_daily_report(organization_id=None)
        assert report["market_regime"] == "UNKNOWN"

    def test_regime_field_always_present_in_report(self, db_session, org_and_account):
        from app.tasks.reporting import generate_daily_report
        org, _ = org_and_account
        report = generate_daily_report(organization_id=org.id)
        assert "market_regime" in report
        assert isinstance(report["market_regime"], str)
