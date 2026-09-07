"""Authorisation — the most security-critical module in the app.

``Remote-User`` and ``Remote-Groups`` are *just HTTP headers*.  If anything can
reach this container off the Traefik path — a stray ``ports:`` line added during
debugging, a compromised neighbour on the same docker network, an
``extra_hosts`` shortcut like the one the fleet already uses for Pi-hole — then
anyone who can reach it can set ``Remote-Groups: purrbrews_infra_admins`` and
become a fleet administrator.

So there are two independent checks, and both must pass:

1. the peer address is one of the configured trusted proxies, and
2. the forwarded groups contain the required group.

Decision D7 (LAN + tailnet only) reduces *who* can attempt this.  It does not
remove the need for the check: a LAN-only service is still trivially reachable
by anything already inside the LAN.

See docs/07-security-model.md §2.2.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass
class Operator:
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    ip: Optional[str] = None

    @property
    def groups_str(self) -> str:
        return ",".join(self.groups)


class ForbiddenSource(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _peer_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _peer_trusted(peer: Optional[str], cidrs: list[str]) -> bool:
    if peer is None:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            log.warning("ignoring malformed trusted_proxy_cidr %r", cidr)
    return False


def _split_groups(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


async def current_operator(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Operator:
    peer = _peer_ip(request)

    # --- development bypass ------------------------------------------------
    # Only ever honoured from loopback, so setting the flag on a deployed
    # instance still does not open it to the network.
    if settings.dev_auth_bypass:
        if peer not in ("127.0.0.1", "::1", "testclient"):
            log.error("dev_auth_bypass set but request came from %s — refusing", peer)
            raise ForbiddenSource("dev auth bypass is loopback-only")
        return Operator(
            username=settings.dev_operator,
            email=f"{settings.dev_operator}@{settings.domain}",
            display_name=settings.dev_operator,
            groups=[settings.required_group],
            ip=peer,
        )

    # --- check 1: the request actually came through the proxy --------------
    if not _peer_trusted(peer, settings.trusted_proxies):
        log.warning("rejected request from untrusted peer %s (forged-header attempt?)", peer)
        raise ForbiddenSource(
            "request did not arrive via the trusted proxy; forwarded auth headers "
            "are not accepted from this source"
        )

    # --- check 2: the forwarded identity carries the required group --------
    username = request.headers.get("remote-user")
    if not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no forwarded identity")

    groups = _split_groups(request.headers.get("remote-groups"))
    if settings.required_group not in groups:
        log.warning("denied %s: not a member of %s", username, settings.required_group)
        raise ForbiddenSource(f"requires membership of {settings.required_group}")

    return Operator(
        username=username,
        email=request.headers.get("remote-email"),
        display_name=request.headers.get("remote-name") or username,
        groups=groups,
        ip=peer,
    )
