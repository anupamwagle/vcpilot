"""
Multi-org membership regression suite.

Covers the feature that lets ONE user belong to several organizations and switch
the active one:

  * model + service layer (pure SQLAlchemy — runs anywhere):
      - backfill of existing single-org users
      - a user belonging to multiple orgs
      - add_user_to_org idempotency + home-org switching
      - UNIQUE(user_id, organization_id) guard
      - User.is_member_of / organization_ids helpers

  * dashboard routes (import web.main lazily inside each test, matching the
    rest of the suite — these run in the app container):
      - POST /switch-org allows a member, denies a non-member
      - org creation with an existing admin email ADDS a membership (no 400)
      - superadmin user-create with an existing email ADDS a membership (no 400)

The route tests import `web.main` *inside* the test body so test collection
never fails in environments where the heavy web dependencies aren't installed.
"""
from types import SimpleNamespace

import pytest

from app.models.auth import User, Role, OrganizationMembership, hash_password
from app.models.account import Organization, OrganizationTier
from app.services.membership import (
    add_user_to_org,
    backfill_memberships,
    switchable_orgs,
    user_can_access,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def three_orgs(db_session):
    orgs = [
        Organization(name="Org Alpha", tier=OrganizationTier.GOLD, is_active=True),
        Organization(name="Org Bravo", tier=OrganizationTier.SILVER, is_active=True),
        Organization(name="Org Charlie", tier=OrganizationTier.BRONZE, is_active=True),
    ]
    db_session.add_all(orgs)
    db_session.commit()
    for o in orgs:
        db_session.refresh(o)
    return orgs


@pytest.fixture()
def homed_user(db_session, three_orgs):
    """A user whose home org is Org Alpha (no membership rows yet)."""
    alpha = three_orgs[0]
    u = User(
        email="multi@astradigital.com.au",
        password_hash=hash_password("pw"),
        name="Multi User",
        organization_id=alpha.id,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _fake_request(org_id, *, user_role="user", user_id=1, email="multi@astradigital.com.au",
                  referer="/", host="localhost:8501"):
    return SimpleNamespace(
        session={
            "authenticated": True,
            "organization_id": org_id,
            "organization_name": "X",
            "user_role": user_role,
            "user_id": user_id,
            "email": email,
        },
        headers={"referer": referer, "host": host},
        url=SimpleNamespace(scheme="http", path="/"),
        query_params={},
    )


# ──────────────────────────────────────────────────────────────────────────
# 1. Model + service layer (pure — no dashboard import)
# ──────────────────────────────────────────────────────────────────────────

def test_backfill_creates_one_default_membership(db_session, homed_user, three_orgs):
    created = backfill_memberships(db_session)
    db_session.commit()
    assert created == 1
    m = db_session.query(OrganizationMembership).filter_by(user_id=homed_user.id).all()
    assert len(m) == 1
    assert m[0].organization_id == three_orgs[0].id
    assert m[0].is_default is True
    # idempotent — second run creates nothing
    assert backfill_memberships(db_session) == 0


def test_user_can_belong_to_multiple_orgs(db_session, homed_user, three_orgs):
    backfill_memberships(db_session)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)
    add_user_to_org(db_session, homed_user, three_orgs[2].id)
    db_session.commit()

    assert sorted(homed_user.organization_ids) == sorted(o.id for o in three_orgs)
    assert homed_user.is_member_of(three_orgs[1].id)
    assert not homed_user.is_member_of(99999)

    opts = switchable_orgs(db_session, homed_user.id)
    assert [o["name"] for o in opts] == ["Org Alpha", "Org Bravo", "Org Charlie"]
    assert sum(1 for o in opts if o["is_default"]) == 1


def test_add_user_to_org_is_idempotent(db_session, homed_user, three_orgs):
    backfill_memberships(db_session)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)  # again
    db_session.commit()
    rows = db_session.query(OrganizationMembership).filter_by(
        user_id=homed_user.id, organization_id=three_orgs[1].id
    ).count()
    assert rows == 1


def test_switching_home_org_moves_the_default_flag(db_session, homed_user, three_orgs):
    backfill_memberships(db_session)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)
    db_session.commit()

    # promote Org Bravo to home
    add_user_to_org(db_session, homed_user, three_orgs[1].id, is_default=True)
    db_session.commit()
    db_session.refresh(homed_user)

    assert homed_user.organization_id == three_orgs[1].id
    defaults = db_session.query(OrganizationMembership).filter_by(
        user_id=homed_user.id, is_default=True
    ).all()
    assert len(defaults) == 1
    assert defaults[0].organization_id == three_orgs[1].id


def test_membership_uniqueness_is_enforced(db_session, homed_user, three_orgs):
    from sqlalchemy.exc import IntegrityError
    db_session.add(OrganizationMembership(user_id=homed_user.id, organization_id=three_orgs[0].id))
    db_session.commit()
    db_session.add(OrganizationMembership(user_id=homed_user.id, organization_id=three_orgs[0].id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_user_can_access_respects_membership(db_session, homed_user, three_orgs):
    backfill_memberships(db_session)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)
    db_session.commit()
    assert user_can_access(db_session, homed_user.id, three_orgs[0].id)  # home
    assert user_can_access(db_session, homed_user.id, three_orgs[1].id)  # member
    assert not user_can_access(db_session, homed_user.id, three_orgs[2].id)  # not a member


# ──────────────────────────────────────────────────────────────────────────
# 2. Dashboard routes (run in the app container)
# ──────────────────────────────────────────────────────────────────────────

def test_switch_org_allows_a_member(db_session, homed_user, three_orgs):
    import asyncio
    from web.main import switch_org

    backfill_memberships(db_session)
    add_user_to_org(db_session, homed_user, three_orgs[1].id)
    db_session.commit()

    req = _fake_request(three_orgs[0].id, user_id=homed_user.id)
    resp = asyncio.run(switch_org(req, three_orgs[1].id, db=db_session))

    assert resp.status_code == 303
    assert req.session["organization_id"] == three_orgs[1].id


def test_switch_org_denies_a_non_member(db_session, homed_user, three_orgs):
    import asyncio
    from web.main import switch_org

    backfill_memberships(db_session)  # only home org (Alpha)
    db_session.commit()

    req = _fake_request(three_orgs[0].id, user_id=homed_user.id)
    resp = asyncio.run(switch_org(req, three_orgs[2].id, db=db_session))  # Charlie: not a member

    assert resp.status_code == 303
    assert "switch_error=not_member" in resp.headers["location"]
    # active org must NOT change
    assert req.session["organization_id"] == three_orgs[0].id


def test_org_create_with_existing_email_adds_membership_no_400(db_session, homed_user, three_orgs):
    """Creating a new org with an admin email that already exists must attach the
    existing account to the new org (302 member_added) — not return a 400."""
    import asyncio
    from web.main import superadmin_organizations_create

    # an "Organisation Admin" role exists in a seeded DB; create one for the test
    db_session.add(Role(name="Organisation Admin", description="org admin"))
    db_session.commit()

    req = _fake_request(three_orgs[0].id, user_role="superadmin", user_id=homed_user.id,
                        email="super@astradigital.com.au")
    resp = asyncio.run(superadmin_organizations_create(
        req, name="Brand New Org", tier="GOLD",
        admin_name="Multi User", admin_email=homed_user.email, db=db_session,
    ))

    assert resp.status_code == 302
    assert "member_added" in resp.headers["location"]

    new_org = db_session.query(Organization).filter_by(name="Brand New Org").first()
    assert new_org is not None
    db_session.refresh(homed_user)
    assert homed_user.is_member_of(new_org.id), "existing user must now be a member of the new org"


def test_superadmin_user_create_existing_email_adds_membership(db_session, homed_user, three_orgs):
    import asyncio
    from web.main import superadmin_users_create

    role = Role(name="Trader", description="trader")
    db_session.add(role)
    db_session.commit()

    req = _fake_request(three_orgs[0].id, user_role="superadmin", user_id=homed_user.id,
                        email="super@astradigital.com.au")
    resp = asyncio.run(superadmin_users_create(
        req, name="Multi User", email=homed_user.email,
        organization_id=three_orgs[1].id, role_id=role.id, send_welcome=None, db=db_session,
    ))

    assert resp.status_code == 302
    assert "member_added" in resp.headers["location"]
    db_session.refresh(homed_user)
    assert homed_user.is_member_of(three_orgs[1].id)
