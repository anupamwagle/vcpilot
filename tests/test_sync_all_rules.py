"""
Regression test for the sync-all rule-backfill bug:

`POST /superadmin/rules/sync-all` only updated org RuleConfig rows that
already existed for a given rule_id. If a global rule was added *after*
an org already existed (e.g. entry_min_share_price / entry_max_share_price),
that org's RuleConfig table never got a row for it — so the rule showed up
on /superadmin/rules (global) but never appeared on the org's own
/admin/rules page, with no toggle, no threshold control, nothing.

Fix: sync-all now also creates a cloned row for any org missing a rule_id
that exists globally, using the same field set as the org-creation clone.
"""
import asyncio
from types import SimpleNamespace

import pytest

from dashboard.main import superadmin_rules_sync_all


def _mock_request(session=None, query_params=None):
    req = SimpleNamespace()
    req.session = session if session is not None else {"authenticated": True, "user_role": "superadmin"}
    req.query_params = query_params if query_params is not None else {}
    return req


def test_sync_all_backfills_missing_org_rule_row(db_session, org_and_account):
    """A global rule with no corresponding org row gets created, not silently skipped."""
    from app.models.config import RuleConfig

    org, _ = org_and_account

    # Simulate the real-world scenario: a brand-new global rule that was added
    # after this org already existed. No org-scoped row exists for it yet.
    db_session.add(RuleConfig(
        rule_id="entry_min_share_price",
        organization_id=None,
        category="ENTRY",
        label="Minimum share price",
        description="Skip equities trading below this price.",
        enabled_globally=False,
        threshold=0.10,
        threshold_label="Minimum share price (AUD/USD)",
        threshold_min=0.0,
        threshold_max=50.0,
        is_mandatory=False,
        sort_order=52,
        asset_types="EQUITY",
    ))
    db_session.commit()

    # Precondition: confirm the org has no row for this rule yet (the bug state).
    org_row = db_session.query(RuleConfig).filter(
        RuleConfig.rule_id == "entry_min_share_price",
        RuleConfig.organization_id == org.id,
    ).first()
    assert org_row is None

    req = _mock_request()
    res = asyncio.run(superadmin_rules_sync_all(request=req, db=db_session))

    assert res.status_code == 302
    assert "created=1" in res.headers["location"]

    # Postcondition: the org now has its own cloned row, matching the global template.
    org_row = db_session.query(RuleConfig).filter(
        RuleConfig.rule_id == "entry_min_share_price",
        RuleConfig.organization_id == org.id,
    ).first()
    assert org_row is not None
    assert org_row.category == "ENTRY"
    assert float(org_row.threshold) == pytest.approx(0.10)
    assert org_row.asset_types == "EQUITY"
    assert org_row.updated_by == "superadmin:sync"


def test_sync_all_does_not_duplicate_existing_org_rows(db_session, org_and_account):
    """An org that already has a row for a rule_id gets updated in place, not duplicated."""
    from app.models.config import RuleConfig

    org, _ = org_and_account

    db_session.add(RuleConfig(
        rule_id="entry_max_share_price", organization_id=None, category="ENTRY",
        label="Maximum share price", enabled_globally=True, threshold=2.00,
        is_mandatory=False, sort_order=53, asset_types="EQUITY",
    ))
    db_session.add(RuleConfig(
        rule_id="entry_max_share_price", organization_id=org.id, category="ENTRY",
        label="Maximum share price", enabled_globally=False, threshold=1.00,
        is_mandatory=False, sort_order=53, asset_types="EQUITY",
        updated_by="migration",
    ))
    db_session.commit()

    req = _mock_request()
    res = asyncio.run(superadmin_rules_sync_all(request=req, db=db_session))

    assert res.status_code == 302
    assert "created=0" in res.headers["location"]
    assert "synced=1" in res.headers["location"]

    rows = db_session.query(RuleConfig).filter(
        RuleConfig.rule_id == "entry_max_share_price",
        RuleConfig.organization_id == org.id,
    ).all()
    assert len(rows) == 1
    assert float(rows[0].threshold) == pytest.approx(2.00)
    assert rows[0].enabled_globally is True


def test_sync_all_requires_superadmin(db_session, org_and_account):
    req = _mock_request(session={"authenticated": True, "user_role": "user"})
    res = asyncio.run(superadmin_rules_sync_all(request=req, db=db_session))
    assert res.status_code == 302
    assert res.headers["location"] == "/"
