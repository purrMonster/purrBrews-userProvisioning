"""The remaining user-facing connectors.

These are short because the tier base classes do the work.  Each one still
carries its own health probe and its own honest caveat, because the
``/connectors`` screen is where those caveats get read — at configuration time,
rather than discovered at 3am.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from .base import (
    TIER_API,
    TIER_LDAP,
    TIER_NONE,
    TIER_OIDC,
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


async def _probe(conn: Connector, path: str, headers: Optional[dict] = None) -> HealthResult:
    t0 = time.monotonic()
    try:
        r = await conn.http.get(f"{conn.base_url}{path}", headers=headers or {})
    except Exception as exc:  # noqa: BLE001
        return HealthResult(ok=False, detail=describe_exception(exc))
    return HealthResult(
        ok=r.status_code < 500,
        detail=f"{conn.base_url} → {r.status_code}",
        latency_ms=(time.monotonic() - t0) * 1000,
    )


# --------------------------------------------------------------------------
# Vikunja — OIDC JIT.  Local registration should be turned off once SSO works.
# --------------------------------------------------------------------------
class VikunjaConnector(JitConnector):
    name = "vikunja"
    label = "Vikunja (tasks)"
    tier = TIER_OIDC
    mechanism = "OIDC (Authelia)"
    caveat = (
        "Account appears on first SSO login. Team and shared-project membership is NOT "
        "provisionable at creation — if this person needs access to a shared project, "
        "that is a manual step in Vikunja. Set DOSER_VIKUNJA_TOKEN (Settings → API Tokens, "
        "in the Vikunja UI) to let doser confirm the login automatically instead of waiting "
        "for you to check by hand."
    )

    @property
    def base_url(self) -> str:
        return self.settings.service_url("vikunja_url", "vikunja")

    async def health(self) -> HealthResult:
        return await _probe(self, "/api/v1/info")

    async def verify(self, user: Any) -> VerifyResult:
        token = self.settings.vikunja_token
        if not token:
            return VerifyResult(
                state="pending_first_login",
                detail="DOSER_VIKUNJA_TOKEN not set — cannot confirm login without an API token",
            )
        try:
            r = await self.http.get(
                f"{self.base_url}/api/v1/users",
                params={"s": user.username},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            return VerifyResult(
                state="pending_first_login",
                detail=f"could not reach Vikunja: {describe_exception(exc)}",
            )
        if r.status_code in (401, 403):
            return VerifyResult(
                state="pending_first_login",
                detail="Vikunja rejected DOSER_VIKUNJA_TOKEN — cannot confirm login",
            )
        if r.status_code == 404:
            return VerifyResult(
                state="pending_first_login",
                detail="Vikunja's /api/v1/users search returned 404 — check the path against your version",
            )
        if r.status_code >= 400:
            return VerifyResult(state="pending_first_login", detail=f"Vikunja returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            return VerifyResult(state="pending_first_login", detail="Vikunja returned a non-JSON body")
        found = any((u.get("username") or "").lower() == user.username.lower() for u in body or [])
        if found:
            return VerifyResult(state="provisioned", external_id=user.username)
        return VerifyResult(
            state="pending_first_login", detail="not found yet — awaiting first SSO login"
        )

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        steps = await super().plan(user, action)
        if action != "disable":
            steps.append(
                PlannedStep(
                    connector=self.name,
                    tier="manual",
                    action="manual",
                    summary="Shared-project access is not automatic",
                    manual_instruction=(
                        f"If {user.display_name} needs an existing shared project, add them "
                        "in Vikunja → Project → Share after their first login."
                    ),
                )
            )
        return steps


# --------------------------------------------------------------------------
# Actual Budget — OIDC JIT, but only when the server allows it.
# --------------------------------------------------------------------------
class ActualConnector(JitConnector):
    name = "actual"
    label = "Actual Budget"
    tier = TIER_OIDC
    mechanism = "OpenID"
    caveat = (
        "Requires ACTUAL_USER_CREATION_MODE=login on the Actual server, or it will NOT "
        "create OIDC users automatically. There is no REST user-creation API, so "
        "pre-creation is impossible. Roles and budget-file access are manual. Actual also "
        "has no public API for listing users, so doser cannot confirm the login itself — "
        "check Settings → User Directory in Actual after the person's first login."
    )

    @property
    def base_url(self) -> str:
        return self.settings.service_url("actual_url", "actualbudget")

    async def health(self) -> HealthResult:
        return await _probe(self, "/health")

    async def verify(self, user: Any) -> VerifyResult:
        # Actual exposes no stable, documented HTTP endpoint for listing OIDC
        # users — only an npm package meant for its own sync client, plus an
        # admin-only OAuth-client API unrelated to user accounts. Guessing at
        # an internal admin-UI route here would be worse than admitting we
        # cannot check: it would either always fail (false "drifted") or break
        # silently on the next Actual release. See the caveat above.
        return VerifyResult(
            state="pending_first_login",
            detail=(
                "Actual has no public API to check this — confirm in Actual → "
                "Settings → User Directory after they log in once"
            ),
        )

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        steps = await super().plan(user, action)
        if action != "disable":
            steps[0].warning = "Needs ACTUAL_USER_CREATION_MODE=login on the server"
            steps.append(
                PlannedStep(
                    connector=self.name,
                    tier="manual",
                    action="manual",
                    summary="Budget-file access must be granted by hand",
                    manual_instruction=(
                        f"After {user.display_name}'s first login: Actual → User Directory → "
                        "set their role, then share the budget file(s) they need."
                    ),
                )
            )
        return steps


# --------------------------------------------------------------------------
# Jellyfin — LDAP-Auth plugin.  Library grants are the trap here.
# --------------------------------------------------------------------------
class JellyfinConnector(JitConnector):
    name = "jellyfin"
    label = "Jellyfin (media)"
    tier = TIER_LDAP
    mechanism = "LDAP"
    caveat = (
        "Account appears on first LDAP login. LIBRARY ACCESS IS NOT GRANTED AUTOMATICALLY — "
        "depending on your defaults a new user sees everything or nothing. Set it explicitly."
    )

    @property
    def base_url(self) -> str:
        return self.settings.jellyfin_base()

    async def health(self) -> HealthResult:
        return await _probe(self, "/System/Info/Public")

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        steps = await super().plan(user, action)
        if action != "disable":
            steps.append(
                PlannedStep(
                    connector=self.name,
                    tier="manual",
                    action="manual",
                    summary="Library access must be set explicitly",
                    manual_instruction=(
                        f"Jellyfin → Users → {user.username} → Library Access. "
                        "Do this even if it looks fine — the default is not least-privilege."
                    ),
                )
            )
        return steps


# --------------------------------------------------------------------------
# Mealie — LDAP.  Env var names have moved across major versions.
# --------------------------------------------------------------------------
class MealieConnector(JitConnector):
    name = "mealie"
    label = "Mealie (recipes)"
    tier = TIER_LDAP
    mechanism = "LDAP"
    caveat = (
        "Account appears on first LDAP login. Mealie's auth env var names have changed "
        "between major versions — confirm LDAP_AUTH_ENABLED et al against your pinned tag. "
        "Set DOSER_MEALIE_TOKEN (an admin account's API token, under /user/profile/api-tokens) "
        "to let doser confirm the login automatically."
    )

    @property
    def base_url(self) -> str:
        return self.settings.service_url("mealie_url", "mealie")

    async def health(self) -> HealthResult:
        return await _probe(self, "/api/app/about")

    async def verify(self, user: Any) -> VerifyResult:
        token = self.settings.mealie_token
        if not token:
            return VerifyResult(
                state="pending_first_login",
                detail="DOSER_MEALIE_TOKEN not set — cannot confirm login without an admin API token",
            )
        try:
            r = await self.http.get(
                f"{self.base_url}/api/admin/users",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            return VerifyResult(
                state="pending_first_login",
                detail=f"could not reach Mealie: {describe_exception(exc)}",
            )
        if r.status_code in (401, 403):
            return VerifyResult(
                state="pending_first_login",
                detail="Mealie rejected DOSER_MEALIE_TOKEN — needs to be an admin account's token",
            )
        if r.status_code == 404:
            return VerifyResult(
                state="pending_first_login",
                detail="Mealie's /api/admin/users returned 404 — check the path against your version",
            )
        if r.status_code >= 400:
            return VerifyResult(state="pending_first_login", detail=f"Mealie returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            return VerifyResult(state="pending_first_login", detail="Mealie returned a non-JSON body")
        # Mealie's list endpoints are usually {"items": [...], ...}; accept a
        # bare list too rather than assume one shape across versions.
        rows = body.get("items", body) if isinstance(body, dict) else body
        found = any(
            (u.get("username") or "").lower() == user.username.lower()
            or (u.get("email") or "").lower() == user.email.lower()
            for u in rows or []
        )
        if found:
            return VerifyResult(state="provisioned", external_id=user.username)
        return VerifyResult(
            state="pending_first_login", detail="not found yet — awaiting first LDAP login"
        )


# --------------------------------------------------------------------------
# Paperless-ngx — allauth OIDC (needs v2.5.0+).
# --------------------------------------------------------------------------
class PaperlessConnector(JitConnector):
    name = "paperless"
    label = "Paperless-ngx (documents)"
    tier = TIER_OIDC
    mechanism = "OIDC (Authelia)"
    caveat = (
        "Requires Paperless-ngx v2.5.0+ with PAPERLESS_APPS allauth OIDC and "
        "PAPERLESS_SOCIAL_AUTO_SIGNUP. If your version does not map OIDC group claims to "
        "permissions, every new user needs manual permission assignment. Set "
        "DOSER_PAPERLESS_TOKEN (My Profile → API token, in the Paperless UI) to let doser "
        "confirm the login automatically."
    )

    @property
    def base_url(self) -> str:
        return self.settings.service_url("paperless_url", "paperless")

    def _headers(self) -> dict[str, str]:
        return (
            {"Authorization": f"Token {self.settings.paperless_token}"}
            if self.settings.paperless_token
            else {}
        )

    async def health(self) -> HealthResult:
        return await _probe(self, "/api/", self._headers())

    async def verify(self, user: Any) -> VerifyResult:
        if not self.settings.paperless_token:
            return VerifyResult(
                state="pending_first_login",
                detail="DOSER_PAPERLESS_TOKEN not set — cannot confirm login without an API token",
            )
        try:
            r = await self.http.get(f"{self.base_url}/api/users/", headers=self._headers())
        except httpx.HTTPError as exc:
            return VerifyResult(
                state="pending_first_login",
                detail=f"could not reach Paperless: {describe_exception(exc)}",
            )
        if r.status_code in (401, 403):
            return VerifyResult(
                state="pending_first_login",
                detail="Paperless rejected DOSER_PAPERLESS_TOKEN — cannot confirm login",
            )
        if r.status_code >= 400:
            return VerifyResult(state="pending_first_login", detail=f"Paperless returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            return VerifyResult(state="pending_first_login", detail="Paperless returned a non-JSON body")
        # DRF paginates by default ({"results": [...]}); fall back to a bare
        # list for installs that have pagination disabled. Filtered client-side
        # rather than via a query param, since the exact filter name is not
        # worth pinning down when a household's user list is a handful of rows.
        rows = body.get("results", body) if isinstance(body, dict) else body
        found = any((u.get("username") or "").lower() == user.username.lower() for u in rows or [])
        if found:
            return VerifyResult(state="provisioned", external_id=user.username)
        return VerifyResult(
            state="pending_first_login", detail="not found yet — awaiting first SSO login"
        )


# --------------------------------------------------------------------------
# Vaultwarden — invitation only, by design.
# --------------------------------------------------------------------------
class VaultwardenConnector(Connector):
    name = "vaultwarden"
    label = "Vaultwarden (passwords)"
    tier = TIER_API
    required = True
    supports_disable = False
    caveat = (
        "Invitation only — Vaultwarden's zero-knowledge design means an administrator "
        "CANNOT set a password or key, and should not be able to. The person completes "
        "registration and chooses their own master password. Never delete a Vaultwarden "
        "account on offboarding: personal items are unrecoverable by design."
    )

    @property
    def configured(self) -> bool:
        return bool(self.settings.vaultwarden_admin_token)

    @property
    def base_url(self) -> str:
        return self.settings.service_url("vaultwarden_url", "vault")

    def _cookies(self) -> dict[str, str]:
        return {"VW_ADMIN_SESSION": self.settings.vaultwarden_admin_token or ""}

    async def health(self) -> HealthResult:
        if not self.configured:
            return HealthResult(ok=False, detail="VAULTWARDEN_ADMIN_TOKEN not set")
        return await _probe(self, "/alive")

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="manual",
                    summary="Vaultwarden accounts are NOT deleted on offboarding",
                    manual_instruction=(
                        "Review organisation items they had access to. Leave the personal "
                        "vault alone — its contents are unrecoverable by design."
                    ),
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="create",
                summary=f"POST /admin/invite — {user.email} sets their own master password",
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if step.action == "manual":
            return ApplyResult(state="needs_manual", message=step.manual_instruction)
        try:
            r = await self.http.post(
                f"{self.base_url}/admin/invite",
                json={"email": user.email},
                cookies=self._cookies(),
                headers={"Authorization": f"Bearer {self.settings.vaultwarden_admin_token}"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"vaultwarden unreachable: {describe_exception(exc)}") from exc
        if r.status_code in (401, 403):
            raise PermanentConnectorError(
                "vaultwarden rejected the admin token — is the admin page enabled?"
            )
        if r.status_code >= 500:
            raise ConnectorError(f"vaultwarden {r.status_code}")
        # Re-inviting an existing address is benign; treat it as success so the
        # job is safely retryable.
        if r.status_code >= 400 and "already" not in r.text.lower():
            raise PermanentConnectorError(f"invite failed {r.status_code}: {r.text[:200]}")
        return ApplyResult(
            state="pending_first_login",
            external_id=user.email,
            message="invited — they set their own master password",
        )

    async def verify(self, user: Any) -> VerifyResult:
        # There is no clean read-back without scraping the admin page, so be
        # honest rather than inventing a green tick.
        return VerifyResult(
            state="pending_first_login",
            detail="invitation sent; acceptance is not observable via the admin API",
        )


# --------------------------------------------------------------------------
# Roundcube — has no user store at all.
# --------------------------------------------------------------------------
class RoundcubeConnector(Connector):
    name = "roundcube"
    label = "Roundcube (webmail)"
    tier = TIER_NONE
    caveat = (
        "Roundcube has NO accounts of its own — it authenticates against IMAP. "
        "'Provisioning a Roundcube user' really means creating a mailbox on the mail "
        "backend. The fleet has no mail server yet, so this is a documented no-op."
    )

    @property
    def base_url(self) -> str:
        return self.settings.service_url("roundcube_url", "roundcube")

    async def health(self) -> HealthResult:
        return await _probe(self, "/")

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        backend = self.settings.mail_backend
        if not backend:
            return [
                PlannedStep(
                    connector=self.name,
                    tier=TIER_NONE,
                    action="none",
                    summary=(
                        "No action — Roundcube has no user store and no mail backend is "
                        "configured. Nothing to provision."
                    ),
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier="manual",
                action="manual",
                summary=f"Mailbox must be created on the mail backend ({backend})",
                manual_instruction=(
                    f"Create the mailbox for {user.email} on {backend}. Roundcube will "
                    "then authenticate against it — no Roundcube-side account is needed."
                ),
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if step.action == "manual":
            return ApplyResult(state="needs_manual", message=step.manual_instruction)
        return ApplyResult(state="not_applicable", message="no user store")

    async def verify(self, user: Any) -> VerifyResult:
        return VerifyResult(state="not_applicable", detail="IMAP-backed; nothing to verify here")
