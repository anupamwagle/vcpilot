"""
Regression tests for the "I promoted TRX from Watchlist and nothing happened" bug.

Root cause chain (see STATUS.md "Watchlist Promotion Silent-Failure Fix"):
  1. The dashboard route optimistically flipped Watchlist.status -> SIGNALLED
     BEFORE confirming the Celery task was queued. The /watchlist view filters
     status == WATCHING, so the item vanished immediately.
  2. `.delay()` failed silently (worker offline / stale heartbeat) and the status
     was never rolled back — the item disappeared with no Signal ever created and
     no feedback anywhere in the UI or audit log.
  3. Separately, `promote_watchlist_item_task` itself had a silent no-op: if a
     Signal already existed for the ticker/date (e.g. created earlier by the
     screener), it flipped the watchlist item to SIGNALLED without creating a new
     Signal or writing any audit trail explaining why — another "nothing happened"
     path.

These tests assert the fixed behaviour for all three failure points.
"""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
from app.models.audit import AuditLog, AuditAction


# ──────────────────────────────────────────────────────────────────────────
# 1. Dashboard route: optimistic status flip must roll back on queue failure
# ──────────────────────────────────────────────────────────────────────────

def _fake_request(org_id, email="admin@astradigital.com.au", user_id=1):
    return SimpleNamespace(session={
        "authenticated": True,
        "organization_id": org_id,
        "email": email,
        "user_id": user_id,
    })


def test_promote_route_rolls_back_status_when_queueing_fails(db_session, org_and_account, watching_trx_item, monkeypatch):
    """
    The exact TRX scenario: Celery `.delay()` raises (broker/worker unavailable).
    The route must revert Watchlist.status back to WATCHING — so the item stays
    visible and actionable — and must write a TASK_ERROR audit entry that explains
    what happened. Previously the status stuck at SIGNALLED and the item just
    vanished with zero feedback.
    """
    from web.main import watchlist_promote
    import app.tasks.trading as trading_module

    org, _account = org_and_account

    def boom(*_args, **_kwargs):
        raise RuntimeError("[Errno 111] Connection refused — broker unavailable")

    monkeypatch.setattr(trading_module.promote_watchlist_item_task, "delay", boom)

    response = asyncio.run(
        watchlist_promote(_fake_request(org.id), watching_trx_item.id, db=db_session)
    )

    db_session.refresh(watching_trx_item)
    assert watching_trx_item.status == WatchlistStatus.WATCHING, (
        "Item must remain visible in the Watching view (status==WATCHING) after a "
        "failed queue attempt — this is precisely what made TRX 'disappear'."
    )
    assert response.status_code == 302
    assert "promotion_failed" in response.headers["location"], (
        "User must be redirected with feedback that the promotion failed, not silence."
    )

    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.TASK_ERROR,
        AuditLog.ticker == "TRX-AUD",
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None, "A TASK_ERROR audit entry must explain the failed promotion"
    assert "reverted to WATCHING" in log.message


def test_promote_route_keeps_signalled_status_when_queueing_succeeds(db_session, org_and_account, watching_trx_item, monkeypatch):
    """Sanity check: the happy path must still flip the item to SIGNALLED and queue the task."""
    from web.main import watchlist_promote
    import app.tasks.trading as trading_module

    org, _account = org_and_account
    calls = []
    monkeypatch.setattr(trading_module.promote_watchlist_item_task, "delay",
                        lambda *a, **kw: calls.append((a, kw)))

    response = asyncio.run(
        watchlist_promote(_fake_request(org.id), watching_trx_item.id, db=db_session)
    )

    db_session.refresh(watching_trx_item)
    assert watching_trx_item.status == WatchlistStatus.SIGNALLED
    assert len(calls) == 1, "promote_watchlist_item_task.delay must be queued exactly once"
    assert response.status_code == 302
    assert "promotion_queued" in response.headers["location"]


# ──────────────────────────────────────────────────────────────────────────
# 2. Celery task: duplicate-signal case must not be a silent no-op
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def trx_price_bar(db_session):
    """A valid latest price bar for TRX-AUD so the task doesn't bail on 'no price data'."""
    from app.models.market import PriceBar
    bar = PriceBar(ticker="TRX-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE",
                   date=date(2026, 6, 8), close=0.21, open=0.20, high=0.22, low=0.19, volume=100000)
    db_session.add(bar)
    db_session.commit()
    return bar


@pytest.fixture()
def patch_today(monkeypatch):
    """Pin get_current_date() (as imported inside the task) to a fixed date."""
    import app.tasks.trading as trading_module
    fixed = date(2026, 6, 8)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: fixed)
    return fixed


def test_promote_task_does_not_silently_noop_on_existing_signal(db_session, org_and_account, watching_trx_item,
                                                                  trx_price_bar, patch_today):
    """
    If a Signal already exists for this ticker/date (e.g. generated earlier by the
    screener), promotion must NOT silently flip the watchlist item to SIGNALLED with
    no trace. It must leave the existing signal alone (no duplicate), still mark the
    watchlist item as handled, and — critically — write an audit entry that names the
    existing signal so an admin can find out exactly what happened from the audit log.
    """
    import app.tasks.trading as trading_module

    org, _account = org_and_account
    today = patch_today

    existing_signal = Signal(
        ticker="TRX-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO",
        currency="AUD", signal_date=today, status=SignalStatus.SKIPPED,
        organization_id=org.id, close_price=0.21, pivot_price=0.21, stop_price=0.17,
    )
    db_session.add(existing_signal)
    db_session.commit()
    db_session.refresh(existing_signal)

    before_count = db_session.query(Signal).filter(Signal.ticker == "TRX-AUD").count()

    trading_module.promote_watchlist_item_task.run(
        watching_trx_item.id, org.id, "admin@astradigital.com.au", 1
    )

    after_count = db_session.query(Signal).filter(Signal.ticker == "TRX-AUD").count()
    assert after_count == before_count, "No duplicate Signal should be created"

    db_session.refresh(watching_trx_item)
    assert watching_trx_item.status == WatchlistStatus.SIGNALLED

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "TRX-AUD",
        AuditLog.action == AuditAction.TASK_ERROR,
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None, (
        "A clear audit entry must be written when promotion finds a pre-existing "
        "signal — otherwise the user just sees the item vanish with no explanation."
    )
    assert f"#{existing_signal.id}" in log.message
    assert "no new signal created" in log.message.lower() or "no duplicate" in log.message.lower()


def test_promote_task_reverts_status_when_no_price_data(db_session, org_and_account, watching_trx_item, monkeypatch):
    """
    If there's no price bar for the ticker, promotion can't proceed. The watchlist
    item must be reverted to WATCHING (so the user can retry) rather than left
    stranded as SIGNALLED with nothing to show for it, and the failure must be
    auditable.
    """
    import app.tasks.trading as trading_module

    org, _account = org_and_account
    # No PriceBar seeded — close_price resolves to 0.0

    trading_module.promote_watchlist_item_task.run(
        watching_trx_item.id, org.id, "admin@astradigital.com.au", 1
    )

    db_session.refresh(watching_trx_item)
    assert watching_trx_item.status == WatchlistStatus.WATCHING, (
        "Item must revert to WATCHING (stay visible/actionable) when promotion can't proceed"
    )

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "TRX-AUD",
        AuditLog.action == AuditAction.TASK_ERROR,
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None
    assert "no price data" in log.message.lower()
