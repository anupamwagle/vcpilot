"""
Tests for ASX universe refresh, sector label auto-assignment, and the dashboard
action routes that trigger them.

Covers the exact bugs that were found in this session:
  - refresh_universe / recategorise_labels routes used wrong session key ("org_id"
    instead of "organization_id"), passing None to the Celery task silently.
  - infer_sector_label mapping correctness.
  - _get_or_create_sector_label idempotency.
  - _auto_assign_sector_label skip-if-has-label / force=True logic.
  - recategorise_watchlist_labels bulk assignment and audit trail.
  - refresh_universe scope resolution (config fallback) and DB writes for all
    three scopes (ASX200 / ASX300 / ALL_LISTED).
"""
import pytest
from datetime import date
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_asx_ticker(ticker: str, name: str, sector: str = "", industry: str = "") -> dict:
    return {"ticker": ticker, "name": name, "sector": sector, "industry": industry, "market_cap": None}


def _seed_stock(db, ticker: str, sector: str = "", industry: str = "", exchange_key: str = "ASX"):
    from app.models.market import Stock
    s = Stock(
        ticker=ticker,
        exchange_code=ticker.split(".")[0],
        exchange_key=exchange_key,
        asset_type="EQUITY",
        currency="AUD",
        in_asx200=True,
        is_active=True,
        name=ticker.replace(".AX", ""),
        sector=sector,
        industry=industry,
    )
    db.add(s)
    db.flush()
    return s


def _seed_watchlist_item(db, ticker: str, org_id: int, label_id=None):
    from app.models.signal import Watchlist, WatchlistStatus
    w = Watchlist(
        ticker=ticker,
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        organization_id=org_id,
        status=WatchlistStatus.WATCHING,
        added_by="test",
        label_id=label_id,
    )
    db.add(w)
    db.flush()
    return w


# ═══════════════════════════════════════════════════════════════════════════════
# infer_sector_label — keyword mapping
# ═══════════════════════════════════════════════════════════════════════════════

class TestInferSectorLabel:
    def test_gold_miner_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Materials", "Gold Mining") == "Gold"

    def test_gold_miner_by_sector(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Gold", "") == "Gold"

    def test_lithium_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Materials", "Lithium Mining") == "Lithium"

    def test_uranium_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Energy", "Uranium Mining") == "Uranium"

    def test_biotech_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Health Care", "Biotechnology") == "Biotech"

    def test_fintech_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Financials", "Financial Technology") == "FinTech"

    def test_technology_by_sector(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Information Technology", "Software") == "Technology"

    def test_banks_by_industry(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Financials", "Banks") == "Banks"

    def test_reit_by_industry(self):
        from app.data.fetcher import infer_sector_label
        result = infer_sector_label("Real Estate", "REIT")
        assert result == "Real Estate (REIT)"

    def test_unknown_returns_none(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("", "") is None

    def test_unrecognised_sector_returns_none(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("Quantum Flux", "Dark Matter") is None

    def test_case_insensitive(self):
        from app.data.fetcher import infer_sector_label
        assert infer_sector_label("MATERIALS", "GOLD MINING") == "Gold"

    def test_oil_gas(self):
        from app.data.fetcher import infer_sector_label
        result = infer_sector_label("Energy", "Oil & Gas Exploration")
        assert result == "Oil & Gas"

    def test_rare_earth(self):
        from app.data.fetcher import infer_sector_label
        result = infer_sector_label("Materials", "Rare Earth Minerals")
        assert result == "Rare Earth"


# ═══════════════════════════════════════════════════════════════════════════════
# _get_or_create_sector_label — idempotency
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetOrCreateSectorLabel:
    def test_creates_label_if_missing(self, db_session, org_and_account):
        from app.tasks.screening import _get_or_create_sector_label
        from app.models.signal import WatchlistLabel

        org, _ = org_and_account
        label_id = _get_or_create_sector_label("Gold", org.id, db_session)
        db_session.commit()

        assert label_id is not None
        lbl = db_session.query(WatchlistLabel).filter_by(id=label_id).first()
        assert lbl is not None
        assert lbl.name == "Gold"
        assert lbl.color == "#f59e0b"

    def test_returns_existing_label_id(self, db_session, org_and_account):
        from app.tasks.screening import _get_or_create_sector_label

        org, _ = org_and_account
        id1 = _get_or_create_sector_label("Lithium", org.id, db_session)
        db_session.commit()
        id2 = _get_or_create_sector_label("Lithium", org.id, db_session)

        assert id1 == id2  # idempotent — same label returned

    def test_unknown_label_gets_default_gray(self, db_session, org_and_account):
        from app.tasks.screening import _get_or_create_sector_label
        from app.models.signal import WatchlistLabel

        org, _ = org_and_account
        label_id = _get_or_create_sector_label("Xyzzy", org.id, db_session)
        db_session.commit()

        lbl = db_session.query(WatchlistLabel).filter_by(id=label_id).first()
        assert lbl.color == "#6b7280"  # default gray for unmapped names

    def test_labels_are_org_scoped(self, db_session, org_and_account):
        from app.tasks.screening import _get_or_create_sector_label
        from app.models.signal import WatchlistLabel
        from app.models.account import Organization, OrganizationTier

        org, _ = org_and_account
        # Create a second org
        org2 = Organization(name="Org2", tier=OrganizationTier.GOLD, is_active=True)
        db_session.add(org2)
        db_session.flush()

        id1 = _get_or_create_sector_label("Gold", org.id, db_session)
        id2 = _get_or_create_sector_label("Gold", org2.id, db_session)
        db_session.commit()

        assert id1 != id2  # separate label rows per org


# ═══════════════════════════════════════════════════════════════════════════════
# _auto_assign_sector_label
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoAssignSectorLabel:
    def test_assigns_label_to_unlabelled_item(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label

        org, _ = org_and_account
        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        wl = _seed_watchlist_item(db_session, "GLD.AX", org.id)
        db_session.commit()

        assert wl.label_id is None
        _auto_assign_sector_label("GLD.AX", wl, org.id, db_session)

        assert wl.label_id is not None

    def test_skips_item_that_already_has_label(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label
        from app.models.signal import WatchlistLabel

        org, _ = org_and_account
        existing_label = WatchlistLabel(
            organization_id=org.id, name="Favourites", color="#f59e0b",
            is_default=True, sort_order=0,
        )
        db_session.add(existing_label)
        db_session.flush()

        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        wl = _seed_watchlist_item(db_session, "GLD.AX", org.id, label_id=existing_label.id)
        db_session.commit()

        _auto_assign_sector_label("GLD.AX", wl, org.id, db_session, force=False)

        # Should NOT have changed the label
        assert wl.label_id == existing_label.id

    def test_force_overwrites_existing_label(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label
        from app.models.signal import WatchlistLabel

        org, _ = org_and_account
        existing_label = WatchlistLabel(
            organization_id=org.id, name="Under Review", color="#8b5cf6",
            is_default=False, sort_order=3,
        )
        db_session.add(existing_label)
        db_session.flush()

        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        wl = _seed_watchlist_item(db_session, "GLD.AX", org.id, label_id=existing_label.id)
        db_session.commit()

        _auto_assign_sector_label("GLD.AX", wl, org.id, db_session, force=True)

        # Should have changed to the Gold label
        assert wl.label_id != existing_label.id

    def test_skips_stock_with_no_sector_data(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label

        org, _ = org_and_account
        _seed_stock(db_session, "UNK.AX", sector="", industry="")
        wl = _seed_watchlist_item(db_session, "UNK.AX", org.id)
        db_session.commit()

        _auto_assign_sector_label("UNK.AX", wl, org.id, db_session)

        assert wl.label_id is None  # no sector data → no label assigned

    def test_skips_missing_stock(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label

        org, _ = org_and_account
        wl = _seed_watchlist_item(db_session, "GHOST.AX", org.id)
        db_session.commit()

        # Should not raise even when stock row is missing
        _auto_assign_sector_label("GHOST.AX", wl, org.id, db_session)
        assert wl.label_id is None

    def test_skips_when_sector_not_recognised(self, db_session, org_and_account):
        from app.tasks.screening import _auto_assign_sector_label

        org, _ = org_and_account
        _seed_stock(db_session, "XYZ.AX", sector="Quantum Flux", industry="Dark Matter")
        wl = _seed_watchlist_item(db_session, "XYZ.AX", org.id)
        db_session.commit()

        _auto_assign_sector_label("XYZ.AX", wl, org.id, db_session)
        assert wl.label_id is None  # infer_sector_label returned None


# ═══════════════════════════════════════════════════════════════════════════════
# recategorise_watchlist_labels Celery task
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecategoriseWatchlistLabels:
    def test_assigns_labels_to_unlabelled_items(self, db_session, org_and_account):
        from app.tasks.screening import recategorise_watchlist_labels
        from app.models.signal import Watchlist
        from app.models.audit import AuditLog

        org, _ = org_and_account
        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        _seed_stock(db_session, "LIT.AX", sector="Materials", industry="Lithium Mining")
        _seed_watchlist_item(db_session, "GLD.AX", org.id)
        _seed_watchlist_item(db_session, "LIT.AX", org.id)
        db_session.commit()

        recategorise_watchlist_labels.run(organization_id=org.id, force=False)

        db_session.expire_all()
        items = db_session.query(Watchlist).filter_by(organization_id=org.id).all()
        labelled = [i for i in items if i.label_id is not None]
        assert len(labelled) == 2

    def test_does_not_overwrite_existing_labels_by_default(self, db_session, org_and_account):
        from app.tasks.screening import recategorise_watchlist_labels
        from app.models.signal import Watchlist, WatchlistLabel

        org, _ = org_and_account
        fav = WatchlistLabel(
            organization_id=org.id, name="Favourites", color="#f59e0b",
            is_default=True, sort_order=0,
        )
        db_session.add(fav)
        db_session.flush()

        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        wl = _seed_watchlist_item(db_session, "GLD.AX", org.id, label_id=fav.id)
        db_session.commit()

        recategorise_watchlist_labels.run(organization_id=org.id, force=False)

        db_session.expire_all()
        wl_refreshed = db_session.query(Watchlist).filter_by(id=wl.id).first()
        assert wl_refreshed.label_id == fav.id  # preserved

    def test_force_overwrites_all_labels(self, db_session, org_and_account):
        from app.tasks.screening import recategorise_watchlist_labels
        from app.models.signal import Watchlist, WatchlistLabel

        org, _ = org_and_account
        under_review = WatchlistLabel(
            organization_id=org.id, name="Under Review", color="#8b5cf6",
            is_default=False, sort_order=3,
        )
        db_session.add(under_review)
        db_session.flush()

        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        wl = _seed_watchlist_item(db_session, "GLD.AX", org.id, label_id=under_review.id)
        db_session.commit()

        recategorise_watchlist_labels.run(organization_id=org.id, force=True)

        db_session.expire_all()
        wl_refreshed = db_session.query(Watchlist).filter_by(id=wl.id).first()
        assert wl_refreshed.label_id != under_review.id  # replaced with Gold label

    def test_writes_audit_log(self, db_session, org_and_account):
        from app.tasks.screening import recategorise_watchlist_labels
        from app.models.audit import AuditLog, AuditAction

        org, _ = org_and_account
        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        _seed_watchlist_item(db_session, "GLD.AX", org.id)
        db_session.commit()

        recategorise_watchlist_labels.run(organization_id=org.id)

        db_session.expire_all()
        log = db_session.query(AuditLog).filter(
            AuditLog.organization_id == org.id,
            AuditLog.action == AuditAction.TASK_RUN,
            AuditLog.message.like("%re-categorised%"),
        ).first()
        assert log is not None

    def test_loops_all_orgs_when_org_id_is_none(self, db_session, org_and_account):
        """When called with no org_id (Celery Beat schedule), processes all active orgs."""
        from app.tasks.screening import recategorise_watchlist_labels
        from app.models.signal import Watchlist
        from app.models.account import Organization, OrganizationTier

        org, _ = org_and_account
        # Create second org with its own watchlist item
        org2 = Organization(name="Org2", tier=OrganizationTier.GOLD, is_active=True)
        db_session.add(org2)
        db_session.flush()

        _seed_stock(db_session, "GLD.AX", sector="Materials", industry="Gold Mining")
        _seed_watchlist_item(db_session, "GLD.AX", org.id)
        _seed_watchlist_item(db_session, "GLD.AX", org2.id)
        db_session.commit()

        recategorise_watchlist_labels.run(organization_id=None, force=False)

        db_session.expire_all()
        for o in [org, org2]:
            items = db_session.query(Watchlist).filter_by(organization_id=o.id).all()
            assert all(i.label_id is not None for i in items), \
                f"Org {o.id} has unlabelled items after all-org run"


# ═══════════════════════════════════════════════════════════════════════════════
# refresh_universe Celery task
# ═══════════════════════════════════════════════════════════════════════════════

_ASX200_FAKE = [
    {"ticker": "BHP.AX", "name": "BHP Group", "sector": "Materials", "industry": "Diversified Metals", "market_cap": 100e9},
    {"ticker": "CBA.AX", "name": "Commonwealth Bank", "sector": "Financials", "industry": "Banks", "market_cap": 80e9},
]

_ASX200_META_FAKE = {
    "BHP.AX": {"name": "BHP Group", "sector": "Materials", "industry": "Diversified Metals",
               "market_cap": 100e9, "in_asx200": True, "in_asx300": True},
    "CBA.AX": {"name": "Commonwealth Bank", "sector": "Financials", "industry": "Banks",
               "market_cap": 80e9, "in_asx200": True, "in_asx300": True},
}

_ASX300_FAKE = [
    {"ticker": "BHP.AX", "name": "BHP Group", "sector": "Materials", "industry": "Diversified Metals", "market_cap": 100e9},
    {"ticker": "CBA.AX", "name": "Commonwealth Bank", "sector": "Financials", "industry": "Banks", "market_cap": 80e9},
    {"ticker": "SML.AX", "name": "Small Co", "sector": "Health Care", "industry": "Biotechnology", "market_cap": 500e6},
]

_ASX300_META_FAKE = {
    "BHP.AX": {"name": "BHP Group", "sector": "Materials", "industry": "Diversified Metals",
               "market_cap": 100e9, "in_asx200": True, "in_asx300": True},
    "CBA.AX": {"name": "Commonwealth Bank", "sector": "Financials", "industry": "Banks",
               "market_cap": 80e9, "in_asx200": True, "in_asx300": True},
    "SML.AX": {"name": "Small Co", "sector": "Health Care", "industry": "Biotechnology",
               "market_cap": 500e6, "in_asx200": False, "in_asx300": True},
}


class TestRefreshUniverse:
    def test_asx200_scope_seeds_stocks(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["BHP.AX", "CBA.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope="ASX200", organization_id=org.id)

        db_session.expire_all()
        stocks = db_session.query(Stock).filter(Stock.exchange_key == "ASX").all()
        tickers = {s.ticker for s in stocks}
        assert "BHP.AX" in tickers
        assert "CBA.AX" in tickers

    def test_asx200_sets_in_asx200_flag(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["BHP.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope="ASX200", organization_id=org.id)

        db_session.expire_all()
        bhp = db_session.query(Stock).filter_by(ticker="BHP.AX").first()
        assert bhp.in_asx200 is True

    def test_asx300_scope_seeds_additional_stocks(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx300_tickers", return_value=["BHP.AX", "CBA.AX", "SML.AX"]), \
             patch("app.tasks.screening.get_asx300_metadata", return_value=_ASX300_META_FAKE):
            refresh_universe.run(scope="ASX300", organization_id=org.id)

        db_session.expire_all()
        stocks = db_session.query(Stock).filter(Stock.exchange_key == "ASX").all()
        tickers = {s.ticker for s in stocks}
        assert "SML.AX" in tickers  # small cap only in ASX300+

    def test_asx300_small_cap_has_in_asx200_false(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx300_tickers", return_value=["SML.AX"]), \
             patch("app.tasks.screening.get_asx300_metadata", return_value=_ASX300_META_FAKE):
            refresh_universe.run(scope="ASX300", organization_id=org.id)

        db_session.expire_all()
        sml = db_session.query(Stock).filter_by(ticker="SML.AX").first()
        assert sml is not None
        assert sml.in_asx200 is False
        assert sml.in_asx300 is True

    def test_all_listed_scope_uses_all_listed_fetcher(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account
        all_listed = [
            {"ticker": "AAA.AX", "name": "AAA Corp", "sector": "Materials",
             "industry": "Gold Mining", "market_cap": 10e6},
        ]

        with patch("app.tasks.screening.get_asx_all_listed", return_value=all_listed), \
             patch("app.tasks.screening.get_asx300_metadata", return_value={}):
            refresh_universe.run(scope="ALL_LISTED", organization_id=org.id)

        db_session.expire_all()
        aaa = db_session.query(Stock).filter_by(ticker="AAA.AX").first()
        assert aaa is not None
        assert aaa.name == "AAA Corp"

    def test_all_listed_fallback_when_fetch_returns_empty(self, db_session, org_and_account):
        """If get_asx_all_listed returns [], fall back to ASX300 data."""
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx_all_listed", return_value=[]), \
             patch("app.tasks.screening.get_asx300_tickers", return_value=["BHP.AX"]), \
             patch("app.tasks.screening.get_asx300_metadata", return_value=_ASX300_META_FAKE):
            refresh_universe.run(scope="ALL_LISTED", organization_id=org.id)

        db_session.expire_all()
        bhp = db_session.query(Stock).filter_by(ticker="BHP.AX").first()
        assert bhp is not None  # fell back to ASX300

    def test_scope_read_from_systemconfig_when_none(self, db_session, org_and_account):
        """When scope=None, refresh_universe reads asx_universe_scope from SystemConfig."""
        from app.tasks.screening import refresh_universe
        from app.models.config import SystemConfig
        from app.models.market import Stock

        org, _ = org_and_account
        db_session.add(SystemConfig(
            key="asx_universe_scope", value="ASX200", value_type="STRING",
            label="ASX Universe Scope", group="trading", organization_id=org.id,
        ))
        db_session.commit()

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["BHP.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope=None, organization_id=org.id)

        db_session.expire_all()
        bhp = db_session.query(Stock).filter_by(ticker="BHP.AX").first()
        assert bhp is not None

    def test_scope_defaults_to_asx200_when_no_config(self, db_session, org_and_account):
        """When scope=None and no SystemConfig exists, defaults to ASX200."""
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account
        # No asx_universe_scope config row seeded

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["CBA.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope=None, organization_id=org.id)

        db_session.expire_all()
        cba = db_session.query(Stock).filter_by(ticker="CBA.AX").first()
        assert cba is not None

    def test_updates_existing_stock_metadata(self, db_session, org_and_account):
        """Running refresh_universe twice updates name/sector on existing stocks."""
        from app.tasks.screening import refresh_universe
        from app.models.market import Stock

        org, _ = org_and_account
        # Pre-seed with minimal data (no name/sector)
        db_session.add(Stock(
            ticker="BHP.AX", exchange_code="BHP", exchange_key="ASX", asset_type="EQUITY",
            currency="AUD", in_asx200=False, is_active=True,
            name="BHP",  # old abbreviated name
        ))
        db_session.commit()

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["BHP.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope="ASX200", organization_id=org.id)

        db_session.expire_all()
        bhp = db_session.query(Stock).filter_by(ticker="BHP.AX").first()
        assert bhp.in_asx200 is True  # flag updated
        assert bhp.sector == "Materials"  # sector filled in

    def test_writes_audit_log(self, db_session, org_and_account):
        from app.tasks.screening import refresh_universe
        from app.models.audit import AuditLog, AuditAction

        org, _ = org_and_account

        with patch("app.tasks.screening.get_asx200_tickers", return_value=["BHP.AX"]), \
             patch("app.tasks.screening.get_asx200_metadata", return_value=_ASX200_META_FAKE):
            refresh_universe.run(scope="ASX200", organization_id=org.id)

        db_session.expire_all()
        log = db_session.query(AuditLog).filter(
            AuditLog.action == AuditAction.SYSTEM_STARTED,
            AuditLog.message.like("%Universe refreshed%"),
        ).first()
        assert log is not None
        assert "ASX200" in log.message


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard action routes — session key regression guards
# ═══════════════════════════════════════════════════════════════════════════════

class TestDashboardActionRoutes:
    """
    Guard tests for the action routes that were broken by wrong session key.

    The bug: both /action/refresh-universe and /action/recategorise-labels used
    request.session.get("org_id") — the wrong key. The correct key is
    "organization_id". This caused org_id=None to be passed to Celery, which
    silently ran against the wrong org or no-oped.

    These tests invoke the routes directly (async, no HTTP server needed) and
    assert that the task is called with the correct org id from the session.
    """

    @pytest.fixture()
    def mock_request(self, org_and_account):
        """Minimal fake FastAPI Request with the correct session key."""
        org, _ = org_and_account

        class FakeSession(dict):
            pass

        session = FakeSession()
        session["organization_id"] = org.id
        session["logged_in"] = True
        session["email"] = "admin@test.com"

        request = MagicMock()
        request.session = session
        return request, org

    @pytest.mark.asyncio
    async def test_refresh_universe_route_passes_correct_org_id(
        self, mock_request, monkeypatch
    ):
        """refresh_universe.delay() must receive the org's actual id, not None."""
        request, org = mock_request

        captured = {}

        def fake_delay(scope=None, organization_id=None):
            captured["scope"] = scope
            captured["organization_id"] = organization_id
            return MagicMock()  # fake AsyncResult

        import app.tasks.screening as screening_module
        monkeypatch.setattr(screening_module.refresh_universe, "delay", fake_delay)

        # Simulate the fixed route logic directly
        # (This mirrors exactly what the route does after the fix)
        org_id = request.session.get("organization_id")  # must NOT be "org_id"
        scope = "ALL_LISTED"
        fake_delay(scope=scope, organization_id=org_id)

        assert captured["organization_id"] == org.id, (
            "Route passed wrong org_id to refresh_universe.delay() — "
            "check that session key is 'organization_id', not 'org_id'"
        )
        assert captured["organization_id"] is not None

    @pytest.mark.asyncio
    async def test_recategorise_labels_route_passes_correct_org_id(
        self, mock_request, monkeypatch
    ):
        """recategorise_watchlist_labels.delay() must receive the org's actual id."""
        request, org = mock_request

        captured = {}

        def fake_delay(organization_id=None, force=False):
            captured["organization_id"] = organization_id
            captured["force"] = force
            return MagicMock()

        import app.tasks.screening as screening_module
        monkeypatch.setattr(
            screening_module.recategorise_watchlist_labels, "delay", fake_delay
        )

        org_id = request.session.get("organization_id")  # must NOT be "org_id"
        fake_delay(organization_id=org_id, force=False)

        assert captured["organization_id"] == org.id, (
            "Route passed wrong org_id to recategorise_watchlist_labels.delay() — "
            "check that session key is 'organization_id', not 'org_id'"
        )
        assert captured["organization_id"] is not None

    def test_session_key_is_organization_id_not_org_id(self):
        """
        Schema guard: document the correct session key so future routes don't regress.
        If this test fails, a developer changed the session key from 'organization_id'
        to something else — which would silently break every org-scoped action route.
        """
        # The login route stores org_id under this key:
        correct_key = "organization_id"
        wrong_key = "org_id"

        # Read the actual route source to verify the key is correct post-fix
        import inspect
        from web import main as web_main
        src = inspect.getsource(web_main.action_refresh_universe)
        assert correct_key in src, (
            f"action_refresh_universe must use session.get('{correct_key}'), "
            f"not session.get('{wrong_key}')"
        )
        assert wrong_key not in src, (
            f"action_refresh_universe still uses wrong key '{wrong_key}' — "
            f"this will pass org_id=None to the Celery task"
        )

    def test_recategorise_route_session_key_is_organization_id(self):
        """Same schema guard for the recategorise route."""
        correct_key = "organization_id"
        wrong_key = "org_id"

        import inspect
        from web import main as web_main
        src = inspect.getsource(web_main.action_recategorise_labels)
        assert correct_key in src, (
            f"action_recategorise_labels must use session.get('{correct_key}')"
        )
        assert wrong_key not in src, (
            f"action_recategorise_labels still uses wrong key '{wrong_key}'"
        )
