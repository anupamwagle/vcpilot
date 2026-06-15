import asyncio

# Ensure there is a default event loop in the thread so that eventkit/ib_insync imports do not fail.
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# Pre-import tasks to cache modules inside sys.modules before asyncio.run() runs and closes any loops
import app.tasks.trading
import app.tasks.screening

from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app.models.auth import User, Role, hash_password
from app.models.account import Organization
from app.models.signal import Watchlist, WatchlistStatus
from app.models.audit import AuditLog, AuditAction
from dashboard.main import (
    superadmin_activity,
    login_post,
    logout,
    login_request_otp,
    login_verify_otp_post,
    superadmin_switch_org,
    watchlist_add,
    watchlist_promote,
    watchlist_remove,
    trader_watchlist_promote,
    superadmin_organizations_create,
    superadmin_users_create,
    superadmin_user_update_role,
    superadmin_user_reset_password
)

def _mock_request(session=None, headers=None, query_params=None):
    req = SimpleNamespace()
    req.session = session if session is not None else {}
    req.headers = headers if headers is not None else {}
    req.query_params = query_params if query_params is not None else {}
    req.url = SimpleNamespace(path="/", scheme="http")
    return req

def test_superadmin_activity_access_control(db_session, org_and_account):
    org, _ = org_and_account
    
    # 1. Unauthenticated
    req = _mock_request(session={})
    res = asyncio.run(superadmin_activity(request=req, db=db_session))
    assert res.status_code == 302
    assert res.headers["location"] == "/login"
    
    # 2. Authenticated but not superadmin
    req = _mock_request(session={"authenticated": True, "user_role": "user"})
    res = asyncio.run(superadmin_activity(request=req, db=db_session))
    assert res.status_code == 302
    assert "access_denied" in res.headers["location"]
    
    # 3. Authenticated superadmin
    req = _mock_request(session={"authenticated": True, "user_role": "superadmin"})
    res = asyncio.run(superadmin_activity(request=req, db=db_session))
    assert res.status_code == 200

def test_login_logout_otp_audit_logging(db_session, org_and_account):
    org, _ = org_and_account
    
    # Seed a user with Super Admin role
    superadmin_role = Role(name="Super Admin")
    db_session.add(superadmin_role)
    db_session.flush()
    
    user = User(
        email="test_sa@astradigital.com.au",
        password_hash=hash_password("password123"),
        name="Test Super Admin",
        organization_id=org.id,
        is_active=True
    )
    user.roles.append(superadmin_role)
    db_session.add(user)
    db_session.commit()
    
    # 1. Test Login Post - Database User Successful Login
    req = _mock_request()
    asyncio.run(login_post(request=req, email="test_sa@astradigital.com.au", password="password123", db=db_session))
    
    # Assert successful login audit log
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.user_id == user.id
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None
    assert "logged in from web dashboard" in log.message
    
    # 2. Test Login Post - Failed Login
    req = _mock_request()
    asyncio.run(login_post(request=req, email="test_sa@astradigital.com.au", password="wrongpassword", db=db_session))
    
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.TASK_ERROR,
        AuditLog.actor == "test_sa@astradigital.com.au"
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None
    assert "Failed login attempt" in log.message
    
    # 3. Test Logout
    req = _mock_request(session={
        "authenticated": True,
        "user_role": "superadmin",
        "email": "test_sa@astradigital.com.au",
        "user_id": user.id,
        "organization_id": org.id
    })
    asyncio.run(logout(request=req, db=db_session))
    
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.actor == "test_sa@astradigital.com.au"
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None
    assert "logged out" in log.message

    # 4. Test OTP Request
    req = _mock_request()
    asyncio.run(login_request_otp(request=req, email="test_sa@astradigital.com.au", next="", db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.actor == "test_sa@astradigital.com.au",
        AuditLog.message.ilike("%OTP passcode requested%")
    ).first()
    assert log is not None
    
    # 5. Test OTP Verify (Success)
    db_session.refresh(user)
    req = _mock_request(session={"authenticated": True})
    asyncio.run(login_verify_otp_post(request=req, email="test_sa@astradigital.com.au", otp_code=user.otp_code, next="", db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.actor == "test_sa@astradigital.com.au",
        AuditLog.message.ilike("%OTP passcode verified successfully%")
    ).first()
    assert log is not None

def test_superadmin_switch_org_audit_logging(db_session, org_and_account):
    org, _ = org_and_account
    org2 = Organization(name="Second Org", is_active=True)
    db_session.add(org2)
    db_session.commit()
    
    req = _mock_request(session={
        "authenticated": True,
        "user_role": "superadmin",
        "email": "sa@astradigital.com.au",
        "user_id": 1
    })
    asyncio.run(superadmin_switch_org(request=req, organization_id=org2.id, db=db_session))
    
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.organization_id == org2.id
    ).first()
    assert log is not None
    assert "switched active organization context to Second Org" in log.message

def test_watchlist_actions_audit_logging(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    
    # Mock screen_single_ticker.delay
    import app.tasks.screening as screening_module
    monkeypatch.setattr(screening_module, "screen_single_ticker", SimpleNamespace(delay=lambda *args, **kwargs: None))
    
    # 1. Watchlist Add
    req = _mock_request(session={
        "authenticated": True,
        "organization_id": org.id,
        "email": "user@test.com",
        "user_id": 123
    })
    asyncio.run(watchlist_add(request=req, ticker="BHP", notes="", label_id="", exchange_key="ASX", db=db_session))
    
    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "BHP.AX",
        AuditLog.message.ilike("%Added BHP.AX to watchlist%")
    ).first()
    assert log is not None
    assert log.organization_id == org.id
    assert log.actor == "user@test.com"

    # Create watchlist item
    wl_item = Watchlist(
        ticker="BHP.AX",
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        organization_id=org.id,
        status=WatchlistStatus.WATCHING
    )
    db_session.add(wl_item)
    db_session.commit()

    # Mock promote_watchlist_item_task.delay
    import app.tasks.trading as trading_module
    monkeypatch.setattr(trading_module, "promote_watchlist_item_task", SimpleNamespace(delay=lambda *args, **kwargs: None))

    # 2. Watchlist Promote
    req = _mock_request(session={
        "authenticated": True,
        "organization_id": org.id,
        "email": "user@test.com",
        "user_id": 123
    })
    asyncio.run(watchlist_promote(request=req, item_id=wl_item.id, db=db_session))
    
    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "BHP.AX",
        AuditLog.message.ilike("%Manual promotion of BHP.AX queued successfully%")
    ).first()
    assert log is not None

    # Reset status for trader promote test
    db_session.refresh(wl_item)
    wl_item.status = WatchlistStatus.WATCHING
    db_session.commit()

    # 3. Trader Watchlist Promote
    asyncio.run(trader_watchlist_promote(request=req, item_id=wl_item.id, db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "BHP.AX",
        AuditLog.message.ilike("%Trader WL terminal: manual promotion of BHP.AX queued successfully%")
    ).first()
    assert log is not None

    # Reset status for remove test
    db_session.refresh(wl_item)
    wl_item.status = WatchlistStatus.WATCHING
    db_session.commit()

    # 4. Watchlist Remove
    asyncio.run(watchlist_remove(request=req, item_id=wl_item.id, db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "BHP.AX",
        AuditLog.message.ilike("%Removed BHP.AX from watchlist%")
    ).first()
    assert log is not None

def test_superadmin_crud_audit_logging(db_session, org_and_account):
    org, _ = org_and_account
    
    # 1. Organization Create
    req = _mock_request(session={
        "authenticated": True,
        "user_role": "superadmin",
        "email": "sa@astradigital.com.au",
        "user_id": 1
    })
    asyncio.run(superadmin_organizations_create(
        request=req,
        name="New Org Created",
        tier="GOLD",
        admin_name="Test Admin",
        admin_email="admin@astradigital.com.au",
        db=db_session
    ))
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.message.ilike("%Super Admin created organization New Org Created%")
    ).first()
    assert log is not None

    # Seed Role for user creation
    trader_role = Role(name="Trader")
    db_session.add(trader_role)
    db_session.commit()

    # 2. User Create
    asyncio.run(superadmin_users_create(
        request=req,
        name="New User",
        email="newuser@test.com",
        organization_id=org.id,
        role_id=trader_role.id,
        send_welcome=None,
        db=db_session
    ))
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.message.ilike("%Super Admin created user account newuser@test.com with role Trader%")
    ).first()
    assert log is not None

    new_user = db_session.query(User).filter(User.email == "newuser@test.com").first()

    # 3. User Update Role
    admin_role = Role(name="Organisation Admin")
    db_session.add(admin_role)
    db_session.commit()
    asyncio.run(superadmin_user_update_role(user_id=new_user.id, request=req, role_id=admin_role.id, db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.message.ilike("%Super Admin updated role of user newuser@test.com to Organisation Admin%")
    ).first()
    assert log is not None

    # 4. User Reset Password
    asyncio.run(superadmin_user_reset_password(user_id=new_user.id, request=req, db=db_session))
    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.CONFIG_CHANGED,
        AuditLog.message.ilike("%Super Admin triggered password reset for user newuser@test.com%")
    ).first()
    assert log is not None

def test_superadmin_activity_filters(db_session, org_and_account):
    org, _ = org_and_account
    
    # Add logs with various attributes
    log1 = AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        organization_id=org.id,
        actor="actor1@test.com",
        ticker="BTC-USD",
        message="Message number one"
    )
    log2 = AuditLog(
        action=AuditAction.TASK_ERROR,
        organization_id=999,  # different org
        actor="actor2@test.com",
        ticker="ETH-USD",
        message="Message number two failure"
    )
    db_session.add_all([log1, log2])
    db_session.commit()
    
    req = _mock_request(session={"authenticated": True, "user_role": "superadmin"})
    
    # Filter by Org
    res = asyncio.run(superadmin_activity(request=req, org_id=str(org.id), db=db_session))
    # Check context in template response
    ctx = res.context
    assert len(ctx["logs"]) == 1
    assert ctx["logs"][0]["actor"] == "actor1@test.com"
    
    # Filter by Action
    res = asyncio.run(superadmin_activity(request=req, action="TASK_ERROR", db=db_session))
    ctx = res.context
    assert len(ctx["logs"]) == 1
    assert ctx["logs"][0]["actor"] == "actor2@test.com"
    
    # Search message
    res = asyncio.run(superadmin_activity(request=req, search="failure", db=db_session))
    ctx = res.context
    assert len(ctx["logs"]) == 1
    assert ctx["logs"][0]["ticker"] == "ETH-USD"
