"""
MEXC Exchange Integration Test Suite
=====================================

Covers all MEXC-specific code paths per the architecture in CLAUDE.md:

  1. _get_mexc_live_price() — public API routing, error handling, symbol conversion
  2. get_intraday_price() — MEXC path selection (asset_type=CRYPTO, -USD ticker)
  3. get_intraday_price() — IR still wins for -AUD tickers (MEXC must not intercept)
  4. get_intraday_price() — equity tickers unaffected by MEXC path (isolation)
  5. _yfinance_to_ccxt() — MEXC USDT pair conversion
  6. CryptoBroker.connect() — MEXC testnet forces simulation, no real connection
  7. CryptoBroker.submit_bracket_order() — simulates correctly without credentials
  8. get_crypto_broker_for_org() — MEXC exchange_key hint
  9. get_top_crypto_tickers() — MEXC returns USD list (not AUD)
  10. EXCHANGE_BENCHMARKS — CRYPTO_MEXC mapped to BTC-USD
  11. CRYPTO_USD_EXCHANGES — MEXC in USD set, not in AUD set
  12. normalize_ticker() — MEXC produces -USD yfinance tickers
  13. update_position_pnl_task — writes live_price cache for MEXC positions
  14. refresh_live_prices_cache_task — covers watchlist/signal crypto tickers
  15. Stock trading isolation — ASX equities unaffected by MEXC changes
"""
import pytest
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# 1. _get_mexc_live_price — successful response
# ─────────────────────────────────────────────────────────────────────────────

def test_mexc_live_price_btc_usd():
    """Happy path: BTC-USD returns a valid price dict from MEXC API."""
    from app.data.fetcher import _get_mexc_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "lastPrice": "68000.5",
        "bidPrice": "67999.0",
        "askPrice": "68001.0",
        "volume": "12345.67",
    }
    with patch("requests.get", return_value=mock_resp):
        result = _get_mexc_live_price("BTC-USD")
    assert result is not None
    assert result["ok"] is True
    assert result["price"] == 68000.5
    assert result["bid"] == 67999.0
    assert result["ask"] == 68001.0
    assert result["data_source"] == "mexc"
    assert result["delay_mins"] == 0


def test_mexc_live_price_eth_usdt():
    """ETH-USDT suffix also routes correctly."""
    from app.data.fetcher import _get_mexc_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "lastPrice": "3200.0",
        "bidPrice": "3199.5",
        "askPrice": "3200.5",
        "volume": "5000.0",
    }
    with patch("requests.get", return_value=mock_resp):
        result = _get_mexc_live_price("ETH-USDT")
    assert result is not None
    assert result["price"] == 3200.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. _get_mexc_live_price — non-USD ticker returns None (no intercept)
# ─────────────────────────────────────────────────────────────────────────────

def test_mexc_live_price_returns_none_for_aud_ticker():
    """-AUD tickers must NOT be routed to MEXC (IR handles those)."""
    from app.data.fetcher import _get_mexc_live_price
    result = _get_mexc_live_price("BTC-AUD")
    assert result is None


def test_mexc_live_price_returns_none_for_equity_ticker():
    """Equity tickers must never reach MEXC."""
    from app.data.fetcher import _get_mexc_live_price
    result = _get_mexc_live_price("BHP.AX")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. _get_mexc_live_price — 400 returns None without retry
# ─────────────────────────────────────────────────────────────────────────────

def test_mexc_live_price_404_returns_none():
    """A 400 (symbol not found on MEXC) should return None immediately, no retry."""
    from app.data.fetcher import _get_mexc_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"code":-1121,"msg":"Invalid symbol."}'
    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = _get_mexc_live_price("UNKNOWNCOIN-USD")
    assert result is None
    # Should only have called the API once (no retry on 400)
    assert mock_get.call_count == 1


def test_mexc_live_price_network_error_returns_none():
    """Network errors should be caught and return None."""
    from app.data.fetcher import _get_mexc_live_price
    with patch("requests.get", side_effect=Exception("Connection refused")):
        result = _get_mexc_live_price("BTC-USD")
    assert result is None


def test_mexc_live_price_missing_last_price_returns_none():
    """Missing lastPrice in response should return None."""
    from app.data.fetcher import _get_mexc_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"volume": "1234"}  # no lastPrice
    with patch("requests.get", return_value=mock_resp):
        result = _get_mexc_live_price("BTC-USD")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. get_intraday_price — MEXC path activated for CRYPTO -USD
# ─────────────────────────────────────────────────────────────────────────────

def test_intraday_price_routes_mexc_for_usd_crypto():
    """CRYPTO + -USD ticker must route through MEXC, not yfinance."""
    from app.data.fetcher import get_intraday_price

    mexc_response = {
        "price": 68000.0, "close": 68000.0, "live_price": 68000.0,
        "bid": 67999.0, "ask": 68001.0,
        "data_source": "mexc", "delay_mins": 0,
        "bar_timestamp": None, "ok": True,
    }
    with patch("app.data.fetcher._get_mexc_live_price", return_value=mexc_response) as mock_mexc, \
         patch("app.data.fetcher._get_ir_live_price", return_value=None) as mock_ir:
        result = get_intraday_price("BTC-USD", asset_type="CRYPTO")
    mock_mexc.assert_called_once_with("BTC-USD")
    mock_ir.assert_not_called()
    assert result["data_source"] == "mexc"
    assert result["price"] == 68000.0


def test_intraday_price_ir_wins_for_aud_ticker():
    """-AUD tickers must use IR, MEXC must NOT be called."""
    from app.data.fetcher import get_intraday_price

    ir_response = {
        "price": 90000.0, "bid": 89999.0, "ask": 90001.0, "volume": 100,
        "data_source": "independentreserve", "delay_mins": 0,
        "bar_timestamp": None, "ok": True,
    }
    with patch("app.data.fetcher._get_ir_live_price", return_value=ir_response) as mock_ir, \
         patch("app.data.fetcher._get_mexc_live_price") as mock_mexc:
        result = get_intraday_price("BTC-AUD", asset_type="CRYPTO")
    mock_ir.assert_called_once_with("BTC-AUD")
    mock_mexc.assert_not_called()
    assert result["data_source"] == "independentreserve"


def test_intraday_price_mexc_fallback_to_yfinance():
    """When MEXC fails, must fall through to yfinance (not leave caller hanging)."""
    import pandas as pd
    from datetime import datetime
    from app.data.fetcher import get_intraday_price

    fake_df = pd.DataFrame([{
        "datetime": datetime.utcnow(),
        "close": 67000.0,
        "volume": 5000,
    }])
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_df

    with patch("app.data.fetcher._get_mexc_live_price", return_value=None), \
         patch("app.data.fetcher._get_ir_live_price", return_value=None), \
         patch("yfinance.Ticker", return_value=fake_ticker):
        result = get_intraday_price("BTC-USD", asset_type="CRYPTO")
    # yfinance fallback path — data_source = "yfinance"
    assert result.get("data_source") == "yfinance"
    assert result.get("price") == 67000.0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Stock trading isolation — equities unaffected
# ─────────────────────────────────────────────────────────────────────────────

def test_intraday_price_equity_never_hits_mexc():
    """ASX equity tickers must never route to MEXC."""
    from app.data.fetcher import get_intraday_price

    with patch("app.data.fetcher._get_mexc_live_price") as mock_mexc, \
         patch("app.data.fetcher._get_ir_live_price") as mock_ir:
        # Don't care about the result — we just want to confirm no crypto API hit
        try:
            get_intraday_price("BHP.AX", asset_type="EQUITY")
        except Exception:
            pass
    mock_mexc.assert_not_called()
    mock_ir.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 6. _yfinance_to_ccxt — MEXC USDT pairs
# ─────────────────────────────────────────────────────────────────────────────

def test_yfinance_to_ccxt_mexc_btc():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("BTC-USD", "mexc") == "BTC/USDT"


def test_yfinance_to_ccxt_mexc_eth():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("ETH-USD", "mexc") == "ETH/USDT"


def test_yfinance_to_ccxt_mexc_sol():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("SOL-USD", "mexc") == "SOL/USDT"


def test_yfinance_to_ccxt_mexc_does_not_remap_to_aud():
    """MEXC must produce USDT pairs, never AUD pairs."""
    from app.broker.crypto import _yfinance_to_ccxt
    result = _yfinance_to_ccxt("ETH-USD", "mexc")
    assert "AUD" not in result
    assert result.endswith("/USDT")


def test_yfinance_to_ccxt_ir_unaffected():
    """IR XBT/AUD mapping must not be broken by MEXC changes."""
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("BTC-AUD", "independentreserve") == "XBT/AUD"
    assert _yfinance_to_ccxt("ETH-AUD", "independentreserve") == "ETH/AUD"


# ─────────────────────────────────────────────────────────────────────────────
# 7. CryptoBroker — MEXC testnet forces simulation
# ─────────────────────────────────────────────────────────────────────────────

def test_crypto_broker_mexc_testnet_forces_simulation():
    """MEXC has no ccxt testnet — testnet=True must return False (simulation)."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="mexc", api_key="key", api_secret="secret", testnet=True)
    connected = b.connect()
    assert connected is False
    assert b.is_connected is False


def test_crypto_broker_mexc_no_credentials_simulation():
    """MEXC without credentials uses simulation mode."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="mexc", api_key="", api_secret="")
    result = b.submit_bracket_order("BTC-USD", "BUY", 0.1, 68000, 64000, 72000)
    assert result["status"] == "simulated"
    assert result["ticker"] == "BTC-USD"
    assert result["broker"] == "simulation"


def test_crypto_broker_mexc_submit_bracket_simulation_includes_stops():
    """Simulation order should include stop and target IDs."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="mexc", api_key="", api_secret="")
    result = b.submit_bracket_order("ETH-USD", "BUY", 1.0, 3200, 3000, 3500)
    assert "stop_order_id" in result
    assert "target_order_id" in result
    assert result["entry_order_id"].startswith("SIM_")


# ─────────────────────────────────────────────────────────────────────────────
# 8. get_crypto_broker_for_org — MEXC exchange_key hint
# ─────────────────────────────────────────────────────────────────────────────

def test_get_crypto_broker_for_org_mexc_hint(db_session, org_and_account):
    """Factory should use mexc as initial_provider when exchange_key=CRYPTO_MEXC."""
    from app.broker.crypto import get_crypto_broker_for_org, CryptoBroker
    org, _ = org_and_account
    broker = get_crypto_broker_for_org(org.id, exchange_key="CRYPTO_MEXC")
    assert isinstance(broker, CryptoBroker)
    # Initial provider should be mexc (before _load_org_credentials may override)
    assert broker.ccxt_provider == "mexc"


def test_get_crypto_broker_for_org_ir_unaffected(db_session, org_and_account):
    """Factory with IR exchange_key must still return IR provider."""
    from app.broker.crypto import get_crypto_broker_for_org
    org, _ = org_and_account
    broker = get_crypto_broker_for_org(org.id, exchange_key="CRYPTO_INDEPENDENTRESERVE")
    assert broker.ccxt_provider == "independentreserve"


# ─────────────────────────────────────────────────────────────────────────────
# 9. get_top_crypto_tickers — MEXC returns USD list, not AUD
# ─────────────────────────────────────────────────────────────────────────────

def test_top_crypto_tickers_mexc_returns_usd():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_MEXC")
    assert len(tickers) > 0
    # All should be USD pairs
    assert all(t.endswith("-USD") for t in tickers)
    assert "BTC-USD" in tickers


def test_top_crypto_tickers_mexc_does_not_return_aud():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_MEXC")
    assert not any(t.endswith("-AUD") for t in tickers)


def test_top_crypto_tickers_ir_returns_aud():
    """IR tickers must still be AUD — not broken by MEXC changes."""
    with patch("app.data.fetcher.get_ir_supported_tickers",
               return_value=["BTC-AUD", "ETH-AUD", "SOL-AUD"]):
        from app.data.fetcher import get_top_crypto_tickers
        tickers = get_top_crypto_tickers("CRYPTO_INDEPENDENTRESERVE")
    assert all(t.endswith("-AUD") for t in tickers)


# ─────────────────────────────────────────────────────────────────────────────
# 10. EXCHANGE_BENCHMARKS — CRYPTO_MEXC present
# ─────────────────────────────────────────────────────────────────────────────

def test_exchange_benchmarks_contains_mexc():
    from app.data.fetcher import EXCHANGE_BENCHMARKS
    assert "CRYPTO_MEXC" in EXCHANGE_BENCHMARKS
    assert EXCHANGE_BENCHMARKS["CRYPTO_MEXC"] == "BTC-USD"


def test_exchange_benchmarks_ir_unaffected():
    from app.data.fetcher import EXCHANGE_BENCHMARKS
    assert EXCHANGE_BENCHMARKS["CRYPTO_INDEPENDENTRESERVE"] == "BTC-AUD"


# ─────────────────────────────────────────────────────────────────────────────
# 11. CRYPTO_USD_EXCHANGES / CRYPTO_AUD_EXCHANGES sets
# ─────────────────────────────────────────────────────────────────────────────

def test_mexc_in_usd_exchanges_not_aud():
    from app.data.fetcher import CRYPTO_AUD_EXCHANGES, CRYPTO_USD_EXCHANGES
    assert "CRYPTO_MEXC" in CRYPTO_USD_EXCHANGES
    assert "CRYPTO_MEXC" not in CRYPTO_AUD_EXCHANGES


def test_ir_in_aud_exchanges_not_usd():
    from app.data.fetcher import CRYPTO_AUD_EXCHANGES, CRYPTO_USD_EXCHANGES
    assert "CRYPTO_INDEPENDENTRESERVE" in CRYPTO_AUD_EXCHANGES
    assert "CRYPTO_INDEPENDENTRESERVE" not in CRYPTO_USD_EXCHANGES


# ─────────────────────────────────────────────────────────────────────────────
# 12. normalize_ticker — MEXC produces -USD yfinance tickers
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_ticker_mexc_btc():
    from app.data.fetcher import normalize_ticker
    result = normalize_ticker("BTC", "CRYPTO_MEXC")
    assert result["yfinance_ticker"] == "BTC-USD"
    assert result["currency"] == "USD"
    assert result["asset_type"] == "CRYPTO"
    assert result["exchange_key"] == "CRYPTO_MEXC"


def test_normalize_ticker_mexc_strips_existing_suffix():
    """User may type 'BTC-USD' or 'BTC/USDT' — both should normalise to 'BTC-USD'."""
    from app.data.fetcher import normalize_ticker
    r1 = normalize_ticker("BTC-USD", "CRYPTO_MEXC")
    r2 = normalize_ticker("BTC/USDT", "CRYPTO_MEXC")
    assert r1["yfinance_ticker"] == "BTC-USD"
    assert r2["yfinance_ticker"] == "BTC-USD"


def test_normalize_ticker_asx_unaffected():
    """ASX equity normalization must not be touched by MEXC changes."""
    from app.data.fetcher import normalize_ticker
    result = normalize_ticker("BHP", "ASX")
    assert result["yfinance_ticker"] == "BHP.AX"
    assert result["currency"] == "AUD"
    assert result["asset_type"] == "EQUITY"


# ─────────────────────────────────────────────────────────────────────────────
# 13. update_position_pnl_task — writes live_price cache for MEXC positions
# ─────────────────────────────────────────────────────────────────────────────

def test_update_position_pnl_writes_live_price_cache(db_session, org_and_account):
    """update_position_pnl_task must write live_price:{ticker} to cache after fetching."""
    from app.models.trade import Position, TradeStatus
    from app.models.market import Stock
    from decimal import Decimal
    from datetime import date

    org, account = org_and_account

    # Create a MEXC crypto position
    stock = Stock(ticker="BTC-USD", exchange_code="BTC", exchange_key="CRYPTO_MEXC", asset_type="CRYPTO", currency="USD")
    db_session.add(stock)
    db_session.flush()

    pos = Position(
        organization_id=org.id,
        account_id=account.id,
        ticker="BTC-USD",
        exchange_key="CRYPTO_MEXC",
        asset_type="CRYPTO",
        currency="USD",
        entry_date=date(2026, 6, 1),
        qty=Decimal("0.01"),
        entry_price=Decimal("68000"),
        current_price=Decimal("68000"),
        status=TradeStatus.OPEN,
        initial_stop=Decimal("64000"),
        current_stop=Decimal("64000"),
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()

    mexc_result = {
        "price": 69000.0, "ok": True,
        "data_source": "mexc", "delay_mins": 0, "bar_timestamp": None,
    }
    cache_written = {}

    with patch("app.data.fetcher.get_intraday_price", return_value=mexc_result), \
         patch("app.utils.cache.cache.set", side_effect=lambda k, v, **kw: cache_written.update({k: v})), \
         patch("app.utils.cache.cache.get", return_value=None):
        from app.tasks.trading import update_position_pnl_task
        update_position_pnl_task.run()

    # Must write the live_price cache key
    assert "live_price:BTC-USD" in cache_written
    cached = cache_written["live_price:BTC-USD"]
    assert cached["price"] == 69000.0
    assert cached["_failed"] is False
    assert cached.get("live_price") is not None


def test_update_position_pnl_writes_failure_sentinel_on_api_error(db_session, org_and_account):
    """When price fetch fails, must write _failed sentinel to prevent stale live display."""
    from app.models.trade import Position, TradeStatus
    from decimal import Decimal

    org, account = org_and_account
    from datetime import date as _date
    pos = Position(
        organization_id=org.id,
        account_id=account.id,
        ticker="ETH-USD",
        exchange_key="CRYPTO_MEXC",
        asset_type="CRYPTO",
        currency="USD",
        entry_date=_date(2026, 6, 1),
        qty=Decimal("1.0"),
        entry_price=Decimal("3200"),
        current_price=Decimal("3200"),
        status=TradeStatus.OPEN,
        initial_stop=Decimal("3000"),
        current_stop=Decimal("3000"),
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()

    failed_result = {"ok": False, "price": None, "data_source": "eod_fallback"}
    cache_written = {}

    with patch("app.data.fetcher.get_intraday_price", return_value=failed_result), \
         patch("app.utils.cache.cache.set", side_effect=lambda k, v, **kw: cache_written.update({k: v})):
        from app.tasks.trading import update_position_pnl_task
        update_position_pnl_task.run()

    assert "live_price:ETH-USD" in cache_written
    assert cache_written["live_price:ETH-USD"]["_failed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 14. refresh_live_prices_cache_task — covers watchlist crypto tickers
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_live_prices_cache_seeds_watchlist_crypto(db_session, org_and_account):
    """refresh_live_prices_cache_task must seed live_price cache for crypto watchlist items."""
    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.market import Stock

    org, _ = org_and_account

    # Seed a MEXC watchlist item
    stock = Stock(ticker="SOL-USD", exchange_code="SOL", exchange_key="CRYPTO_MEXC", asset_type="CRYPTO", currency="USD")
    db_session.add(stock)
    db_session.flush()

    wl = Watchlist(
        organization_id=org.id,
        ticker="SOL-USD",
        exchange_key="CRYPTO_MEXC",
        asset_type="CRYPTO",
        currency="USD",
        status=WatchlistStatus.WATCHING,
    )
    db_session.add(wl)
    db_session.commit()

    mexc_result = {
        "price": 120.0, "ok": True,
        "data_source": "mexc", "delay_mins": 0, "bar_timestamp": None,
    }
    cache_written = {}

    with patch("app.data.fetcher.get_intraday_price", return_value=mexc_result), \
         patch("app.utils.cache.cache.set", side_effect=lambda k, v, **kw: cache_written.update({k: v})):
        from app.tasks.trading import refresh_live_prices_cache_task
        refresh_live_prices_cache_task.run()

    assert "live_price:SOL-USD" in cache_written
    assert cache_written["live_price:SOL-USD"]["price"] == 120.0


def test_refresh_live_prices_cache_skips_equity_tickers(db_session, org_and_account):
    """refresh_live_prices_cache_task must ONLY process CRYPTO asset_type — equities skipped."""
    from app.models.signal import Watchlist, WatchlistStatus

    org, _ = org_and_account
    wl = Watchlist(
        organization_id=org.id,
        ticker="BHP.AX",
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        status=WatchlistStatus.WATCHING,
    )
    db_session.add(wl)
    db_session.commit()

    with patch("app.tasks.trading.get_intraday_price") as mock_price:
        from app.tasks.trading import refresh_live_prices_cache_task
        refresh_live_prices_cache_task.run()

    # BHP.AX (equity) must not trigger an intraday price fetch in this task
    for call in mock_price.call_args_list:
        assert "BHP.AX" not in str(call)


# ─────────────────────────────────────────────────────────────────────────────
# 15. ExchangeKey enum — CRYPTO_MEXC present
# ─────────────────────────────────────────────────────────────────────────────

def test_exchange_key_enum_contains_mexc():
    from app.models.exchange import ExchangeKey
    assert ExchangeKey.CRYPTO_MEXC == "CRYPTO_MEXC"


def test_exchange_key_enum_asx_unaffected():
    from app.models.exchange import ExchangeKey
    assert ExchangeKey.ASX == "ASX"
    assert ExchangeKey.CRYPTO_INDEPENDENTRESERVE == "CRYPTO_INDEPENDENTRESERVE"


# ─────────────────────────────────────────────────────────────────────────────
# 16. CryptoBroker — MEXC connected (mocked ccxt exchange)
# ─────────────────────────────────────────────────────────────────────────────

def _make_mexc_broker():
    """Create a MEXC CryptoBroker with a mocked ccxt exchange (simulates live credentials)."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker.__new__(CryptoBroker)
    b.ccxt_provider = "mexc"
    b.api_key = "mexc_key"
    b.api_secret = "mexc_secret"
    b.testnet = False
    b.organization_id = None
    mock_exchange = MagicMock()
    b._exchange = mock_exchange
    b._connected = True
    return b, mock_exchange


def test_mexc_broker_submit_bracket_order_connected():
    b, mock_ex = _make_mexc_broker()
    mock_ex.create_limit_order.return_value = {"id": "MEXC_ORDER_123"}
    mock_ex.create_order.return_value = {"id": "MEXC_SL_456"}
    with patch("app.broker.crypto.CCXT_AVAILABLE", True):
        result = b.submit_bracket_order("BTC-USD", "BUY", 0.01, 68000, 64000, 72000)
    assert result["status"] == "submitted"
    assert result["entry_order_id"] == "MEXC_ORDER_123"
    assert result["exchange"] == "mexc"


def test_mexc_broker_get_market_snapshot_returns_price():
    b, mock_ex = _make_mexc_broker()
    mock_ex.fetch_ticker.return_value = {
        "last": 68000.0, "bid": 67999.0, "ask": 68001.0, "baseVolume": 5000.0
    }
    with patch("app.broker.crypto.CCXT_AVAILABLE", True):
        snap = b.get_market_snapshot("BTC-USD")
    assert snap is not None
    assert snap["last"] == 68000.0
    assert snap["data_source"] == "ccxt"


def test_mexc_broker_get_positions_excludes_usdt():
    """USDT must be excluded from positions (it's the quote currency, not an asset)."""
    b, mock_ex = _make_mexc_broker()
    mock_ex.fetch_balance.return_value = {
        "total": {"BTC": 0.1, "USDT": 1000.0, "ETH": 2.0, "USD": 0.0}
    }
    with patch("app.broker.crypto.CCXT_AVAILABLE", True):
        positions = b.get_positions()
    tickers = [p["ticker"] for p in positions]
    assert any("BTC" in t for t in tickers)
    assert any("ETH" in t for t in tickers)
    # Stablecoins must be excluded
    assert not any("USDT" in t for t in tickers)
    assert not any("USD-" in t for t in tickers)


# ─────────────────────────────────────────────────────────────────────────────
# 17. ASX equities — complete isolation check
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_ticker_asx_not_crypto():
    """ASX tickers must never produce -USD or -USDT suffixes."""
    from app.data.fetcher import normalize_ticker
    result = normalize_ticker("CBA", "ASX")
    assert result["yfinance_ticker"] == "CBA.AX"
    assert "-USD" not in result["yfinance_ticker"]
    assert result["asset_type"] == "EQUITY"
    assert result["currency"] == "AUD"


def test_top_crypto_tickers_asx_key_falls_through_to_usd():
    """Passing 'ASX' as exchange_key returns USD crypto list (safe fallback)."""
    from app.data.fetcher import get_top_crypto_tickers
    # ASX is not in CRYPTO_AUD_EXCHANGES so it falls through to USD list
    tickers = get_top_crypto_tickers("ASX")
    # Should return USD list (not AUD) — this is intentional defensive behaviour
    assert any(t.endswith("-USD") for t in tickers)
