"""
Watchlist VCP-persistence + enrichment performance regression suite.

Why this exists: the watchlist page used to re-run `detect_vcp` (and rebuild a
RuleEngine) for every row on every cold load, pulling full price history — the
5-second load the user hit. The fix persists VCP geometry on the Watchlist row
during the screener run and lets the dashboard read it instead of recomputing.

Layers:
  * pure (runs anywhere): `resolve_watchlist_geometry` + the persisted columns.
  * container: `_upsert_watchlist` persistence, and the dashboard enrichment
    fast-path (fresh persisted geometry => no price history / no detect_vcp) and
    compute-path (missing geometry => compute once + write back).
"""
from datetime import date

import pytest

from app.models.signal import Watchlist, WatchlistStatus
from app.screener.vcp import resolve_watchlist_geometry, VCPResult


# ──────────────────────────────────────────────────────────────────────────
# 1. Pure: geometry resolver (single source of truth, incl. fallback)
# ──────────────────────────────────────────────────────────────────────────

def test_geometry_uses_detected_pivot():
    g = resolve_watchlist_geometry(
        VCPResult(detected=True, pivot_price=10.0, stop_price=9.2,
                  contraction_count=4, base_weeks=8),
        close=9.8, high_52w=10.5, atr_14=0.3,
    )
    assert g["pivot_price"] == 10.0
    assert g["stop_price"] == 9.2
    assert g["target_price"] == pytest.approx(12.0)
    assert g["vcp_contractions"] == 4
    assert g["vcp_base_weeks"] == 8


def test_geometry_fallback_uses_52w_high_and_atr_stop():
    g = resolve_watchlist_geometry(VCPResult(), close=9.8, high_52w=10.5, atr_14=0.3)
    assert g["pivot_price"] == 10.5             # 52w high
    assert g["stop_price"] == pytest.approx(10.5 - 0.6)  # pivot - 2*ATR
    assert g["target_price"] == pytest.approx(12.6)
    assert g["vcp_contractions"] == 0
    assert g["vcp_base_weeks"] == 0


def test_geometry_fallback_pct_stop_when_no_atr():
    g = resolve_watchlist_geometry(VCPResult(), close=100.0, high_52w=0, atr_14=0)
    assert g["pivot_price"] == 100.0            # falls back to close
    assert g["stop_price"] == pytest.approx(92.0)  # -8%


def test_watchlist_persists_geometry_columns(db_session):
    w = Watchlist(
        ticker="BHP.AX", organization_id=1, status=WatchlistStatus.WATCHING,
        pivot_price=45.5, stop_price=41.9, target_price=54.6,
        vcp_contractions=3, vcp_base_weeks=7, vcp_computed_date=date(2026, 6, 29),
    )
    db_session.add(w)
    db_session.commit()
    db_session.refresh(w)
    assert float(w.pivot_price) == 45.5
    assert w.vcp_contractions == 3
    assert w.vcp_computed_date == date(2026, 6, 29)


# ──────────────────────────────────────────────────────────────────────────
# 2. Container: screener persistence
# ──────────────────────────────────────────────────────────────────────────

def test_upsert_watchlist_persists_vcp_geometry(db_session, org_and_account):
    """_upsert_watchlist(..., vcp_result=...) must store geometry + the bar date."""
    from app.tasks.screening import _upsert_watchlist
    from app.models.market import Stock, PriceBar

    org, _acct = org_and_account
    db_session.add(Stock(ticker="CSL.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD"))
    db_session.add(PriceBar(ticker="CSL.AX", exchange_key="ASX", date=date(2026, 6, 29),
                            open=280, high=300, low=270, close=295, volume=1_000_000,
                            high_52w=305, atr_14=5.0))
    db_session.commit()

    vcp = VCPResult(detected=False, pivot_price=300.0, stop_price=288.0,
                    contraction_count=2, base_weeks=5)
    _upsert_watchlist("CSL.AX", {"trend_x": True}, db_session, organization_id=org.id, vcp_result=vcp)
    db_session.commit()

    row = db_session.query(Watchlist).filter_by(ticker="CSL.AX", organization_id=org.id).first()
    assert row is not None
    assert float(row.pivot_price) == 300.0
    assert float(row.target_price) == pytest.approx(360.0)
    assert row.vcp_computed_date == date(2026, 6, 29)


# ──────────────────────────────────────────────────────────────────────────
# 3. Container: dashboard enrichment fast-path vs compute-path
# ──────────────────────────────────────────────────────────────────────────

def _item(ticker, **kw):
    base = {
        "ticker": ticker, "asset_type": "EQUITY", "exchange_key": "ASX",
        "currency": "AUD", "_wl_id": kw.get("wl_id"),
        "_latest_bar_date": kw.get("latest"),
        "_persisted": kw.get("persisted", {}),
    }
    return base


def test_enrich_fast_path_uses_persisted_without_price_history(db_session, org_and_account):
    """Fresh persisted geometry => pivot/stop/target read straight from the dict;
    NO PriceBar rows exist, proving detect_vcp / history are skipped."""
    from web.main import _enrich_watchlist_vcp_and_sizing
    org, _acct = org_and_account
    items = [_item(
        "BHP.AX", latest=date(2026, 6, 29),
        persisted={"pivot": 45.5, "stop": 41.9, "target": 54.6,
                   "contractions": 3, "weeks": 7, "computed_date": date(2026, 6, 29)},
    )]
    _enrich_watchlist_vcp_and_sizing(items, db_session, org.id)
    assert items[0]["pivot"] == 45.5
    assert items[0]["target"] == 54.6
    assert items[0]["vcp_contractions"] == 3
    # internal keys stripped
    assert "_persisted" not in items[0] and "_geo" not in items[0]


def test_enrich_compute_path_computes_and_writes_back(db_session, org_and_account):
    """Missing geometry + a stale/no computed_date => compute once from price bars
    and write the result back onto the Watchlist row for next time."""
    from web.main import _enrich_watchlist_vcp_and_sizing
    from app.models.market import PriceBar

    org, _acct = org_and_account
    row = Watchlist(ticker="NAB.AX", organization_id=org.id, status=WatchlistStatus.WATCHING,
                    exchange_key="ASX", asset_type="EQUITY", currency="AUD")
    db_session.add(row)
    # a few bars so resolve falls back to 52w-high pivot
    for d, c in [(date(2026, 6, 25), 30.0), (date(2026, 6, 26), 31.0), (date(2026, 6, 29), 32.0)]:
        db_session.add(PriceBar(ticker="NAB.AX", exchange_key="ASX", date=d,
                                open=c, high=c + 1, low=c - 1, close=c, volume=500000,
                                high_52w=35.0, atr_14=0.5, avg_vol_50=500000))
    db_session.commit()

    items = [_item("NAB.AX", wl_id=row.id, latest=date(2026, 6, 29), persisted={})]
    _enrich_watchlist_vcp_and_sizing(items, db_session, org.id)
    db_session.commit()

    assert items[0]["pivot"] == pytest.approx(35.0)  # 52w-high fallback
    db_session.refresh(row)
    assert row.pivot_price is not None and float(row.pivot_price) == pytest.approx(35.0)
    assert row.vcp_computed_date == date(2026, 6, 29)
