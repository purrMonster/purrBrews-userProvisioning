"""Health report generation.

This report is *designed to leave the machine* — the whole point is that it gets
pasted into a chat or a ticket. That makes redaction a hard requirement, not a
nicety, because the things most likely to appear in a connector error message
are exactly the things that must never be shared:

* a URL with embedded basic-auth credentials
* an upstream echoing back an ``Authorization`` header
* a Postgres DSN with the password in it
* a token quoted verbatim in a 401 body

So the scrubber works two ways. It replaces every *known* secret value from the
settings by exact match, and it also pattern-matches things that merely look
like credentials. Belt and braces: the first catches your real tokens, the
second catches a secret this code does not know about — an upstream's own key
quoted in its error text, say.
"""

from __future__ import annotations

import platform
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings

REDACTED = "[REDACTED]"

#: Settings fields whose values must never appear in the report.
_SECRET_FIELDS = (
    "lldap_admin_password",
    "immich_api_key",
    "vaultwarden_admin_token",
    "nextcloud_admin_password",
    "hass_token",
    "vikunja_token",
    "mealie_token",
    "paperless_token",
    "enrolment_signing_key",
)

#: Shapes that look like credentials even when we do not know the value.
_PATTERNS = [
    # scheme://user:password@host  → keep the host, drop the credentials
    (re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api[_-]?key)\s+\S+"), r"\1 " + REDACTED),
    (re.compile(r"(?i)([\"']?(?:password|passwd|secret|token|api[_-]?key|authorization)"
                r"[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+"), r"\1" + REDACTED),
    # long opaque strings: JWTs and 32+ char keys
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"), REDACTED),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), REDACTED),
]


def scrub(text: Optional[str], settings: Settings) -> str:
    """Remove anything credential-shaped. Applied to every string in the report."""
    if not text:
        return ""
    out = str(text)

    # 1. exact known values first — most reliable
    known: list[str] = []
    for field in _SECRET_FIELDS:
        val = getattr(settings, field, None)
        if val and len(str(val)) >= 4:
            known.append(str(val))
    if settings.database_url:
        m = re.search(r"://[^:/\s]+:([^@/\s]+)@", settings.database_url)
        if m and len(m.group(1)) >= 4:
            known.append(m.group(1))
    for secret in sorted(known, key=len, reverse=True):  # longest first
        out = out.replace(secret, REDACTED)

    # 2. shape-based, for secrets this code does not know about
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def safe_url(url: Optional[str], settings: Settings) -> str:
    if not url:
        return "(not set)"
    return scrub(url, settings)


def explain(detail: str, base_url: str, settings: Settings) -> str:
    """Turn a raw socket/HTTP error into something actionable.

    ``[Errno 11001] getaddrinfo failed`` is accurate and useless. This is an
    operator tool used a handful of times a year, so the error has to say what
    to do about it — the person reading it will not remember that 11001 is
    Windows for "host not found".
    """
    d = detail or ""
    low = d.lower()
    host = base_url.split("://")[-1].split("/")[0] if base_url else "?"

    dns = ("getaddrinfo" in low or "11001" in d or "name or service not known" in low
           or "nodename nor servname" in low or "temporary failure in name resolution" in low)
    if dns:
        return (
            f"{d} — the hostname '{host}' does not resolve. Either DOSER_DOMAIN is still a "
            f"placeholder (currently '{settings.domain}'), or this service is not at that "
            f"subdomain. Set a per-service override, e.g. DOSER_<SERVICE>_URL=http://<ip>:<port>."
        )
    if "connection refused" in low or "10061" in d or "actively refused" in low:
        return f"{d} — '{host}' resolved but nothing is listening. Wrong port, or the container is down."
    if "timed out" in low or "timeout" in low:
        return f"{d} — '{host}' resolved but did not answer. Firewall, or the wrong host entirely."
    if "certificate" in low or "ssl" in low or "tls" in low:
        return (
            f"{d} — TLS verification failed for '{host}'. If this fleet uses self-signed "
            "certs on the LAN path, set DOSER_VERIFY_TLS=false for testing only."
        )
    if "401" in d or "403" in d or "unauthor" in low or "forbidden" in low:
        return f"{d} — reachable, but the credential was rejected. Check the token for this service."
    return d


async def build_report(registry: Any, settings: Settings) -> dict[str, Any]:
    """Run every connector's health check concurrently and assemble the report."""
    import asyncio

    connectors = registry.all()

    async def one(c: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": c.name,
            "label": c.label,
            "tier": c.tier,
            "required": c.required,
            "configured": c.configured,
            "base_url": safe_url(c.base_url, settings),
        }
        if not c.configured:
            entry.update(
                ok=False,
                status="not_configured",
                detail="Required credential is not set; this connector is disabled.",
                latency_ms=None,
            )
            return entry
        try:
            result = await c.health()
            detail = scrub(result.detail, settings)
            entry.update(
                ok=bool(result.ok),
                status="ok" if result.ok else "unhealthy",
                detail=detail if result.ok else explain(detail, entry["base_url"], settings),
                latency_ms=round(result.latency_ms) if result.latency_ms else None,
            )
        except Exception as exc:  # noqa: BLE001 — the report is where errors belong
            entry.update(
                ok=False,
                status="error",
                detail=explain(
                    scrub(f"{type(exc).__name__}: {exc}", settings), entry["base_url"], settings
                ),
                latency_ms=None,
            )
        return entry

    results = await asyncio.gather(*(one(c) for c in connectors))

    def version(mod: str) -> str:
        try:
            from importlib.metadata import version as v

            return v(mod)
        except Exception:  # noqa: BLE001
            return "?"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "doser_version": version("doser"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "fastapi": version("fastapi"),
            "starlette": version("starlette"),
            "pydantic": version("pydantic"),
            "sqlalchemy": version("sqlalchemy"),
            "httpx": version("httpx"),
        },
        "config": {
            # Non-sensitive settings only. No credentials, ever.
            "domain": settings.domain,
            "database_backend": settings.database_url.split("://")[0],
            "dev_auth_bypass": settings.dev_auth_bypass,
            "required_group": settings.required_group,
            "trusted_proxy_cidrs": settings.trusted_proxy_cidrs,
            "verify_tls": settings.verify_tls,
            "nextcloud_mode": settings.nextcloud_mode,
            "hass_mode": settings.hass_mode,
            "worker_concurrency": settings.worker_concurrency,
        },
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r["ok"]),
            "unhealthy": sum(1 for r in results if r["status"] in ("unhealthy", "error")),
            "not_configured": sum(1 for r in results if r["status"] == "not_configured"),
        },
        "connectors": results,
    }


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text or "", width=width) or [""]


def as_text(report: dict[str, Any]) -> str:
    L: list[str] = []
    A = L.append
    A("=" * 72)
    A("doser — connector health report")
    A(f"generated {report['generated_at']}")
    A("=" * 72)
    A("")
    A("Credentials are redacted. Safe to share.")
    A("")

    A("ENVIRONMENT")
    for k, v in report["environment"].items():
        A(f"  {k:<16} {v}")
    A("")

    A("CONFIG (non-sensitive)")
    for k, v in report["config"].items():
        A(f"  {k:<22} {v}")
    A("")

    s = report["summary"]
    A("SUMMARY")
    A(f"  {s['ok']} ok · {s['unhealthy']} unhealthy · "
      f"{s['not_configured']} not configured · {s['total']} total")
    A("")

    A("CONNECTORS")
    A("-" * 72)
    for c in report["connectors"]:
        mark = {"ok": "[ OK ]", "unhealthy": "[FAIL]", "error": "[ERR ]",
                "not_configured": "[ -- ]"}.get(c["status"], "[ ?? ]")
        bits = [f"tier={c['tier']}"]
        if c["required"]:
            bits.append("required")
        if c["latency_ms"] is not None:
            bits.append(f"{c['latency_ms']}ms")
        A(f"{mark} {c['name']:<14} {' · '.join(bits)}")
        A(f"       url:    {c['base_url']}")
        for i, line in enumerate(_wrap(c["detail"], 62)):
            A(f"       {'detail: ' if i == 0 else '        '}{line}")
        A("")
    A("=" * 72)
    A("end of report")
    return "\n".join(L)


def as_xml(report: dict[str, Any]) -> bytes:
    root = ET.Element("doserHealthReport", generatedAt=report["generated_at"])
    root.append(ET.Comment(" Credentials are redacted. Safe to share. "))

    env = ET.SubElement(root, "environment")
    for k, v in report["environment"].items():
        ET.SubElement(env, "item", name=k).text = str(v)

    cfg = ET.SubElement(root, "config")
    for k, v in report["config"].items():
        ET.SubElement(cfg, "item", name=k).text = str(v)

    ET.SubElement(root, "summary", **{k: str(v) for k, v in report["summary"].items()})

    conns = ET.SubElement(root, "connectors")
    for c in report["connectors"]:
        node = ET.SubElement(
            conns,
            "connector",
            name=c["name"],
            tier=c["tier"],
            status=c["status"],
            ok=str(c["ok"]).lower(),
            required=str(c["required"]).lower(),
            configured=str(c["configured"]).lower(),
        )
        ET.SubElement(node, "label").text = c["label"]
        ET.SubElement(node, "baseUrl").text = c["base_url"]
        ET.SubElement(node, "detail").text = c["detail"]
        if c["latency_ms"] is not None:
            ET.SubElement(node, "latencyMs").text = str(c["latency_ms"])

    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")
