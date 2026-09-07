"""Home Assistant.

Decision D5 chose the HACS OIDC auth provider, which makes HA tier B: the
account appears on first SSO login and ``doser`` holds no HA credentials at all.
That is the default (``hass_mode = "oidc_jit"``) and it is what should run in
production.

``hass_mode = "local_ws"`` exists **for testing only**.  It creates the account
through HA's *internal* WebSocket API — the one the frontend uses.  Three things
are true about it and all three matter:

1. It is undocumented and unversioned.  It can break on any HA release.
2. It requires ``doser`` to mint an initial password, which breaks the "this
   tool never handles a password" rule that otherwise holds absolutely
   (docs/07 §3).  The password is generated, returned once, and never stored.
3. HA has no clean force-change-at-first-login, so the person has to be told to
   change it.

Whichever mode you use: keep the local ``barista`` owner account as break-glass,
and test it with Authelia stopped *before* relying on OIDC.  Being locked out of
the thing that controls your front door is the failure this guards against.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

import httpx

from .base import (
    TIER_API,
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


class HomeAssistantConnector(JitConnector):
    name = "homeassistant"
    label = "Home Assistant"
    required = True
    supports_disable = True
    mechanism = "OIDC (Authelia)"

    def __init__(self, settings: Any, http: httpx.AsyncClient) -> None:
        super().__init__(settings, http)
        self.ws_mode = settings.hass_mode == "local_ws"
        self.tier = TIER_API if self.ws_mode else TIER_OIDC
        self.caveat = (
            "TESTING MODE (local_ws): creates the account via HA's undocumented internal "
            "WebSocket API and mints an initial password. Fragile across HA upgrades. "
            "Production should use hass_mode=oidc_jit (decision D5)."
            if self.ws_mode
            else "Mode oidc_jit (D5): a HACS OIDC provider creates the account on first SSO "
            "login. doser holds no HA credentials by default — set DOSER_HASS_TOKEN "
            "(a long-lived access token) if you want doser to confirm the login "
            "automatically instead of checking by hand; it is only ever used to read the "
            "user list, never to create one. Keep and TEST a local break-glass owner."
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.hass_token) if self.ws_mode else True

    @property
    def base_url(self) -> str:
        return self.settings.service_url("hass_url", "homeassistant")

    def _ws_url(self) -> str:
        return self.base_url.replace("https://", "wss://").replace("http://", "ws://") + (
            "/api/websocket"
        )

    async def health(self) -> HealthResult:
        t0 = time.monotonic()
        try:
            headers = (
                {"Authorization": f"Bearer {self.settings.hass_token}"}
                if self.settings.hass_token
                else {}
            )
            r = await self.http.get(f"{self.base_url}/api/", headers=headers)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, detail=describe_exception(exc))
        note = " (local_ws mode)" if self.ws_mode else " (oidc_jit — no writes)"
        # oidc_jit holds no HA credentials by design, so an unauthenticated ping
        # to /api/ always comes back 401/400 — that means "reachable," not
        # "unhealthy." Only local_ws (which does carry a token) should treat a
        # 4xx as a real auth failure.
        ok = r.status_code < 500 if not self.ws_mode else r.status_code < 400
        return HealthResult(
            ok=ok,
            detail=f"{self.base_url} → {r.status_code}{note}",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        if not self.ws_mode:
            steps = await super().plan(user, action)
            if action != "disable":
                steps[0].warning = (
                    "Break-glass: keep a local HA owner account that never uses OIDC, "
                    "and re-test it after every HA upgrade"
                )
            return steps
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="disable",
                    summary=f"Deactivate HA user {user.username} via the WebSocket API",
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="create",
                summary=(
                    f"WS config/auth/create + auth_provider/homeassistant/create for "
                    f"{user.username}; an initial password is generated and shown ONCE"
                ),
                warning=(
                    "Undocumented internal API — verify against your HA version. "
                    "Tell the person to change the password at first login."
                ),
            )
        ]

    # --- WebSocket plumbing ----------------------------------------------
    async def _ws_call(self, messages: list[dict]) -> list[dict]:
        """Authenticate, send each message in order, collect results.

        Uses httpx's underlying network stack via `websockets` if available;
        falls back to a clear error rather than pretending to work.
        """
        try:
            import websockets  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise PermanentConnectorError(
                "hass_mode=local_ws needs the 'websockets' package installed"
            ) from exc

        results: list[dict] = []
        try:
            async with websockets.connect(self._ws_url(), open_timeout=10) as ws:
                await ws.recv()  # auth_required
                await ws.send(
                    json.dumps({"type": "auth", "access_token": self.settings.hass_token})
                )
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise PermanentConnectorError("Home Assistant rejected the access token")
                for i, msg in enumerate(messages, start=1):
                    await ws.send(json.dumps({"id": i, **msg}))
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("id") == i:
                            results.append(reply)
                            break
        except PermanentConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                f"home assistant websocket failed: {describe_exception(exc)}"
            ) from exc
        return results

    async def _find(self, username: str) -> Optional[dict]:
        replies = await self._ws_call([{"type": "config/auth/list"}])
        for u in (replies[0].get("result") or []) if replies else []:
            if u.get("username") == username or u.get("name") == username:
                return u
        return None

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if not self.ws_mode:
            return await super().apply(user, step)
        if step.action == "disable":
            return await self.disable(user)

        existing = await self._find(user.username)
        if existing:
            return ApplyResult(
                state="provisioned",
                external_id=existing.get("id"),
                message="already existed — adopted",
            )

        # Step 1: the user record.  Step 2: attach credentials.
        created = await self._ws_call(
            [{"type": "config/auth/create", "name": user.display_name, "group_ids": ["system-users"]}]
        )
        result = created[0].get("result") or {}
        ha_user = result.get("user") or result
        ha_id = ha_user.get("id")
        if not ha_id:
            raise PermanentConnectorError(f"HA create returned no user id: {created[0]}")

        password = secrets.token_urlsafe(18)
        await self._ws_call(
            [
                {
                    "type": "config/auth_provider/homeassistant/create",
                    "user_id": ha_id,
                    "username": user.username,
                    "password": password,
                }
            ]
        )
        return ApplyResult(
            state="provisioned",
            external_id=ha_id,
            # Returned once so the operator can hand it over; never persisted.
            external_ref={"initial_password_shown_once": password, "must_change": True},
            message="created — give this password to the person and tell them to change it",
        )

    async def verify(self, user: Any) -> VerifyResult:
        if not self.ws_mode:
            if not self.settings.hass_token:
                return VerifyResult(
                    state="pending_first_login",
                    detail=(
                        "oidc_jit — appears on first SSO login "
                        "(set DOSER_HASS_TOKEN to confirm automatically)"
                    ),
                )
            # hass_token is optional here — used only to read the user list and
            # confirm the login, never to create anything. D5 still holds: doser
            # does not *need* HA credentials for oidc_jit to work.
            try:
                found = await self._find(user.username)
            except Exception as exc:  # noqa: BLE001
                return VerifyResult(
                    state="pending_first_login",
                    detail=f"could not confirm via HA's internal API: {exc}",
                )
            if not found:
                return VerifyResult(
                    state="pending_first_login", detail="not found yet — awaiting first SSO login"
                )
            return VerifyResult(state="provisioned", external_id=found.get("id"))
        # local_ws pre-creates the account in apply(), so absence here really is
        # drift rather than "hasn't logged in yet".
        found = await self._find(user.username)
        if not found:
            return VerifyResult(state="drifted", detail="no HA user with this username")
        return VerifyResult(state="provisioned", external_id=found.get("id"))

    async def disable(self, user: Any) -> ApplyResult:
        if not self.ws_mode:
            return ApplyResult(
                state="disabled",
                message="blocked upstream at lldap/Authelia — VERIFY live HA sessions expire too",
            )
        found = await self._find(user.username)
        if not found:
            return ApplyResult(state="disabled", message="no HA user to disable")
        await self._ws_call(
            [{"type": "config/auth/update", "user_id": found["id"], "is_active": False}]
        )
        return ApplyResult(state="disabled", external_id=found.get("id"))
