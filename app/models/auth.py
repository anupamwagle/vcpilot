"""
Authentication models: User, Role, Permission and associations.
Also contains helper utilities for secure password hashing and verification.
"""
import enum
import hashlib
import os
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


# Password Hashing Helpers
def hash_password(password: str) -> str:
    """Hash password using secure PBKDF2 algorithm with salt."""
    salt = os.urandom(16)
    # Use PBKDF2 with SHA-256 and 100,000 iterations
    hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return f"{salt.hex()}:{hash_bytes.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verify standard text password against its stored hash."""
    if not hashed or ":" not in hashed:
        return False
    try:
        salt_hex, hash_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        hash_bytes = bytes.fromhex(hash_hex)
        check_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return check_bytes == hash_bytes
    except Exception:
        return False


# Many-to-Many Association Tables
role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(Base):
    """
    Permissions represent capabilities (e.g. view_trading, trade, view_admin, manage_config).
    """
    __tablename__ = "permissions"

    id          = Column(Integer, primary_key=True)
    name        = Column(String(64), unique=True, nullable=False)
    description = Column(String(256), nullable=True)

    roles = relationship("Role", secondary=role_permissions, back_populates="permissions")

    def __repr__(self):
        return f"<Permission {self.name}>"


class Role(Base):
    """
    Roles bundle permissions together (e.g. Organisation Admin, Trader, Viewer).
    """
    __tablename__ = "roles"

    id          = Column(Integer, primary_key=True)
    name        = Column(String(64), unique=True, nullable=False)
    description = Column(String(256), nullable=True)

    permissions = relationship("Permission", secondary=role_permissions, back_populates="roles")
    users       = relationship("User", secondary=user_roles, back_populates="roles")

    def __repr__(self):
        return f"<Role {self.name}>"


class User(Base):
    """
    User account belonging to an organization.
    """
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True)
    email           = Column(String(128), unique=True, nullable=False, index=True)
    password_hash   = Column(String(256), nullable=False)
    name            = Column(String(128), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    is_active       = Column(Boolean, default=True, nullable=False)

    # OTP login support
    otp_code        = Column(String(32), nullable=True)
    otp_expires_at  = Column(DateTime, nullable=True)

    # Password reset support
    reset_token         = Column(String(128), nullable=True)
    reset_token_expires = Column(DateTime, nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")
    roles        = relationship("Role", secondary=user_roles, back_populates="users")

    # Multi-org: a user can belong to several organizations and switch between them.
    # `organization_id` above is retained as the user's "home" org (first/default
    # membership) for backward compatibility with code that reads it directly.
    memberships  = relationship(
        "OrganizationMembership",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="OrganizationMembership.user_id",
    )

    def has_permission(self, perm_name: str) -> bool:
        """Check if user has a permission through any of their roles."""
        for role in self.roles:
            for perm in role.permissions:
                if perm.name == perm_name:
                    return True
        return False

    @property
    def organization_ids(self) -> list[int]:
        """All organization ids this user is a member of (de-duplicated)."""
        ids = {m.organization_id for m in self.memberships if m.organization_id is not None}
        if self.organization_id is not None:
            ids.add(self.organization_id)  # home org is always accessible
        return sorted(ids)

    @property
    def all_organizations(self) -> list:
        """All Organization objects this user is associated with (de-duplicated)."""
        orgs = {}
        if self.organization:
            orgs[self.organization.id] = self.organization
        for m in self.memberships:
            if m.organization:
                orgs[m.organization.id] = m.organization
        return list(orgs.values())

    def is_member_of(self, organization_id: int) -> bool:
        """True if the user belongs to (or is homed at) the given organization."""
        if organization_id is None:
            return False
        if organization_id == self.organization_id:
            return True
        return any(m.organization_id == organization_id for m in self.memberships)

    def __repr__(self):
        return f"<User {self.email}>"


class OrganizationMembership(Base):
    """
    Many-to-many link between a user and an organization. Lets one user belong to
    several organizations and switch the active one. Optionally records the role the
    user holds *within* that organization (per-org role; global `user_roles` still
    drives permission checks for now).
    """
    __tablename__ = "organization_memberships"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id         = Column(Integer, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True)
    is_default      = Column(Boolean, default=False, nullable=False)  # the user's home org
    created_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_user_org_membership"),
    )

    user         = relationship("User", back_populates="memberships", foreign_keys=[user_id])
    organization = relationship("Organization")
    role         = relationship("Role")

    def __repr__(self):
        return f"<OrganizationMembership user={self.user_id} org={self.organization_id}>"
