"""
Tests for the ibkr_account one-org-per-account guard on POST
/admin/config/{config_id}/update (I2 / CLAUDE.md #41).

Without this guard, two orgs could save the same ibkr_account and both submit
orders to / reconcile against the same real IBKR account — double entries,
cross-org closes.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.config import SystemConfig


def _fake_request(org_id, email="admin@astradigital.com.au", user_id=1, user_role=None):
    return SimpleNamespace(session={
        "authenticated": True,
        "organization_id": org_id,
        "email": email,
        "user_id": user_id,
        "user_role": user_role,
    })


def _make_other_org(db):
    from app.models.account import Organization, OrganizationTier
    other_org = Organization(name="Other Org", tier=OrganizationTier.GOLD, is_active=True)
    db.add(other_org)
    db.flush()
    return other_org


def test_update_config_rejects_duplicate_ibkr_account(db_session, org_and_account):
    from web.main import update_config

    org, _account = org_and_account
    other_org = _make_other_org(db_session)
    db_session.add(SystemConfig(key="ibkr_account", value="DU999999", organization_id=other_org.id,
                                label="IBKR Account ID", group="broker"))
    cfg = SystemConfig(key="ibkr_account", value="", organization_id=org.id,
                       label="IBKR Account ID", group="broker")
    db_session.add(cfg)
    db_session.commit()

    response = asyncio.run(update_config(_fake_request(org.id), cfg.id, value="DU999999", db=db_session))

    assert response.status_code == 302
    assert "already%20in%20use" in response.headers["location"]

    db_session.refresh(cfg)
    assert cfg.value == "", "Must NOT save the duplicate value"


def test_update_config_rejects_duplicate_case_and_whitespace_insensitive(db_session, org_and_account):
    """The duplicate check must not be defeated by case or surrounding whitespace."""
    from web.main import update_config

    org, _account = org_and_account
    other_org = _make_other_org(db_session)
    db_session.add(SystemConfig(key="ibkr_account", value="du999999", organization_id=other_org.id,
                                label="IBKR Account ID", group="broker"))
    cfg = SystemConfig(key="ibkr_account", value="", organization_id=org.id,
                       label="IBKR Account ID", group="broker")
    db_session.add(cfg)
    db_session.commit()

    response = asyncio.run(update_config(_fake_request(org.id), cfg.id, value="  DU999999  ", db=db_session))

    assert response.status_code == 302
    assert "already%20in%20use" in response.headers["location"]
    db_session.refresh(cfg)
    assert cfg.value == ""


def test_update_config_allows_unique_ibkr_account(db_session, org_and_account):
    from web.main import update_config

    org, _account = org_and_account
    other_org = _make_other_org(db_session)
    db_session.add(SystemConfig(key="ibkr_account", value="DU999999", organization_id=other_org.id,
                                label="IBKR Account ID", group="broker"))
    cfg = SystemConfig(key="ibkr_account", value="", organization_id=org.id,
                       label="IBKR Account ID", group="broker")
    db_session.add(cfg)
    db_session.commit()

    response = asyncio.run(update_config(_fake_request(org.id), cfg.id, value="DU111111", db=db_session))

    assert response.status_code == 302
    assert "saved" in response.headers["location"]
    db_session.refresh(cfg)
    assert cfg.value == "DU111111"


def test_update_config_allows_org_to_keep_its_own_existing_value(db_session, org_and_account):
    """Re-saving the same value an org already owns must not trip the duplicate check."""
    from web.main import update_config

    org, _account = org_and_account
    cfg = SystemConfig(key="ibkr_account", value="DU123456", organization_id=org.id,
                       label="IBKR Account ID", group="broker")
    db_session.add(cfg)
    db_session.commit()

    response = asyncio.run(update_config(_fake_request(org.id), cfg.id, value="DU123456", db=db_session))

    assert response.status_code == 302
    assert "saved" in response.headers["location"]
