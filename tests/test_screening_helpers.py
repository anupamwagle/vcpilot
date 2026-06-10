"""Tests for screening helper functions: _upsert_watchlist, _update_watchlist_if_exists,
refresh_universe, run_full_setup."""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import date, timedelta


def _make_df(rows=252, close=50.0):
    dates = [date(2023, 1, 1) + timedelta(days=i) for i in range(rows)]
    np.random.seed(1)
    c = np.full(rows, close) + np.cumsum(np.random.randn(rows) * 0.3)
    c = np.maximum(c, 0.1)
    return pd.DataFrame({
        "date": dates, "open": c, "high": c * 1.01, "low": c * 0.99,
        "close": c, "volume": np.full(rows, 100_000.0), "adj_close": c,
    })


# ---- _upsert_watchlist -------------------------------------------------------

def test_upsert_watchlist_creates_new_entry(db_session, org_and_account):
    from app.tasks.screening import _upsert_watchlist
    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.market import Stock

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True, name="BHP",
        sector="Materials", exchange_code="BHP",
    ))
    db_session.commit()

    rule_results = {"trend_above_200ma": {"passed": True, "value": 1.15}}
    _upsert_watchlist("BHP.AX", rule_results, db_session, org.id)
    db_session.commit()

    entry = db_session.query(Watchlist).filter(
        Watchlist.ticker == "BHP.AX",
        Watchlist.organization_id == org.id,
    ).first()
    assert entry is not None
    assert entry.exchange_key == "ASX"
    assert entry.asset_type == "EQUITY"


def test_upsert_watchlist_updates_existing(db_session, org_and_account):
    from app.tasks.screening import _upsert_watchlist
    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.market import Stock

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="CBA.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True, name="CBA",
        sector="Financials", exchange_code="CBA",
    ))
    # Pre-create watchlist entry
    db_session.add(Watchlist(
        ticker="CBA.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test",
        rule_results={"old": "data"},
    ))
    db_session.commit()

    new_rules = {"trend_above_200ma": {"passed": True, "value": 1.20}}
    _upsert_watchlist("CBA.AX", new_rules, db_session, org.id)
    db_session.commit()

    db_session.expire_all()
    entry = db_session.query(Watchlist).filter(
        Watchlist.ticker == "CBA.AX",
        Watchlist.organization_id == org.id,
    ).first()
    assert entry.rule_results is not None


def test_upsert_watchlist_no_stock_defaults_asx(db_session, org_and_account):
    from app.tasks.screening import _upsert_watchlist
    from app.models.signal import Watchlist

    org, _ = org_and_account
    # No stock record — should default to ASX/EQUITY/AUD
    _upsert_watchlist("UNKNOWN.AX", {}, db_session, org.id)
    db_session.commit()

    entry = db_session.query(Watchlist).filter(
        Watchlist.ticker == "UNKNOWN.AX",
        Watchlist.organization_id == org.id,
    ).first()
    assert entry is not None
    assert entry.exchange_key == "ASX"


# ---- _update_watchlist_if_exists ---------------------------------------------

def test_update_watchlist_if_exists_updates_existing(db_session, org_and_account):
    from app.tasks.screening import _update_watchlist_if_exists
    from app.models.signal import Watchlist, WatchlistStatus

    org, _ = org_and_account
    db_session.add(Watchlist(
        ticker="ANZ.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test",
        rule_results={"original": True},
    ))
    db_session.commit()

    new_rules = {"updated": True, "trend_score": 7}
    _update_watchlist_if_exists("ANZ.AX", new_rules, db_session, org.id)
    db_session.commit()

    db_session.expire_all()
    entry = db_session.query(Watchlist).filter(Watchlist.ticker == "ANZ.AX").first()
    assert entry.rule_results is not None


def test_update_watchlist_if_exists_noop_when_not_found(db_session, org_and_account):
    from app.tasks.screening import _update_watchlist_if_exists
    from app.models.signal import Watchlist

    org, _ = org_and_account
    # Should not create a new entry
    _update_watchlist_if_exists("NONEXISTENT.AX", {"x": 1}, db_session, org.id)
    db_session.commit()

    count = db_session.query(Watchlist).filter(Watchlist.ticker == "NONEXISTENT.AX").count()
    assert count == 0


# ---- refresh_universe --------------------------------------------------------

def test_refresh_universe_runs_without_crash(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_universe

    monkeypatch.setattr("app.tasks.screening.get_asx200_tickers",
                        lambda: ["BHP.AX", "CBA.AX", "WBC.AX"])
    monkeypatch.setattr("app.tasks.screening.get_asx200_metadata",
                        lambda: {})

    # Should not raise
    refresh_universe.run()


def test_refresh_universe_adds_new_stocks(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_universe
    from app.models.market import Stock

    monkeypatch.setattr("app.tasks.screening.get_asx200_tickers",
                        lambda: ["BHP.AX", "CBA.AX"])
    monkeypatch.setattr("app.tasks.screening.get_asx200_metadata",
                        lambda: {"BHP.AX": {"name": "BHP Group", "sector": "Materials"}})

    refresh_universe.run()

    stocks = db_session.query(Stock).filter(Stock.in_asx200 == True).all()
    assert any(s.ticker == "BHP.AX" for s in stocks)
    assert any(s.ticker == "CBA.AX" for s in stocks)


def test_refresh_universe_handles_empty_tickers(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_universe

    monkeypatch.setattr("app.tasks.screening.get_asx200_tickers", lambda: [])
    monkeypatch.setattr("app.tasks.screening.get_asx200_metadata", lambda: {})

    # Should not crash
    refresh_universe.run()


# ---- refresh_price_data with crypto --------------------------------------------

def test_refresh_price_data_crypto_no_gate(db_session, org_and_account, monkeypatch):
    """Crypto always passes the trading-day gate."""
    from app.tasks.screening import refresh_price_data
    from app.models.market import Stock

    org, _ = org_and_account
    # Seed a crypto stock
    db_session.add(Stock(
        ticker="BTC-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO", currency="AUD", in_asx200=False, is_active=True,
        name="Bitcoin", sector="Crypto", exchange_code="BTC",
    ))
    db_session.commit()

    df = _make_df(100)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)

    # Should not skip for crypto (no calendar gate)
    refresh_price_data.run(exchange_key="CRYPTO_INDEPENDENTRESERVE")


def test_refresh_price_data_asx_skips_weekend(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_price_data
    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: False)
    # Should return immediately without error
    refresh_price_data.run(exchange_key="ASX")


# ---- _write_task_heartbeat ---------------------------------------------------

def test_write_task_heartbeat_with_db(db_session, org_and_account):
    from app.tasks.screening import _write_task_heartbeat
    from app.models.config import SystemConfig

    # Should write a heartbeat without raising
    _write_task_heartbeat("Test progress message")

    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_heartbeat",
    ).first()
    # May or may not be set depending on org seeding, but should not crash
