"""
Tests targeting uncovered paths in app/tasks/screening.py.
Focuses on refresh_price_data, refresh_crypto_universe, run_daily_screen
fundamentals paths, and screen_single_ticker existing-watchlist update.
"""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _make_df(ticker="BHP.AX", close=50.0, date_val=None):
    """Return a minimal DataFrame that refresh_price_data will accept."""
    import pandas as pd
    d = date_val or date.today()
    row = {
        "date": d, "open": close, "high": close + 1, "low": close - 1,
        "close": close, "adj_close": close, "volume": 1_000_000,
        "ma_10": close, "ma_21": close, "ma_50": close, "ma_150": close,
        "ma_200": close, "ma_200_prev": close, "avg_vol_50": 500_000,
        "vol_ratio": 1.0, "high_52w": close + 5, "low_52w": close - 5,
        "pct_from_52w_high": -2.0, "pct_from_52w_low": 10.0, "atr_14": 1.5,
        "rs_rating": 75.0,
    }
    return pd.DataFrame([row])


def _make_rule_result(passed=True):
    from app.screener.rules import RuleResult
    return RuleResult(rule_id="test", passed=passed, message="ok", value=1.0)


# ────────────────────────────────────────────────────────────
# refresh_crypto_universe — update-existing-stock path
# ────────────────────────────────────────────────────────────

def test_refresh_crypto_universe_seeds_new_stock(db_session, org_and_account, monkeypatch):
    """refresh_crypto_universe seeds a new stock when it doesn't exist."""
    from app.tasks.screening import refresh_crypto_universe
    from app.models.market import Stock

    org, _ = org_and_account

    # Mock normalize_ticker to return a new ticker that doesn't exist yet
    monkeypatch.setattr("app.tasks.screening.normalize_ticker",
                        lambda t, k: {"yfinance_ticker": "LTC-AUD", "display_code": "LTC",
                                      "currency": "AUD", "asset_type": "CRYPTO"})

    with patch("app.tasks.screening.get_top_crypto_tickers", return_value=["LTC"]):
        refresh_crypto_universe.run(exchange_key="CRYPTO_INDEPENDENTRESERVE")

    db_session.expire_all()
    s = db_session.query(Stock).filter(Stock.ticker == "LTC-AUD").first()
    assert s is not None
    assert s.exchange_key == "CRYPTO_INDEPENDENTRESERVE"
    assert s.asset_type == "CRYPTO"


# ────────────────────────────────────────────────────────────
# refresh_price_data — upserts a price bar for today
# ────────────────────────────────────────────────────────────

def test_refresh_price_data_upserts_bar(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_price_data
    from app.models.market import Stock, PriceBar

    org, _ = org_and_account
    s = Stock(ticker="CBA.AX", exchange_code="CBA", exchange_key="ASX", asset_type="EQUITY",
              currency="AUD", in_asx200=True, is_active=True, blacklisted=False)
    db_session.add(s)
    db_session.commit()

    df = _make_df("CBA.AX")

    monkeypatch.setattr("app.tasks.screening.get_batch_prices",
                        lambda tickers, period="2y": {"CBA.AX": df})
    monkeypatch.setattr("app.tasks.screening.compute_rs_ratings",
                        lambda prices: {"CBA.AX": 75.0})
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.screening._write_task_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr("app.data.calendar.today_is_trading_day", lambda *a, **kw: True)

    refresh_price_data.run("ASX")

    db_session.expire_all()
    bar = db_session.query(PriceBar).filter(PriceBar.ticker == "CBA.AX").first()
    assert bar is not None
    assert bar.close == 50.0


def test_refresh_price_data_updates_existing_bar(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_price_data
    from app.models.market import Stock, PriceBar

    org, _ = org_and_account
    today = date.today()
    s = Stock(ticker="NAB.AX", exchange_code="NAB", exchange_key="ASX", asset_type="EQUITY",
              currency="AUD", in_asx200=True, is_active=True, blacklisted=False)
    db_session.add(s)
    # Pre-existing bar for today
    b = PriceBar(ticker="NAB.AX", date=today, close=30.0)
    db_session.add(b)
    db_session.commit()

    df = _make_df("NAB.AX", close=31.0, date_val=today)
    monkeypatch.setattr("app.tasks.screening.get_batch_prices",
                        lambda tickers, period="2y": {"NAB.AX": df})
    monkeypatch.setattr("app.tasks.screening.compute_rs_ratings",
                        lambda prices: {})
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: today)
    monkeypatch.setattr("app.tasks.screening._write_task_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr("app.data.calendar.today_is_trading_day", lambda *a, **kw: True)

    refresh_price_data.run("ASX")

    db_session.expire_all()
    bar = db_session.query(PriceBar).filter(PriceBar.ticker == "NAB.AX").first()
    assert bar.close == 31.0


def test_refresh_price_data_no_tickers_aborts(db_session, org_and_account, monkeypatch):
    """When no stocks found and not a crypto key, task returns early."""
    from app.tasks.screening import refresh_price_data

    monkeypatch.setattr("app.tasks.screening.get_batch_prices",
                        lambda *a, **kw: {})
    monkeypatch.setattr("app.data.calendar.today_is_trading_day", lambda *a, **kw: True)

    # Should not raise
    refresh_price_data.run("NYSE")


# ────────────────────────────────────────────────────────────
# run_daily_screen — fund-fail → watchlist path
# ────────────────────────────────────────────────────────────

def test_run_daily_screen_fund_fail_adds_watchlist(db_session, org_and_account, monkeypatch):
    """Stock passes 8/8 trend but fails fundamentals → watchlist."""
    from app.tasks.screening import run_daily_screen
    from app.models.market import Stock, PriceBar
    from app.models.signal import Watchlist

    org, _ = org_and_account
    today = date.today()

    s = Stock(ticker="ORG.AX", exchange_code="ORG", exchange_key="ASX", asset_type="EQUITY",
              currency="AUD", in_asx200=False, is_active=True, blacklisted=False)
    db_session.add(s)
    db_session.commit()

    df = _make_df("ORG.AX")

    from app.screener.rules import RuleResult
    from app.screener.vcp import VCPResult

    pass_rule = RuleResult(rule_id="t", passed=True, message="ok", value=1.0)
    fail_rule = RuleResult(rule_id="f", passed=False, message="fail", value=0.0)

    trend_results = {f"trend_{i}": pass_rule for i in range(8)}
    fund_results = {"fundamental_eps_growth": fail_rule}
    vcp_result = VCPResult(
        detected=False, contraction_count=0, base_weeks=0,
        pivot_price=0, stop_price=0, volume_dried_up=False,
    )

    # Make df have 200+ rows as required
    import pandas as pd
    big_df = pd.concat([df] * 210, ignore_index=True)

    monkeypatch.setattr("app.tasks.screening.get_batch_prices",
                        lambda tickers, period="2y": {"ORG.AX": big_df})
    monkeypatch.setattr("app.tasks.screening.get_price_history",
                        lambda *a, **kw: big_df)
    monkeypatch.setattr("app.tasks.screening.compute_rs_ratings", lambda p: {})
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: today)
    monkeypatch.setattr("app.data.calendar.today_is_trading_day", lambda *a, **kw: True)
    monkeypatch.setattr("app.tasks.screening._write_task_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr("app.tasks.screening.get_notifier",
                        lambda organization_id=None: MagicMock())

    mock_engine = MagicMock()
    mock_engine.threshold.return_value = None
    mock_engine.is_enabled.return_value = True
    monkeypatch.setattr("app.tasks.screening.RuleEngine", lambda **kw: mock_engine)
    # trend all pass (8/8)
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template",
                        lambda *a, **kw: trend_results)
    # fundamentals all fail (< 75% threshold)
    monkeypatch.setattr("app.tasks.screening.evaluate_fundamentals",
                        lambda *a, **kw: fund_results)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals",
                        lambda *a, **kw: {})
    monkeypatch.setattr("app.tasks.screening.detect_vcp", lambda *a, **kw: (vcp_result, {}))
    monkeypatch.setattr("app.tasks.screening.evaluate_market_regime",
                        lambda db, engine, exchange_key: "BULL")

    run_daily_screen.run("ASX")

    db_session.expire_all()
    wl = db_session.query(Watchlist).filter(
        Watchlist.ticker == "ORG.AX",
        Watchlist.organization_id == org.id,
    ).first()
    assert wl is not None


# ────────────────────────────────────────────────────────────
# screen_single_ticker — existing watchlist update path
# ────────────────────────────────────────────────────────────

def test_screen_single_ticker_updates_existing_watchlist(db_session, org_and_account, monkeypatch):
    """When ticker already on watchlist, screen_single_ticker updates it."""
    from app.tasks.screening import screen_single_ticker
    from app.models.market import Stock, PriceBar
    from app.models.signal import Watchlist, WatchlistStatus

    org, _ = org_and_account
    today = date.today()

    # Pre-seed stock + watchlist
    s = Stock(ticker="WES.AX", exchange_code="WES", exchange_key="ASX", asset_type="EQUITY",
              currency="AUD", in_asx200=False, is_active=True)
    db_session.add(s)
    wl = Watchlist(ticker="WES.AX", exchange_key="ASX", asset_type="EQUITY",
                   currency="AUD", organization_id=org.id, status=WatchlistStatus.WATCHING,
                   added_date=today)
    db_session.add(wl)
    db_session.commit()

    df = _make_df("WES.AX")

    from app.screener.rules import RuleResult
    from app.screener.vcp import VCPResult

    pass_rule = RuleResult(rule_id="t", passed=True, message="ok", value=1.0)
    trend_results = {f"trend_{i}": pass_rule for i in range(8)}
    fund_results = {f"fundamental_{i}": pass_rule for i in range(3)}
    vcp_result = VCPResult(
        detected=False, contraction_count=0, base_weeks=0,
        pivot_price=0, stop_price=0, volume_dried_up=False,
    )

    monkeypatch.setattr("app.tasks.screening.get_price_history",
                        lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: today)
    monkeypatch.setattr("app.tasks.screening.get_notifier",
                        lambda organization_id=None: MagicMock())

    mock_engine = MagicMock()
    mock_engine.run_trend_template.return_value = trend_results
    mock_engine.run_fundamentals.return_value = fund_results
    mock_engine.threshold.return_value = None
    monkeypatch.setattr("app.tasks.screening.RuleEngine", lambda **kw: mock_engine)
    monkeypatch.setattr("app.tasks.screening.detect_vcp", lambda *a, **kw: vcp_result)
    monkeypatch.setattr("app.tasks.screening.normalize_ticker",
                        lambda t, k: {"yfinance_ticker": t, "display_code": t,
                                      "currency": "AUD", "asset_type": "EQUITY"})

    screen_single_ticker.run("WES.AX", exchange_key="ASX", organization_id=org.id)

    db_session.expire_all()
    wl2 = db_session.query(Watchlist).filter(
        Watchlist.ticker == "WES.AX",
        Watchlist.organization_id == org.id,
    ).first()
    assert wl2 is not None


def test_screen_single_ticker_no_price_data(db_session, org_and_account, monkeypatch):
    """When no price history returned, task should exit gracefully."""
    from app.tasks.screening import screen_single_ticker

    org, _ = org_and_account

    monkeypatch.setattr("app.tasks.screening.get_price_history",
                        lambda *a, **kw: None)
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.screening.get_notifier",
                        lambda organization_id=None: MagicMock())
    monkeypatch.setattr("app.tasks.screening.normalize_ticker",
                        lambda t, k: {"yfinance_ticker": t, "display_code": t,
                                      "currency": "AUD", "asset_type": "EQUITY"})

    # Should not raise
    screen_single_ticker.run("UNKNOWN.AX", exchange_key="ASX", organization_id=org.id)


def test_screen_single_ticker_vcp_signal_already_exists(db_session, org_and_account, monkeypatch):
    """When VCP detected but signal already exists, no duplicate signal created."""
    from app.tasks.screening import screen_single_ticker
    from app.models.market import Stock
    from app.models.signal import Signal, SignalStatus

    org, _ = org_and_account
    today = date.today()

    s = Stock(ticker="FMG.AX", exchange_code="FMG", exchange_key="ASX", asset_type="EQUITY",
              currency="AUD", in_asx200=False, is_active=True)
    db_session.add(s)
    # Existing pending signal
    sig = Signal(
        ticker="FMG.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", signal_date=today, status=SignalStatus.PENDING,
        close_price=20.0, pivot_price=21.0, stop_price=19.0,
        organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    df = _make_df("FMG.AX", close=21.0)

    from app.screener.rules import RuleResult
    from app.screener.vcp import VCPResult

    pass_rule = RuleResult(rule_id="t", passed=True, message="ok", value=1.0)
    trend_results = {f"trend_{i}": pass_rule for i in range(8)}
    fund_results = {f"fundamental_{i}": pass_rule for i in range(3)}
    vcp_result = VCPResult(
        detected=True, contraction_count=3, base_weeks=8,
        pivot_price=21.0, stop_price=19.0, volume_dried_up=True,
    )

    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_current_date", lambda: today)
    monkeypatch.setattr("app.tasks.screening.get_notifier",
                        lambda organization_id=None: MagicMock())
    monkeypatch.setattr("app.tasks.screening.normalize_ticker",
                        lambda t, k: {"yfinance_ticker": t, "display_code": t,
                                      "currency": "AUD", "asset_type": "EQUITY"})

    mock_engine = MagicMock()
    mock_engine.run_trend_template.return_value = trend_results
    mock_engine.run_fundamentals.return_value = fund_results
    mock_engine.threshold.return_value = None
    monkeypatch.setattr("app.tasks.screening.RuleEngine", lambda **kw: mock_engine)
    monkeypatch.setattr("app.tasks.screening.detect_vcp", lambda *a, **kw: vcp_result)

    screen_single_ticker.run("FMG.AX", exchange_key="ASX", organization_id=org.id)

    db_session.expire_all()
    count = db_session.query(Signal).filter(
        Signal.ticker == "FMG.AX", Signal.organization_id == org.id
    ).count()
    assert count == 1  # No duplicate
