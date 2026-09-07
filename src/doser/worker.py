"""Background worker and reconciler.

The worker is an in-process asyncio loop rather than Celery: at household scale
a separate broker is unjustified weight on an 8 GB node, and Postgres (or
SQLite) already gives a correct at-least-once queue.

That "at-least-once" is why every connector's ``apply()`` has to read before it
writes — a lease expiring during a slow API call *guarantees* a second attempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import get_settings
from .connectors import get_registry
from .db import SessionLocal
from .engine import Engine
from .models import Job, Run, ServiceAccount, User, VCardImport, utcnow

log = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

#: Set by the API layer so SSE streams can wake immediately instead of polling.
job_completed = asyncio.Event()


async def worker_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    engine = Engine(get_registry())
    log.info("worker %s started (concurrency=%s)", WORKER_ID, settings.worker_concurrency)

    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                jobs = await engine.claim(session, WORKER_ID, settings.worker_concurrency)

            if not jobs:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            async def run_one(job_id: int) -> None:
                # Each job gets its own session so one failure cannot roll back
                # a sibling's progress.
                async with SessionLocal() as s:
                    job = (
                        await s.execute(select(Job).where(Job.id == job_id))
                    ).scalar_one()
                    await engine.execute(s, job)
                    await s.commit()

            await asyncio.gather(*(run_one(j.id) for j in jobs), return_exceptions=True)

            run_ids = {j.run_id for j in jobs}
            async with SessionLocal() as s:
                for rid in run_ids:
                    status = await engine.settle(s, rid)
                    if status:
                        log.info("run %s settled: %s", rid, status)
                await s.commit()

            job_completed.set()
            job_completed.clear()

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("worker iteration failed; continuing")
            await asyncio.sleep(5)

    log.info("worker %s stopped", WORKER_ID)


async def reconcile_once() -> dict[str, int]:
    """Ask every connector whether reality still matches what we think.

    Drift is *reported*, not silently repaired.  An account that vanished from
    Immich might be a bug — or might be you deleting it deliberately last
    Tuesday.  A tool that quietly recreates it is fighting its operator.
    """
    registry = get_registry()
    stats = {"checked": 0, "promoted": 0, "drifted": 0, "unchanged": 0}

    async with SessionLocal() as session:
        users = (
            (await session.execute(select(User).where(User.status.in_(("active", "partial")))))
            .scalars()
            .all()
        )
        for user in users:
            accounts = (
                (
                    await session.execute(
                        select(ServiceAccount).where(ServiceAccount.user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            for acct in accounts:
                connector = registry.get(acct.connector)
                if connector is None or not connector.configured:
                    continue
                if acct.state in ("not_applicable", "needs_manual", "disabled"):
                    continue
                stats["checked"] += 1
                try:
                    result = await connector.verify(user)
                except Exception as exc:  # noqa: BLE001
                    acct.last_error = f"verify failed: {exc}"
                    continue

                acct.last_verified_at = utcnow()
                if acct.state == "pending_first_login" and result.state == "provisioned":
                    acct.state = "provisioned"
                    acct.external_id = result.external_id or acct.external_id
                    stats["promoted"] += 1
                elif acct.state == "provisioned" and result.state == "drifted":
                    acct.state = "drifted"
                    acct.last_error = result.detail
                    stats["drifted"] += 1
                    log.warning(
                        "drift: %s/%s — %s", user.username, acct.connector, result.detail
                    )
                else:
                    stats["unchanged"] += 1
        await session.commit()
    return stats


async def reconciler_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    # Don't reconcile the instant the app boots; let it settle.
    try:
        await asyncio.wait_for(stop.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass

    while not stop.is_set():
        try:
            stats = await reconcile_once()
            log.info("reconcile: %s", stats)
            await _sweep_expired_imports()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("reconcile failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.reconcile_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _sweep_expired_imports() -> None:
    """Hard-delete unresolved vCard imports.

    These hold third-party personal data lifted out of someone's address book —
    the most sensitive thing this tool touches. They do not linger.
    """
    cutoff = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(VCardImport).where(
                        VCardImport.resolved.is_(False), VCardImport.expires_at < cutoff
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await session.delete(row)
        if rows:
            log.info("swept %d expired vcard import(s)", len(rows))
        await session.commit()


async def cancel_stale_runs() -> None:
    """On boot, any run left 'running' by a crash gets re-settled."""
    async with SessionLocal() as session:
        stale = (
            (
                await session.execute(
                    select(Run).where(
                        Run.status == "running",
                        Run.started_at < datetime.now(timezone.utc) - timedelta(hours=6),
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in stale:
            run.status = "partial"
            run.finished_at = utcnow()
            log.warning("run %s was stale on boot; marked partial", run.id)
        await session.commit()
