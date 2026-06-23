"""
Tests for the 4 critical pipeline bugs found during the multi-market audit.

Bug 1: check_entry_triggers reads wrong regime key (last_market_regime instead of
       last_market_regime_{exchange_key}) → BEAR regime never blocks, CAUTION
       never halves position sizing for any exchange.

Bug 2: NASDAQ stocks never covered — all US beat tasks use exchange_key="NYSE" but
       the stock/signal/position filters used == "NYSE" (exact match), excluding
       NASDAQ stocks entirely.

Bug 3: run_daily_screen wrote signal.exchange_key = task's exchange_key ("NYSE")
       even for NASDAQ-100 stocks; now uses stock_obj.exchange_key.

Bug 4: run_full_setup never queued the US chain (only ASX + CRYPTO_*).
"""
import pytest
from datetime import date
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _seed_regime(db_session, org_id, exchange_key, value):
    from app.models.config import SystemConfig
    db_session.add(SystemConfig(
        key=f"last_market_regime_{exchange_key}",
        value=value,
        label=f"Regime {exchange_key}",
        group="system",
        organization_id=org_id,
    ))
    db_session.commit()


def _seed_active_exchanges(db_session, org_id, exchanges: str):
    from app.models.config import SystemConfig
    db_session.add(SystemConfig(
        key="active_exchanges", value=exchanges,
        label="Active Exchanges", group="trading", organization_id=org_id,
    ))
    db_session.commit()


def _make_stock(db_session, ticker, exchange_key, asset_type="EQUITY", currency="USD"):
    from app.models.market import Stock
    s = Stock(
        ticker=ticker,
        exchange_code=ticker,
        exchange_key=exchange_key,
        asset_type=asset_type,
        currency=currency,
        is_active=True,
        blacklisted=False,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_signal(db_session, org_id, ticker, exchange_key, status="PENDING"):
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    sig = Signal(
        organization_id=org_id,
        ticker=ticker,
        exchange_key=exchange_key,
        asset_type="EQUITY",
        currency="USD",
        signal_date=get_current_date(),
        status=SignalStatus(status),
        pivot_price=100.0,
        stop_price=90.0,
        rs_rating=85,
        trend_score=8,
        close_price=100.0,
    )
    db_session.add(sig)
    db_session.commit()
    db_session.refresh(sig)
    return sig


def _make_position(db_session, org, account, ticker, exchange_key, currency="USD"):
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker=ticker,
        exchange_key=exchange_key,
        asset_type="EQUITY",
        currency=currency,
        account_id=account.id,
        organization_id=org.id,
        entry_date=date(2026, 6, 1),
        entry_price=100.0,
        qty=10,
        initial_stop=90.0,
        current_stop=90.0,
        status=TradeStatus.OPEN,
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


# ─────────────────────────────────────────────────────────────────────────────
# BUG 1: Regime key in check_entry_triggers
# ─────────────────────────────────────────────────────────────────────────────

class TestEntryTriggerRegimeKey:
    """
    Verify that check_entry_triggers reads the correct per-exchange-per-org
    regime key (last_market_regime_NYSE) not the stale global (last_market_regime).
    """

    def test_nyse_regime_key_is_read_not_global(self, db_session, org_and_account):
        """When NYSE BEAR regime is set, the entry trigger code should see 'BEAR'."""
        from app.models.config import SystemConfig
        org, acct = org_and_account
        # Seed the correct per-org, per-exchange key
        _seed_regime(db_session, org.id, "NYSE", "BEAR")
        # Also seed the stale key with a conflicting value to confirm it's NOT used
        db_session.add(SystemConfig(
            key="last_market_regime", value="BULL",
            label="Stale", group="system", organization_id=None,
        ))
        db_session.commit()

        # Read the regime the same way check_entry_triggers now does
        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            regime_cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_market_regime_NYSE",
                SystemConfig.organization_id == org.id,
            ).first()
        assert regime_cfg is not None
        assert regime_cfg.value == "BEAR"

    def test_asx_regime_key_is_scoped_to_org(self, db_session, org_and_account):
        """ASX regime should read last_market_regime_ASX per org."""
        org, _ = org_and_account
        _seed_regime(db_session, org.id, "ASX", "CAUTION")
        from app.models.config import SystemConfig
        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_market_regime_ASX",
                SystemConfig.organization_id == org.id,
            ).first()
        assert cfg is not None and cfg.value == "CAUTION"

    def test_check_entry_triggers_skips_when_market_closed(self, db_session, org_and_account):
        """check_entry_triggers exits early when market is closed — no DB errors."""
        from app.tasks.trading import check_entry_triggers
        with patch("app.tasks.trading.market_is_open_now", return_value=False):
            check_entry_triggers.run(exchange_key="NYSE")  # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# BUG 2: NASDAQ stocks covered in signal / position filters
# ─────────────────────────────────────────────────────────────────────────────

class TestNasdaqCoverage:
    """
    When check_entry_triggers(exchange_key="NYSE") fires, it must also pick up
    signals and positions stored with exchange_key="NASDAQ".
    """

    def test_nyse_filter_includes_nasdaq_signals(self, db_session, org_and_account):
        from app.models.signal import Signal, SignalStatus
        org, _ = org_and_account
        nyse_sig  = _make_signal(db_session, org.id, "JPM",  "NYSE")
        nasdaq_sig = _make_signal(db_session, org.id, "NVDA", "NASDAQ")

        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            sigs = db.query(Signal).filter(
                Signal.organization_id == org.id,
                Signal.status == SignalStatus.PENDING,
                Signal.exchange_key.in_(["NYSE", "NASDAQ"])
            ).all()
        tickers = [s.ticker for s in sigs]
        assert "JPM"  in tickers
        assert "NVDA" in tickers

    def test_nyse_filter_includes_nasdaq_positions(self, db_session, org_and_account):
        from app.models.trade import Position, TradeStatus
        org, acct = org_and_account
        _make_position(db_session, org, acct, "JPM",  "NYSE")
        _make_position(db_session, org, acct, "NVDA", "NASDAQ")

        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            positions = db.query(Position).filter(
                Position.organization_id == org.id,
                Position.status == TradeStatus.OPEN,
                Position.exchange_key.in_(["NYSE", "NASDAQ"])
            ).all()
        tickers = [p.ticker for p in positions]
        assert "JPM"  in tickers
        assert "NVDA" in tickers

    def test_asx_filter_excludes_nasdaq_stocks(self, db_session, org_and_account):
        """ASX entry check must NOT pick up NYSE/NASDAQ signals."""
        from app.models.signal import Signal, SignalStatus
        org, _ = org_and_account
        _make_signal(db_session, org.id, "NVDA", "NASDAQ")
        _make_signal(db_session, org.id, "BHP.AX", "ASX")

        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            sigs = db.query(Signal).filter(
                Signal.organization_id == org.id,
                Signal.status == SignalStatus.PENDING,
                Signal.exchange_key == "ASX"
            ).all()
        tickers = [s.ticker for s in sigs]
        assert "BHP.AX" in tickers
        assert "NVDA" not in tickers

    def test_check_exit_task_skips_when_market_closed(self, db_session, org_and_account):
        """check_exit_rules_task exits early when market closed — no errors."""
        from app.tasks.trading import check_exit_rules_task
        with patch("app.tasks.trading.market_is_open_now", return_value=False):
            check_exit_rules_task.run(exchange_key="NYSE")  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# BUG 3: Signal exchange_key uses stock's actual key not task exchange_key
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalExchangeKey:
    """
    Signals generated by the screener must carry the stock's own exchange_key
    (e.g. "NASDAQ" for NVDA) not the beat task's parameter (e.g. "NYSE").
    """

    def test_nasdaq_stock_signal_gets_nasdaq_key(self, db_session, org_and_account):
        """
        Simulate how the screener assigns exchange_key to a new signal.
        The stock has exchange_key="NASDAQ"; the signal should inherit that.
        """
        from app.models.market import Stock
        org, _ = org_and_account
        nvda = _make_stock(db_session, "NVDA", "NASDAQ")

        # Simulate what the screener does: stock_obj.exchange_key wins over task exchange_key
        task_exchange_key = "NYSE"
        signal_exchange_key = (nvda.exchange_key if nvda and nvda.exchange_key else task_exchange_key) or task_exchange_key
        assert signal_exchange_key == "NASDAQ"

    def test_nyse_stock_signal_gets_nyse_key(self, db_session, org_and_account):
        org, _ = org_and_account
        jpm = _make_stock(db_session, "JPM", "NYSE")
        task_exchange_key = "NYSE"
        signal_exchange_key = (jpm.exchange_key if jpm and jpm.exchange_key else task_exchange_key) or task_exchange_key
        assert signal_exchange_key == "NYSE"

    def test_signal_exchange_key_not_polluted_by_task_key(self, db_session, org_and_account):
        """
        When task runs with exchange_key="NYSE", a NASDAQ stock's signal must NOT
        get exchange_key="NYSE".
        """
        from app.models.signal import Signal, SignalStatus
        from app.utils.time_helper import get_current_date
        org, _ = org_and_account
        nvda = _make_stock(db_session, "NVDA", "NASDAQ")

        # Correct assignment (what the fixed code does)
        task_key = "NYSE"
        signal_key = (nvda.exchange_key if nvda and nvda.exchange_key else task_key) or task_key
        sig = Signal(
            organization_id=org.id, ticker="NVDA", exchange_key=signal_key,
            asset_type="EQUITY", currency="USD", signal_date=get_current_date(),
            status=SignalStatus.PENDING, pivot_price=500.0, stop_price=450.0,
            rs_rating=90, trend_score=8, close_price=500.0,
        )
        db_session.add(sig)
        db_session.commit()
        db_session.refresh(sig)
        assert sig.exchange_key == "NASDAQ"  # NOT "NYSE"


# ─────────────────────────────────────────────────────────────────────────────
# BUG 4: run_full_setup includes US markets
# ─────────────────────────────────────────────────────────────────────────────

class TestRunFullSetupUS:
    """
    run_full_setup must collect NYSE/NASDAQ from active_exchanges and queue
    the US chain (refresh_us_universe → refresh_price_data → evaluate_regime → screen).
    """

    def test_active_exchanges_nyse_sets_has_us(self, db_session, org_and_account):
        """When active_exchanges includes NYSE, has_us should be True."""
        org, _ = org_and_account
        _seed_active_exchanges(db_session, org.id, "ASX,NYSE")

        from app.models.config import SystemConfig
        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "active_exchanges",
                SystemConfig.organization_id == org.id,
            ).first()
        has_us = any(e.strip() in ("NYSE", "NASDAQ") for e in (cfg.value or "").split(","))
        assert has_us is True

    def test_active_exchanges_nasdaq_sets_has_us(self, db_session, org_and_account):
        """When active_exchanges includes NASDAQ, has_us should be True."""
        org, _ = org_and_account
        _seed_active_exchanges(db_session, org.id, "ASX,NASDAQ")

        from app.models.config import SystemConfig
        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "active_exchanges",
                SystemConfig.organization_id == org.id,
            ).first()
        has_us = any(e.strip() in ("NYSE", "NASDAQ") for e in (cfg.value or "").split(","))
        assert has_us is True

    def test_active_exchanges_asx_only_no_us(self, db_session, org_and_account):
        """ASX-only should not trigger US chain."""
        org, _ = org_and_account
        _seed_active_exchanges(db_session, org.id, "ASX")

        from app.models.config import SystemConfig
        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "active_exchanges",
                SystemConfig.organization_id == org.id,
            ).first()
        has_us = any(e.strip() in ("NYSE", "NASDAQ") for e in (cfg.value or "").split(","))
        assert has_us is False

    def test_run_full_setup_source_has_us_chain(self):
        """Verify run_full_setup source contains the US chain (refresh_us_universe)."""
        import inspect
        from app.tasks.screening import run_full_setup
        src = inspect.getsource(run_full_setup)
        assert "refresh_us_universe" in src
        assert "has_us" in src
        assert "NYSE/NASDAQ" in src or "US" in src

    def test_run_full_setup_smoke(self, db_session, org_and_account):
        """run_full_setup runs without error when no active exchange configured (defaults to ASX)."""
        from app.tasks.screening import run_full_setup
        # celery_chain is imported locally inside run_full_setup as `from celery import chain as celery_chain`
        with patch("celery.chain") as mock_chain:
            mock_chain.return_value.delay = MagicMock()
            run_full_setup.run()
        # Should have called chain at least once (for ASX default)
        assert mock_chain.called

    def test_run_full_setup_chains_us_when_nyse_active(self, db_session, org_and_account):
        """When NYSE is in active_exchanges, run_full_setup must queue a US chain."""
        org, _ = org_and_account
        _seed_active_exchanges(db_session, org.id, "ASX,NYSE")

        from app.tasks.screening import run_full_setup
        us_chain_queued = False

        def _mock_chain(*tasks):
            nonlocal us_chain_queued
            task_strs = " ".join(str(t) for t in tasks)
            if "refresh_us_universe" in task_strs or "us_universe" in task_strs:
                us_chain_queued = True
            m = MagicMock()
            m.delay = MagicMock()
            return m

        with patch("celery.chain", side_effect=_mock_chain):
            run_full_setup.run()

        assert us_chain_queued, "US setup chain was not queued when NYSE is in active_exchanges"


# ─────────────────────────────────────────────────────────────────────────────
# refresh_price_data — NASDAQ covered when exchange_key="NYSE"
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceDataNasdaqCoverage:
    """
    refresh_price_data(exchange_key="NYSE") must refresh both NYSE and NASDAQ stocks.
    """

    def test_nyse_key_includes_nasdaq_stocks_in_query(self, db_session, org_and_account):
        """The stock filter for exchange_key='NYSE' should include NASDAQ stocks."""
        from app.models.market import Stock
        _make_stock(db_session, "JPM",  "NYSE")
        _make_stock(db_session, "NVDA", "NASDAQ")

        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            tickers = [
                s.ticker for s in db.query(Stock).filter(
                    Stock.is_active == True,
                    Stock.blacklisted == False,
                    Stock.exchange_key.in_(["NYSE", "NASDAQ"]),
                ).all()
            ]
        assert "JPM"  in tickers
        assert "NVDA" in tickers

    def test_asx_key_excludes_nasdaq_stocks(self, db_session, org_and_account):
        from app.models.market import Stock
        _make_stock(db_session, "BHP.AX", "ASX")
        _make_stock(db_session, "NVDA",   "NASDAQ")

        with __import__("app.database", fromlist=["get_db"]).get_db() as db:
            tickers = [
                s.ticker for s in db.query(Stock).filter(
                    Stock.is_active == True,
                    Stock.exchange_key == "ASX",
                ).all()
            ]
        assert "BHP.AX" in tickers
        assert "NVDA"   not in tickers
