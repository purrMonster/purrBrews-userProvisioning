"""Immich — one of the few services where pre-creation genuinely beats JIT.

Immich has both a real admin user API *and* OIDC auto-registration.  We
pre-create, because that is the only way to set the storage quota and storage
label before the person uploads their first photo; afterwards you are migrating
files rather than configuring an account.

The OAuth link then has to match by **email** onto the account we made.  If it
matches on the subject claim instead, you get two accounts and a confused user
— which is exactly what docs/12 §5.2 tells you to test first.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from .base import (
    TIER_API,
    ApplyResult,
    Connector,
    ConnectorError,
    HealthResult,
    PermanentConnectorError,
    PlannedStep,
    VerifyResult,
    describe_exception,
)


class ImmichConnector(Connector):
    name = "immich"
    label = "Immich (photos)"
    tier = TIER_API
    required = True
    supports_disable = True
    caveat = (
        "Pre-creates the account so quota and storage label can be set before first upload. "
        "VERIFY that OAuth auto-register links to this account by email rather than "
        "creating a second one (docs/12 §5.2)."
    )

    @property
    def configured(self) -> bool:
        return bool(self.settings.immich_api_key)

    @property
    def base_url(self) -> str:
        return self.settings.service_url("immich_url", "immich")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.settings.immich_api_key or "",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self.http.request(
                method, f"{self.base_url}{path}", headers=self._headers(), **kw
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"immich unreachable: {describe_exception(exc)}") from exc
        if r.status_code in (401, 403):
            raise PermanentConnectorError("immich rejected the API key")
        if r.status_code >= 500:
            raise ConnectorError(f"immich {r.status_code}: {r.text[:200]}")
        return r

    async def _find_by_email(self, email: str) -> Optional[dict]:
        # Endpoint paths have moved between Immich versions; try the current
        # admin route first, then the older one, before giving up.
        for path in ("/api/admin/users", "/api/users"):
            r = await self._request("GET", path)
            if r.status_code == 404:
                continue
            if r.status_code >= 400:
                continue
            for u in r.json() or []:
                if (u.get("email") or "").lower() == email.lower():
                    return u
            return None
        raise PermanentConnectorError(
            "no usable Immich user-listing endpoint found — check the API path for your version"
        )

    async def health(self) -> HealthResult:
        if not self.configured:
            return HealthResult(ok=False, detail="IMMICH_API_KEY not set")
        t0 = time.monotonic()
        try:
            r = await self._request("GET", "/api/server/about")
            if r.status_code >= 400:
                r = await self._request("GET", "/api/admin/users")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, detail=describe_exception(exc))
        ok = r.status_code < 400
        return HealthResult(
            ok=ok,
            detail=f"{self.base_url} → {r.status_code}",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="disable",
                    summary=f"Deactivate {user.email} in Immich (photos are retained)",
                )
            ]
        quota = self.settings.immich_quota_gb
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="create",
                summary=(
                    f"POST /api/admin/users — {user.email}, quota {quota} GB, "
                    f"storage label '{user.username}'"
                ),
                warning="OAuth must link by email or you get a duplicate account",
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if step.action == "disable":
            return await self.disable(user)

        existing = await self._find_by_email(user.email)
        if existing:
            return ApplyResult(
                state="provisioned",
                external_id=existing.get("id"),
                external_ref={"adopted": True},
                message="account already existed — adopted rather than duplicated",
            )

        body = {
            "email": user.email,
            "name": user.display_name,
            "storageLabel": user.username,
            "quotaSizeInBytes": self.settings.immich_quota_gb * 1024**3,
            "shouldChangePassword": True,
            # Immich requires *a* password on create even when OAuth will be the
            # real login path.  It is random, never stored, never shown.
            "password": _throwaway(),
            "notify": False,
        }
        r = await self._request("POST", "/api/admin/users", json=body)
        if r.status_code == 404:
            r = await self._request("POST", "/api/users", json=body)
        if r.status_code >= 400:
            raise PermanentConnectorError(f"immich create failed {r.status_code}: {r.text[:200]}")
        created = r.json()
        return ApplyResult(
            state="provisioned",
            external_id=created.get("id"),
            external_ref={"storageLabel": user.username, "quotaGb": self.settings.immich_quota_gb},
            message="created",
        )

    async def verify(self, user: Any) -> VerifyResult:
        found = await self._find_by_email(user.email)
        if not found:
            return VerifyResult(state="drifted", detail="no Immich account with this email")
        return VerifyResult(state="provisioned", external_id=found.get("id"))

    async def disable(self, user: Any) -> ApplyResult:
        found = await self._find_by_email(user.email)
        if not found:
            return ApplyResult(state="disabled", message="no account to disable")
        uid = found.get("id")
        r = await self._request("DELETE", f"/api/admin/users/{uid}", json={"force": False})
        if r.status_code >= 400:
            return ApplyResult(
                state="needs_manual",
                external_id=uid,
                message=f"soft-delete returned {r.status_code} — disable by hand in Immich",
            )
        return ApplyResult(state="disabled", external_id=uid, message="soft-deleted; photos kept")

    async def compensate(self, user: Any, step: PlannedStep) -> ApplyResult:
        return await self.disable(user)


def _throwaway() -> str:
    import secrets

    return secrets.token_urlsafe(32)
