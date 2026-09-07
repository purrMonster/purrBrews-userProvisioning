"""Server-rendered views.

A thin second renderer over the same service layer the JSON API uses — so a
later SPA would be a rewrite of this file only, not the app.

Design brief, restated because it drives every choice here: this is an operator
tool used three times a year. Nothing may depend on remembering how it works.
The plan preview is the most important screen, not the form.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Operator, current_operator
from .connectors import get_registry
from .db import get_session
from .engine import Engine
from .models import PROFILES, Avatar, Job, Run, User
from .ingest import avatar as avatar_ingest
from .ingest.vcard import RESERVED, USERNAME_RE

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

TIER_LABEL = {
    "source": "identity source",
    "ldap": "LDAP — appears on first login",
    "oidc": "SSO — appears on first login",
    "api": "API call",
    "manual": "needs you",
    "none": "no account model",
}
STATE_LABEL = {
    "not_started": ("queued", "wait"),
    "not_applicable": ("not applicable", "na"),
    "pending_first_login": ("awaiting first login", "wait"),
    "provisioned": ("provisioned", "ok"),
    "needs_manual": ("needs you", "manual"),
    "failed": ("failed", "bad"),
    "disabled": ("disabled", "na"),
    "drifted": ("drifted", "bad"),
}


def ctx(request: Request, op: Operator, **kw: Any) -> dict[str, Any]:
    return {
        "request": request,
        "op": op,
        "tier_label": TIER_LABEL,
        "state_label": STATE_LABEL,
        "profiles": PROFILES,
        **kw,
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    users = (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
    return TEMPLATES.TemplateResponse(request, "index.html", ctx(request, op, users=users))


@router.get("/users/new", response_class=HTMLResponse)
async def new_user_form(
    request: Request,
    op: Operator = Depends(current_operator),
):
    return TEMPLATES.TemplateResponse(request, "new_user.html", ctx(request, op, errors=None, form={}))


@router.post("/users/new", response_class=HTMLResponse)
async def new_user_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    display_name: str = Form(""),
    profile: str = Form("household"),
    confirm_privileged: Optional[str] = Form(None),
    avatar: Optional[UploadFile] = File(None),
    notes: str = Form(""),
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    form = {
        "username": username.strip().lower(),
        "email": email.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "display_name": display_name.strip(),
        "profile": profile,
        "notes": notes.strip(),
    }
    errors: list[str] = []

    if not USERNAME_RE.match(form["username"]):
        errors.append(
            "Username must start with a letter and use only lowercase letters, "
            "digits, dot, underscore or hyphen (2–32 characters)."
        )
    if form["username"] in RESERVED:
        errors.append(f"{form['username']!r} is a reserved name.")
    if "@" not in form["email"]:
        errors.append("That does not look like an email address.")
    if profile not in PROFILES:
        errors.append("Unknown profile.")
    elif PROFILES[profile]["is_privileged"] and not confirm_privileged:
        errors.append(
            "That profile grants fleet-admin access. Tick the confirmation box to proceed."
        )
    if (
        profile in PROFILES
        and PROFILES[profile]["is_privileged"]
        and form["username"] == op.username
    ):
        errors.append("You cannot grant yourself fleet-admin through this tool.")

    existing = (
        await session.execute(select(User).where(User.username == form["username"]))
    ).scalar_one_or_none()
    if existing:
        errors.append(f"{form['username']!r} already exists here.")

    avatar_id = None
    if avatar is not None and avatar.filename:
        raw = await avatar.read()
        if raw:
            try:
                norm = avatar_ingest.normalise(raw)
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
            except avatar_ingest.AvatarError as exc:
                errors.append(str(exc))

    if errors:
        return TEMPLATES.TemplateResponse(
            request, "new_user.html", ctx(request, op, errors=errors, form=form),
            status_code=400,
        )

    user = User(
        username=form["username"],
        email=form["email"],
        first_name=form["first_name"],
        last_name=form["last_name"],
        display_name=form["display_name"] or f"{form['first_name']} {form['last_name']}",
        profile=profile,
        avatar_sha256=avatar_id,
        notes=form["notes"] or None,
        created_by=op.username,
        status="draft",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return RedirectResponse(f"/users/{user.id}/plan", status_code=303)


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    user_id: str,
    request: Request,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "No such user.")
    runs = (
        (await session.execute(select(Run).where(Run.user_id == user_id).order_by(Run.created_at.desc())))
        .scalars()
        .all()
    )
    reg = get_registry()
    return TEMPLATES.TemplateResponse(request, "user_detail.html",
        ctx(request, op, user=user, runs=runs, registry={c.name: c for c in reg.all()}),
    )


@router.get("/users/{user_id}/plan", response_class=HTMLResponse)
async def plan_view(
    user_id: str,
    request: Request,
    kind: str = "provision",
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "No such user.")
    plan = await Engine(get_registry()).build_plan(user, kind)
    run = Run(
        user_id=user.id, kind=kind, status="awaiting_confirmation",
        plan=plan, requested_by=op.username,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return TEMPLATES.TemplateResponse(
        request, "plan.html", ctx(request, op, user=user, run=run, plan=plan)
    )


@router.post("/runs/{run_id}/confirm", response_class=HTMLResponse)
async def confirm_view(
    run_id: str,
    request: Request,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None or run.status != "awaiting_confirmation":
        raise HTTPException(409, "That plan is no longer pending.")
    user = (await session.execute(select(User).where(User.id == run.user_id))).scalar_one()
    ok, err = await Engine(get_registry()).confirm(session, user, run, op.username)
    await session.commit()
    if not ok:
        return TEMPLATES.TemplateResponse(
            request, "commit_failed.html",
            ctx(request, op, user=user, run=run, error=err), status_code=502,
        )
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_view(
    run_id: str,
    request: Request,
    op: Operator = Depends(current_operator),
    session: AsyncSession = Depends(get_session),
):
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "No such run.")
    user = (await session.execute(select(User).where(User.id == run.user_id))).scalar_one()
    jobs = (await session.execute(select(Job).where(Job.run_id == run_id))).scalars().all()
    reg = get_registry()
    return TEMPLATES.TemplateResponse(request, "run.html",
        ctx(request, op, run=run, user=user, jobs=jobs, registry={c.name: c for c in reg.all()}),
    )


@router.get("/connectors", response_class=HTMLResponse)
async def connectors_view(
    request: Request,
    op: Operator = Depends(current_operator),
):
    return TEMPLATES.TemplateResponse(
        request, "connectors.html", ctx(request, op, connectors=get_registry().all())
    )
