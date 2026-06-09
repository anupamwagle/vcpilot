"""
Shared pytest fixtures for VCPilot's critical-path test suite.

Strategy: stand up an isolated in-memory SQLite database that mirrors the
production schema (all models are portable — no Postgres-specific column types),
and transparently redirect `app.database.get_db()` / `SessionLocal` to it for the
duration of each test. This lets us exercise the *real* production code paths
(Celery tasks, dashboard routes, MCP tools) — the exact code that shipped the
bugs described in STATUS.md — against a throwaway database, with zero risk to
the live org data.

Run with:  pytest tests/ -v   (from the project root, inside the app container)
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_module
from app.database import Base


@pytest.fixture()
def test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.models import all_models  # noqa: F401 — registers every mapped model
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def TestSessionLocal(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine, expire_on_commit=False)


@pytest.fixture()
def db_session(TestSessionLocal):
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def patch_get_db(monkeypatch, TestSessionLocal):
    """
    Redirect every `with get_db() as db:` / `SessionLocal()` call made by production
    code under test — Celery tasks, dashboard routes, MCP tools — to the isolated
    in-memory test database, so tests never touch the live org DB.

    `get_db` is defined in `app.database` and looks up `SessionLocal` in its own
    module globals at call time, so patching `database_module.SessionLocal` is
    sufficient regardless of how other modules imported `get_db`.
    """
    monkeypatch.setattr(database_module, "SessionLocal", TestSessionLocal)
    yield


@pytest.fixture()
def org_and_account(db_session):
    """A minimal org + account-tier + trading account, mirroring bootstrap data."""
    from app.models.account import Organization, Account, AccountTier, TierLevel, OrganizationTier

    tier = AccountTier(
        level=TierLevel.STANDARD, label="Standard", universe="ASX300",
        max_positions=5, max_risk_pct_per_trade=1.0, max_portfolio_heat_pct=10.0,
    )
    db_session.add(tier)
    db_session.flush()

    org = Organization(name="Test Org", tier=OrganizationTier.GOLD, is_active=True)
    db_session.add(org)
    db_session.flush()

    account = Account(
        name="Test Account", organization_id=org.id, tier_id=tier.id,
        capital_aud=1000.0, is_active=True, is_paper=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(org)
    db_session.refresh(account)
    return org, account


@pytest.fixture()
def open_crypto_position(db_session, org_and_account):
    """An open TRX-AUD crypto position — the exact shape involved in the reported bug."""
    from app.models.trade import Position, TradeStatus

    org, account = org_and_account
    pos = Position(
        ticker="TRX-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO",
        currency="AUD",
        account_id=account.id,
        organization_id=org.id,
        entry_date=date(2026, 6, 1),
        entry_price=0.20,
        qty=500,
        initial_stop=0.16,
        current_stop=0.16,
        status=TradeStatus.OPEN,
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


@pytest.fixture()
def watching_trx_item(db_session, org_and_account):
    """A WATCHING crypto watchlist item — mirrors the TRX item the user promoted."""
    from app.models.signal import Watchlist, WatchlistStatus

    org, _account = org_and_account
    w = Watchlist(
        ticker="TRX-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO",
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING,
        added_by="admin",
    )
    db_session.add(w)
    db_session.commit()
    db_session.refresh(w)
    return w
