"""Verification tests for the JIT/OIDC connectors.

These connectors never create anything themselves — the account appears on
first login, and doser can only ever be honest about whether it happened.
Before 2026-09-07, four of them (Vikunja, Actual, Paperless, Mealie) either
never overrode ``verify()`` at all (silently reporting "not_applicable", which
the UI has no pill for) or — for Home Assistant's default oidc_jit mode —
used a health-check status-code threshold that misclassified a normal
"reachable but unauthenticated" response as unhealthy.

The tests below pin two things for each connector:

1. With no credential configured, verify() is HONEST — it reports
   "pending_first_login", never "not_applicable" and never a hard error.
2. With a credential configured, verify() reads the target app's own admin
   API and reports "provisioned" only when the account is actually found —
   and degrades to "pending_first_login" (never "drifted") on any response
   shape it doesn't recognise, an auth rejection, or a network failure. A JIT
   account that simply hasn't logged in yet must never be shown as "drifted"
   — that pill means something was provisioned and then vanished, which is
   not what "hasn't shown up yet" means.

Actual Budget is the deliberate exception: it has no public API for listing
users at all, so its verify() always returns "pending_first_login" and never
attempts a network call — that is asserted directly, not worked around.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from doser.config import Settings
from doser.connectors.base import describe_exception
from doser.connectors.homeassistant import HomeAssistantConnector
from doser.connectors.nextcloud import NextcloudConnector
from doser.connectors.simple import (
    ActualConnector,
    MealieConnector,
    PaperlessConnector,
    VikunjaConnector,
)


class U:
    username = "bubbles"
    email = "bubbles@whiskertreat.fyi"


# ------------------------------------------------------------ describe_exception
# Found live on 2026-09-07: httpx.ConnectTimeout's str() is empty (the real
# info lives on __cause__), which turned several health checks into a blank
# detail line with no indication of what went wrong. Every place that used to
# do `detail=str(exc)` now goes through this instead.
def test_describe_exception_falls_back_to_class_name_when_message_is_empty():
    assert describe_exception(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_describe_exception_keeps_a_real_message():
    out = describe_exception(ConnectionRefusedError("actively refused"))
    assert out == "ConnectionRefusedError: actively refused"


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def settings(**overrides: Any) -> Settings:
    return Settings(domain="whiskertreat.fyi", **overrides)


# --------------------------------------------------------------- vikunja ---
@pytest.mark.asyncio
async def test_vikunja_verify_pending_without_token():
    c = VikunjaConnector(settings(), client(lambda r: httpx.Response(200, json=[])))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "DOSER_VIKUNJA_TOKEN" in result.detail


@pytest.mark.asyncio
async def test_vikunja_verify_found():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/users"
        assert request.url.params["s"] == "bubbles"
        assert request.headers["authorization"] == "Bearer vtok"
        return httpx.Response(200, json=[{"username": "bubbles"}])

    c = VikunjaConnector(settings(vikunja_token="vtok"), client(handler))
    result = await c.verify(U())
    assert result.state == "provisioned"
    assert result.external_id == "bubbles"


@pytest.mark.asyncio
async def test_vikunja_verify_not_found_is_pending_not_drifted():
    c = VikunjaConnector(
        settings(vikunja_token="vtok"), client(lambda r: httpx.Response(200, json=[]))
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"


@pytest.mark.asyncio
async def test_vikunja_verify_rejected_token_is_pending_not_error():
    c = VikunjaConnector(
        settings(vikunja_token="stale"), client(lambda r: httpx.Response(401))
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "rejected" in result.detail


@pytest.mark.asyncio
async def test_vikunja_verify_404_degrades_gracefully():
    """A guessed endpoint path being wrong must not crash the reconciler."""
    c = VikunjaConnector(
        settings(vikunja_token="vtok"), client(lambda r: httpx.Response(404))
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "404" in result.detail


# -------------------------------------------------------------- paperless --
@pytest.mark.asyncio
async def test_paperless_verify_pending_without_token():
    c = PaperlessConnector(settings(), client(lambda r: httpx.Response(200, json=[])))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "DOSER_PAPERLESS_TOKEN" in result.detail


@pytest.mark.asyncio
async def test_paperless_verify_found_paginated_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Token ptok"
        return httpx.Response(200, json={"count": 1, "results": [{"username": "bubbles"}]})

    c = PaperlessConnector(settings(paperless_token="ptok"), client(handler))
    result = await c.verify(U())
    assert result.state == "provisioned"


@pytest.mark.asyncio
async def test_paperless_verify_found_bare_list_shape():
    """Pagination disabled installs return a plain list — must not assume DRF's wrapper."""
    c = PaperlessConnector(
        settings(paperless_token="ptok"),
        client(lambda r: httpx.Response(200, json=[{"username": "bubbles"}])),
    )
    result = await c.verify(U())
    assert result.state == "provisioned"


@pytest.mark.asyncio
async def test_paperless_verify_not_found_is_pending():
    c = PaperlessConnector(
        settings(paperless_token="ptok"),
        client(lambda r: httpx.Response(200, json={"results": []})),
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"


@pytest.mark.asyncio
async def test_paperless_verify_network_failure_is_pending_not_raised():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    c = PaperlessConnector(settings(paperless_token="ptok"), client(handler))
    result = await c.verify(U())  # must not raise
    assert result.state == "pending_first_login"
    assert "could not reach Paperless" in result.detail


# ----------------------------------------------------------------- mealie --
@pytest.mark.asyncio
async def test_mealie_verify_pending_without_token():
    c = MealieConnector(settings(), client(lambda r: httpx.Response(200, json=[])))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "DOSER_MEALIE_TOKEN" in result.detail


@pytest.mark.asyncio
async def test_mealie_verify_found_items_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/admin/users"
        assert request.headers["authorization"] == "Bearer mtok"
        return httpx.Response(200, json={"items": [{"username": "bubbles", "email": "x@y.z"}]})

    c = MealieConnector(settings(mealie_token="mtok"), client(handler))
    result = await c.verify(U())
    assert result.state == "provisioned"


@pytest.mark.asyncio
async def test_mealie_verify_matches_on_email_too():
    """Mealie's LDAP integration may key off email; match either field."""
    c = MealieConnector(
        settings(mealie_token="mtok"),
        client(
            lambda r: httpx.Response(
                200, json=[{"username": "someone-else", "email": "bubbles@whiskertreat.fyi"}]
            )
        ),
    )
    result = await c.verify(U())
    assert result.state == "provisioned"


@pytest.mark.asyncio
async def test_mealie_verify_forbidden_token_is_pending():
    c = MealieConnector(
        settings(mealie_token="not-admin"), client(lambda r: httpx.Response(403))
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "admin" in result.detail


# ----------------------------------------------------------------- actual --
@pytest.mark.asyncio
async def test_actual_verify_never_calls_the_network():
    """There is no public API for this — verify() must not pretend otherwise."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ActualConnector.verify() must not make a network call")

    c = ActualConnector(settings(), client(handler))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "no public API" in result.detail


# ----------------------------------------------------------- home assistant
@pytest.mark.asyncio
async def test_hass_oidc_jit_verify_pending_without_token():
    c = HomeAssistantConnector(settings(hass_mode="oidc_jit"), client(lambda r: httpx.Response(200)))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "DOSER_HASS_TOKEN" in result.detail


@pytest.mark.asyncio
async def test_hass_oidc_jit_verify_found_via_optional_token():
    c = HomeAssistantConnector(
        settings(hass_mode="oidc_jit", hass_token="hatok"), client(lambda r: httpx.Response(200))
    )
    c._find = AsyncMock(return_value={"id": "ha-123"})
    result = await c.verify(U())
    assert result.state == "provisioned"
    assert result.external_id == "ha-123"


@pytest.mark.asyncio
async def test_hass_oidc_jit_verify_not_found_is_pending_not_drifted():
    """Absence in oidc_jit mode means 'has not logged in yet', not drift —
    doser never pre-creates anything for this tier."""
    c = HomeAssistantConnector(
        settings(hass_mode="oidc_jit", hass_token="hatok"), client(lambda r: httpx.Response(200))
    )
    c._find = AsyncMock(return_value=None)
    result = await c.verify(U())
    assert result.state == "pending_first_login"


@pytest.mark.asyncio
async def test_hass_oidc_jit_verify_ws_failure_degrades_gracefully():
    """The internal WS API is undocumented and can break on any HA release —
    a failure there must never surface as a hard error to the reconciler."""
    c = HomeAssistantConnector(
        settings(hass_mode="oidc_jit", hass_token="hatok"), client(lambda r: httpx.Response(200))
    )
    c._find = AsyncMock(side_effect=RuntimeError("websockets not installed"))
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "could not confirm" in result.detail


@pytest.mark.asyncio
async def test_hass_local_ws_verify_absence_is_drift():
    """local_ws pre-creates the account in apply(), so — unlike oidc_jit —
    absence here really is drift, not a normal waiting state."""
    c = HomeAssistantConnector(
        settings(hass_mode="local_ws", hass_token="hatok"), client(lambda r: httpx.Response(200))
    )
    c._find = AsyncMock(return_value=None)
    result = await c.verify(U())
    assert result.state == "drifted"


@pytest.mark.asyncio
async def test_hass_health_threshold_differs_by_mode():
    """Regression test for the 2026-09-06 fix: oidc_jit holds no HA
    credentials by design, so an unauthenticated 400 from /api/ means
    'reachable', not 'unhealthy'. local_ws carries a real token, so the same
    4xx there is a genuine auth failure."""
    jit = HomeAssistantConnector(
        settings(hass_mode="oidc_jit"), client(lambda r: httpx.Response(400))
    )
    assert (await jit.health()).ok is True

    ws = HomeAssistantConnector(
        settings(hass_mode="local_ws", hass_token="x"), client(lambda r: httpx.Response(400))
    )
    assert (await ws.health()).ok is False

    down = HomeAssistantConnector(
        settings(hass_mode="oidc_jit"), client(lambda r: httpx.Response(500))
    )
    assert (await down.health()).ok is False


# -------------------------------------------------------------- nextcloud --
@pytest.mark.asyncio
async def test_nextcloud_ldap_verify_pending_without_admin_creds():
    c = NextcloudConnector(
        settings(nextcloud_mode="ldap"), client(lambda r: httpx.Response(200))
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"
    assert "DOSER_NEXTCLOUD_ADMIN_USER" in result.detail


@pytest.mark.asyncio
async def test_nextcloud_ldap_verify_found_via_optional_readonly_creds():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ocs/v2.php/cloud/users/bubbles"
        return httpx.Response(
            200,
            json={"ocs": {"meta": {"statuscode": 100}, "data": {"id": "bubbles"}}},
        )

    c = NextcloudConnector(
        settings(
            nextcloud_mode="ldap",
            nextcloud_admin_user="admin",
            nextcloud_admin_password="pw",
        ),
        client(handler),
    )
    result = await c.verify(U())
    assert result.state == "provisioned"


@pytest.mark.asyncio
async def test_nextcloud_ldap_verify_not_found_is_pending_not_drifted():
    """LDAP mode never pre-creates — absence pre-login is normal, not drift."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ocs": {"meta": {"statuscode": 404}}})

    c = NextcloudConnector(
        settings(
            nextcloud_mode="ldap",
            nextcloud_admin_user="admin",
            nextcloud_admin_password="pw",
        ),
        client(handler),
    )
    result = await c.verify(U())
    assert result.state == "pending_first_login"


@pytest.mark.asyncio
async def test_nextcloud_ocs_verify_not_found_is_still_drift():
    """ocs mode DOES pre-create in apply() — this behaviour must not regress."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ocs": {"meta": {"statuscode": 404}}})

    c = NextcloudConnector(
        settings(
            nextcloud_mode="ocs",
            nextcloud_admin_user="admin",
            nextcloud_admin_password="pw",
        ),
        client(handler),
    )
    result = await c.verify(U())
    assert result.state == "drifted"
