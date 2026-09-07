from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import api, web
from .config import get_settings
from .connectors import get_registry, shutdown_registry, startup_registry
from .db import init_db
from .worker import cancel_stale_runs, reconciler_loop, worker_loop

log = logging.getLogger("doser")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if settings.dev_auth_bypass:
        log.warning(
            "DEV AUTH BYPASS IS ON — forward-auth is not enforced. "
            "Loopback only. Never set this on silo."
        )
    if settings.enrolment_signing_key == "dev-only-change-me":
        log.warning("ENROLMENT_SIGNING_KEY is still the default. Change it before real use.")

    await init_db()
    await startup_registry()
    await cancel_stale_runs()

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(worker_loop(stop), name="worker"),
        asyncio.create_task(reconciler_loop(stop), name="reconciler"),
    ]
    log.info("doser ready — domain=%s db=%s", settings.domain, settings.database_url.split("://")[0])
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await shutdown_registry()


app = FastAPI(
    title="doser — PurrBrews user provisioning",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.include_router(api.router)
app.include_router(web.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # All CSS and JS are local — no CDN — so a strict CSP costs nothing.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="3" fill="#6f4e37"/>'
    b'<path d="M4 4h8l-1.5 5.5a2.5 2.5 0 0 1-2.4 1.8h-.2a2.5 2.5 0 0 1-2.4-1.8Z" fill="#faf8f5"/>'
    b'<rect x="6" y="12" width="4" height="1.2" rx=".6" fill="#faf8f5"/></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Inline so the page stays self-contained — no CDN, strict CSP."""
    return Response(
        content=_FAVICON,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness. No auth, no data."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness: DB and lldap only.

    Deliberately NOT every connector — a down Immich must not make this service
    unready and get it restarted.
    """
    from sqlalchemy import text

    from .db import SessionLocal

    checks: dict[str, str] = {}
    ok = True
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
        ok = False

    lldap = get_registry().get("lldap")
    if lldap is None or not lldap.configured:
        checks["lldap"] = "not configured"
        ok = False
    else:
        health = await lldap.health()
        checks["lldap"] = "ok" if health.ok else f"error: {health.detail}"
        ok = ok and health.ok

    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)
