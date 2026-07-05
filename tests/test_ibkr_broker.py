"""
Broker-layer safety tests for IBKRBroker.

Covers two live-trading-critical behaviours:
  1. get_market_snapshot must tolerate NaN fields (IBKR returns float('nan') for
     unavailable data, e.g. ASX outside hours) instead of crashing with
     "cannot convert float NaN to integer".
  2. submit_bracket_order only falls back to an internal SIMULATION when the
     broker is genuinely not connected — a connected (paper or live) gateway
     gets a real order. (The entry task no longer fakes paper fills.)
"""
import pytest

# These tests exercise the real ib_insync-backed code; skip if not installed.
pytest.importorskip("ib_insync")

from app.broker.ibkr import IBKRBroker


class _FakeIB:
    def qualifyContracts(self, c):
        return [c]
    def sleep(self, n):
        pass
    def reqMarketDataType(self, n):
        pass
    def cancelMktData(self, c):
        pass


def _broker(connected: bool):
    b = IBKRBroker.__new__(IBKRBroker)   # bypass DB-resolving __init__
    b.organization_id = 1
    b._connected = connected
    b._ib = _FakeIB() if connected else None
    b.account = "DUR090436"
    b.host = "ibkr"; b.port = 4004; b.client_id = 1; b.paper_mode = True
    b.last_error = ""
    return b


def test_snapshot_all_nan_returns_none_not_crash(monkeypatch):
    nan = float("nan")
    ticker = type("T", (), {"last": nan, "close": nan, "bid": nan, "ask": nan, "volume": nan})()
    b = _broker(True)
    monkeypatch.setattr(b._ib, "reqMktData", lambda *a, **k: ticker, raising=False)
    # Must not raise "cannot convert float NaN to integer"
    assert b.get_market_snapshot("WGN.AX", "ASX") is None


def test_snapshot_valid_last_with_nan_volume(monkeypatch):
    nan = float("nan")
    ticker = type("T", (), {"last": 10.5, "close": 10.4, "bid": 10.45, "ask": 10.55, "volume": nan})()
    b = _broker(True)
    monkeypatch.setattr(b._ib, "reqMktData", lambda *a, **k: ticker, raising=False)
    snap = b.get_market_snapshot("BHP.AX", "ASX")
    assert snap is not None
    assert snap["last"] == 10.5
    assert snap["volume"] == 0          # NaN volume coerced to 0, no crash
    assert snap["bid"] == 10.45


def test_submit_simulates_only_when_disconnected():
    sim = _broker(False).submit_bracket_order(
        "BHP", "BUY", 10, 40.0, 36.0, 48.0, exchange_key="ASX", order_ref="t"
    )
    assert sim["status"] == "simulated"   # disconnected → internal simulation


def test_is_connected_false_when_not_connected():
    assert _broker(False).is_connected is False
