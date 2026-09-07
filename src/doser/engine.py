"""The provisioning engine — a saga, not a for-loop.

Shape of a run:

    PLAN     every connector's plan() runs concurrently.  Read-only.  The
             assembled plan is shown to the operator and frozen on confirm.

    COMMIT   lldap runs SYNCHRONOUSLY and blocks.  If it fails, nothing is
             enqueued and the run is 'failed' — there is no identity, so
             nothing downstream would be meaningful.

    FAN-OUT  one job per remaining step, executed concurrently with per-job
             retry and backoff.  One failure does not block its siblings.

    SETTLE   succeeded / partial / failed, reported per service.

The engine never decides *what* a service needs; connectors do.  It only
decides ordering, concurrency, retries and honesty about the outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .connectors import Registry
from .connectors.base import ConnectorError, PermanentConnectorError, PlannedStep
from .models import PROFILES, AuditLog, Job, Run, ServiceAccount, User, utcnow

log = logging.getLogger(__name__)

_SECRET_KEYS = ("password", "token", "secret", "key", "authorization", "cookie")


def scrub(data: dict[str, Any]) -> dict[str, Any]:
    """Redaction backstop for the audit log.

    The primary defence is never putting a secret in an audit detail in the
    first place; this is the net underneath that.
    """
    out: dict[str, Any] = {}
    for k, v in (data or {}).items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = scrub(v)
        else:
            out[k] = v
    return out


async def audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    outcome: str,
    subject_user: Optional[str] = None,
    connector: Optional[str] = None,
    run_id: Optional[str] = None,
    groups: Optional[list[str]] = None,
    ip: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            actor_groups=",".join(groups or []),
            actor_ip=ip,
            action=action,
            subject_user=subject_user,
            connector=connector,
            run_id=run_id,
            outcome=outcome,
            detail=scrub(detail or {}),
        )
    )


def idem_key(run_id: str, connector: str, action: str, user_id: str) -> str:
    return hashlib.sha256(f"{run_id}|{connector}|{action}|{user_id}".encode()).hexdigest()


class Engine:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    # ------------------------------------------------------------------ plan
    async def build_plan(self, user: User, kind: str) -> dict[str, Any]:
        connectors = self.registry.for_profile(user.profile)
        lldap = self.registry.get("lldap")

        async def one(c):
            if not c.configured:
                return c, [
                    PlannedStep(
                        connector=c.name,
                        tier=c.tier,
                        action="none",
                        summary="DISABLED — required credential is not configured",
                        warning="This connector will be skipped. Configure it or accept the gap.",
                    )
                ]
            try:
                return c, await c.plan(user, kind)
            except Exception as exc:  # noqa: BLE001
                log.warning("plan failed for %s: %s", c.name, exc)
                return c, [
                    PlannedStep(
                        connector=c.name,
                        tier=c.tier,
                        action="none",
                        summary=f"Could not plan: {exc}",
                        warning="Planning failed — this service may be unreachable",
                    )
                ]

        results = await asyncio.gather(*(one(c) for c in connectors))

        commit_step = None
        steps: list[dict] = []
        manual: list[str] = []
        warnings: list[str] = []
        for c, planned in results:
            for s in planned:
                d = s.as_dict()
                d["label"] = c.label
                d["required"] = c.required
                if c.name == "lldap" and s.action == "create":
                    commit_step = d
                    continue
                steps.append(d)
                if s.manual_instruction:
                    manual.append(f"{c.label}: {s.manual_instruction}")
                if s.warning:
                    warnings.append(f"{c.label}: {s.warning}")

        if commit_step is None and lldap is not None:
            # disable/enable runs have no create step; keep lldap first anyway
            for d in list(steps):
                if d["connector"] == "lldap":
                    commit_step = d
                    steps.remove(d)
                    break

        return {
            "kind": kind,
            "profile": user.profile,
            "profile_description": PROFILES[user.profile]["description"],
            "commit_step": commit_step,
            "steps": steps,
            "manual_tasks": manual,
            "warnings": warnings,
            "counts": {
                "will_call": sum(1 for s in steps if s["action"] in ("create", "disable")),
                "no_action": sum(1 for s in steps if s["action"] == "none"),
                "manual": sum(1 for s in steps if s["action"] == "manual"),
            },
        }

    # --------------------------------------------------------------- confirm
    async def confirm(
        self, session: AsyncSession, user: User, run: Run, operator: str
    ) -> tuple[bool, Optional[str]]:
        """Run the commit step inline, then enqueue the rest.

        Returns (ok, error).  On failure nothing is enqueued.
        """
        plan = run.plan or {}
        commit = plan.get("commit_step")

        run.status = "running"
        run.started_at = utcnow()

        if commit:
            lldap = self.registry.get("lldap")
            if lldap is None or not lldap.configured:
                run.status = "failed"
                run.finished_at = utcnow()
                user.status = "failed"
                return False, "lldap is not configured — cannot establish an identity"
            step = PlannedStep(
                connector="lldap",
                tier=commit["tier"],
                action=commit["action"],
                summary=commit["summary"],
            )
            try:
                result = await lldap.apply(user, step)
            except Exception as exc:  # noqa: BLE001
                run.status = "failed"
                run.finished_at = utcnow()
                user.status = "failed"
                await self._record(session, user, "lldap", "failed", error=str(exc))
                await audit(
                    session,
                    actor=operator,
                    action="run.commit",
                    outcome="error",
                    subject_user=user.id,
                    connector="lldap",
                    run_id=run.id,
                    detail={"error": str(exc)},
                )
                return False, f"lldap write failed: {exc}"

            user.lldap_uid = result.external_id or user.username
            await self._record(
                session, user, "lldap", result.state, external_id=result.external_id
            )

        user.status = "provisioning"
        await self._enqueue(session, user, run, plan.get("steps", []))
        await audit(
            session,
            actor=operator,
            action=f"run.{plan.get('kind', 'provision')}",
            outcome="ok",
            subject_user=user.id,
            run_id=run.id,
            detail={"profile": user.profile, "steps": len(plan.get("steps", []))},
        )
        return True, None

    async def _enqueue(
        self, session: AsyncSession, user: User, run: Run, steps: list[dict]
    ) -> None:
        for s in steps:
            action = s["action"]
            if action == "none":
                # Nothing to call, but the state is still meaningful: tier A/B
                # means "waiting for them to show up", tier E means "n/a".
                state = "not_applicable" if s["tier"] == "none" else "pending_first_login"
                await self._record(session, user, s["connector"], state)
                continue
            if action == "manual":
                await self._record(
                    session,
                    user,
                    s["connector"],
                    "needs_manual",
                    manual=s.get("manual_instruction") or s["summary"],
                )
                continue
            session.add(
                Job(
                    run_id=run.id,
                    user_id=user.id,
                    connector=s["connector"],
                    action=action,
                    payload=s,
                    idempotency_key=idem_key(run.id, s["connector"], action, user.id),
                    max_attempts=self.registry.settings.job_max_attempts,
                )
            )
            await self._record(session, user, s["connector"], "not_started")

    async def _record(
        self,
        session: AsyncSession,
        user: User,
        connector: str,
        state: str,
        *,
        external_id: Optional[str] = None,
        external_ref: Optional[dict] = None,
        error: Optional[str] = None,
        manual: Optional[str] = None,
    ) -> ServiceAccount:
        row = (
            await session.execute(
                select(ServiceAccount).where(
                    ServiceAccount.user_id == user.id, ServiceAccount.connector == connector
                )
            )
        ).scalar_one_or_none()
        c = self.registry.get(connector)
        if row is None:
            row = ServiceAccount(
                user_id=user.id, connector=connector, tier=c.tier if c else "none", state=state
            )
            session.add(row)
        row.state = state
        row.tier = c.tier if c else row.tier
        if external_id:
            row.external_id = external_id
        if external_ref:
            row.external_ref = {**(row.external_ref or {}), **external_ref}
        row.last_error = error
        if manual:
            row.manual_instruction = manual
        if state in ("provisioned", "not_applicable", "disabled"):
            row.last_verified_at = utcnow()
        return row

    # ---------------------------------------------------------------- worker
    async def claim(self, session: AsyncSession, worker_id: str, limit: int) -> list[Job]:
        now = datetime.now(timezone.utc)
        # Return expired leases to the pool first.
        await session.execute(
            update(Job)
            .where(Job.status == "leased", Job.leased_until < now)
            .values(status="queued", leased_until=None, leased_by=None)
        )
        stmt = (
            select(Job)
            .where(Job.status == "queued", Job.run_after <= now)
            .order_by(Job.id)
            .limit(limit)
        )
        # FOR UPDATE SKIP LOCKED is what makes this correct with several
        # workers.  SQLite has no row locks and would raise on the clause, so
        # it is applied only on Postgres — where it matters.  On SQLite the
        # single worker plus the status transition below is sufficient.
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        rows = (await session.execute(stmt)).scalars().all()
        lease = now + timedelta(seconds=self.registry.settings.job_lease_seconds)
        for j in rows:
            j.status = "leased"
            j.leased_until = lease
            j.leased_by = worker_id
            j.attempts += 1
        await session.commit()
        return list(rows)

    async def execute(self, session: AsyncSession, job: Job) -> None:
        connector = self.registry.get(job.connector)
        user = (await session.execute(select(User).where(User.id == job.user_id))).scalar_one()

        if connector is None or not connector.configured:
            job.status = "skipped"
            job.finished_at = utcnow()
            await self._record(
                session, user, job.connector, "failed", error="connector not configured"
            )
            return

        step = PlannedStep(
            connector=job.connector,
            tier=(job.payload or {}).get("tier", connector.tier),
            action=job.action,
            summary=(job.payload or {}).get("summary", ""),
            manual_instruction=(job.payload or {}).get("manual_instruction"),
        )

        try:
            result = await connector.apply(user, step)
        except PermanentConnectorError as exc:
            job.status = "dead_letter"
            job.last_error = str(exc)
            job.finished_at = utcnow()
            await self._record(session, user, job.connector, "failed", error=str(exc))
            log.warning("job %s (%s) dead-lettered: %s", job.id, job.connector, exc)
            return
        except (ConnectorError, Exception) as exc:  # noqa: BLE001
            job.last_error = str(exc)
            if job.attempts >= job.max_attempts:
                job.status = "dead_letter"
                job.finished_at = utcnow()
                await self._record(session, user, job.connector, "failed", error=str(exc))
                log.warning("job %s (%s) exhausted retries: %s", job.id, job.connector, exc)
            else:
                # Exponential backoff with a 1-hour ceiling.
                delay = min(30 * (2 ** (job.attempts - 1)), 3600)
                job.status = "queued"
                job.leased_until = None
                job.leased_by = None
                job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
                log.info("job %s (%s) retrying in %ss: %s", job.id, job.connector, delay, exc)
            return

        job.status = "succeeded"
        job.finished_at = utcnow()
        job.last_error = None
        await self._record(
            session,
            user,
            job.connector,
            result.state,
            external_id=result.external_id,
            external_ref=result.external_ref,
            manual=result.message if result.state == "needs_manual" else None,
        )

    async def settle(self, session: AsyncSession, run_id: str) -> Optional[str]:
        """If every job for a run is terminal, close the run out honestly."""
        jobs = (
            (await session.execute(select(Job).where(Job.run_id == run_id))).scalars().all()
        )
        if any(j.status in ("queued", "leased") for j in jobs):
            return None
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None or run.status not in ("running",):
            return None
        user = (await session.execute(select(User).where(User.id == run.user_id))).scalar_one()
        accounts = (
            (await session.execute(select(ServiceAccount).where(ServiceAccount.user_id == user.id)))
            .scalars()
            .all()
        )

        failed_required = any(
            a.state == "failed"
            and (c := self.registry.get(a.connector)) is not None
            and c.required
            for a in accounts
        )
        any_failed = any(a.state == "failed" for a in accounts)
        any_manual = any(a.state == "needs_manual" for a in accounts)

        if failed_required or any_failed or any_manual:
            run.status = "partial"
            user.status = "partial" if (failed_required or any_failed) else "active"
        else:
            run.status = "succeeded"
            user.status = "disabled" if run.kind == "disable" else "active"
        run.finished_at = utcnow()
        return run.status
