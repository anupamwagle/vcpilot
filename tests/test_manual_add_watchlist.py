"""
Regression test for the "I added SPCX manually but it never appeared" bug.

Root cause: screen_single_ticker bailed out with `return` when a ticker had
insufficient price history (e.g. a newly-listed stock like SPCX with only ~10
trading days), creating NO watchlist entry and giving the user no feedback — the
manual add silently vanished.

Fix: a manual add ALWAYS lands on the watchlist. When there isn't enough history
to screen, it's added as WATCHING with an "awaiting data" note, so it's visible
and gets re-screened by the scheduled refresh/screen as history accumulates.
"""
import pandas as pd
import pytest

import app.tasks.screening as screening
from app.models.signal import Watchlist, WatchlistStatus
from app.models.market import Stock


class _DummyNotifier:
    def __getattr__(self, _name):
        return lambda *a, **k: True


@pytest.fixture(autouse=True)
def _patch_externals(monkeypatch):
    # No network: notifier is a no-op for these tests.
    monkeypatch.setattr(screening, "get_notifier", lambda organization_id=None: _DummyNotifier())


def test_insufficient_history_still_adds_to_watchlist(db_session, org_and_account, monkeypatch):
    org, _account = org_and_account
    # Simulate a brand-new ticker: yfinance returns only a handful of bars.
    monkeypatch.setattr(screening, "get_price_history",
                        lambda ticker, period="2y": pd.DataFrame({"close": [1, 2, 3]}))

    screening.screen_single_ticker.run(
        ticker="SPCX", exchange_key="NASDAQ", asset_type="EQUITY",
        currency="USD", organization_id=org.id,
    )

    wl = db_session.query(Watchlist).filter(Watchlist.ticker == "SPCX").first()
    assert wl is not None, "manual add must create a watchlist entry even with thin data"
    assert wl.status == WatchlistStatus.WATCHING
    assert wl.exchange_key == "NASDAQ"
    assert "waiting" in (wl.notes or "").lower()  # "Awaiting price data ..."

    # And the Stock row is persisted active, so the scheduled NASDAQ screen
    # will pick it up and re-screen it as history accumulates.
    stock = db_session.query(Stock).filter(Stock.ticker == "SPCX").first()
    assert stock is not None and stock.is_active and stock.exchange_key == "NASDAQ"


def test_no_data_at_all_still_adds_to_watchlist(db_session, org_and_account, monkeypatch):
    org, _account = org_and_account
    monkeypatch.setattr(screening, "get_price_history", lambda ticker, period="2y": None)

    screening.screen_single_ticker.run(
        ticker="ZZZZ", exchange_key="NASDAQ", asset_type="EQUITY",
        currency="USD", organization_id=org.id,
    )

    wl = db_session.query(Watchlist).filter(Watchlist.ticker == "ZZZZ").first()
    assert wl is not None and wl.status == WatchlistStatus.WATCHING
