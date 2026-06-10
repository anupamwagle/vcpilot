"""Tests for the Celery tasks in app/tasks/screening.py."""
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


def _make_price_df(rows=250, close=50.0):
    """Build a minimal price DataFrame with indicators."""
    from datetime import date, timedelta
    dates = [date(2023, 1, 1) + timedelta(days=i) for i in range(rows)]
    np.random.seed(0)
    c = np.full(rows, close) + np.cumsum(np.random.randn(rows) * 0.3)
    c = np.maximum(c, 0.1)
    df = pd.DataFrame({
        "date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": np.full(rows, 500_000.0), "adj_close": c,
    })
    # Add indicator columns expected by the screener
    for col in ["ma_10", "ma_21", "ma_50", "ma_150", "ma_200", "ma_200_prev",
                "avg_vol_50", "vol_ratio", "high_52w", "low_52w",
                "pct_from_52w_high", "pct_from_52w_low", "atr_14"]:
        df[col] = df["close"].rolling(min(rows, 50), min_periods=1).mean()
    return df


# --- evaluate_market_regime_task ---

def test_evaluate_market_regime_task_writes_regime(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import evaluate_market_regime_task
    from app.models.config import SystemConfig
    from app.screener.market_regime import MarketRegime

    org, _ = org_and_account
    # Mock price data fetch
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: _make_price_df())
    # Mock notifier
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)

    evaluate_market_regime_task.run(exchange_key="ASX")

    # Should have written a regime SystemConfig row for the org
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_ASX",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg is not None
    assert cfg.value in ("BULL", "CAUTION", "BEAR")


def test_evaluate_market_regime_task_no_price_data(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import evaluate_market_regime_task
    from app.models.config import SystemConfig

    org, _ = org_and_account
    # Mock price data returns None
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: None)

    # Should not raise — just log warning
    evaluate_market_regime_task.run(exchange_key="ASX")
    # No regime written for org
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_market_regime_ASX",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg is None


# --- refresh_crypto_universe ---

def test_refresh_crypto_universe_seeds_stocks(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_crypto_universe
    from app.models.market import Stock

    org, _ = org_and_account
    # No existing stocks
    assert db_session.query(Stock).count() == 0

    refresh_crypto_universe.run(exchange_key="CRYPTO_INDEPENDENTRESERVE")

    stocks = db_session.query(Stock).filter(Stock.asset_type == "CRYPTO").all()
    assert len(stocks) > 50  # Should have seeded top-N tokens
    assert any(s.ticker == "BTC-AUD" for s in stocks)


def test_refresh_crypto_universe_skips_existing_stocks(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_crypto_universe
    from app.models.market import Stock

    org, _ = org_and_account
    # Pre-seed one stock
    db_session.add(Stock(
        ticker="BTC-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE",
        asset_type="CRYPTO", currency="AUD", in_asx200=False, is_active=True,
        name="BTC", sector="Crypto", exchange_code="BTC",
    ))
    db_session.commit()

    refresh_crypto_universe.run(exchange_key="CRYPTO_INDEPENDENTRESERVE")

    # BTC should still only have one row
    btc_count = db_session.query(Stock).filter(Stock.ticker == "BTC-AUD").count()
    assert btc_count == 1


# --- _run_screen_force ---

def test_run_screen_force_with_no_stocks(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import _run_screen_force
    org, _ = org_and_account
    # No stocks → should return without crashing
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    # Should not raise
    _run_screen_force.run(organization_id=org.id, exchange_key="ASX")


def test_run_screen_force_screens_stocks(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import _run_screen_force
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal

    org, _ = org_and_account
    # Seed one stock with price history
    db_session.add(Stock(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True, name="BHP",
        sector="Materials", exchange_code="BHP",
    ))
    db_session.commit()

    # Mock price data fetch for this stock
    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_batch_prices", lambda *a, **kw: {})
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    # Should not raise
    _run_screen_force.run(organization_id=org.id, exchange_key="ASX")


# --- screen_single_ticker ---

def test_screen_single_ticker_not_a_trading_day_skips_on_flag(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker
    from app.models.signal import Watchlist

    org, _ = org_and_account
    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    # Should add to watchlist or signal, or at minimum not crash
    screen_single_ticker.run(
        ticker="BHP.AX", exchange_key="ASX", organization_id=org.id
    )


def test_screen_single_ticker_no_price_data_returns_early(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker

    org, _ = org_and_account
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: None)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    # Should not crash with no price data
    screen_single_ticker.run(
        ticker="NONEXISTENT.AX", exchange_key="ASX", organization_id=org.id
    )


# --- run_daily_screen ---

def test_run_daily_screen_skips_on_non_trading_day(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import run_daily_screen

    org, _ = org_and_account
    # Mock calendar to say it's not a trading day
    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: False)

    # Should silently return without running screener
    run_daily_screen.run(exchange_key="ASX")


def test_run_daily_screen_with_empty_universe(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import run_daily_screen

    org, _ = org_and_account
    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: True)
    # No stocks in universe but auto-bootstrap skipped
    monkeypatch.setattr("app.tasks.screening.get_asx200_tickers", lambda: [])
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: None)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    # Should not crash
    run_daily_screen.run(exchange_key="ASX")


# --- refresh_price_data ---

def test_refresh_price_data_skips_non_trading_day(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_price_data

    org, _ = org_and_account
    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: False)

    # Should skip without crashing
    refresh_price_data.run(exchange_key="ASX")


def test_refresh_price_data_no_tickers(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import refresh_price_data

    org, _ = org_and_account
    # Calendar says it's a trading day
    monkeypatch.setattr("app.data.calendar.today_is_trading_day", lambda *a: True)

    # No stocks → aborts gracefully
    refresh_price_data.run(exchange_key="NYSE")


# --- run_daily_screen with stocks (exercises per-ticker loop) ---

def _make_rule_result(passed=True):
    from app.screener.rules import RuleResult
    return RuleResult(rule_id="test", passed=passed, value=1.0, threshold=1.0, message="ok")


def test_run_daily_screen_with_stock_trend_fails_low_score(db_session, org_and_account, monkeypatch):
    """Trend passes < 6 → no watchlist entry (update_if_exists only)."""
    from app.tasks.screening import run_daily_screen
    from app.models.market import Stock
    from app.models.signal import Watchlist

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="ANZ.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True,
        name="ANZ", sector="Financials", exchange_code="ANZ",
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: True)
    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})

    # 4 out of 8 trend rules pass — not enough for watchlist
    fail_r = _make_rule_result(False)
    pass_r = _make_rule_result(True)
    fake_trend = {f"rule_{i}": (pass_r if i < 4 else fail_r) for i in range(8)}
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template", lambda *a, **kw: fake_trend)

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    run_daily_screen.run(exchange_key="ASX")
    # No watchlist entry added (< 6 criteria)
    assert db_session.query(Watchlist).count() == 0


def test_run_daily_screen_with_stock_trend_passes_6_adds_watchlist(db_session, org_and_account, monkeypatch):
    """6/8 trend rules pass → stock added to watchlist."""
    from app.tasks.screening import run_daily_screen
    from app.models.market import Stock
    from app.models.signal import Watchlist

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="CBA.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True,
        name="CBA", sector="Financials", exchange_code="CBA",
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: True)
    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})

    # 6 of 8 trend rules pass → watchlist eligible
    fail_r = _make_rule_result(False)
    pass_r = _make_rule_result(True)
    fake_trend = {f"rule_{i}": (pass_r if i < 6 else fail_r) for i in range(8)}
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template", lambda *a, **kw: fake_trend)

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    run_daily_screen.run(exchange_key="ASX")
    # Watchlist entry added
    assert db_session.query(Watchlist).filter_by(ticker="CBA.AX").count() >= 1


def test_run_daily_screen_full_pass_creates_signal(db_session, org_and_account, monkeypatch):
    """All rules pass + VCP detected → Signal created."""
    from app.tasks.screening import run_daily_screen
    from app.models.market import Stock
    from app.models.signal import Signal

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="WBC.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True,
        name="Westpac", sector="Financials", exchange_code="WBC",
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.screening.today_is_trading_day", lambda *a: True)
    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {
        "company_name": "Westpac", "sector": "Financials", "industry": "Banks",
        "eps_quarterly": [1.0, 1.1, 1.2, 1.3],
        "revenue_quarterly": [10, 11, 12, 13],
        "roe": 0.15, "net_margin": 0.2,
        "inst_ownership_pct": 70, "next_earnings_date": None,
    })

    pass_r = _make_rule_result(True)
    all_pass = {f"rule_{i}": pass_r for i in range(8)}
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template", lambda *a, **kw: all_pass)
    monkeypatch.setattr("app.tasks.screening.evaluate_fundamentals", lambda *a, **kw: all_pass)

    from app.screener.vcp import VCPResult
    fake_vcp = VCPResult(detected=True, contraction_count=3, base_weeks=8,
                         pivot_price=50.5, stop_price=47.0, volume_dried_up=True)
    monkeypatch.setattr("app.tasks.screening.detect_vcp",
                        lambda *a, **kw: (fake_vcp, {}))

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    run_daily_screen.run(exchange_key="ASX")
    sig = db_session.query(Signal).filter_by(ticker="WBC.AX", organization_id=org.id).first()
    assert sig is not None
    assert sig.pivot_price is not None


# --- _run_screen_force deeper coverage ---

def test_run_screen_force_vcp_detected_creates_signal(db_session, org_and_account, monkeypatch):
    """_run_screen_force: all rules pass + VCP → Signal."""
    from app.tasks.screening import _run_screen_force
    from app.models.market import Stock
    from app.models.signal import Signal

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="NAB.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True,
        name="NAB", sector="Financials", exchange_code="NAB",
    ))
    db_session.commit()

    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_batch_prices", lambda *a, **kw: {"NAB.AX": df})
    monkeypatch.setattr("app.tasks.screening.compute_rs_ratings", lambda *a, **kw: {"NAB.AX": 75})
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {
        "company_name": "NAB", "sector": "Financials", "industry": "Banks",
        "eps_quarterly": [1.0, 1.1, 1.2, 1.3],
        "revenue_quarterly": [10, 11, 12, 13],
        "roe": 0.15, "net_margin": 0.2,
        "inst_ownership_pct": 70, "next_earnings_date": None,
    })

    pass_r = _make_rule_result(True)
    all_pass = {f"rule_{i}": pass_r for i in range(8)}
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template", lambda *a, **kw: all_pass)
    monkeypatch.setattr("app.tasks.screening.evaluate_fundamentals", lambda *a, **kw: all_pass)

    from app.screener.vcp import VCPResult
    fake_vcp = VCPResult(detected=True, contraction_count=3, base_weeks=8,
                         pivot_price=50.5, stop_price=47.0, volume_dried_up=True)
    monkeypatch.setattr("app.tasks.screening.detect_vcp",
                        lambda *a, **kw: (fake_vcp, {}))

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    _run_screen_force.run(organization_id=org.id, exchange_key="ASX")
    sig = db_session.query(Signal).filter_by(ticker="NAB.AX", organization_id=org.id).first()
    assert sig is not None


def test_run_screen_force_no_vcp_adds_watchlist(db_session, org_and_account, monkeypatch):
    """_run_screen_force: all trend/fund rules pass but VCP not detected → watchlist."""
    from app.tasks.screening import _run_screen_force
    from app.models.market import Stock
    from app.models.signal import Watchlist

    org, _ = org_and_account
    db_session.add(Stock(
        ticker="WES.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", in_asx200=True, is_active=True,
        name="Wesfarmers", sector="Retail", exchange_code="WES",
    ))
    db_session.commit()

    df = _make_price_df(252)
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_batch_prices", lambda *a, **kw: {"WES.AX": df})
    monkeypatch.setattr("app.tasks.screening.compute_rs_ratings", lambda *a, **kw: {})
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})

    pass_r = _make_rule_result(True)
    all_pass = {f"rule_{i}": pass_r for i in range(8)}
    monkeypatch.setattr("app.tasks.screening.evaluate_trend_template", lambda *a, **kw: all_pass)
    monkeypatch.setattr("app.tasks.screening.evaluate_fundamentals", lambda *a, **kw: all_pass)

    from app.screener.vcp import VCPResult
    no_vcp = VCPResult(detected=False, contraction_count=0, base_weeks=0,
                       pivot_price=0, stop_price=0, volume_dried_up=False)
    monkeypatch.setattr("app.tasks.screening.detect_vcp",
                        lambda *a, **kw: (no_vcp, {}))

    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    _run_screen_force.run(organization_id=org.id, exchange_key="ASX")
    # Should be in watchlist
    wl = db_session.query(Watchlist).filter_by(ticker="WES.AX", organization_id=org.id).first()
    assert wl is not None
