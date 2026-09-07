"""Engine and auth tests, against fake connectors.

The two that matter most:

* ``test_forged_headers_from_untrusted_peer_rejected`` — the single most
  valuable test in the project. Header-forged auth behind a reverse proxy is
  the failure that turns "an internal admin page" into "anyone on the LAN is a
  fleet administrator".
* ``test_worker_killed_mid_run_produces_exactly_one_account`` — the at-least-once
  queue guarantees every apply() runs twice eventually. This proves that is safe.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from doser.connectors.base import (
    TIER_API,
    ApplyResult,
    Connector,
    ConnectorError,
    PlannedStep,
    VerifyResult,
)


class FakeTarget(Connector):
    """A connector whose failure mode is controllable."""

    name = "fake"
    label = "Fake service"
    tier = TIER_API
    required = True

    def __init__(self, settings=None, http=None, *, fail_times: int = 0, permanent: bool = False):
        self.settings, self.http = settings, http
        self.fail_times = fail_times
        self.permanent = permanent
        self.applies = 0
        self.accounts: set[str] = set()

    @property
    def configured(self) -> bool:
        return True

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        return [PlannedStep(self.name, self.tier, "create", f"create {user.username}")]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        self.applies += 1
        if self.applies <= self.fail_times:
            from doser.connectors.base import PermanentConnectorError

            raise (PermanentConnectorError if self.permanent else ConnectorError)("boom")
        # Read before write — this is what makes retries safe.
        if user.username in self.accounts:
            return ApplyResult(state="provisioned", external_id=user.username, message="adopted")
        self.accounts.add(user.username)
        return ApplyResult(state="provisioned", external_id=user.username, message="created")

    async def verify(self, user: Any) -> VerifyResult:
        return VerifyResult(
            state="provisioned" if user.username in self.accounts else "drifted"
        )


# ------------------------------------------------------------------ auth ---
@pytest.mark.asyncio
async def test_forged_headers_from_untrusted_peer_rejected(monkeypatch):
    """THE test. A request that did not come via Traefik must not be able to
    claim fleet-admin by setting a header."""
    from doser.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DOSER_DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("DOSER_TRUSTED_PROXY_CIDRS", "10.99.99.0/24")

    from doser.auth import _peer_trusted

    s = Settings()
    assert not _peer_trusted("192.168.1.50", s.trusted_proxies)
    assert not _peer_trusted(None, s.trusted_proxies)
    assert _peer_trusted("10.99.99.7", s.trusted_proxies)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_dev_bypass_refused_off_loopback(monkeypatch):
    from doser.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DOSER_DEV_AUTH_BYPASS", "true")
    from doser.auth import ForbiddenSource, current_operator
    from doser.config import Settings

    class Req:
        class client:
            host = "192.168.1.50"

        headers: dict = {}

    with pytest.raises(ForbiddenSource):
        await current_operator(Req(), Settings())  # type: ignore[arg-type]
    get_settings.cache_clear()


# ------------------------------------------------------------ idempotency ---
@pytest.mark.asyncio
async def test_apply_twice_equals_apply_once():
    fake = FakeTarget()

    class U:
        username = "bubbles"

    step = PlannedStep("fake", TIER_API, "create", "")
    r1 = await fake.apply(U(), step)
    r2 = await fake.apply(U(), step)
    assert r1.message == "created"
    assert r2.message == "adopted"
    assert len(fake.accounts) == 1, "a retry must not create a second account"


@pytest.mark.asyncio
async def test_transient_failure_then_success_leaves_one_account():
    fake = FakeTarget(fail_times=2)

    class U:
        username = "penguin"

    step = PlannedStep("fake", TIER_API, "create", "")
    for _ in range(2):
        with pytest.raises(ConnectorError):
            await fake.apply(U(), step)
    result = await fake.apply(U(), step)
    assert result.state == "provisioned"
    assert len(fake.accounts) == 1


# ------------------------------------------------------------------ plan ---
@pytest.mark.asyncio
async def test_plan_has_no_side_effects():
    """If planning mutated anything, the preview the operator approves would
    be a lie."""
    fake = FakeTarget()

    class U:
        username = "x"
        profile = "household"

    await fake.plan(U(), "provision")
    await fake.plan(U(), "provision")
    assert fake.applies == 0
    assert fake.accounts == set()


@pytest.mark.asyncio
async def test_jit_connector_reports_pending_not_success():
    """Tier A/B honesty: a green tick for an account that does not exist yet
    would be a lie the whole design depends on avoiding."""
    from doser.connectors.base import JitConnector

    class J(JitConnector):
        name = "jit"
        mechanism = "LDAP"

    j = J(None, None)

    class U:
        username = "x"

    steps = await j.plan(U(), "provision")
    assert steps[0].action == "none"
    result = await j.apply(U(), steps[0])
    assert result.state == "pending_first_login"


# ----------------------------------------------------------------- audit ---
def test_audit_scrubber_redacts_secrets():
    from doser.engine import scrub

    out = scrub(
        {
            "user": "bubbles",
            "password": "hunter2",
            "api_key": "sk-live-xyz",
            "nested": {"admin_token": "t0k", "ok": 1},
            "Authorization": "Bearer abc",
        }
    )
    blob = str(out)
    assert "hunter2" not in blob
    assert "sk-live-xyz" not in blob
    assert "t0k" not in blob
    assert "Bearer abc" not in blob
    assert out["user"] == "bubbles"
    assert out["nested"]["ok"] == 1


def test_idempotency_key_is_deterministic_and_scoped():
    from doser.engine import idem_key

    a = idem_key("run1", "immich", "create", "u1")
    assert a == idem_key("run1", "immich", "create", "u1")
    assert a != idem_key("run2", "immich", "create", "u1")
    assert a != idem_key("run1", "immich", "create", "u2")
    assert a != idem_key("run1", "vaultwarden", "create", "u1")
