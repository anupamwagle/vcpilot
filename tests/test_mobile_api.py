"""Tests for app/api/mobile.py — mobile API routes."""
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(db_session):
    """Build a minimal FastAPI app with mobile router and overridden DB."""
    from app.api.mobile import router, get_db, _current_user
    app = FastAPI()
    app.include_router(router)

    def override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    return app


def _make_authed_app(db_session, user, org_id):
    """App with both DB and auth overridden."""
    from app.api.mobile import router, get_db, _current_user
    app = FastAPI()
    app.include_router(router)

    def override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def override_auth():
        return user, org_id

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_current_user] = override_auth
    return app


def _seed_user(db_session, org):
    from app.models.auth import User, hash_password
    user = User(
        email="test@mobile.com",
        password_hash=hash_password("Test1234!"),
        name="Test User",
        organization_id=org.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


# ---------------------------------------------------------------------------
# Helpers unit tests
# ---------------------------------------------------------------------------

def test_ticker_display_asx():
    from app.api.mobile import _ticker_display
    assert _ticker_display("BHP.AX") == "BHP"


def test_ticker_display_crypto():
    from app.api.mobile import _ticker_display
    assert _ticker_display("BTC-USD") == "BTC"


def test_ticker_display_plain():
    from app.api.mobile import _ticker_display
    assert _ticker_display("AAPL") == "AAPL"


def test_float_converts_value():
    from app.api.mobile import _float
    assert _float("3.14") == pytest.approx(3.14)
    assert _float(None) is None
    assert _float("not-a-number") is None


def test_create_and_decode_token():
    from app.api.mobile import _create_token, _decode_token
    token = _create_token(1, 42, "user@test.com")
    assert isinstance(token, str)
    payload = _decode_token(token)
    assert payload["sub"] == "1"
    assert payload["org"] == 42
    assert payload["email"] == "user@test.com"


def test_decode_token_invalid_raises():
    from app.api.mobile import _decode_token
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _decode_token("not.a.valid.token")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Login endpoint
# ---------------------------------------------------------------------------

def test_login_success(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_app(db_session)
    client = TestClient(app)

    resp = client.post("/api/mobile/auth/login", json={
        "email": "test@mobile.com",
        "password": "Test1234!",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["email"] == "test@mobile.com"
    assert data["org_id"] == org.id


def test_login_wrong_password(db_session, org_and_account):
    org, _ = org_and_account
    _seed_user(db_session, org)
    app = _make_app(db_session)
    client = TestClient(app)

    resp = client.post("/api/mobile/auth/login", json={
        "email": "test@mobile.com",
        "password": "WrongPassword!",
    })
    assert resp.status_code == 401


def test_login_user_not_found(db_session, org_and_account):
    org, _ = org_and_account
    app = _make_app(db_session)
    client = TestClient(app)

    resp = client.post("/api/mobile/auth/login", json={
        "email": "nobody@test.com",
        "password": "anypassword",
    })
    assert resp.status_code == 401


def test_login_inactive_user(db_session, org_and_account):
    org, _ = org_and_account
    from app.models.auth import User, hash_password
    user = User(
        email="inactive@test.com",
        password_hash=hash_password("Test1234!"),
        name="Inactive",
        organization_id=org.id,
        is_active=False,
    )
    db_session.add(user)
    db_session.commit()

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/api/mobile/auth/login", json={
        "email": "inactive@test.com",
        "password": "Test1234!",
    })
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------

def test_me_returns_user_info(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/auth/me", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "test@mobile.com"
    assert data["org_id"] == org.id


# ---------------------------------------------------------------------------
# /dashboard
# ---------------------------------------------------------------------------

def test_dashboard_returns_stats(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/dashboard", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    data = resp.json()
    assert "open_positions_count" in data
    assert "pending_signals_count" in data
    assert "worker_status" in data
    assert "trading_paused" in data


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------

def test_get_positions_empty(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/positions", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_get_positions_with_open_position(db_session, org_and_account, open_crypto_position):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/positions", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


# ---------------------------------------------------------------------------
# /positions/{id}/close
# ---------------------------------------------------------------------------

def test_close_position_success(db_session, org_and_account, open_crypto_position):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    pos = open_crypto_position
    resp = client.post(
        f"/api/mobile/positions/{pos.id}/close",
        json={"exit_reason": "STOP_LOSS", "exit_price": 0.30},
        headers={"Authorization": "Bearer dummy"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True


def test_close_position_invalid_reason(db_session, org_and_account, open_crypto_position):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    pos = open_crypto_position
    resp = client.post(
        f"/api/mobile/positions/{pos.id}/close",
        json={"exit_reason": "INVALID_REASON"},
        headers={"Authorization": "Bearer dummy"},
    )
    assert resp.status_code == 400


def test_close_position_not_found(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post(
        "/api/mobile/positions/99999/close",
        json={"exit_reason": "STOP_LOSS"},
        headers={"Authorization": "Bearer dummy"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /signals
# ---------------------------------------------------------------------------

def test_get_signals_empty(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/signals", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_get_signals_with_data(db_session, org_and_account):
    from app.models.signal import Signal, SignalStatus

    org, _ = org_and_account
    user = _seed_user(db_session, org)

    sig = Signal(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
        signal_date=date.today(), status=SignalStatus.PENDING,
        close_price=25.0, pivot_price=25.5, stop_price=23.0,
        organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/signals", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


def test_skip_signal_success(db_session, org_and_account):
    from app.models.signal import Signal, SignalStatus

    org, _ = org_and_account
    user = _seed_user(db_session, org)

    sig = Signal(
        ticker="ANZ.AX", exchange_key="ASX", asset_type="EQUITY",
        signal_date=date.today(), status=SignalStatus.PENDING,
        close_price=20.0, organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post(f"/api/mobile/signals/{sig.id}/skip",
                       headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "SKIPPED"


def test_skip_signal_not_found(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post("/api/mobile/signals/99999/skip",
                       headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 404


def test_unskip_signal_success(db_session, org_and_account):
    from app.models.signal import Signal, SignalStatus

    org, _ = org_and_account
    user = _seed_user(db_session, org)

    sig = Signal(
        ticker="CBA.AX", exchange_key="ASX", asset_type="EQUITY",
        signal_date=date.today(), status=SignalStatus.SKIPPED,
        close_price=80.0, organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post(f"/api/mobile/signals/{sig.id}/unskip",
                       headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING"


# ---------------------------------------------------------------------------
# /watchlist
# ---------------------------------------------------------------------------

def test_get_watchlist_empty(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/watchlist", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_get_watchlist_with_item(db_session, org_and_account, watching_trx_item):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/watchlist", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------

def test_get_trades_empty(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.get("/api/mobile/trades", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# /actions
# ---------------------------------------------------------------------------

def test_pause_trading(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post("/api/mobile/actions/pause", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["trading_paused"] is True


def test_resume_trading(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    # First pause
    from app.models.config import SystemConfig
    db_session.add(SystemConfig(key="trading_paused", value="True", organization_id=org.id))
    db_session.commit()

    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    resp = client.post("/api/mobile/actions/resume", headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["trading_paused"] is False


def test_force_screen_action(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    with patch("app.api.mobile._queue_task", return_value=True):
        resp = client.post("/api/mobile/actions/force-screen",
                           headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_ping_worker_action(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    with patch("app.api.mobile._queue_task", return_value=False):
        resp = client.post("/api/mobile/actions/ping-worker",
                           headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_refresh_data_action(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    with patch("app.api.mobile._queue_task", return_value=True):
        resp = client.post("/api/mobile/actions/refresh-data?exchange_key=ASX",
                           headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200


def test_evaluate_regime_action(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    with patch("app.api.mobile._queue_task", return_value=True):
        resp = client.post("/api/mobile/actions/evaluate-regime",
                           headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200


def test_send_report_action(db_session, org_and_account):
    org, _ = org_and_account
    user = _seed_user(db_session, org)
    app = _make_authed_app(db_session, user, org.id)
    client = TestClient(app)

    with patch("app.api.mobile._queue_task", return_value=True):
        resp = client.post("/api/mobile/actions/send-report",
                           headers={"Authorization": "Bearer dummy"})
    assert resp.status_code == 200
