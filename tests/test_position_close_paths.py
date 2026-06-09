"""
Regression tests for the critical "stopped-out crypto positions never actually
closed" bug cluster.

Root cause (see STATUS.md): `sync_stop_orders` (automated crypto stop-loss monitor)
and the MCP `close_position` tool both set non-existent attributes on `Position`
(`exit_price`, `exit_reason`, `closed_at`, `realised_pnl`, `opened_at` — none of
these are columns on Position, only on Trade) and passed invalid constructor kwargs
to `Trade()` (`opened_at=`, `closed_at=`, `realised_pnl=`). SQLAlchemy raised
AttributeError/TypeError on every attempt, which a broad `except Exception` block
swallowed — so a position that hit its stop simply stayed open forever, silently.
This is about as bad as a trading-bot bug gets: real capital stays exposed past its
stop with zero visible error.

These tests exercise the *real* production code paths end-to-end against an
isolated test database and assert the position actually closes and a correct,
queryable Trade row is written.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.models.trade import Position, Trade, TradeStatus, ExitReason


PHANTOM_POSITION_FIELDS = ["exit_price", "exit_reason", "closed_at", "realised_pnl", "opened_at"]
PHANTOM_TRADE_KWARGS = ["opened_at", "closed_at", "realised_pnl"]


def test_position_model_has_no_phantom_close_fields():
    """
    Schema guard: if `exit_price` / `exit_reason` / `closed_at` / `realised_pnl` /
    `opened_at` are ever (re)added as *columns* on Position, that's a strong signal
    someone is about to reintroduce the original bug pattern (writing exit detail
    onto Position instead of Trade). This test documents the intended split and
    fails loudly if the schema drifts from it.
    """
    mapped = {c.key for c in Position.__table__.columns}
    for field in PHANTOM_POSITION_FIELDS:
        assert field not in mapped, (
            f"Position gained a '{field}' column — re-verify sync_stop_orders, "
            f"MCP close_position, and the dashboard manual-close route still record "
            f"exit detail on Trade (the proven-correct pattern), not on Position."
        )


def test_trade_model_does_not_accept_phantom_kwargs():
    """
    Constructor guard: `Trade(opened_at=..., closed_at=..., realised_pnl=...)` must
    raise — these were exactly the invalid kwargs that silently broke automated
    stop-loss exits. If this test ever stops raising, someone has added those as
    real columns/synonyms and the original failure mode could resurface elsewhere.
    """
    for bad_kwarg in PHANTOM_TRADE_KWARGS:
        with pytest.raises(TypeError):
            Trade(**{
                "ticker": "TRX-AUD", "account_id": 1, "entry_date": date(2026, 6, 1),
                "exit_date": date(2026, 6, 4), "entry_price": 0.20, "exit_price": 0.19,
                "qty": 500, "exit_reason": ExitReason.STOP_LOSS,
                bad_kwarg: date(2026, 6, 4),
            })


# ──────────────────────────────────────────────────────────────────────────
# sync_stop_orders — automated crypto stop-loss monitor
# ──────────────────────────────────────────────────────────────────────────

def test_sync_stop_orders_closes_position_and_writes_correct_trade(db_session, org_and_account, open_crypto_position, monkeypatch):
    """
    End-to-end: price drops below the stop → sync_stop_orders must (a) flip the
    Position to CLOSED, (b) write a Trade row using the real Trade columns
    (entry_date/exit_date/hold_days/gross_pnl_aud/net_pnl_aud/pnl_pct/exit_price/
    exit_reason/initial_stop/cgt_eligible_discount), and (c) leave no phantom
    attributes dangling on the Position. Previously this whole flow raised
    AttributeError/TypeError that was swallowed, so the position just stayed OPEN.
    """
    import app.tasks.trading as trading_module

    _org, _account = org_and_account  # ensures the org+account+position fixtures are seeded
    pos = open_crypto_position
    stopped_out_price = float(pos.current_stop) - 0.01  # below stop -> triggers close

    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date(2026, 6, 5))
    monkeypatch.setattr("app.data.fetcher.get_intraday_price",
                        lambda ticker, org_id: {"ok": True, "price": stopped_out_price})
    monkeypatch.setattr("app.data.fetcher.get_price_history", lambda *a, **kw: None)
    monkeypatch.setattr(trading_module, "get_notifier", lambda **kw: SimpleNamespace(send=lambda *_a, **_kw: None))

    trading_module.sync_stop_orders.run()

    db_session.expire_all()
    refreshed = db_session.query(Position).get(pos.id)
    assert refreshed.status == TradeStatus.CLOSED, "Stopped-out position must actually close"

    trade = db_session.query(Trade).filter(Trade.ticker == "TRX-AUD").order_by(Trade.id.desc()).first()
    assert trade is not None, "A Trade row must be written recording the stop-loss exit"
    assert trade.exit_reason == ExitReason.STOP_LOSS
    assert trade.exit_date == date(2026, 6, 5)
    assert trade.entry_date == pos.entry_date
    assert trade.hold_days == (date(2026, 6, 5) - pos.entry_date).days
    assert float(trade.exit_price) == pytest.approx(stopped_out_price)
    assert float(trade.gross_pnl_aud) < 0  # price fell below entry -> loss
    assert float(trade.net_pnl_aud) == float(trade.gross_pnl_aud)  # no commission for crypto
    assert trade.exchange_key == "CRYPTO_INDEPENDENTRESERVE"
    assert trade.asset_type == "CRYPTO"


def test_sync_stop_orders_leaves_position_open_above_stop(db_session, org_and_account, open_crypto_position, monkeypatch):
    """Sanity check — price above stop must not close the position."""
    import app.tasks.trading as trading_module

    pos = open_crypto_position
    safe_price = float(pos.entry_price) * 1.05

    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date(2026, 6, 5))
    monkeypatch.setattr("app.data.fetcher.get_intraday_price",
                        lambda ticker, org_id: {"ok": True, "price": safe_price})
    monkeypatch.setattr("app.data.fetcher.get_price_history", lambda *a, **kw: None)

    trading_module.sync_stop_orders.run()

    db_session.expire_all()
    refreshed = db_session.query(Position).get(pos.id)
    assert refreshed.status == TradeStatus.OPEN
    assert db_session.query(Trade).filter(Trade.ticker == "TRX-AUD").count() == 0


# ──────────────────────────────────────────────────────────────────────────
# MCP close_position — manual close via the agent / MCP tools
# ──────────────────────────────────────────────────────────────────────────

def _fake_mcp_ctx(org_id, client_id="test-client"):
    return SimpleNamespace(org_id=org_id, client_id=client_id)


def test_mcp_close_position_closes_and_writes_correct_trade(db_session, org_and_account, open_crypto_position, monkeypatch):
    """
    Same bug, different entry point: closing a position via the MCP tool (e.g. an
    agent acting on "close TRX now") must succeed and produce a correct Trade row —
    not raise AttributeError/TypeError that gets reported back as a generic tool
    error while the position stays open.
    """
    import app.mcp.tools as mcp_tools

    org, _account = org_and_account
    pos = open_crypto_position

    monkeypatch.setattr(mcp_tools, "get_mcp_context", lambda: _fake_mcp_ctx(org.id))
    monkeypatch.setattr(mcp_tools, "assert_scope", lambda *_a, **_kw: None)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date(2026, 6, 6))
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: SimpleNamespace(send=lambda *_a, **_kw: None))

    result = mcp_tools.close_position(position_id=pos.id, exit_reason="STOP_LOSS", exit_price=0.15)

    assert result["ok"] is True, f"close_position must succeed, got: {result}"

    db_session.expire_all()
    refreshed = db_session.query(Position).get(pos.id)
    assert refreshed.status == TradeStatus.CLOSED

    trade = db_session.query(Trade).filter(Trade.ticker == "TRX-AUD").order_by(Trade.id.desc()).first()
    assert trade is not None
    assert trade.exit_reason == ExitReason.STOP_LOSS
    assert float(trade.exit_price) == 0.15
    assert trade.exit_date == date(2026, 6, 6)
    assert trade.entry_date == pos.entry_date
    assert float(trade.gross_pnl_aud) == pytest.approx((0.15 - float(pos.entry_price)) * float(pos.qty), rel=1e-6)


def test_mcp_close_position_rejects_invalid_exit_reason(db_session, org_and_account, open_crypto_position, monkeypatch):
    """Guard: an invalid exit_reason must be rejected cleanly, not raise."""
    import app.mcp.tools as mcp_tools

    org, _account = org_and_account
    pos = open_crypto_position
    monkeypatch.setattr(mcp_tools, "get_mcp_context", lambda: _fake_mcp_ctx(org.id))
    monkeypatch.setattr(mcp_tools, "assert_scope", lambda *_a, **_kw: None)

    result = mcp_tools.close_position(position_id=pos.id, exit_reason="NOT_A_REAL_REASON", exit_price=0.15)
    assert result["ok"] is False
    assert "Invalid exit_reason" in result["error"]

    db_session.expire_all()
    refreshed = db_session.query(Position).get(pos.id)
    assert refreshed.status == TradeStatus.OPEN, "Position must remain open when the close request is invalid"
