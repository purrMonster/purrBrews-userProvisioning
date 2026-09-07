from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Operator, current_operator
from .connectors import get_registry
from .db import get_session
from .engine import Engine, audit
from .ingest import avatar as avatar_ingest
from .ingest import vcard as vcard_ingest
from .models import PROFILES, Avatar, Job, Run, ServiceAccount, User, VCardImport, utcnow
from .schemas import RunCreate, UserCreate, VCardCommit, user_out
from .worker import job_completed, reconcile_once

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def _engine() -> Engine:
    return Engine(get_registry())


async def _get_user(session: AsyncSession, user_id: str) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")
    return user


# ---------------------------------------------------------------- users -----
@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if PROFILES[body.profile]["is_privileged"] and not body.confirm_privileged:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Profile {body.profile!r} grants fleet-admin access. "
            "Re-submit with confirm_privileged=true to acknowledge that.",
        )
    # Guard against an operator quietly escalating themselves.
    if PROFILES[body.profile]["is_privileged"] and body.username == op.username:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You cannot create a privileged account for yourself through this tool.",
        )

    clash = (
        await session.execute(select(User).where(func.lower(User.username) == body.username))
    ).scalar_one_or_none()
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.username!r} already exists here.")

    warnings: list[dict[str, str]] = []
    dupe = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()
    if dupe:
        warnings.append(
            {
                "code": "email_in_use",
                "detail": f"{dupe.username} already uses this address. Allowed, but check it is deliberate.",
            }
        )

    # Live check against lldap — it is the source of truth and may hold entries
    # this tool never created.
    lldap = get_registry().get("lldap")
    if lldap is not None and lldap.configured:
        try:
            if await lldap._fetch(body.username):  # noqa: SLF001
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{body.username!r} already exists in lldap. "
                    "Adopt it via reconcile rather than creating a duplicate.",
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            warnings.append({"code": "lldap_unchecked", "detail": f"Could not check lldap: {exc}"})

    user = User(
        username=body.username,
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
        display_name=body.display_name or f"{body.first_name} {body.last_name}",
        profile=body.profile,
        avatar_sha256=body.avatar_id,
        notes=body.notes,
        created_by=op.username,
        status="draft",
    )
    session.add(user)
    await audit(
        session,
        actor=op.username,
        action="user.create_draft",
        outcome="ok",
        subject_user=user.id,
        groups=op.groups,
        ip=op.ip,
        detail={"username": user.username, "profile": user.profile},
    )
    await session.commit()
    await session.refresh(user)
    return {"id": user.id, "status": user.status, "warnings": warnings}


@router.get("/users")
async def list_users(
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
    return {"users": [user_out(u) for u in rows]}


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return user_out(await _get_user(session, user_id))


@router.get("/users/check-username")
async def check_username(
    q: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    q = (q or "").strip().lower()
    if not vcard_ingest.USERNAME_RE.match(q):
        return {"ok": False, "reason": "Must start with a letter; lowercase, digits, . _ - only."}
    if q in vcard_ingest.RESERVED:
        return {"ok": False, "reason": f"{q!r} is reserved."}
    local = (
        await session.execute(select(User).where(func.lower(User.username) == q))
    ).scalar_one_or_none()
    if local:
        return {"ok": False, "reason": "Already used by another account here."}
    return {"ok": True, "reason": "Available."}


@router.delete("/users/{user_id}", status_code=204)
async def delete_draft(
    user_id: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> Response:
    user = await _get_user(session, user_id)
    if user.status not in ("draft", "failed"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only drafts and failed users can be deleted. Active accounts are "
            "disabled, not deleted — POST /users/{id}/runs with kind=disable.",
        )
    await audit(
        session, actor=op.username, action="user.delete_draft", outcome="ok",
        subject_user=user.id, groups=op.groups, ip=op.ip,
    )
    await session.delete(user)
    await session.commit()
    return Response(status_code=204)


# ----------------------------------------------------------------- runs -----
@router.post("/users/{user_id}/runs", status_code=202)
async def plan_run(
    user_id: str,
    body: RunCreate,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await _get_user(session, user_id)
    plan = await _engine().build_plan(user, body.kind)
    run = Run(
        user_id=user.id,
        kind=body.kind,
        status="awaiting_confirmation",
        plan=plan,
        requested_by=op.username,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return {"run_id": run.id, "status": run.status, "plan": plan}


@router.post("/runs/{run_id}/confirm")
async def confirm_run(
    run_id: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "No such run.")
    if run.status != "awaiting_confirmation":
        raise HTTPException(409, f"Run is {run.status}; only a pending plan can be confirmed.")
    user = await _get_user(session, run.user_id)

    ok, err = await _engine().confirm(session, user, run, op.username)
    await session.commit()
    if not ok:
        # Nothing was enqueued. The identity does not exist, so the run failed
        # cleanly rather than half-succeeding.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, err or "commit step failed")
    return {"run_id": run.id, "status": run.status}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "No such run.")
    jobs = (await session.execute(select(Job).where(Job.run_id == run_id))).scalars().all()
    cancelled = [j for j in jobs if j.status == "queued"]
    for j in cancelled:
        j.status = "skipped"
        j.finished_at = utcnow()
    applied = [j.connector for j in jobs if j.status == "succeeded"]
    run.status = "cancelled" if not applied else "partial"
    run.finished_at = utcnow()
    await session.commit()
    return {
        "run_id": run.id,
        "status": run.status,
        "cancelled": [j.connector for j in cancelled],
        "already_applied": applied,
        "note": "Already-applied steps were NOT rolled back automatically."
        if applied
        else "Nothing had been applied yet.",
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "No such run.")
    jobs = (await session.execute(select(Job).where(Job.run_id == run_id))).scalars().all()
    user = await _get_user(session, run.user_id)
    return {
        "run_id": run.id,
        "kind": run.kind,
        "status": run.status,
        "plan": run.plan,
        "user": user_out(user),
        "jobs": [
            {
                "connector": j.connector,
                "action": j.action,
                "status": j.status,
                "attempts": j.attempts,
                "max_attempts": j.max_attempts,
                "last_error": j.last_error,
                "run_after": j.run_after.isoformat() if j.run_after else None,
            }
            for j in jobs
        ],
    }


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    op: Operator = Depends(current_operator),
) -> StreamingResponse:
    """SSE progress. Reconnect-safe: state is re-read from the DB each tick."""

    async def gen():
        from .db import SessionLocal

        last: Optional[str] = None
        for _ in range(600):  # ~20 minutes at 2s
            async with SessionLocal() as s:
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
                if run is None:
                    yield "event: error\ndata: {\"detail\":\"run vanished\"}\n\n"
                    return
                jobs = (
                    (await s.execute(select(Job).where(Job.run_id == run_id))).scalars().all()
                )
                accts = (
                    (
                        await s.execute(
                            select(ServiceAccount).where(ServiceAccount.user_id == run.user_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            payload = json.dumps(
                {
                    "run": run.status,
                    "jobs": [
                        {
                            "connector": j.connector,
                            "status": j.status,
                            "attempts": j.attempts,
                            "error": j.last_error,
                        }
                        for j in jobs
                    ],
                    "services": [
                        {
                            "connector": a.connector,
                            "state": a.state,
                            "manual": a.manual_instruction,
                            "error": a.last_error,
                        }
                        for a in sorted(accts, key=lambda a: a.connector)
                    ],
                }
            )
            if payload != last:
                yield f"event: progress\ndata: {payload}\n\n"
                last = payload
            if run.status in ("succeeded", "partial", "failed", "cancelled"):
                yield f"event: done\ndata: {json.dumps({'status': run.status})}\n\n"
                return
            yield ": keepalive\n\n"  # stops Traefik reaping the connection
            try:
                await asyncio.wait_for(job_completed.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------- service accounts ---
@router.post("/users/{user_id}/services/{connector}/retry")
async def retry_service(
    user_id: str,
    connector: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    jobs = (
        (
            await session.execute(
                select(Job)
                .where(Job.user_id == user_id, Job.connector == connector)
                .order_by(Job.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    if not jobs:
        raise HTTPException(404, "No job to retry for that service.")
    job = jobs[0]
    job.status = "queued"
    job.attempts = 0
    job.run_after = datetime.now(timezone.utc)
    job.leased_until = None
    run = (await session.execute(select(Run).where(Run.id == job.run_id))).scalar_one_or_none()
    if run and run.status in ("partial", "failed"):
        run.status = "running"
    await audit(
        session, actor=op.username, action="service.retry", outcome="ok",
        subject_user=user_id, connector=connector, groups=op.groups, ip=op.ip,
    )
    await session.commit()
    return {"connector": connector, "status": "queued"}


@router.post("/users/{user_id}/services/{connector}/verify")
async def verify_service(
    user_id: str,
    connector: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await _get_user(session, user_id)
    c = get_registry().get(connector)
    if c is None:
        raise HTTPException(404, "No such connector.")
    acct = (
        await session.execute(
            select(ServiceAccount).where(
                ServiceAccount.user_id == user_id, ServiceAccount.connector == connector
            )
        )
    ).scalar_one_or_none()
    try:
        result = await c.verify(user)
    except Exception as exc:  # noqa: BLE001
        # A probe failing (bad admin creds, the app being down) is not the
        # same as the account not existing — never let it clobber a
        # known-good state or 500 the button the operator just clicked.
        detail = f"verify failed: {exc}"
        if acct:
            acct.last_error = detail
            await session.commit()
        return {
            "connector": connector,
            "state": acct.state if acct else "not_applicable",
            "detail": detail,
        }
    if acct:
        acct.state = result.state
        acct.external_id = result.external_id or acct.external_id
        acct.last_verified_at = utcnow()
        acct.last_error = result.detail if result.state == "drifted" else None
    await session.commit()
    return {"connector": connector, "state": result.state, "detail": result.detail}


@router.post("/users/{user_id}/services/{connector}/ack-manual")
async def ack_manual(
    user_id: str,
    connector: str,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    acct = (
        await session.execute(
            select(ServiceAccount).where(
                ServiceAccount.user_id == user_id, ServiceAccount.connector == connector
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise HTTPException(404, "No such service account.")
    acct.state = "provisioned"
    acct.last_verified_at = utcnow()
    # The tool cannot verify a manual step, so the operator's assertion IS the
    # record. Audit it as such.
    await audit(
        session, actor=op.username, action="service.ack_manual", outcome="ok",
        subject_user=user_id, connector=connector, groups=op.groups, ip=op.ip,
        detail={"asserted": acct.manual_instruction},
    )
    await session.commit()
    return {"connector": connector, "state": acct.state}


# -------------------------------------------------------------- avatars -----
@router.post("/avatars", status_code=201)
async def upload_avatar(
    file: UploadFile = File(...),
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        norm = avatar_ingest.normalise(raw)
    except avatar_ingest.AvatarError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    existing = (
        await session.execute(select(Avatar).where(Avatar.sha256 == norm.sha256))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Avatar(
                sha256=norm.sha256,
                mime=norm.mime,
                width=norm.width,
                height=norm.height,
                bytes_=norm.data,
                original_mime=norm.original_mime,
            )
        )
        await session.commit()
    return {
        "avatar_id": norm.sha256,
        "width": norm.width,
        "height": norm.height,
        "bytes": len(norm.data),
        "note": "Re-encoded to JPEG; EXIF and location metadata discarded.",
    }


@router.get("/avatars/{sha256}")
async def get_avatar(sha256: str, session: AsyncSession = Depends(get_session)) -> Response:
    row = (
        await session.execute(select(Avatar).where(Avatar.sha256 == sha256))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No such avatar.")
    return Response(
        content=row.bytes_,
        media_type=row.mime,
        headers={"Cache-Control": "private, immutable, max-age=31536000"},
    )


# ---------------------------------------------------------------- vcard -----
@router.post("/vcard/parse")
async def parse_vcard(
    file: UploadFile = File(...),
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    raw = await file.read()
    taken = set(
        (await session.execute(select(User.username))).scalars().all()
    )
    try:
        candidates = vcard_ingest.parse(raw, file.filename or "upload.vcf", taken)
    except vcard_ingest.VCardError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    staged = []
    for c in candidates:
        d = c.as_dict()
        d["_avatar_b64"] = c.avatar_b64
        staged.append(d)

    imp = VCardImport(
        filename=file.filename or "upload.vcf",
        uploaded_by=op.username,
        card_count=len(candidates),
        staged=staged,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(imp)
    await session.commit()
    await session.refresh(imp)
    return {
        "import_id": imp.id,
        "card_count": imp.card_count,
        "candidates": [c.as_dict() for c in candidates],
        "note": "Nothing has been created. Select the entries you want, then commit. "
        "Unselected data is deleted within 24 hours.",
    }


@router.post("/vcard/{import_id}/commit")
async def commit_vcard(
    import_id: str,
    body: VCardCommit,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    imp = (
        await session.execute(select(VCardImport).where(VCardImport.id == import_id))
    ).scalar_one_or_none()
    if imp is None:
        raise HTTPException(404, "That import has expired or does not exist.")

    by_index = {s["index"]: s for s in imp.staged}
    created, skipped = [], []
    for item in body.items:
        staged = by_index.get(item.index)
        if staged is None:
            skipped.append({"index": item.index, "reason": "not in this import"})
            continue
        username = (item.username or staged["username"]).strip().lower()
        email = item.email or staged["email"]
        if not username or not email:
            skipped.append({"index": item.index, "reason": "missing username or email"})
            continue

        avatar_id = None
        if staged.get("_avatar_b64"):
            try:
                norm = avatar_ingest.normalise(base64.b64decode(staged["_avatar_b64"]))
                if not (
                    await session.execute(select(Avatar).where(Avatar.sha256 == norm.sha256))
                ).scalar_one_or_none():
                    session.add(
                        Avatar(
                            sha256=norm.sha256, mime=norm.mime, width=norm.width,
                            height=norm.height, bytes_=norm.data,
                            original_mime=norm.original_mime,
                        )
                    )
                avatar_id = norm.sha256
            except Exception as exc:  # noqa: BLE001
                log.info("vcard avatar for index %s rejected: %s", item.index, exc)

        user = User(
            username=username,
            email=email,
            first_name=item.first_name or staged["first_name"],
            last_name=item.last_name or staged["last_name"],
            display_name=item.display_name or staged["display_name"],
            profile=item.profile,
            avatar_sha256=avatar_id,
            source="vcard",
            source_ref=imp.id,
            created_by=op.username,
            status="draft",
        )
        session.add(user)
        created.append({"index": item.index, "username": username})

    imp.resolved = True
    imp.staged = []  # drop the third-party data as soon as it is resolved
    await audit(
        session, actor=op.username, action="vcard.commit", outcome="ok",
        groups=op.groups, ip=op.ip, detail={"created": len(created), "skipped": len(skipped)},
    )
    await session.commit()
    return {"created": created, "skipped": skipped, "note": "Drafts created; each still needs its own run."}


# ----------------------------------------------------------- connectors -----
@router.get("/connectors")
async def list_connectors(op: Operator = Depends(current_operator)) -> dict[str, Any]:
    reg = get_registry()
    return {
        "connectors": [
            {
                "name": c.name,
                "label": c.label,
                "tier": c.tier,
                "required": c.required,
                "configured": c.configured,
                "base_url": c.base_url,
                "caveat": c.caveat,
            }
            for c in reg.all()
        ],
        "profiles": {
            k: {"description": v["description"], "connectors": v["connectors"],
                "privileged": v["is_privileged"]}
            for k, v in PROFILES.items()
        },
    }


@router.post("/connectors/health")
async def check_all_health(op: Operator = Depends(current_operator)) -> dict[str, Any]:
    """Probe every connector concurrently. Credentials are redacted."""
    from .config import get_settings
    from .report import build_report

    return await build_report(get_registry(), get_settings())


@router.get("/connectors/health/report.txt")
async def health_report_txt(op: Operator = Depends(current_operator)) -> Response:
    from .config import get_settings
    from .report import as_text, build_report

    settings = get_settings()
    report = await build_report(get_registry(), settings)
    stamp = report["generated_at"][:19].replace(":", "").replace("-", "")
    return Response(
        content=as_text(report),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="doser-health-{stamp}.txt"'
        },
    )


@router.get("/connectors/health/report.xml")
async def health_report_xml(op: Operator = Depends(current_operator)) -> Response:
    from .config import get_settings
    from .report import as_xml, build_report

    settings = get_settings()
    report = await build_report(get_registry(), settings)
    stamp = report["generated_at"][:19].replace(":", "").replace("-", "")
    return Response(
        content=as_xml(report),
        media_type="application/xml; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="doser-health-{stamp}.xml"'
        },
    )


@router.post("/connectors/{name}/health")
async def connector_health(name: str, op: Operator = Depends(current_operator)) -> dict[str, Any]:
    c = get_registry().get(name)
    if c is None:
        raise HTTPException(404, "No such connector.")
    result = await c.health()
    return {"name": name, "ok": result.ok, "detail": result.detail, "latency_ms": result.latency_ms}


@router.post("/reconcile")
async def trigger_reconcile(op: Operator = Depends(current_operator)) -> dict[str, Any]:
    return await reconcile_once()


@router.get("/audit")
async def get_audit(
    limit: int = 100,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from .models import AuditLog

    rows = (
        (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))))
        .scalars()
        .all()
    )
    return {
        "entries": [
            {
                "at": r.at.isoformat(),
                "actor": r.actor,
                "action": r.action,
                "outcome": r.outcome,
                "connector": r.connector,
                "subject_user": r.subject_user,
                "detail": r.detail,
            }
            for r in rows
        ]
    }
