from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from .ingest.vcard import RESERVED, USERNAME_RE


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    profile: str = "household"
    avatar_id: Optional[str] = None
    notes: Optional[str] = None
    #: Required when profile is privileged — makes granting fleet-admin an
    #: explicit act rather than a checkbox someone clicks past.
    confirm_privileged: bool = False

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        v = v.strip().lower()
        if not USERNAME_RE.match(v):
            raise ValueError(
                "Username must start with a letter and contain only lowercase "
                "letters, digits, dot, underscore or hyphen (2–32 characters)."
            )
        if v in RESERVED:
            raise ValueError(f"{v!r} is reserved and cannot be used as an account name.")
        return v

    @field_validator("first_name", "last_name", "display_name")
    @classmethod
    def _clean(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from .ingest.vcard import clean

        out = clean(v)
        if not out:
            raise ValueError("Cannot be blank once control characters are removed.")
        return out

    @field_validator("profile")
    @classmethod
    def _profile(cls, v: str) -> str:
        from .models import PROFILES

        if v not in PROFILES:
            raise ValueError(f"Unknown profile {v!r}. Choose one of: {', '.join(PROFILES)}")
        return v


class RunCreate(BaseModel):
    kind: str = "provision"

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("provision", "update", "disable", "enable"):
            raise ValueError("kind must be provision, update, disable or enable")
        return v


class VCardCommitItem(BaseModel):
    index: int
    username: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: Optional[str] = None
    profile: str = "household"


class VCardCommit(BaseModel):
    items: list[VCardCommitItem]


class ConnectorPatch(BaseModel):
    enabled: Optional[bool] = None
    required: Optional[bool] = None


def user_out(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "display_name": user.display_name,
        "profile": user.profile,
        "status": user.status,
        "source": user.source,
        "lldap_uid": user.lldap_uid,
        "avatar_id": user.avatar_sha256,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "services": [
            {
                "connector": a.connector,
                "tier": a.tier,
                "state": a.state,
                "external_id": a.external_id,
                "manual_instruction": a.manual_instruction,
                "last_error": a.last_error,
            }
            for a in sorted(user.accounts, key=lambda a: a.connector)
        ],
    }
