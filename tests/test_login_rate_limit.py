"""Integration tests for S3 rate limiting/lockout wired into the login and
OTP-verify routes in web/main.py. Uses the same in-memory fake Redis pattern
as tests/test_rate_limit.py so lockout/invalidation thresholds actually trip
(the real cache singleton fails open with no Redis in the test sandbox)."""
import asyncio
from types import SimpleNamespace

import pytest


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, seconds):
        pass

    def exists(self, key):
        return 1 if key in self.store else 0

    def delete(self, key):
        self.store.pop(key, None)

    def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    from app.utils import rate_limit
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "_client", lambda: fake)
    return fake


def _mock_request(session=None):
    req = SimpleNamespace()
    req.session = session if session is not None else {}
    req.headers = {}
    req.client = SimpleNamespace(host="1.2.3.4")
    req.query_params = {}
    req.url = SimpleNamespace(path="/", scheme="http")
    return req


def _make_user(db_session, org, email="lockout@astradigital.com.au", password="password123"):
    from app.models.auth import User, hash_password
    user = User(email=email, password_hash=hash_password(password), name="Lockout Test",
                organization_id=org.id, is_active=True)
    db_session.add(user)
    db_session.commit()
    return user


def test_login_locks_account_after_ten_failed_attempts(db_session, org_and_account, fake_redis):
    from web.main import login_post
    org, _ = org_and_account
    _make_user(db_session, org)

    for _ in range(10):
        req = _mock_request()
        resp = asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="wrongpass", db=db_session))
        assert resp.status_code == 401

    # 11th attempt (even with the correct password) is blocked by the lockout,
    # not by credential checking — locked accounts don't get to try again.
    req = _mock_request()
    resp = asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="password123", db=db_session))
    assert resp.status_code == 429


def test_login_lockout_audit_logged(db_session, org_and_account, fake_redis):
    from web.main import login_post
    from app.models.audit import AuditLog

    org, _ = org_and_account
    _make_user(db_session, org)

    for _ in range(10):
        req = _mock_request()
        asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="wrongpass", db=db_session))

    log = db_session.query(AuditLog).filter(
        AuditLog.actor == "lockout@astradigital.com.au",
        AuditLog.message.ilike("%locked for 15 minutes%"),
    ).first()
    assert log is not None


def test_login_success_resets_failure_counter(db_session, org_and_account, fake_redis):
    from web.main import login_post
    from app.utils.rate_limit import is_set

    org, _ = org_and_account
    _make_user(db_session, org)

    for _ in range(5):
        req = _mock_request()
        asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="wrongpass", db=db_session))

    # A successful login resets the counter — five more failures afterward
    # should not carry over toward the 10-attempt threshold.
    req = _mock_request()
    resp = asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="password123", db=db_session))
    assert resp.status_code in (302,)

    for _ in range(5):
        req = _mock_request()
        asyncio.run(login_post(request=req, email="lockout@astradigital.com.au", password="wrongpass", db=db_session))

    assert is_set("login_lock:lockout@astradigital.com.au") is False


def test_otp_invalidated_after_five_failed_verifies(db_session, org_and_account, fake_redis):
    from web.main import login_verify_otp_post
    from datetime import datetime, timedelta

    org, _ = org_and_account
    user = _make_user(db_session, org, email="otp@astradigital.com.au")
    user.otp_code = "123456"
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db_session.commit()

    for _ in range(5):
        req = _mock_request()
        resp = asyncio.run(login_verify_otp_post(request=req, email="otp@astradigital.com.au", otp_code="000000", db=db_session, next=""))
        assert resp.status_code == 400

    db_session.refresh(user)
    assert user.otp_code is None
    assert user.otp_expires_at is None


def test_otp_invalidation_audit_logged(db_session, org_and_account, fake_redis):
    from web.main import login_verify_otp_post
    from app.models.audit import AuditLog
    from datetime import datetime, timedelta

    org, _ = org_and_account
    user = _make_user(db_session, org, email="otp2@astradigital.com.au")
    user.otp_code = "123456"
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db_session.commit()

    for _ in range(5):
        req = _mock_request()
        asyncio.run(login_verify_otp_post(request=req, email="otp2@astradigital.com.au", otp_code="000000", db=db_session, next=""))

    log = db_session.query(AuditLog).filter(
        AuditLog.actor == "otp2@astradigital.com.au",
        AuditLog.message.ilike("%OTP invalidated after%"),
    ).first()
    assert log is not None


def test_otp_verify_success_resets_failure_counter(db_session, org_and_account, fake_redis):
    from web.main import login_verify_otp_post
    from app.utils.rate_limit import is_set
    from datetime import datetime, timedelta

    org, _ = org_and_account
    user = _make_user(db_session, org, email="otp3@astradigital.com.au")
    user.otp_code = "123456"
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db_session.commit()

    for _ in range(3):
        req = _mock_request()
        asyncio.run(login_verify_otp_post(request=req, email="otp3@astradigital.com.au", otp_code="000000", db=db_session, next=""))

    # Correct code on the 4th try succeeds and clears the OTP — verifies the
    # invalidate-after-5 threshold never trips when the user gets it right.
    req = _mock_request()
    resp = asyncio.run(login_verify_otp_post(request=req, email="otp3@astradigital.com.au", otp_code="123456", db=db_session, next=""))
    assert resp.status_code == 302
    db_session.refresh(user)
    assert user.otp_code is None
