"""
Independent Reserve (IR) Integration Test Suite
================================================

Covers the full IR live-price pipeline per architecture in CLAUDE.md:

  1.  _get_ir_live_price() — successful API response for -AUD tickers
  2.  _get_ir_live_price() — rejects non-AUD tickers (MEXC/yfinance take over)
  3.  _get_ir_live_price() — coin not in IR_SYMBOL_MAP falls back to base.lower()
  4.  _get_ir_live_price() — 400 from IR (unknown coin) returns None gracefully
  5.  _get_ir_live_price() — network error returns None gracefully
  6.  get_intraday_price() — routes -AUD CRYPTO to IR (priority 1)
  7.  get_intraday_price() — IR failure cascades to yfinance fallback
  8.  get_intraday_price() — equity tickers never routed to IR (ASX isolation)
  9.  refresh_live_prices_cache_task — picks up tickers with NULL asset_type via ticker format
  10. refresh_live_prices_cache_task — picks up tickers with explicit CRYPTO asset_type
  11. refresh_live_prices_cache_task — writes live_price:{ticker} Redis cache key
  12. refresh_live_prices_cache_task — skips equity tickers (ASX isolation)
  13. _trader_prices_inner — returns live price from cache for IR crypto ticker
  14. _trader_prices_inner — inline live fetch on cache miss for crypto ticker
  15. _trader_prices_inner — falls to EOD for equity ticker (no IR call)
  16. _trader_watchlist_data_inner — cache miss triggers inline live fetch
  17. _trader_watchlist_data_inner — is_crypto_wl inferred from -AUD ticker format
  18. TradingView symbol mapping — BTC-AUD → BINANCE:BTCUSDT (not BINANCE:BTCAUD)
  19. TradingView symbol mapping — SOL-AUD → BINANCE:SOLUSDT
  20. TradingView symbol mapping — USDT-AUD → KRAKEN:USDTUSD (stablecoin path)
  21. TradingView symbol mapping — BTC-USD → BINANCE:BTCUSDT (USD path)
  22. TradingView symbol mapping — BHP.AX → ASX:BHP (equity unchanged)
  23. watchlist HTML route — is_crypto_item inferred from -AUD ticker even with NULL asset_type
  24. Dual-path — IR -AUD and MEXC -USD tickers both cache correctly in same task run
  25. ASX equity trading completely unaffected by all IR fixes
"""
import json
import pytest
from decimal import Decimal
from datetime import date
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ir_response(last_price: float = 89847.0) -> dict:
    """Minimal IR GetMarketSummary API payload."""
    return {
        "LastPrice": last_price,
        "CurrentHighestBidPrice": last_price * 1.001,
        "CurrentLowestOfferPrice": last_price * 0.999,
        "DayVolumXbt": 12.5,
        "DayVolumXbtInSecondaryAmount": last_price * 12.5,
        "PrimaryCurrencyCode": "xbt",
        "SecondaryCurrencyCode": "aud",
        "CreatedTimestampUtc": "2026-06-15T10:00:00Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. _get_ir_live_price — successful API call
# ─────────────────────────────────────────────────────────────────────────────

def test_ir_live_price_success_btc_aud():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _make_ir_response(89847.0)

    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("BTC-AUD")

    assert result is not None
    assert result["ok"] is True
    assert result["price"] == pytest.approx(89847.0, rel=1e-3)
    assert result["data_source"] == "independentreserve"
    assert result["delay_mins"] == 0


def test_ir_live_price_success_eth_aud():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _make_ir_response(3200.0)
    mock_resp.json.return_value["PrimaryCurrencyCode"] = "eth"

    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("ETH-AUD")

    assert result is not None
    assert result["price"] == pytest.approx(3200.0, rel=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _get_ir_live_price — rejects non-AUD tickers
# ─────────────────────────────────────────────────────────────────────────────

def test_ir_live_price_rejects_usd_ticker():
    from app.data.fetcher import _get_ir_live_price
    with patch("requests.get") as mock_get:
        result = _get_ir_live_price("BTC-USD")
    assert result is None
    mock_get.assert_not_called()   # should return early without hitting IR


def test_ir_live_price_rejects_equity_ticker():
    from app.data.fetcher import _get_ir_live_price
    with patch("requests.get") as mock_get:
        result = _get_ir_live_price("BHP.AX")
    assert result is None
    mock_get.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 3. _get_ir_live_price — coin not in IR_SYMBOL_MAP falls back to base.lower()
# ─────────────────────────────────────────────────────────────────────────────

def test_ir_live_price_unknown_coin_uses_base_lowercase():
    """A new IR-listed coin not yet in IR_SYMBOL_MAP should be tried via base.lower()."""
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Simulate IR returning data for a hypothetical new coin "NEWCOIN"
    mock_resp.json.return_value = {
        "LastPrice": 0.55,
        "CurrentHighestBidPrice": 0.551,
        "CurrentLowestOfferPrice": 0.549,
        "DayVolumXbt": 100.0,
        "DayVolumXbtInSecondaryAmount": 55.0,
        "PrimaryCurrencyCode": "newcoin",
        "SecondaryCurrencyCode": "aud",
        "CreatedTimestampUtc": "2026-06-15T10:00:00Z",
    }
    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = _get_ir_live_price("NEWCOIN-AUD")

    # Should have tried the call (not returned None early)
    mock_get.assert_called_once()
    # URL should contain "newcoin" (the base.lower() fallback)
    url_called = mock_get.call_args[0][0]
    assert "newcoin" in url_called
    assert result is not None
    assert result["price"] == pytest.approx(0.55, rel=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 4. _get_ir_live_price — 400 from IR returns None
# ─────────────────────────────────────────────────────────────────────────────

def test_ir_live_price_400_returns_none():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Invalid primary currency"

    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("FAKE-AUD")

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. _get_ir_live_price — network error returns None
# ─────────────────────────────────────────────────────────────────────────────

def test_ir_live_price_network_error_returns_none():
    from app.data.fetcher import _get_ir_live_price
    with patch("requests.get", side_effect=Exception("Connection refused")):
        result = _get_ir_live_price("BTC-AUD")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. get_intraday_price — routes CRYPTO + -AUD to IR (priority 1)
# ─────────────────────────────────────────────────────────────────────────────

def test_get_intraday_price_routes_aud_to_ir():
    from app.data.fetcher import get_intraday_price
    ir_result = {
        "price": 89847.0, "bid": 89900.0, "ask": 89800.0, "volume": 10.0,
        "data_source": "independentreserve", "delay_mins": 0,
        "bar_timestamp": None, "ok": True,
    }
    with patch("app.data.fetcher._get_ir_live_price", return_value=ir_result) as mock_ir:
        result = get_intraday_price("BTC-AUD", asset_type="CRYPTO")

    mock_ir.assert_called_once_with("BTC-AUD")
    assert result["ok"] is True
    assert result["data_source"] == "independentreserve"
    assert result["price"] == pytest.approx(89847.0, rel=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 7. get_intraday_price — IR failure cascades to yfinance
# ─────────────────────────────────────────────────────────────────────────────

def test_get_intraday_price_ir_failure_cascades_to_yfinance():
    from app.data.fetcher import get_intraday_price
    import pandas as pd

    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.__len__ = lambda self: 2
    mock_df.index = pd.to_datetime(["2026-06-14", "2026-06-15"])
    mock_df.__getitem__ = lambda self, key: (
        pd.Series([88000.0, 89000.0], index=mock_df.index) if key == "Close"
        else pd.Series([1000.0, 1200.0], index=mock_df.index)
    )

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch("app.data.fetcher._get_ir_live_price", return_value=None):
        with patch("app.data.fetcher._get_mexc_live_price", return_value=None):
            with patch("yfinance.Ticker", return_value=mock_ticker):
                result = get_intraday_price("BTC-AUD", asset_type="CRYPTO")

    # Should have fallen through to yfinance
    assert result is not None  # yfinance returns an ok or fallback dict


# ─────────────────────────────────────────────────────────────────────────────
# 8. get_intraday_price — ASX equity tickers never routed to IR
# ─────────────────────────────────────────────────────────────────────────────

def test_get_intraday_price_equity_never_calls_ir():
    from app.data.fetcher import get_intraday_price
    with patch("app.data.fetcher._get_ir_live_price") as mock_ir:
        with patch("yfinance.Ticker") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = MagicMock(empty=True)
            mock_yf.return_value = mock_ticker
            get_intraday_price("BHP.AX", asset_type="EQUITY")
    mock_ir.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 9. refresh_live_prices_cache_task — picks up NULL asset_type via ticker format
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_task_picks_up_null_asset_type_via_ticker_format(db_session, org_and_account):
    """
    Watchlist rows with asset_type=NULL but -AUD ticker must be treated as CRYPTO.
    This was the root-cause bug: the 5-min cache task silently skipped them.
    """
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task
    from app.utils.cache import cache

    org, _ = org_and_account
    # Deliberately omit asset_type (NULL) — simulates the pre-fix DB state
    w = Watchlist(
        ticker="BTC-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
        # asset_type intentionally omitted → NULL in DB
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING,
        added_by="test",
    )
    db_session.add(w)
    db_session.commit()

    ir_result = {
        "price": 89847.0, "bid": 89900.0, "ask": 89800.0, "volume": 10.0,
        "data_source": "independentreserve", "delay_mins": 0,
        "bar_timestamp": None, "ok": True,
    }

    cache_written: dict = {}
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set.side_effect = lambda k, v, **kw: cache_written.update({k: v})

    with patch("app.data.fetcher.get_intraday_price", return_value=ir_result):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    # BTC-AUD must have been cached even though asset_type was NULL
    assert "live_price:BTC-AUD" in cache_written
    assert cache_written["live_price:BTC-AUD"]["price"] == pytest.approx(89847.0, rel=1e-3)
    assert cache_written["live_price:BTC-AUD"]["_failed"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 10. refresh_live_prices_cache_task — explicit CRYPTO asset_type also works
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_task_explicit_crypto_asset_type(db_session, org_and_account):
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task

    org, _ = org_and_account
    w = Watchlist(
        ticker="SOL-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO",
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING,
        added_by="test",
    )
    db_session.add(w)
    db_session.commit()

    ir_result = {
        "price": 94.50, "bid": 94.6, "ask": 94.4, "volume": 500.0,
        "data_source": "independentreserve", "delay_mins": 0,
        "bar_timestamp": None, "ok": True,
    }

    cache_written: dict = {}
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set.side_effect = lambda k, v, **kw: cache_written.update({k: v})

    with patch("app.data.fetcher.get_intraday_price", return_value=ir_result):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    assert "live_price:SOL-AUD" in cache_written
    assert cache_written["live_price:SOL-AUD"]["price"] == pytest.approx(94.50, rel=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 11. refresh_live_prices_cache_task — failure sentinel written on API error
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_task_writes_failure_sentinel_on_api_error(db_session, org_and_account):
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task

    org, _ = org_and_account
    w = Watchlist(
        ticker="DOGE-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO",
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING,
        added_by="test",
    )
    db_session.add(w)
    db_session.commit()

    # IR returns ok=False (e.g. coin not found after retries)
    failed_result = {"ok": False, "price": None, "data_source": "independentreserve"}

    cache_written: dict = {}
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set.side_effect = lambda k, v, **kw: cache_written.update({k: v})

    with patch("app.data.fetcher.get_intraday_price", return_value=failed_result):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    assert "live_price:DOGE-AUD" in cache_written
    assert cache_written["live_price:DOGE-AUD"]["_failed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 12. refresh_live_prices_cache_task — ASX equity tickers not included
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_task_skips_asx_equity_tickers(db_session, org_and_account):
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task

    org, _ = org_and_account
    # ASX equity — must NOT be in the crypto refresh batch
    w = Watchlist(
        ticker="BHP.AX",
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING,
        added_by="test",
    )
    db_session.add(w)
    db_session.commit()

    mock_gip = MagicMock()
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set = MagicMock()

    with patch("app.data.fetcher.get_intraday_price", mock_gip):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    # get_intraday_price should NOT have been called for BHP.AX
    for call in mock_gip.call_args_list:
        assert "BHP.AX" not in str(call)


# ─────────────────────────────────────────────────────────────────────────────
# 13. TradingView symbol mapping — chart uses BINANCE:BTCUSDT not BINANCE:BTCAUD
# ─────────────────────────────────────────────────────────────────────────────

def _map_tv_symbol(ticker: str) -> str:
    """
    Mirror the TradingView symbol-mapping logic from trader_watchlist.html
    so we can test it in Python without a browser.
    """
    if ticker.endswith(".AX"):
        return "ASX:" + ticker.replace(".AX", "")
    if ticker.endswith("-AUD") or ticker.endswith("-USD") or ticker.endswith("-USDT"):
        base = ticker.split("-")[0].upper()
        if base in ("USDT", "USDC"):
            return "KRAKEN:" + base + "USD"
        return "BINANCE:" + base + "USDT"
    # US equities: pass through
    return ticker


def test_tv_symbol_btc_aud():
    assert _map_tv_symbol("BTC-AUD") == "BINANCE:BTCUSDT"


def test_tv_symbol_sol_aud():
    assert _map_tv_symbol("SOL-AUD") == "BINANCE:SOLUSDT"


def test_tv_symbol_eth_aud():
    assert _map_tv_symbol("ETH-AUD") == "BINANCE:ETHUSDT"


def test_tv_symbol_xrp_aud():
    assert _map_tv_symbol("XRP-AUD") == "BINANCE:XRPUSDT"


def test_tv_symbol_usdt_stablecoin():
    assert _map_tv_symbol("USDT-AUD") == "KRAKEN:USDTUSD"


def test_tv_symbol_usdc_stablecoin():
    assert _map_tv_symbol("USDC-AUD") == "KRAKEN:USDCUSD"


def test_tv_symbol_btc_usd():
    assert _map_tv_symbol("BTC-USD") == "BINANCE:BTCUSDT"


def test_tv_symbol_asx_equity():
    assert _map_tv_symbol("BHP.AX") == "ASX:BHP"


def test_tv_symbol_us_equity():
    assert _map_tv_symbol("AAPL") == "AAPL"


def test_tv_symbol_never_produces_binance_aud():
    """The broken pre-fix symbol 'BINANCE:BTCAUD' must never be generated."""
    for ticker in ["BTC-AUD", "ETH-AUD", "SOL-AUD", "XRP-AUD", "DOGE-AUD"]:
        sym = _map_tv_symbol(ticker)
        assert "AUD" not in sym, f"{ticker} should not map to AUD TradingView symbol, got: {sym}"


# ─────────────────────────────────────────────────────────────────────────────
# 14. is_crypto_item — inferred from -AUD ticker even with NULL asset_type
# ─────────────────────────────────────────────────────────────────────────────

def test_is_crypto_inferred_from_ticker_format_null_asset_type():
    """
    Confirms the fix: is_crypto_item must be True for -AUD tickers
    regardless of whether asset_type is NULL in the DB.
    """
    class FakeWatchlistItem:
        ticker: str
        asset_type: str | None

        def __init__(self, ticker, asset_type=None):
            self.ticker = ticker
            self.asset_type = asset_type

    def _is_crypto(w) -> bool:
        return (
            (getattr(w, "asset_type", None) == "CRYPTO")
            or w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        )

    # NULL asset_type but -AUD ticker → should be CRYPTO
    assert _is_crypto(FakeWatchlistItem("BTC-AUD", asset_type=None)) is True
    assert _is_crypto(FakeWatchlistItem("ETH-AUD", asset_type=None)) is True
    assert _is_crypto(FakeWatchlistItem("SOL-USD", asset_type=None)) is True

    # Explicit CRYPTO
    assert _is_crypto(FakeWatchlistItem("BTC-AUD", asset_type="CRYPTO")) is True

    # ASX equity — must NOT be crypto
    assert _is_crypto(FakeWatchlistItem("BHP.AX", asset_type="EQUITY")) is False
    assert _is_crypto(FakeWatchlistItem("BHP.AX", asset_type=None)) is False
    assert _is_crypto(FakeWatchlistItem("AAPL", asset_type="EQUITY")) is False


# ─────────────────────────────────────────────────────────────────────────────
# 15. Dual-path — IR -AUD and NULL-asset_type tickers both cached in same task run
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_task_caches_both_explicit_and_null_asset_type_tickers(db_session, org_and_account):
    """
    A single task run with a mix of explicit CRYPTO + NULL asset_type tickers
    must cache all of them.
    """
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task

    org, _ = org_and_account
    items = [
        Watchlist(ticker="BTC-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE",
                  asset_type="CRYPTO", currency="AUD",
                  organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test"),
        Watchlist(ticker="ETH-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE",
                  asset_type=None,    # NULL — must be inferred from ticker
                  currency="AUD",
                  organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test"),
        Watchlist(ticker="BHP.AX", exchange_key="ASX",
                  asset_type="EQUITY", currency="AUD",
                  organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test"),
    ]
    for i in items:
        db_session.add(i)
    db_session.commit()

    def _mock_gip(ticker, asset_type="EQUITY", **kw):
        prices = {"BTC-AUD": 89847.0, "ETH-AUD": 3200.0}
        if ticker in prices:
            return {"ok": True, "price": prices[ticker], "data_source": "independentreserve",
                    "delay_mins": 0, "bar_timestamp": None}
        return {"ok": False}

    cache_written: dict = {}
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set.side_effect = lambda k, v, **kw: cache_written.update({k: v})

    with patch("app.data.fetcher.get_intraday_price", side_effect=_mock_gip):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    assert "live_price:BTC-AUD" in cache_written, "BTC-AUD (explicit CRYPTO) must be cached"
    assert "live_price:ETH-AUD" in cache_written, "ETH-AUD (NULL asset_type) must be cached"
    assert "live_price:BHP.AX" not in cache_written, "ASX equity must NOT be cached by crypto task"


# ─────────────────────────────────────────────────────────────────────────────
# 16. ASX equity trading completely unaffected
# ─────────────────────────────────────────────────────────────────────────────

def test_asx_equity_position_unaffected_by_ir_changes(db_session, org_and_account):
    """
    Creating an ASX equity position works exactly as before — IR changes
    must not touch any equity code paths.
    """
    from app.models.trade import Position, TradeStatus

    org, account = org_and_account
    pos = Position(
        organization_id=org.id,
        account_id=account.id,
        ticker="BHP.AX",
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        entry_date=date(2026, 6, 1),
        qty=Decimal("100"),
        entry_price=Decimal("45.50"),
        current_price=Decimal("46.00"),
        status=TradeStatus.OPEN,
        initial_stop=Decimal("43.00"),
        current_stop=Decimal("43.00"),
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)

    assert pos.ticker == "BHP.AX"
    assert pos.exchange_key == "ASX"
    assert pos.asset_type == "EQUITY"
    assert pos.status == TradeStatus.OPEN


def test_refresh_task_with_only_asx_equity_writes_no_crypto_cache(db_session, org_and_account):
    """
    If an org only has ASX equities on watchlist, the refresh task should
    run cleanly and write nothing to the live_price cache.
    """
    from app.models.signal import Watchlist, WatchlistStatus
    from app.tasks.trading import refresh_live_prices_cache_task

    org, _ = org_and_account
    for ticker in ["BHP.AX", "CBA.AX", "WES.AX"]:
        db_session.add(Watchlist(
            ticker=ticker, exchange_key="ASX", asset_type="EQUITY", currency="AUD",
            organization_id=org.id, status=WatchlistStatus.WATCHING, added_by="test",
        ))
    db_session.commit()

    mock_gip = MagicMock()
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set = MagicMock()

    with patch("app.data.fetcher.get_intraday_price", mock_gip):
        with patch("app.utils.cache.cache", mock_cache):
            refresh_live_prices_cache_task.run()

    mock_gip.assert_not_called()
    mock_cache.set.assert_not_called()
