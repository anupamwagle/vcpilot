"""
Regression tests for the User-Activity logging middleware.

THE BUG (must never come back — it broke EVERY request, including order entry):
  The first version reassigned `request._receive` inside a BaseHTTPMiddleware
  (`@app.middleware("http")`) to replay a buffered request body. That desyncs
  Starlette's receive stream and raises:
      RuntimeError: Unexpected message received: http.request
  on any POST whose route reads the form body (e.g. /admin/config save, Sync
  Positions). The fix reimplements it as a pure-ASGI middleware that buffers and
  replays the body cleanly.

These tests stand up a minimal app that mounts the REAL ActivityLoggerMiddleware
(imported from web.main) behind SessionMiddleware, drive it with TestClient,
and assert:
  * a POST whose handler reads the form body succeeds (no RuntimeError) AND the
    handler receives the full, uncorrupted body  ← the core regression
  * access (GET) and change (POST) rows are written with feature/IP/method
  * secrets are redacted, query + form params captured
  * skipped paths and unauthenticated requests are NOT logged
"""
import pytest

pytest.importorskip("starlette.testclient")

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from web.main import (
    ActivityLoggerMiddleware,
    _activity_feature_for,
    _activity_should_skip,
    _redact_params,
)
from app.models.audit import AuditLog, AuditAction


# ── Minimal app exercising the real middleware ──────────────────────────────
async def _login(request):
    request.session.update({
        "authenticated": True, "user_id": 5,
        "email": "trader@astradigital.com.au", "organization_id": 1,
    })
    return PlainTextResponse("ok")


async def _config_save(request):
    # Reads the form body (the exact pattern that crashed) then redirects.
    form = await request.form()
    # Echo back what the handler actually received so the test can prove the
    # body survived the middleware's buffer/replay intact.
    return RedirectResponse(
        f"/admin/config?saved=1&got_account={form.get('ibkr_account','')}", status_code=302
    )


async def _positions(request):
    return PlainTextResponse("positions")


async def _json_body(request):
    body = await request.body()
    return JSONResponse({"len": len(body)})


def _make_client():
    routes = [
        Route("/__login", _login, methods=["GET"]),
        Route("/admin/config", _config_save, methods=["POST"]),
        Route("/positions", _positions, methods=["GET"]),
        Route("/api/echo", _json_body, methods=["POST"]),  # /api/ is skipped
    ]
    app = Starlette(routes=routes, middleware=[
        Middleware(ActivityLoggerMiddleware),                 # outermost
        Middleware(SessionMiddleware, secret_key="test-secret"),
    ])
    return TestClient(app, raise_server_exceptions=True)


def _activity_rows(db, **filters):
    q = db.query(AuditLog).filter(
        AuditLog.action.in_([AuditAction.FEATURE_ACCESS, AuditAction.FEATURE_ACTION])
    )
    for k, v in filters.items():
        q = q.filter(getattr(AuditLog, k) == v)
    return q.order_by(AuditLog.id).all()


# ── Pure-function unit tests (fast) ─────────────────────────────────────────
def test_feature_mapping():
    assert _activity_feature_for("/admin/config") == "Org Config"
    assert _activity_feature_for("/admin/config/save") == "Org Config"
    assert _activity_feature_for("/superadmin/organizations/1") == "Organisations"
    assert _activity_feature_for("/positions/5/close") == "Positions"
    assert _activity_feature_for("/trader/watchlist") == "Watchlist Terminal"
    assert _activity_feature_for("/") == "Dashboard Home"


def test_redaction():
    out = _redact_params({"ibkr_account": ["DU1"], "ibkr_password": ["hunter2"],
                          "client_secret": ["x"], "threshold": ["80"]})
    assert out["ibkr_account"] == "DU1"
    assert out["ibkr_password"] == "***redacted***"
    assert out["client_secret"] == "***redacted***"
    assert out["threshold"] == "80"


def test_skip_rules():
    assert _activity_should_skip("/login")
    assert _activity_should_skip("/api/echo")
    assert _activity_should_skip("/admin/tasks/poll")
    assert not _activity_should_skip("/admin/config")


# ── Core regression: POST with form body must NOT crash and body intact ─────
def test_post_form_body_intact_and_no_crash(db_session):
    client = _make_client()
    client.get("/__login")
    # If the receive-stream bug returns, TestClient re-raises the RuntimeError here.
    resp = client.post(
        "/admin/config?saved=1",
        data={"ibkr_account": "DUR090436", "ibkr_password": "secret123", "threshold": "80"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Handler received the full, uncorrupted form body:
    assert "got_account=DUR090436" in resp.headers["location"]

    rows = _activity_rows(db_session, action=AuditAction.FEATURE_ACTION, feature="Org Config")
    assert len(rows) == 1
    row = rows[0]
    assert row.http_method == "POST"
    assert row.user_id == 5
    assert row.detail["params"]["ibkr_account"] == "DUR090436"
    assert row.detail["params"]["ibkr_password"] == "***redacted***"   # secret never stored
    assert row.detail["params"]["threshold"] == "80"
    assert row.detail["params"]["saved"] == "1"                        # query param captured
    assert row.detail["status"] == 302


def test_get_logged_as_feature_access(db_session):
    client = _make_client()
    client.get("/__login")
    resp = client.get("/positions")
    assert resp.status_code == 200
    rows = _activity_rows(db_session, action=AuditAction.FEATURE_ACCESS, feature="Positions")
    assert len(rows) == 1
    assert rows[0].http_method == "GET"
    assert rows[0].user_id == 5


def test_skipped_path_not_logged(db_session):
    client = _make_client()
    client.get("/__login")
    client.post("/api/echo", json={"a": 1})  # /api/ is in the skip list
    assert _activity_rows(db_session, feature="Other") == []
    # nothing logged for the skipped /api/echo path
    assert all(r.detail["path"] != "/api/echo" for r in _activity_rows(db_session))


def test_unauthenticated_not_logged(db_session):
    client = _make_client()
    # No /__login → no session → must not log
    client.get("/positions")
    assert _activity_rows(db_session) == []


def test_non_urlencoded_body_passes_through(db_session):
    """JSON bodies must not be buffered/garbled — handler still reads them fully."""
    client = _make_client()
    client.get("/__login")
    # /api/echo is skipped for logging but still must work end-to-end (body intact)
    payload = {"hello": "world", "n": 123}
    resp = client.post("/api/echo", json=payload)
    assert resp.status_code == 200
    assert resp.json()["len"] > 0
