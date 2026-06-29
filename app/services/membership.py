"""
Organization membership service.

Pure SQLAlchemy helpers for the multi-org model: a single user can belong to
several organizations and switch the active one. These functions are deliberately
free of any network / framework dependency so they can be unit-tested directly
against an in-memory SQLite database (see tests/test_multi_org_membership.py).

Key concepts
------------
* `users.organization_id`  -> the user's "home" org (kept for backward compat).
* `organization_memberships` -> every org the user can access, one row each.
  The home org always has a membership row with `is_default=True`.

`add_user_to_org` is idempotent: calling it twice for the same (user, org) will
not create a duplicate membership (also guarded by a UNIQUE constraint).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.auth import User, OrganizationMembership, Role
from app.models.account import Organization


def add_user_to_org(
    db: Session,
    user: User,
    organization_id: int,
    role: Optional[Role] = None,
    is_default: bool = False,
) -> OrganizationMembership:
    """
    Ensure `user` is a member of `organization_id`. Returns the membership row
    (existing or newly created). Idempotent — safe to call repeatedly.

    The membership is appended through the `user.memberships` relationship so the
    in-session object stays consistent (``user.organization_ids`` / ``is_member_of``
    reflect the change immediately, not just after a reload).

    If `is_default` is True this becomes the user's home org: `users.organization_id`
    is updated and any other membership's `is_default` flag is cleared.
    """
    existing = next(
        (m for m in user.memberships if m.organization_id == organization_id),
        None,
    )

    if existing is None:
        existing = OrganizationMembership(
            organization_id=organization_id,
            role_id=role.id if role else None,
            is_default=is_default,
        )
        user.memberships.append(existing)
        db.flush()
    else:
        if role is not None and existing.role_id is None:
            existing.role_id = role.id
        if is_default:
            existing.is_default = True

    if is_default:
        # Exactly one home org per user.
        for m in user.memberships:
            m.is_default = (m.organization_id == organization_id)
        user.organization_id = organization_id
        db.flush()

    return existing


def switchable_orgs(db: Session, user_id: int) -> list[dict]:
    """
    Return the organizations a user may switch to, as a list of
    ``{"id", "name", "is_default"}`` dicts ordered by name. Includes the home org.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return []

    org_ids = set(user.organization_ids)
    if not org_ids:
        return []

    default_id = user.organization_id
    orgs = (
        db.query(Organization)
        .filter(Organization.id.in_(org_ids), Organization.is_active == True)  # noqa: E712
        .order_by(Organization.name)
        .all()
    )
    return [
        {"id": o.id, "name": o.name, "is_default": (o.id == default_id)}
        for o in orgs
    ]


def user_can_access(db: Session, user_id: int, organization_id: int) -> bool:
    """True if the user is a member of (or homed at) the organization."""
    user = db.query(User).filter(User.id == user_id).first()
    return bool(user and user.is_member_of(organization_id))


def backfill_memberships(db: Session) -> int:
    """
    Create a default membership row for every user that has a `organization_id`
    but no corresponding membership yet. Returns the number of rows created.

    Safe to run repeatedly (idempotent) — used by the SaaS migration so existing
    single-org users transparently gain a membership for their current org.
    """
    created = 0
    users = db.query(User).all()
    for u in users:
        if u.organization_id is None:
            continue
        if not any(m.organization_id == u.organization_id for m in u.memberships):
            u.memberships.append(
                OrganizationMembership(organization_id=u.organization_id, is_default=True)
            )
            created += 1
    if created:
        db.flush()
    return created
