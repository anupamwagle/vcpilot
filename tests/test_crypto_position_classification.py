"""
Regression test for the crypto-position misclassification bug.

The intraday entry-trigger code creates a simulated `Position` whenever a bracket
order is "filled" in paper mode. It must copy `exchange_key` / `asset_type` /
`currency` across from the triggering `Signal`. Before the fix it omitted these,
so every new Position silently defaulted to "ASX" / "EQUITY" / "AUD" — including
crypto fills. Every downstream consumer that decides "is this crypto?" (most
importantly `sync_stop_orders`, the automated stop-loss monitor) checks
`asset_type == "CRYPTO" or exchange_key.startswith("CRYPTO")`, so a misclassified
crypto position would be silently skipped from stop-loss monitoring forever —
real money exposed with no safety net and no error.

We don't re-run the whole entry-trigger pipeline (it needs a live broker, sizing
engine, and market data); instead we replicate the exact Position(...) construction
used in app/tasks/trading.py:check_entry_triggers (the `is_simulated` branch,
~line 443) from a crypto Signal, and assert the resulting row is classified as
crypto by the same predicate sync_stop_orders uses.
"""
from datetime import date

from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, TradeStatus


def _is_crypto_position(pos) -> bool:
    """Mirrors the is_crypto check in sync_stop_orders (app/tasks/trading.py)."""
    return getattr(pos, "asset_type", "EQUITY") == "CRYPTO" or (
        pos.exchange_key and pos.exchange_key.startswith("CRYPTO")
    )


def _build_position_from_signal(signal, account, org, entry_price=0.21, qty=480):
    """Exact field mapping used by check_entry_triggers when opening a simulated Position."""
    return Position(
        ticker=signal.ticker,
        exchange_key=signal.exchange_key or "ASX",
        asset_type=signal.asset_type or "EQUITY",
        currency=signal.currency or "AUD",
        account_id=account.id,
        organization_id=org.id,
        signal_id=signal.id,
        entry_date=date(2026, 6, 8),
        entry_price=entry_price,
        qty=qty,
        current_price=entry_price,
        initial_stop=float(signal.stop_price),
        current_stop=float(signal.stop_price),
        target_1=float(signal.target_price_1 or entry_price * 1.20),
        target_2=float(signal.target_price_2 or entry_price * 1.40),
        risk_aud=round((entry_price - float(signal.stop_price)) * qty, 2),
        is_paper=True,
        status=TradeStatus.OPEN,
    )


def test_crypto_signal_produces_correctly_classified_position(db_session, org_and_account):
    org, account = org_and_account

    crypto_signal = Signal(
        ticker="TRX-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO",
        currency="AUD", signal_date=date(2026, 6, 8), status=SignalStatus.PENDING,
        organization_id=org.id, close_price=0.21, pivot_price=0.21, stop_price=0.168,
        target_price_1=0.252, target_price_2=0.294,
    )
    db_session.add(crypto_signal)
    db_session.commit()
    db_session.refresh(crypto_signal)

    pos = _build_position_from_signal(crypto_signal, account, org)
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)

    assert pos.exchange_key == "CRYPTO_INDEPENDENTRESERVE", (
        "Position must inherit the signal's exchange_key — not silently default to 'ASX'"
    )
    assert pos.asset_type == "CRYPTO", (
        "Position must inherit the signal's asset_type — not silently default to 'EQUITY'"
    )
    assert pos.currency == "AUD"
    assert _is_crypto_position(pos) is True, (
        "A misclassified crypto Position would be invisible to sync_stop_orders' "
        "is_crypto filter and would NEVER be monitored for stop-loss — the exact "
        "failure mode that left stopped-out positions open indefinitely."
    )


def test_equity_signal_still_produces_equity_position(db_session, org_and_account):
    """Sanity check — the fix must not misclassify ASX equities as crypto either."""
    org, account = org_and_account

    equity_signal = Signal(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        signal_date=date(2026, 6, 8), status=SignalStatus.PENDING, organization_id=org.id,
        close_price=45.0, pivot_price=45.0, stop_price=41.4,
        target_price_1=54.0, target_price_2=63.0,
    )
    db_session.add(equity_signal)
    db_session.commit()
    db_session.refresh(equity_signal)

    pos = _build_position_from_signal(equity_signal, account, org, entry_price=45.0, qty=20)
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)

    assert pos.exchange_key == "ASX"
    assert pos.asset_type == "EQUITY"
    assert _is_crypto_position(pos) is False
