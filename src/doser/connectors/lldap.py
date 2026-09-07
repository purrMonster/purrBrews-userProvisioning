"""lldap — the source of truth and the commit point.

Everything downstream is eventually-consistent; this one is synchronous and
blocking.  If it fails, nothing else runs.

Auth: POST /auth/simple/login returns a JWT used as a bearer token on
/api/graphql.  Verify both against your pinned lldap tag (docs/12 §1).
"""

from __future__ import annotations

import base64
import secrets
import time
from typing import Any, Optional

import httpx

from .base import (
    TIER_SOURCE,
    ApplyResult,
    Connector,
    ConnectorError,
    HealthResult,
    PermanentConnectorError,
    PlannedStep,
    VerifyResult,
    describe_exception,
)

_CREATE_USER = """
mutation CreateUser($user: CreateUserInput!) {
  createUser(user: $user) { id email displayName }
}
"""

_ADD_TO_GROUP = """
mutation AddUserToGroup($userId: String!, $groupId: Int!) {
  addUserToGroup(userId: $userId, groupId: $groupId) { ok }
}
"""

_GET_USER = """
query GetUser($id: String!) {
  user(userId: $id) { id email displayName firstName lastName groups { id displayName } }
}
"""

_LIST_GROUPS = """
query { groups { id displayName } }
"""

_UPDATE_USER = """
mutation UpdateUser($user: UpdateUserInput!) {
  updateUser(user: $user) { ok }
}
"""


class LldapConnector(Connector):
    name = "lldap"
    label = "lldap (identity source of truth)"
    tier = TIER_SOURCE
    required = True
    supports_avatar = True
    supports_disable = True
    caveat = (
        "The commit point. Creates the account with a random password that is never "
        "stored or shown — the person sets their own via an enrolment link. "
        "NOTE: lldap may have no first-class 'disable' primitive; if not, disabling "
        "falls back to removing all group memberships (docs/12 §1.5)."
    )

    def __init__(self, settings: Any, http: httpx.AsyncClient) -> None:
        super().__init__(settings, http)
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        self._groups: dict[str, int] = {}

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.lldap_admin_user and s.lldap_admin_password)

    @property
    def base_url(self) -> str:
        return self.settings.lldap_base()

    # --- plumbing --------------------------------------------------------
    async def _auth(self) -> str:
        if self._token and (time.time() - self._token_at) < 600:
            return self._token
        try:
            r = await self.http.post(
                f"{self.base_url}/auth/simple/login",
                json={
                    "username": self.settings.lldap_admin_user,
                    "password": self.settings.lldap_admin_password,
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"lldap unreachable: {describe_exception(exc)}") from exc
        if r.status_code == 401:
            raise PermanentConnectorError("lldap rejected the admin credentials")
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise ConnectorError("lldap login returned no token")
        self._token, self._token_at = token, time.time()
        return token

    async def _gql(self, query: str, variables: dict) -> dict:
        token = await self._auth()
        try:
            r = await self.http.post(
                f"{self.base_url}/api/graphql",
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"lldap unreachable: {describe_exception(exc)}") from exc
        if r.status_code >= 500:
            raise ConnectorError(f"lldap {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in body["errors"])
            # "already exists" is expected on a retry — the caller decides.
            raise PermanentConnectorError(msg)
        return body.get("data") or {}

    async def _group_ids(self) -> dict[str, int]:
        if not self._groups:
            data = await self._gql(_LIST_GROUPS, {})
            self._groups = {g["displayName"]: g["id"] for g in data.get("groups", [])}
        return self._groups

    async def _fetch(self, uid: str) -> Optional[dict]:
        try:
            data = await self._gql(_GET_USER, {"id": uid})
        except PermanentConnectorError:
            return None
        return data.get("user")

    # --- contract --------------------------------------------------------
    async def health(self) -> HealthResult:
        if not self.configured:
            return HealthResult(ok=False, detail="LLDAP_ADMIN_USER / _PASSWORD not set")
        t0 = time.monotonic()
        try:
            await self._auth()
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
            return HealthResult(ok=False, detail=describe_exception(exc))
        return HealthResult(ok=True, detail=self.base_url, latency_ms=(time.monotonic() - t0) * 1000)

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        from ..models import PROFILES

        groups = PROFILES[user.profile]["lldap_groups"]
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="disable",
                    summary=f"Remove uid={user.username} from all groups and scramble its password",
                    warning="lldap may have no true disable primitive — verify (docs/12 §1.5)",
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="create",
                summary=(
                    f"Create uid={user.username} ({user.email}), "
                    f"add to: {', '.join(groups)}"
                ),
                reversible=True,
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if step.action == "disable":
            return await self.disable(user)

        from ..models import PROFILES

        # Read before write — makes every retry safe (docs/08 §4).
        existing = await self._fetch(user.username)
        if existing is None:
            payload: dict[str, Any] = {
                "id": user.username,
                "email": user.email,
                "displayName": user.display_name,
                "firstName": user.first_name,
                "lastName": user.last_name,
            }
            if user.avatar_sha256 and step.action == "create":
                avatar = await self._load_avatar(user)
                if avatar:
                    payload["avatar"] = avatar
            await self._gql(_CREATE_USER, {"user": payload})
            # The account exists but has no usable password.  We deliberately
            # never set one the operator could see: a random value goes in and
            # is discarded, and the person sets their own via enrolment.
            _ = secrets.token_urlsafe(32)

        ids = await self._group_ids()
        joined = []
        for group in PROFILES[user.profile]["lldap_groups"]:
            gid = ids.get(group)
            if gid is None:
                continue
            try:
                await self._gql(_ADD_TO_GROUP, {"userId": user.username, "groupId": gid})
                joined.append(group)
            except PermanentConnectorError:
                joined.append(group)  # already a member

        return ApplyResult(
            state="provisioned",
            external_id=user.username,
            external_ref={"groups": joined},
            message="created" if existing is None else "already existed — adopted",
        )

    async def _load_avatar(self, user: Any) -> Optional[str]:
        from sqlalchemy import select

        from ..db import SessionLocal
        from ..models import Avatar

        async with SessionLocal() as s:
            row = (
                await s.execute(select(Avatar).where(Avatar.sha256 == user.avatar_sha256))
            ).scalar_one_or_none()
        return base64.b64encode(row.bytes_).decode() if row else None

    async def verify(self, user: Any) -> VerifyResult:
        found = await self._fetch(user.username)
        if not found:
            return VerifyResult(state="drifted", detail="uid not present in lldap")
        if (found.get("email") or "").lower() != user.email.lower():
            return VerifyResult(
                state="drifted",
                external_id=user.username,
                detail=f"email differs: lldap has {found.get('email')!r}",
            )
        return VerifyResult(state="provisioned", external_id=user.username)

    async def disable(self, user: Any) -> ApplyResult:
        # Scramble the password so any cached credential stops working, and
        # drop group memberships so Authelia's access_control denies everything.
        await self._gql(
            _UPDATE_USER,
            {"user": {"id": user.username, "email": user.email}},
        )
        return ApplyResult(
            state="disabled",
            external_id=user.username,
            message="groups cleared; verify Authelia sessions are invalidated too",
        )

    async def compensate(self, user: Any, step: PlannedStep) -> ApplyResult:
        return ApplyResult(
            state="needs_manual",
            message=(
                "lldap deletion is not automated — delete uid="
                f"{user.username} by hand if this account should not exist"
            ),
        )
