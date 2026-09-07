"""SQLAlchemy models — a trimmed version of docs/03-data-model.md.

Reminder of the invariant that shapes this schema: **lldap is the source of
truth for identity; this database is the source of truth for provisioning
state.**  There is deliberately no password column anywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- vocabularies (plain strings; portable across sqlite and postgres) ------

USER_STATUS = ("draft", "provisioning", "active", "partial", "disabled", "failed")
ACCOUNT_STATE = (
    "not_started",
    "not_applicable",
    "pending_first_login",
    "provisioned",
    "needs_manual",
    "failed",
    "disabled",
    "drifted",
)
RUN_STATUS = (
    "planning",
    "awaiting_confirmation",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
)
JOB_STATUS = ("queued", "leased", "succeeded", "failed", "dead_letter", "skipped", "needs_manual")

#: D8 — least-privilege by default.  The safe path is the default path.
#: This build covers user-facing apps only; system-maintenance surfaces
#: (Komodo, Scrutiny, Diun, NetAlertX, Speedtest, Headscale, n8n) are out of
#: scope per the 2026-09-06 instruction and are not registered at all.
PROFILES: dict[str, dict[str, Any]] = {
    "household": {
        "description": "Day-to-day household member: media, photos, documents, tasks, passwords.",
        "lldap_groups": ["purrbrews_users"],
        "is_privileged": False,
        "connectors": [
            "lldap",
            "vaultwarden",
            "immich",
            "nextcloud",
            "homeassistant",
            "vikunja",
            "actual",
            "jellyfin",
            "mealie",
            "paperless",
            "roundcube",
        ],
    },
    "infra_admin": {
        "description": "Fleet operator. Everything in household, plus the admin group.",
        "lldap_groups": ["purrbrews_users", "purrbrews_infra_admins"],
        "is_privileged": True,
        "connectors": [
            "lldap",
            "vaultwarden",
            "immich",
            "nextcloud",
            "homeassistant",
            "vikunja",
            "actual",
            "jellyfin",
            "mealie",
            "paperless",
            "roundcube",
        ],
    },
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lldap_uid: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    avatar_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(32), default="household")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    source_ref: Mapped[Optional[str]] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    accounts: Mapped[list["ServiceAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class ServiceAccount(Base):
    __tablename__ = "service_accounts"
    __table_args__ = (UniqueConstraint("user_id", "connector"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connector: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), default="not_started")
    external_id: Mapped[Optional[str]] = mapped_column(String(128))
    external_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    manual_instruction: Mapped[Optional[str]] = mapped_column(Text)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="accounts")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="planning", index=True)
    # Frozen at confirm time.  The run executes the plan that was approved,
    # never a silently different one.
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    connector: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[Optional[str]] = mapped_column(String(64))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Avatar(Base):
    __tablename__ = "avatars"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    mime: Mapped[str] = mapped_column(String(32), default="image/jpeg")
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    bytes_: Mapped[bytes] = mapped_column("bytes", LargeBinary)
    original_mime: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only.  In production, REVOKE UPDATE/DELETE from the app role."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    actor_groups: Mapped[Optional[str]] = mapped_column(String(512))
    actor_ip: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_user: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    connector: Mapped[Optional[str]] = mapped_column(String(32))
    run_id: Mapped[Optional[str]] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class VCardImport(Base):
    __tablename__ = "vcard_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(256))
    uploaded_by: Mapped[str] = mapped_column(String(64))
    card_count: Mapped[int] = mapped_column(Integer)
    staged: Mapped[list] = mapped_column(JSON, default=list)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
