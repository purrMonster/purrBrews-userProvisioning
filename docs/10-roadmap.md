# 10 — Roadmap

Six phases. Each has an exit criterion that is a **demonstrable behaviour**, not
"the code is written." Effort is expressed in relative shape rather than hours,
since this is your own time and pace.

The ordering is deliberate: **the phase that delivers the most value is Phase 1,
and it involves writing almost no connectors.** Most of the fleet is reachable
through lldap and Authelia alone.

---

## Phase 0 — Decide, verify, and stop guessing

**No code.** This phase exists because most of the risk in this project is in
assumptions, not implementation.

**All ten policy decisions are settled** (doc 00, 2026-09-06). What remains in
Phase 0 is verification and setup, not deliberation:

- [x] ~~Home Assistant Option A vs B~~ → **D5: HACS OIDC + break-glass.** HA is
      tier B; the connector is deleted from the plan.
- [x] ~~Silo secrets pattern~~ → **D6: plain `local.env`, 0600.** Accepted risk.
- [x] ~~Tunnel-exposed or LAN-only~~ → **D7: LAN + tailnet only.**
- [x] ~~Homepage vs Homarr, Stirling-PDF, ntfy, shares, profiles, scope, name~~
      → D8–D13.
- [ ] **Create and test the HA break-glass owner account** (doc 02 §9) — before
      installing the OIDC component, not after. Verify it works with Authelia
      deliberately stopped.
- [ ] Read the HACS OIDC component's source, then test it on a **throwaway HA
      instance**, never the live one.
- [ ] Confirm Authelia is on **`two_factor`** for this route before routing it.
- [ ] Confirm `provision.${DOMAIN}` has **no** Cloudflare Zero Trust hostname
      route, and verify from off-LAN that it does not answer (doc 09 §3.1).
- [ ] Create the **`svc-doser` service accounts** on lldap, Vaultwarden, Immich
      and n8n — dedicated, not `barista`'s own credentials (doc 07 §4.2).
- [ ] Work through **doc 12**, the verification checklist: curl each target's
      API against your actual running containers and pinned tags. Record the real
      request/response shapes. Sections 3 (HA) and 10.6 (ntfy) are now moot.
- [ ] Investigate whether **Samba on cellar can bind to lldap** (D9). If yes,
      shares become tier A for free. If no, they stay deferred.
- [ ] Measure **silo's actual RAM headroom** with Komodo running (doc 09 §8).

**Exit criterion:** doc 12 has no unchecked ⚠️ boxes for anything in Phase 1–3;
the HA break-glass account is created and tested; and `provision.${DOMAIN}` is
confirmed unreachable from outside the LAN.

**This phase will find things that change the plan.** That's the point of doing
it first — and it is now purely empirical work, since the judgement calls are
already made.

---

## Phase 1 — Identity core

The smallest thing that is genuinely useful.

- FastAPI skeleton, config, Postgres + Alembic.
- Forward-auth verification **with the peer-IP check and the forged-header test
  written first** (doc 07 §9.4).
- lldap connector: create, verify, disable, group membership, avatar.
- Manual-entry form + avatar upload pipeline (doc 06 §3).
- Plan preview screen (doc 05 §2.4) — even though the plan is nearly all "no
  action needed" at this stage. Build the habit and the component now.
- Audit log with the redaction scrubber.
- Deploy to silo behind Authelia.

**Exit criterion:** you can create `penguin` and `bubbles` through the web page,
with avatars, in the right groups, and then log in to **Nextcloud, Jellyfin,
Mealie, Paperless-ngx, Vikunja and Actual Budget** as those users — with zero
per-service connectors written, because tiers A and B do the work.

This is the phase that closes out the 2026-08-30 intent ("create `penguin`/
`bubbles` via lldap's API rather than by hand in the UI"). Everything after this
is refinement.

---

## Phase 2 — Engine and honesty

Turn the straight-line implementation into the saga.

- Postgres job queue with `SKIP LOCKED`, leases, backoff, dead-letter.
- Connector `Protocol` + tier base classes + the contract test suite.
- Register every tier-A/B/E service as a config-driven connector so the plan
  preview shows all ~30 and the status grid is complete.
- `verify()` for tier A/B — the `pending_first_login` → `provisioned` transition.
- SSE live progress.
- Reconciler, including the **unmanaged-account discovery pass** — run it once
  against the live fleet to inventory what already exists.

**Exit criterion:** kill the worker mid-run, restart it, and the end state is
exactly one account per service with an honest run report (doc 08 §9). The
reconciler produces a correct inventory of pre-existing accounts.

---

## Phase 3 — The connectors that matter

Roughly in value order.

**Four connectors, down from seven** — D5, D9, D11 and D12 removed the rest.

| Connector | Why this order |
|---|---|
| **Vaultwarden** | Highest-value service, invite-only API, clean semantics. In both profiles. |
| **Immich** | Real admin API; pre-creation genuinely buys you quota + storage label |
| **n8n** | Clean `POST /api/v1/users` returning an `inviteAcceptUrl` — no SMTP needed. `infra_admin` profile only |
| **Headscale** | Only for users with devices joining the tailnet. `infra_admin` profile only |
| **Komodo (tier D)** | Manual-task emission only, ~20 lines. `infra_admin` only |

~~Home Assistant~~ — zero work under D5. ~~ntfy~~ — removed by D12.
~~FreshRSS~~ — zero work if `http_auth` auto-creates (verify: doc 12 §9.10).

**Exit criterion:** a single run provisions a real new user end-to-end across all
of them, the status grid is accurate, and one deliberately-broken target produces
a `partial` run that is clear about what to do next.

---

## Phase 4 — vCard and bulk

- vCard parser with the full 12-case fixture corpus (doc 06 §4) — including the
  assertion that a `PHOTO;VALUE=uri` is **never fetched**.
- Import review screen with per-row issue badges and opt-in selection.
- Staging with 24 h expiry and the nightly hard-delete sweeper.

**Exit criterion:** import a real 50-contact `.vcf` exported from a phone, review
it, select two, and provision them — with the SSRF and RTL-override fixtures
passing in CI.

Deliberately after Phase 3: vCard import is the most-requested feature and the
least load-bearing. It is a convenience on top of a form that already works.

---

## Phase 5 — Lifecycle

- Update runs (name/email/avatar propagation to lldap and to connectors that
  copied the values).
- Disable / enable runs, per-connector `disable()`.
- Offboarding checklist generation (doc 08 §7).
- Enrolment-link flow: generation, single use, 72 h TTL, audit on redemption.
- Drift detection with per-field diffs and one-click repair.
- ntfy alerting to ristretto.

**Exit criterion:** offboard a test user; every service either disables cleanly
or produces an explicit, actionable manual item; nothing is silently missed.

---

## Phase 6 — Optional, only if wanted

- SCIM server interface (only if something in the fleet ever speaks it).
- Prometheus `/metrics` (only if the fleet gains a scraper — monitoring is
  currently Gatus + ntfy).
- Group and permission management as a first-class screen.
- Self-service profile updates for `penguin`/`bubbles`.
- A React rewrite of the view layer, if the console genuinely outgrows HTMX
  (doc 01 §2.1).

**None of Phase 6 should be started until Phases 1–5 have been in real use for a
while.** The most likely outcome is that you never want any of it — which is
information worth having before building it.

---

## Sequencing notes

**What can be parallelised:** nothing meaningfully, given a single developer.
The phases are genuinely dependency-ordered — the engine (2) needs the lldap
connector (1) to have something to run, and the connectors (3) need the contract
(2).

**What can be cut:** Phase 4 entirely, if vCard turns out to be a novelty. Phase
6 entirely. Phase 3 already shrank from seven connectors to four via the doc 00
decisions — most of the work that was going to be in this project no longer
exists.

**The honest minimum viable version** is Phase 1 plus the Vaultwarden connector.
That covers the actual stated need from 2026-08-30 — creating `penguin` and
`bubbles` properly, via API, with a UI — and everything past it is building the
"ecosystem" ambition on a foundation that already works.

**Biggest risk to the schedule:** Phase 0 finding that two or three of the API
assumptions in doc 02 are wrong for your pinned image versions. That is not a
failure of the plan; it is the reason Phase 0 exists.

**Second-biggest risk, now that D5 is chosen:** the HACS OIDC component not
working cleanly against your Authelia version, or breaking on a later HA upgrade.
The break-glass account is the mitigation, which is why creating and *testing* it
is the first unchecked box in Phase 0 rather than a footnote.
