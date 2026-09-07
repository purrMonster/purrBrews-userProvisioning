"""Nextcloud — two modes, and mixing them is the classic mistake.

``nextcloud_mode = "ldap"``  (default, recommended)
    The ``user_ldap`` app binds to lldap and the account appears on first login.
    Nothing to call.  Group mapping comes free.

``nextcloud_mode = "ocs"``
    Pre-create through the OCS Provisioning API.  Only correct when
    ``user_ldap`` is NOT enabled — otherwise the local account shadows the LDAP
    one and the person ends up with two homes and one set of files they cannot
    find.

The connector refuses to guess: the mode is explicit configuration.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from .base import (
    TIER_API,
    TIER_LDAP,
    ApplyResult,
    Connector,
    ConnectorError,
    HealthResult,
    JitConnector,
    PermanentConnectorError,
    PlannedStep,
    VerifyResult,
    describe_exception,
)

_OCS = {"OCS-APIRequest": "true", "Accept": "application/json"}


class NextcloudConnector(JitConnector):
    name = "nextcloud"
    label = "Nextcloud (files)"
    required = True
    supports_disable = True
    mechanism = "LDAP"

    def __init__(self, settings: Any, http: httpx.AsyncClient) -> None:
        super().__init__(settings, http)
        self.ocs_mode = settings.nextcloud_mode == "ocs"
        self.tier = TIER_API if self.ocs_mode else TIER_LDAP
        self.caveat = (
            "Mode 'ocs': pre-creates via the Provisioning API. Only safe when user_ldap is "
            "DISABLED — otherwise the local account shadows the LDAP one."
            if self.ocs_mode
            else "Mode 'ldap': binds to lldap, account appears on first login. Nothing is called "
            "to create it. Verify the jpegPhoto mapping if you want avatars to flow (docs/12 "
            "§4.4). Set DOSER_NEXTCLOUD_ADMIN_USER/_PASSWORD to let doser confirm the login "
            "automatically via the read-only Provisioning API — it is never used to create "
            "or modify anything while nextcloud_mode=ldap."
        )

    @property
    def configured(self) -> bool:
        if not self.ocs_mode:
            return True  # nothing needed for the LDAP path
        return bool(self.settings.nextcloud_admin_user and self.settings.nextcloud_admin_password)

    @property
    def base_url(self) -> str:
        return self.settings.service_url("nextcloud_url", "nextcloud")

    def _auth(self) -> Optional[tuple[str, str]]:
        s = self.settings
        if s.nextcloud_admin_user and s.nextcloud_admin_password:
            return (s.nextcloud_admin_user, s.nextcloud_admin_password)
        return None

    async def _ocs(self, method: str, path: str, **kw: Any) -> dict:
        try:
            r = await self.http.request(
                method, f"{self.base_url}{path}", headers=_OCS, auth=self._auth(), **kw
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"nextcloud unreachable: {describe_exception(exc)}") from exc
        if r.status_code in (401, 403):
            raise PermanentConnectorError("nextcloud rejected the admin credentials")
        if r.status_code >= 500:
            raise ConnectorError(f"nextcloud {r.status_code}")
        try:
            return (r.json() or {}).get("ocs", {})
        except ValueError as exc:
            raise ConnectorError(f"nextcloud returned non-JSON: {r.text[:120]}") from exc

    async def health(self) -> HealthResult:
        t0 = time.monotonic()
        try:
            r = await self.http.get(f"{self.base_url}/status.php")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, detail=describe_exception(exc))
        return HealthResult(
            ok=r.status_code < 400,
            detail=f"{self.base_url} → {r.status_code}"
            + ("" if self.ocs_mode else " (LDAP mode — no writes)"),
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        if not self.ocs_mode:
            return await super().plan(user, action)
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="disable",
                    summary=f"PUT /ocs .../users/{user.username}/disable",
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="create",
                summary=(
                    f"POST /ocs/v2.php/cloud/users — {user.username}, "
                    f"quota {self.settings.nextcloud_quota}"
                ),
                warning="Only correct if user_ldap is disabled, or you will shadow the LDAP account",
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if not self.ocs_mode:
            return await super().apply(user, step)
        if step.action == "disable":
            return await self.disable(user)

        found = await self._get(user.username)
        if found:
            return ApplyResult(
                state="provisioned", external_id=user.username, message="already existed — adopted"
            )

        ocs = await self._ocs(
            "POST",
            "/ocs/v2.php/cloud/users",
            data={
                "userid": user.username,
                "displayName": user.display_name,
                "email": user.email,
                "quota": self.settings.nextcloud_quota,
                "groups[]": "purrbrews_users",
            },
        )
        code = ocs.get("meta", {}).get("statuscode")
        if code not in (100, 200):
            # 102 = username already exists; treat as adopted, not a failure.
            if code == 102:
                return ApplyResult(
                    state="provisioned", external_id=user.username, message="already existed"
                )
            raise PermanentConnectorError(
                f"nextcloud create failed: {ocs.get('meta', {}).get('message')}"
            )
        return ApplyResult(state="provisioned", external_id=user.username, message="created")

    async def _get(self, uid: str) -> Optional[dict]:
        # The OCS read is safe regardless of mode — an LDAP-backed account shows
        # up here too once Nextcloud has provisioned it internally on first
        # bind. Only writes are gated on ocs_mode.
        if self._auth() is None:
            return None
        ocs = await self._ocs("GET", f"/ocs/v2.php/cloud/users/{uid}")
        if ocs.get("meta", {}).get("statuscode") in (100, 200):
            return ocs.get("data")
        return None

    async def verify(self, user: Any) -> VerifyResult:
        if not self.ocs_mode:
            if self._auth() is None:
                # No credentials configured, so we cannot probe. Honest
                # answer: we do not know until they log in.
                return VerifyResult(
                    state="pending_first_login",
                    detail=(
                        "LDAP mode — set DOSER_NEXTCLOUD_ADMIN_USER/_PASSWORD to confirm "
                        "automatically"
                    ),
                )
            try:
                found = await self._get(user.username)
            except ConnectorError as exc:
                # Wrong admin creds or Nextcloud unreachable — unknown, not
                # absent. Never let a probe failure read as "not provisioned".
                return VerifyResult(
                    state="pending_first_login", detail=f"could not confirm via Nextcloud: {exc}"
                )
            if found:
                return VerifyResult(state="provisioned", external_id=user.username)
            return VerifyResult(
                state="pending_first_login", detail="not found yet — awaiting first LDAP login"
            )
        try:
            found = await self._get(user.username)
        except ConnectorError as exc:
            return VerifyResult(
                state="pending_first_login", detail=f"could not confirm via Nextcloud: {exc}"
            )
        if not found:
            return VerifyResult(state="drifted", detail="account not present")
        return VerifyResult(state="provisioned", external_id=user.username)

    async def disable(self, user: Any) -> ApplyResult:
        if not self.ocs_mode:
            return ApplyResult(state="disabled", message="blocked upstream at lldap")
        await self._ocs("PUT", f"/ocs/v2.php/cloud/users/{user.username}/disable")
        return ApplyResult(state="disabled", external_id=user.username, message="disabled; files kept")
