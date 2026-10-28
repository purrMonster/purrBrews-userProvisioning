---
project: purrBrews-userProvisioning
status: parked
status_reason: One commit (2026-09-07) of plans plus untested code; no work since, and the fleet it was written for has changed
resume_trigger: The owner decides to give another person (a guest or family) accounts across the fleet
last_activity: 2026-09-07
last_reviewed: 2026-10-29
repo: https://github.com/purrMonster/purrBrews-userProvisioning
related: ["[[purrbrews-containers]]", "[[purrbrews]]"]
tags: [project]
---

# purrBrews-userProvisioning

## What it is
`doser`: a single admin page that takes a person's details (or a `.vcf` card and a picture) and gives them a working identity across the fleet, plus disable, re-enable, update and offboard. Core idea: lldap is the single source of truth and the commit point; every other service is reconciled from it by tier (LDAP/OIDC on first login, group membership only, API call, or a printed manual step), as a saga with compensation, not a fan-out loop. Stack: Python, FastAPI, SQLAlchemy, Jinja2, vobject, Pillow.

## Current state
| Area | State | Evidence |
|---|---|---|
| Plan | 9 design docs: decisions, architecture, service matrix, data model, API, frontend, vCard/avatar, security, engine | [docs/](docs/) |
| Code | ~4,900 lines in `src/doser/` (engine, connectors, web UI); **never booted**: the sandbox it was written in had no PyPI | [RUNNING.md](RUNNING.md) §5 |
| Tests / CI | 67 tests written; only the avatar pipeline (9 cases) and vCard sanitising were actually run. No CI | [tests/](tests/), [RUNNING.md](RUNNING.md) §5 |
| Release | None (no tags or images) | `git tag` is empty |
| Deployment | Not deployed (the plan targets silo, a node that was never built) | [RUNNING.md](RUNNING.md) §7 |

## Open points
- The service matrix names apps the fleet no longer runs (e.g. Jellyfin) and misses newer ones (meowGram, purrCafe, pourOver, persianPerch).
- The deploy target (silo) doesn't exist; it would go on percolator next to LLDAP and Authelia.
- First boot will need small fixes; [RUNNING.md](RUNNING.md) §6 lists where to look.

## Branches
- None besides `main`.

## Key decisions
- 2026-09-06 · lldap is the single source of truth; provisioning is "write once, then reconcile" · [README](README.md) §2
- 2026-09-06 · Every service classified into five integration tiers · [docs/02-service-integration-matrix.md](docs/02-service-integration-matrix.md)
- 2026-09-06 · Avatars re-encoded with all EXIF dropped, so no home GPS leaks · [RUNNING.md](RUNNING.md) §5

## How to resume
1. Orient: [README.md](README.md), [docs/00-decisions.md](docs/00-decisions.md), [RUNNING.md](RUNNING.md).
2. Refresh the service matrix against purrbrews-containers' current stacks.
3. Boot it locally with `DOSER_DEV_AUTH_BYPASS` (RUNNING.md §1), fix what breaks, get the 67 tests green, then add CI.
4. Only then plan a percolator stack in purrbrews-containers.

## Decisions to remake
- Deploy target: silo → percolator.
- Scope: with only two people on the fleet, manual LLDAP + Authelia group edits may be enough; the full saga engine earns its keep only with more users.

## Questions for Roastmaster
- Keep parked, or archive like purrBrews-infra?

## Card history
- 2026-10-29 · First card, written by Claude from the repo at `24f0e09`.
