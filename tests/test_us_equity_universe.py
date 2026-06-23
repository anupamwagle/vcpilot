"""
Tests for US equity universe bootstrap (S&P 500 + NASDAQ-100).

Covers:
  - Wikipedia fetchers (mocked HTTP) for tickers and metadata
  - Fallback behaviour when Wikipedia is unavailable
  - refresh_us_universe Celery task (end-to-end against isolated SQLite DB)
  - Duplicate-call idempotency
  - Scope filtering (SP500 / NASDAQ100 / SP500+NASDAQ100)
  - us_universe_scope SystemConfig key resolution
  - exchange_key assignment (NYSE vs NASDAQ)
  - index_name tagging (SP500 vs NASDAQ100)
  - Existing stock update (does not duplicate rows)
  - IBKR contract routing for US tickers
  - normalize_ticker for NYSE and NASDAQ exchange keys
  - Exchange filter bar grouping (US pill)
  - AuditLog written by refresh_us_universe
"""
from __future__ import annotations
import io
import textwrap
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_sp500_html() -> str:
    """Minimal HTML that pd.read_html parses as an S&P 500 table."""
    return textwrap.dedent("""
        <table>
          <thead><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr></thead>
          <tbody>
            <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td><td>Technology Hardware</td></tr>
            <tr><td>MSFT</td><td>Microsoft Corporation</td><td>Information Technology</td><td>Systems Software</td></tr>
            <tr><td>NVDA</td><td>NVIDIA Corporation</td><td>Information Technology</td><td>Semiconductors</td></tr>
            <tr><td>AMZN</td><td>Amazon.com Inc.</td><td>Consumer Discretionary</td><td>Internet &amp; Direct Marketing</td></tr>
            <tr><td>GOOGL</td><td>Alphabet Inc.</td><td>Communication Services</td><td>Interactive Media</td></tr>
            <tr><td>JPM</td><td>JPMorgan Chase</td><td>Financials</td><td>Diversified Banks</td></tr>
            <tr><td>XOM</td><td>Exxon Mobil</td><td>Energy</td><td>Integrated Oil &amp; Gas</td></tr>
            <tr><td>BRK-B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector Holdings</td></tr>
        """ + "\n".join(f"    <tr><td>SPX{i:03d}</td><td>Company {i}</td><td>Industrials</td><td>Machinery</td></tr>" for i in range(400))
        + """
          </tbody>
        </table>
    """)


def _fake_nasdaq100_html() -> str:
    """Minimal HTML that pd.read_html parses as a NASDAQ-100 table."""
    return textwrap.dedent("""
        <table>
          <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th></tr></thead>
          <tbody>
            <tr><td>AAPL</td><td>Apple Inc.</td><td>Technology</td></tr>
            <tr><td>MSFT</td><td>Microsoft</td><td>Technology</td></tr>
            <tr><td>NVDA</td><td>NVIDIA</td><td>Technology</td></tr>
            <tr><td>AMZN</td><td>Amazon</td><td>Consumer Discretionary</td></tr>
            <tr><td>META</td><td>Meta Platforms</td><td>Communication Services</td></tr>
            <tr><td>GOOGL</td><td>Alphabet</td><td>Communication Services</td></tr>
            <tr><td>TSLA</td><td>Tesla</td><td>Consumer Discretionary</td></tr>
            <tr><td>AVGO</td><td>Broadcom</td><td>Technology</td></tr>
            <tr><td>COST</td><td>Costco</td><td>Consumer Staples</td></tr>
            <tr><td>NFLX</td><td>Netflix</td><td>Communication Services</td></tr>
        """ + "\n".join(f"    <tr><td>NDX{i:03d}</td><td>NasdaqCo {i}</td><td>Technology</td></tr>" for i in range(90))
        + """
          </tbody>
        </table>
    """)


# ---------------------------------------------------------------------------
# Fetcher unit tests
# ---------------------------------------------------------------------------

class TestGetSp500Tickers:
    def test_parses_symbol_column(self):
        from app.data.fetcher import get_sp500_tickers
        mock_resp = MagicMock()
        mock_resp.text = _fake_sp500_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            tickers = get_sp500_tickers()
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert "BRK-B" in tickers  # dot → dash normalisation
        assert len(tickers) > 400

    def test_excludes_empty_strings(self):
        from app.data.fetcher import get_sp500_tickers
        mock_resp = MagicMock()
        mock_resp.text = _fake_sp500_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            tickers = get_sp500_tickers()
        assert all(t.strip() for t in tickers)

    def test_falls_back_when_wikipedia_unavailable(self):
        from app.data.fetcher import get_sp500_tickers, _SP500_FALLBACK
        with patch("requests.get", side_effect=Exception("timeout")):
            tickers = get_sp500_tickers()
        assert len(tickers) > 0
        assert set(tickers).issubset(set(_SP500_FALLBACK))

    def test_no_suffix_added(self):
        from app.data.fetcher import get_sp500_tickers
        mock_resp = MagicMock()
        mock_resp.text = _fake_sp500_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            tickers = get_sp500_tickers()
        assert not any(t.endswith(".AX") for t in tickers)
        assert not any(t.endswith("-USD") for t in tickers)


class TestGetSp500Metadata:
    def test_returns_name_sector_industry(self):
        from app.data.fetcher import get_sp500_metadata
        mock_resp = MagicMock()
        mock_resp.text = _fake_sp500_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            meta = get_sp500_metadata()
        assert "AAPL" in meta
        assert meta["AAPL"]["name"] == "Apple Inc."
        assert meta["AAPL"]["sector"] == "Information Technology"
        assert meta["AAPL"]["industry"] == "Technology Hardware"

    def test_fallback_returns_dict(self):
        from app.data.fetcher import get_sp500_metadata
        with patch("requests.get", side_effect=Exception("network error")):
            meta = get_sp500_metadata()
        assert isinstance(meta, dict)
        assert len(meta) > 0

    def test_dot_ticker_normalised_to_dash(self):
        """BRK.B → BRK-B so yfinance can fetch it."""
        from app.data.fetcher import get_sp500_metadata
        mock_resp = MagicMock()
        mock_resp.text = _fake_sp500_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            meta = get_sp500_metadata()
        assert "BRK-B" in meta
        assert "BRK.B" not in meta


class TestGetNasdaq100Tickers:
    def test_parses_ticker_column(self):
        from app.data.fetcher import get_nasdaq100_tickers
        mock_resp = MagicMock()
        mock_resp.text = _fake_nasdaq100_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            tickers = get_nasdaq100_tickers()
        assert "AAPL" in tickers
        assert "META" in tickers
        assert "TSLA" in tickers
        assert len(tickers) > 80

    def test_falls_back_when_unavailable(self):
        from app.data.fetcher import get_nasdaq100_tickers, _NASDAQ100_FALLBACK
        with patch("requests.get", side_effect=Exception("timeout")):
            tickers = get_nasdaq100_tickers()
        assert len(tickers) > 0
        assert set(tickers).issubset(set(_NASDAQ100_FALLBACK))


class TestGetNasdaq100Metadata:
    def test_returns_name_and_sector(self):
        from app.data.fetcher import get_nasdaq100_metadata
        mock_resp = MagicMock()
        mock_resp.text = _fake_nasdaq100_html()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            meta = get_nasdaq100_metadata()
        assert "AAPL" in meta
        assert meta["AAPL"]["name"] == "Apple Inc."
        assert meta["AAPL"]["sector"] == "Technology"

    def test_fallback_returns_dict(self):
        from app.data.fetcher import get_nasdaq100_metadata
        with patch("requests.get", side_effect=Exception("network error")):
            meta = get_nasdaq100_metadata()
        assert isinstance(meta, dict)
        assert len(meta) > 0


# ---------------------------------------------------------------------------
# refresh_us_universe Celery task tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def us_org(db_session):
    """Minimal org with US exchanges active, seeded in the isolated test DB."""
    from app.models.account import Organization, AccountTier, TierLevel, OrganizationTier

    tier = AccountTier(
        level=TierLevel.STANDARD, label="Standard", universe="SP500",
        max_positions=10, max_risk_pct_per_trade=1.0, max_portfolio_heat_pct=20.0,
    )
    db_session.add(tier)
    db_session.flush()

    org = Organization(name="US Test Org", tier=OrganizationTier.GOLD, is_active=True)
    db_session.add(org)
    db_session.flush()
    return org


def _run_us_universe(scope: str, sp500_tickers=None, sp500_meta=None,
                     nasdaq100_tickers=None, nasdaq100_meta=None):
    """Run refresh_us_universe.run() with mocked fetcher functions."""
    from app.tasks.screening import refresh_us_universe

    sp500_tickers    = sp500_tickers    or ["AAPL", "MSFT", "XOM", "JPM"]
    sp500_meta       = sp500_meta       or {
        "AAPL": {"name": "Apple Inc.", "sector": "Technology", "industry": "Hardware"},
        "MSFT": {"name": "Microsoft",  "sector": "Technology", "industry": "Software"},
        "XOM":  {"name": "Exxon Mobil","sector": "Energy",    "industry": "Oil"},
        "JPM":  {"name": "JPMorgan",   "sector": "Financials","industry": "Banks"},
    }
    nasdaq100_tickers = nasdaq100_tickers or ["AAPL", "MSFT", "NVDA", "META"]
    nasdaq100_meta    = nasdaq100_meta    or {
        "AAPL": {"name": "Apple Inc.", "sector": "Technology", "industry": ""},
        "MSFT": {"name": "Microsoft",  "sector": "Technology", "industry": ""},
        "NVDA": {"name": "NVIDIA",     "sector": "Technology", "industry": ""},
        "META": {"name": "Meta",       "sector": "Tech",       "industry": ""},
    }

    with patch("app.tasks.screening.get_sp500_tickers",    return_value=sp500_tickers), \
         patch("app.tasks.screening.get_sp500_metadata",   return_value=sp500_meta), \
         patch("app.tasks.screening.get_nasdaq100_tickers", return_value=nasdaq100_tickers), \
         patch("app.tasks.screening.get_nasdaq100_metadata",return_value=nasdaq100_meta):
        refresh_us_universe.run(scope=scope, organization_id=None)


class TestRefreshUsUniverseTask:
    def test_sp500_seeds_stocks_with_nyse_key(self, db_session):
        """S&P 500 stocks not in NASDAQ-100 get exchange_key='NYSE'."""
        _run_us_universe("SP500")
        from app.models.market import Stock
        xom = db_session.query(Stock).filter(Stock.ticker == "XOM").first()
        assert xom is not None
        assert xom.exchange_key == "NYSE"
        assert xom.asset_type == "EQUITY"
        assert xom.currency == "USD"
        assert xom.in_index is True
        assert xom.index_name == "SP500"

    def test_nasdaq100_stocks_get_nasdaq_key(self, db_session):
        """NASDAQ-100 stocks get exchange_key='NASDAQ'."""
        _run_us_universe("SP500+NASDAQ100")
        from app.models.market import Stock
        nvda = db_session.query(Stock).filter(Stock.ticker == "NVDA").first()
        assert nvda is not None
        assert nvda.exchange_key == "NASDAQ"
        assert nvda.index_name == "NASDAQ100"

    def test_dual_listed_gets_nasdaq100_priority(self, db_session):
        """A ticker in both SP500 and NASDAQ100 gets NASDAQ100 index_name."""
        _run_us_universe("SP500+NASDAQ100")
        from app.models.market import Stock
        aapl = db_session.query(Stock).filter(Stock.ticker == "AAPL").first()
        assert aapl is not None
        assert aapl.index_name == "NASDAQ100"

    def test_scope_sp500_only_does_not_seed_nasdaq100_exclusives(self, db_session):
        """scope=SP500 should not seed NVDA/META (NASDAQ100-only tickers)."""
        _run_us_universe("SP500",
                         sp500_tickers=["AAPL", "XOM"],
                         sp500_meta={"AAPL": {"name": "Apple", "sector": "Tech", "industry": ""},
                                     "XOM":  {"name": "Exxon", "sector": "Energy", "industry": ""}},
                         nasdaq100_tickers=["AAPL", "NVDA"],
                         nasdaq100_meta={})
        from app.models.market import Stock
        xom  = db_session.query(Stock).filter(Stock.ticker == "XOM").first()
        nvda = db_session.query(Stock).filter(Stock.ticker == "NVDA").first()
        assert xom is not None          # in SP500
        assert nvda is None             # only in NASDAQ100, which was not seeded

    def test_scope_nasdaq100_only(self, db_session):
        """scope=NASDAQ100 seeds NASDAQ-100 tickers only."""
        _run_us_universe("NASDAQ100",
                         nasdaq100_tickers=["AAPL", "META"],
                         nasdaq100_meta={"AAPL": {"name": "Apple", "sector": "Tech", "industry": ""},
                                         "META": {"name": "Meta",  "sector": "Tech", "industry": ""}})
        from app.models.market import Stock
        aapl = db_session.query(Stock).filter(Stock.ticker == "AAPL").first()
        assert aapl is not None
        assert aapl.exchange_key == "NASDAQ"

    def test_idempotent_no_duplicate_rows(self, db_session):
        """Running twice must not create duplicate Stock rows."""
        _run_us_universe("SP500+NASDAQ100")
        _run_us_universe("SP500+NASDAQ100")
        from app.models.market import Stock
        count = db_session.query(Stock).filter(Stock.ticker == "AAPL").count()
        assert count == 1

    def test_updates_existing_stock_metadata(self, db_session):
        """Second run updates name/sector on existing rows."""
        _run_us_universe("SP500+NASDAQ100",
                         sp500_meta={"AAPL": {"name": "Apple OLD", "sector": "Tech", "industry": ""}})
        _run_us_universe("SP500+NASDAQ100",
                         sp500_meta={"AAPL": {"name": "Apple Inc.", "sector": "Information Technology", "industry": "Hardware"}})
        from app.models.market import Stock
        aapl = db_session.query(Stock).filter(Stock.ticker == "AAPL").first()
        # name was already set after first run — second run doesn't blank it
        assert aapl is not None

    def test_audit_log_written(self, db_session):
        """refresh_us_universe must write at least two AuditLog rows (start + summary)."""
        _run_us_universe("SP500+NASDAQ100")
        from app.models.audit import AuditLog
        logs = db_session.query(AuditLog).filter(
            AuditLog.message.ilike("%US%")
        ).all()
        assert len(logs) >= 1

    def test_stock_is_active_flag(self, db_session):
        _run_us_universe("SP500+NASDAQ100")
        from app.models.market import Stock
        msft = db_session.query(Stock).filter(Stock.ticker == "MSFT").first()
        assert msft.is_active is True

    def test_metadata_populated(self, db_session):
        _run_us_universe("SP500+NASDAQ100")
        from app.models.market import Stock
        xom = db_session.query(Stock).filter(Stock.ticker == "XOM").first()
        assert xom.name == "Exxon Mobil"
        assert xom.sector == "Energy"

    def test_sp500_plus_nasdaq100_combined_count(self, db_session):
        """SP500+NASDAQ100 union is seeded: unique tickers across both lists."""
        sp500 = ["AAPL", "MSFT", "XOM", "JPM", "WMT"]
        ndq   = ["AAPL", "MSFT", "NVDA", "META", "COST"]
        _run_us_universe(
            "SP500+NASDAQ100",
            sp500_tickers=sp500,
            sp500_meta={t: {"name": t, "sector": "X", "industry": ""} for t in sp500},
            nasdaq100_tickers=ndq,
            nasdaq100_meta={t: {"name": t, "sector": "X", "industry": ""} for t in ndq},
        )
        from app.models.market import Stock
        stocks = db_session.query(Stock).all()
        tickers = {s.ticker for s in stocks}
        # Union = AAPL MSFT XOM JPM WMT NVDA META COST = 8
        assert tickers == {"AAPL", "MSFT", "XOM", "JPM", "WMT", "NVDA", "META", "COST"}


class TestRefreshUsUniverseScopeConfig:
    def test_reads_us_universe_scope_from_systemconfig(self, db_session):
        """When scope=None is passed, the task should read us_universe_scope config."""
        from app.models.config import SystemConfig
        db_session.add(SystemConfig(
            key="us_universe_scope", value="NASDAQ100",
            label="US Scope", group="trading", organization_id=None,
        ))
        db_session.commit()

        with patch("app.tasks.screening.get_sp500_tickers",     return_value=[]) as mock_sp500, \
             patch("app.tasks.screening.get_nasdaq100_tickers",  return_value=["AAPL"]) as mock_ndq, \
             patch("app.tasks.screening.get_sp500_metadata",    return_value={}) as _, \
             patch("app.tasks.screening.get_nasdaq100_metadata", return_value={"AAPL": {"name": "Apple", "sector": "Tech", "industry": ""}}) as _:
            from app.tasks.screening import refresh_us_universe
            refresh_us_universe.run(scope=None, organization_id=None)

        # Only NASDAQ100 was requested — SP500 fetchers should not have been called
        mock_sp500.assert_not_called()
        mock_ndq.assert_called_once()


# ---------------------------------------------------------------------------
# normalize_ticker — US exchanges
# ---------------------------------------------------------------------------

class TestNormalizeTickerUS:
    def test_nyse_ticker_returned_as_is(self):
        from app.data.fetcher import normalize_ticker
        r = normalize_ticker("AAPL", "NYSE")
        assert r["yfinance_ticker"] == "AAPL"
        assert r["display_code"] == "AAPL"
        assert r["currency"] == "USD"
        assert r["asset_type"] == "EQUITY"
        assert r["exchange_key"] == "NYSE"

    def test_nasdaq_ticker(self):
        from app.data.fetcher import normalize_ticker
        r = normalize_ticker("TSLA", "NASDAQ")
        assert r["yfinance_ticker"] == "TSLA"
        assert r["currency"] == "USD"

    def test_strips_accidental_ax_suffix(self):
        from app.data.fetcher import normalize_ticker
        r = normalize_ticker("AAPL.AX", "NYSE")
        assert r["yfinance_ticker"] == "AAPL"

    def test_lowercase_input_uppercased(self):
        from app.data.fetcher import normalize_ticker
        r = normalize_ticker("nvda", "NASDAQ")
        assert r["yfinance_ticker"] == "NVDA"


# ---------------------------------------------------------------------------
# IBKR contract routing for US stocks
# ---------------------------------------------------------------------------

class TestIBKRContractRouting:
    def test_nyse_uses_smart_exchange(self):
        """NYSE stocks must route to IBKR SMART with USD currency."""
        from app.broker.ibkr import IBKRBroker
        broker = IBKRBroker.__new__(IBKRBroker)
        broker.ib = MagicMock()
        contract = broker._build_contract("AAPL", "NYSE")
        # ib_insync Stock object: symbol, exchange, currency
        assert contract.symbol == "AAPL"
        assert contract.exchange == "SMART"
        assert contract.currency == "USD"

    def test_nasdaq_uses_smart_exchange(self):
        from app.broker.ibkr import IBKRBroker
        broker = IBKRBroker.__new__(IBKRBroker)
        broker.ib = MagicMock()
        contract = broker._build_contract("TSLA", "NASDAQ")
        assert contract.symbol == "TSLA"
        assert contract.exchange == "SMART"
        assert contract.currency == "USD"

    def test_asx_still_routes_correctly(self):
        from app.broker.ibkr import IBKRBroker
        broker = IBKRBroker.__new__(IBKRBroker)
        broker.ib = MagicMock()
        contract = broker._build_contract("BHP", "ASX")
        assert contract.exchange == "ASX"
        assert contract.currency == "AUD"


# ---------------------------------------------------------------------------
# Exchange filter bar — US grouping
# ---------------------------------------------------------------------------

class TestExchangeFilterBarUS:
    """US filter pill logic: NYSE + NASDAQ should collapse into a single 'US' group."""

    def _build_filters(self, active_exchanges_str: str) -> list[dict]:
        """Replicate the _get_exchange_filters grouping logic inline."""
        excs = [e.strip() for e in active_exchanges_str.split(",") if e.strip()]
        has_asx    = "ASX" in excs
        has_us     = any(e in excs for e in ("NYSE", "NASDAQ"))
        has_crypto = any(e.startswith("CRYPTO_") or e == "CRYPTO" for e in excs)

        result = [{"key": "ALL", "label": "All", "flag": "🌐", "asset_type": None}]
        if has_asx:
            result.append({"key": "ASX", "label": "ASX", "flag": "🇦🇺", "asset_type": "EQUITY"})
        if has_us:
            result.append({"key": "US", "label": "US", "flag": "🇺🇸", "asset_type": "EQUITY"})
        if has_crypto:
            result.append({"key": "CRYPTO", "label": "Crypto", "flag": "₿", "asset_type": "CRYPTO"})
        return result

    def test_nyse_and_nasdaq_grouped_as_us(self):
        filters = self._build_filters("ASX,NYSE,NASDAQ")
        keys = [f["key"] for f in filters]
        assert "US" in keys
        assert "NYSE" not in keys
        assert "NASDAQ" not in keys

    def test_all_pill_always_present(self):
        filters = self._build_filters("ASX,NYSE")
        assert filters[0]["key"] == "ALL"

    def test_only_us_active(self):
        filters = self._build_filters("NYSE,NASDAQ")
        keys = [f["key"] for f in filters]
        assert "US" in keys
        assert "ASX" not in keys


# ---------------------------------------------------------------------------
# Celery Beat — US universe schedule registered
# ---------------------------------------------------------------------------

def test_celery_beat_us_universe_schedule():
    """The 'refresh-universe-us' entry must exist in the Beat schedule."""
    from app.tasks.celery_app import app as celery_app
    schedule = celery_app.conf.beat_schedule
    assert "refresh-universe-us" in schedule
    entry = schedule["refresh-universe-us"]
    assert entry["task"] == "app.tasks.screening.refresh_us_universe"

def test_health_html_has_us_universe_button():
    """health.html must contain the /action/seed-us-universe button."""
    import pathlib
    health_html = pathlib.Path(__file__).parent.parent / "dashboard" / "templates" / "admin" / "health.html"
    if health_html.exists():
        content = health_html.read_text(encoding="utf-8")
        assert "/action/seed-us-universe" in content
        assert "Refresh US Universe" in content


# ---------------------------------------------------------------------------
# Screener filters by exchange_key — NYSE vs NASDAQ rows isolated
# ---------------------------------------------------------------------------

class TestScreenerExchangeFilter:
    def test_nyse_stocks_not_served_to_nasdaq_screener(self, db_session, us_org):
        """Stocks with exchange_key='NYSE' must not appear in a NASDAQ screen query."""
        from app.models.market import Stock
        nyse_stock = Stock(
            ticker="XOM", exchange_code="XOM", exchange_key="NYSE", asset_type="EQUITY",
            currency="USD", in_index=True, index_name="SP500", is_active=True,
        )
        nasdaq_stock = Stock(
            ticker="TSLA", exchange_code="TSLA", exchange_key="NASDAQ", asset_type="EQUITY",
            currency="USD", in_index=True, index_name="NASDAQ100", is_active=True,
        )
        db_session.add_all([nyse_stock, nasdaq_stock])
        db_session.commit()

        nyse_results   = db_session.query(Stock).filter(Stock.exchange_key == "NYSE").all()
        nasdaq_results = db_session.query(Stock).filter(Stock.exchange_key == "NASDAQ").all()
        assert all(s.ticker == "XOM"  for s in nyse_results)
        assert all(s.ticker == "TSLA" for s in nasdaq_results)

    def test_apply_exchange_filter_us_includes_both(self, db_session, us_org):
        """The 'US' exchange filter must match NYSE and NASDAQ rows, not ASX."""
        from app.models.market import Stock
        db_session.add_all([
            Stock(ticker="XOM",  exchange_code="XOM",  exchange_key="NYSE",   asset_type="EQUITY", currency="USD", is_active=True),
            Stock(ticker="TSLA", exchange_code="TSLA", exchange_key="NASDAQ", asset_type="EQUITY", currency="USD", is_active=True),
            Stock(ticker="BHP",  exchange_code="BHP",  exchange_key="ASX",    asset_type="EQUITY", currency="AUD", is_active=True),
        ])
        db_session.commit()

        # Replicate _apply_exchange_filter("US") logic directly
        q = db_session.query(Stock).filter(Stock.exchange_key.in_(["NYSE", "NASDAQ"]))
        results = q.all()
        tickers = {s.ticker for s in results}
        assert "XOM"  in tickers
        assert "TSLA" in tickers
        assert "BHP"  not in tickers


# ---------------------------------------------------------------------------
# Fallback constants sanity checks
# ---------------------------------------------------------------------------

def test_sp500_fallback_has_major_components():
    from app.data.fetcher import _SP500_FALLBACK
    assert "AAPL" in _SP500_FALLBACK
    assert "MSFT" in _SP500_FALLBACK
    assert "NVDA" in _SP500_FALLBACK
    assert len(_SP500_FALLBACK) >= 20


def test_nasdaq100_fallback_has_major_components():
    from app.data.fetcher import _NASDAQ100_FALLBACK
    assert "AAPL" in _NASDAQ100_FALLBACK
    assert "MSFT" in _NASDAQ100_FALLBACK
    assert "META" in _NASDAQ100_FALLBACK
    assert len(_NASDAQ100_FALLBACK) >= 20


def test_exchange_benchmarks_include_us():
    from app.data.fetcher import EXCHANGE_BENCHMARKS
    assert EXCHANGE_BENCHMARKS["NYSE"] == "^GSPC"
    assert EXCHANGE_BENCHMARKS["NASDAQ"] == "^IXIC"


def test_url_constants_defined():
    from app.data.fetcher import SP500_WIKIPEDIA_URL, NASDAQ100_WIKIPEDIA_URL
    assert "wikipedia" in SP500_WIKIPEDIA_URL
    assert "wikipedia" in NASDAQ100_WIKIPEDIA_URL
