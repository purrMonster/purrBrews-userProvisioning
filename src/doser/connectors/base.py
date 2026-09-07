"""Connector contract.

Every target service implements this one interface, so adding a service means
writing one class and registering it — the engine never changes.

Three rules that are not negotiable, because the whole design rests on them:

* ``plan()`` has **no side effects.**  The operator sees its output and approves
  it before anything happens; if planning mutated things, the preview would be
  a lie.
* ``apply()`` is **idempotent**.  The job queue is at-least-once, so every
  ``apply()`` *will* run twice eventually.  Read before write.
* ``compensate()`` is allowed to say **no**.  Guessing at an undo is worse than
  admitting one is not available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..config import Settings

# tier vocabulary — see docs/02-service-integration-matrix.md §1
TIER_SOURCE = "source"  # lldap: the commit point
TIER_LDAP = "ldap"  # A: binds to lldap, account appears on first login
TIER_OIDC = "oidc"  # B: Authelia OIDC relying party, JIT create
TIER_API = "api"  # C: needs a real API/CLI call
TIER_MANUAL = "manual"  # D: a human must finish it
TIER_NONE = "none"  # E: no user model at all


@dataclass
class PlannedStep:
    connector: str
    tier: str
    action: str  # "create" | "none" | "manual" | "disable" | "verify"
    summary: str
    reversible: bool = True
    manual_instruction: Optional[str] = None
    warning: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "tier": self.tier,
            "action": self.action,
            "summary": self.summary,
            "reversible": self.reversible,
            "manual_instruction": self.manual_instruction,
            "warning": self.warning,
        }


@dataclass
class ApplyResult:
    state: str  # an ACCOUNT_STATE value
    external_id: Optional[str] = None
    external_ref: dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None


@dataclass
class VerifyResult:
    state: str
    external_id: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class HealthResult:
    ok: bool
    detail: str
    latency_ms: Optional[float] = None


def describe_exception(exc: BaseException) -> str:
    """``str(exc)`` is empty for some exception types — ``httpx.ConnectTimeout``
    is the common one, since the real information lives on ``__cause__`` rather
    than the exception's own message. A blank detail line in a health report or
    the ``/connectors`` screen tells the operator nothing; the exception's class
    name at least says *what kind* of failure this was.
    """
    msg = str(exc)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class ConnectorError(RuntimeError):
    """Raised for a failure that should be retried."""


class PermanentConnectorError(ConnectorError):
    """Raised for a failure that retrying will not fix — dead-letter at once."""


class Connector:
    name: str = "abstract"
    label: str = "Abstract"
    tier: str = TIER_NONE
    #: Does failure of this connector make the whole run "partial"?
    required: bool = False
    supports_avatar: bool = False
    supports_disable: bool = False
    #: Human-readable note shown on the /connectors screen: what it does, and
    #: importantly what it *cannot* do.
    caveat: Optional[str] = None

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http

    # --- configuration ---------------------------------------------------
    @property
    def configured(self) -> bool:
        """False when a required credential is absent.

        A connector that is not configured is *disabled and shown as disabled*.
        It must never silently no-op — that is how you discover in six months
        that nobody was ever provisioned into Vaultwarden.
        """
        return True

    @property
    def base_url(self) -> Optional[str]:
        return None

    # --- lifecycle -------------------------------------------------------
    async def health(self) -> HealthResult:
        return HealthResult(ok=True, detail="no health check implemented")

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        raise NotImplementedError

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        return ApplyResult(state="not_applicable")

    async def verify(self, user: Any) -> VerifyResult:
        return VerifyResult(state="not_applicable")

    async def disable(self, user: Any) -> ApplyResult:
        return ApplyResult(state="not_applicable", message="no disable path")

    async def compensate(self, user: Any, step: PlannedStep) -> ApplyResult:
        return ApplyResult(state="needs_manual", message="no automatic undo available")


# --------------------------------------------------------------------------
# Tier base classes.  Most of the fleet is a few lines because of these.
# --------------------------------------------------------------------------


class NoUserModelConnector(Connector):
    """Tier E — access is entirely Authelia forward-auth. Nothing to create."""

    tier = TIER_NONE

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="none",
                summary="No user model — access is granted by Authelia group membership only",
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        return ApplyResult(state="not_applicable")

    async def verify(self, user: Any) -> VerifyResult:
        return VerifyResult(state="not_applicable")


class JitConnector(Connector):
    """Tiers A and B — the account materialises on first login.

    Pre-creating one here would usually produce **two unlinked accounts**,
    because the service matches on a different key than the API does.  So
    ``apply()`` deliberately does nothing and records ``pending_first_login``;
    the reconciler promotes it to ``provisioned`` once the person actually
    shows up.
    """

    mechanism = "SSO"

    async def plan(self, user: Any, action: str) -> list[PlannedStep]:
        if action == "disable":
            return [
                PlannedStep(
                    connector=self.name,
                    tier=self.tier,
                    action="disable",
                    summary=(
                        f"No direct call — disabling in lldap blocks the {self.mechanism} "
                        "login. Verify existing sessions do not outlive it."
                    ),
                )
            ]
        return [
            PlannedStep(
                connector=self.name,
                tier=self.tier,
                action="none",
                summary=f"No action — account appears on first {self.mechanism} login",
            )
        ]

    async def apply(self, user: Any, step: PlannedStep) -> ApplyResult:
        if step.action == "disable":
            return await self.disable(user)
        return ApplyResult(
            state="pending_first_login",
            message=f"waiting for first {self.mechanism} login",
        )

    async def disable(self, user: Any) -> ApplyResult:
        return ApplyResult(state="disabled", message="blocked upstream at lldap")
