"""Connector registry.

Scope note: this build registers **user-facing applications only**.  The
system-maintenance surfaces — Komodo, Scrutiny, Diun, NetAlertX, Speedtest
Tracker, Headscale, n8n, Pi-hole, Traefik — are deliberately absent.  They are
either tier E (Authelia group membership is the whole story) or operator-only
tools that no household member should have an account in.
"""

from __future__ import annotations

from typing import Iterable, Optional

import httpx

from ..config import Settings, get_settings
from .base import Connector, HealthResult, PlannedStep  # noqa: F401  (re-exported)
from .homeassistant import HomeAssistantConnector
from .immich import ImmichConnector
from .lldap import LldapConnector
from .nextcloud import NextcloudConnector
from .simple import (
    ActualConnector,
    JellyfinConnector,
    MealieConnector,
    PaperlessConnector,
    RoundcubeConnector,
    VaultwardenConnector,
    VikunjaConnector,
)

#: Order matters for display, and lldap must be first — it is the commit point.
CONNECTOR_CLASSES = [
    LldapConnector,
    VaultwardenConnector,
    ImmichConnector,
    NextcloudConnector,
    HomeAssistantConnector,
    JellyfinConnector,
    PaperlessConnector,
    VikunjaConnector,
    ActualConnector,
    MealieConnector,
    RoundcubeConnector,
]


class Registry:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._by_name: dict[str, Connector] = {}
        for cls in CONNECTOR_CLASSES:
            inst = cls(settings, http)
            self._by_name[inst.name] = inst

    def get(self, name: str) -> Optional[Connector]:
        return self._by_name.get(name)

    def all(self) -> list[Connector]:
        return list(self._by_name.values())

    def enabled(self) -> list[Connector]:
        """Connectors with the credentials they need.

        An unconfigured connector is reported as disabled, never quietly
        skipped — see docs/09 §4.
        """
        return [c for c in self._by_name.values() if c.configured]

    def for_profile(self, profile: str) -> list[Connector]:
        from ..models import PROFILES

        wanted: Iterable[str] = PROFILES[profile]["connectors"]
        return [c for name in wanted if (c := self._by_name.get(name)) is not None]


_registry: Optional[Registry] = None
_client: Optional[httpx.AsyncClient] = None


async def startup_registry() -> Registry:
    global _registry, _client
    settings = get_settings()
    _client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        verify=settings.verify_tls,
        follow_redirects=True,
        headers={"User-Agent": "doser/0.1 (purrbrews provisioning)"},
    )
    _registry = Registry(settings, _client)
    return _registry


async def shutdown_registry() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_registry() -> Registry:
    if _registry is None:
        raise RuntimeError("connector registry not initialised")
    return _registry
