# 01 — Architecture

## 1. Component overview

```
                        ┌─────────────────────────────────────┐
   operator (barista) ──┤ sieve: Traefik → Authelia forward-auth│
                        └───────────────┬─────────────────────┘
                                        │ requires group:purrbrews_infra_admins
                                        ▼
        ┌───────────────────────────────────────────────────────────┐
        │  silo: doser (FastAPI)                                    │
        │                                                           │
        │  ┌────────────┐  ┌───────────────┐  ┌──────────────────┐  │
        │  │ Web layer  │  │ Provisioning  │  │ Connector        │  │
        │  │ Jinja+HTMX │─▶│ engine (saga) │─▶│ registry         │  │
        │  │ + SSE      │  │ + job queue   │  │ (one per service)│  │
        │  └────────────┘  └───────┬───────┘  └────────┬─────────┘  │
        │                          │                   │            │
        │                  ┌───────▼────────┐          │            │
        │                  │ Postgres       │          │            │
        │                  │ (doser)        │          │            │
        │                  └────────────────┘          │            │
        │                  ┌────────────────┐          │            │
        │                  │ Reconciler     │──────────┘            │
        │                  │ (periodic)     │                       │
        │                  └────────────────┘                       │
        └──────────────────────────┬────────────────────────────────┘
                                   │
      ┌────────────────────────────┼────────────────────────────────┐
      ▼                            ▼                                ▼
 sieve: lldap              percolator: HA,            mochaPot: Jellyfin,
 (SOURCE OF TRUTH)         Nextcloud, Paperless       Immich, Vikunja, n8n,
 GraphQL + LDAP            cellar: Vaultwarden        Mealie, FreshRSS,
                                                      Actual, Stirling
```

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Best library coverage for every input this tool takes: LDAP (`bonsai`/`ldap3`), vCard (`vobject`), images (`Pillow`), and REST clients for every target |
| Web framework | FastAPI | Async (essential — the engine fans out to ~10 network targets), Pydantic validation on the way in, auto-generated OpenAPI for free |
| Templating | Jinja2 + HTMX + SSE | **No JS build toolchain.** See §2.1 |
| DB | Postgres 16 (dedicated instance) | Matches the fleet's "per-app DB isolation, no shared instances" rule. `SELECT ... FOR UPDATE SKIP LOCKED` gives a correct job queue with no extra broker |
| Job execution | In-process `asyncio` worker + Postgres-backed queue | At household scale a separate Celery/Redis is unjustified weight on an 8 GB node |
| LDAP client | `bonsai` (async) for reads, lldap GraphQL over `httpx` for writes | lldap's GraphQL API exposes things plain LDAP writes do not (see doc 02) |
| Migrations | Alembic | |
| Packaging | Single Docker image, multi-stage build | Built on roastery/americano, pushed, pulled on silo — no live coding on fleet nodes |

### 2.1 Why HTMX and not React

Three constraints push hard in one direction:

- **No live coding on fleet nodes.** A React SPA means a Node build step. That's
  a second toolchain to pin, build off-node, and ship — for a form with about
  fifteen fields.
- **8 GB node.** Server-rendered HTML costs nothing extra at runtime.
- **The one genuinely dynamic requirement is live per-service progress.** That is
  exactly what Server-Sent Events + `hx-swap` on a status grid does natively,
  in about thirty lines, with no client state to desynchronise.

The honest counter-argument: if the "full-scale ecosystem" ambition from
2026-08-30 grows into a real multi-screen admin console, HTMX starts to strain
and you'll wish you had a component model. The mitigation is that the API layer
(doc 04) is a clean JSON API with the HTML views as a thin second renderer over
the same service layer — so a later SPA is a rewrite of the view layer only, not
the app.

## 3. Node placement — recommendation: **silo**

The question was left to this plan. Here is the reasoning, including what silo
costs you.

### Candidates

**sieve (8 GB, gateway).** Co-located with lldap and Authelia — the shortest
possible path to the identity core, no cross-node LDAP traffic, no extra
firewall surface.
*Against:* sieve is the node that must never go down. It already carries
Pi-hole (which now also owns household DHCP), Traefik, Authelia, Redis,
Headscale, Cloudflare Tunnel and lldap on 8 GB. Adding an admin app plus a
Postgres to the single node whose failure takes out DNS, DHCP, auth and external
routing simultaneously is the wrong risk trade for a tool used a handful of
times a year. **Rejected on blast radius.**

**percolator (16 GB, core stateful).** Most RAM headroom, already runs four
Postgres instances so a fifth is a well-trodden pattern, and it physically hosts
three of the five hardest provisioning targets (Home Assistant, Nextcloud,
Paperless-ngx).
*Against:* percolator's role is *user-facing stateful apps*. Putting an
operator tool with fleet-wide credentials there mixes two very different trust
domains on one node — the node whose data you least want to lose becomes the
node holding admin tokens for everything else. The RAM argument is also weaker
than it looks: this service's steady-state footprint is small. **Rejected on
role separation.**

**silo (8 GB, security / QoL / fleet ops).** ✅ **Recommended.**

- **It is already the admin plane.** Komodo, NetAlertX, Diun, Scrutiny,
  Speedtest Tracker and Homepage all live here. This tool is the same class of
  thing: an operator surface that reaches out to other nodes.
- **Cross-node egress is already normal there.** Komodo alone means silo already
  holds credentials that reach every other node. Adding provisioning credentials
  widens an existing trust boundary rather than opening a new one — a meaningful
  difference for both firewalling and for reasoning about compromise.
- **Blast radius is right.** If this service wedges, hangs, or eats memory, it
  takes down monitoring and dashboards. It does not take down DNS, auth, the
  tunnel, or anyone's photos.
- **Failure independence from its own dependency.** Running on silo means a
  sieve outage makes the tool *unusable* (no lldap, no Authelia) rather than
  *half-usable and dangerous*. Being unable to start a provisioning run during
  an identity outage is the correct behaviour.

**What silo costs you, stated plainly:**

| Cost | Mitigation |
|---|---|
| 8 GB node gains ~250 MB app + ~256 MB Postgres | Cap Postgres (`shared_buffers=64MB`, `max_connections=20`). Realistic total under 500 MB. If silo is tight after Komodo, the fallback is SQLite + a single worker — genuinely adequate at ≤ 20 users, at the cost of departing from the fleet's Postgres-everywhere pattern |
| Every lldap call is a cross-node hop | Irrelevant at this volume (single-digit calls per provisioning run). LAN, not WAN |
| One more node holding high-value secrets | See doc 07 §4 — this is the real cost. Argument is that silo already holds Komodo's, so the marginal increase is smaller than it would be on cellar or percolator |

## 4. Request flow (happy path)

1. Operator hits `https://provision.${DOMAIN}` → sieve's Traefik → Authelia
   forward-auth → requires `group:purrbrews_infra_admins`.
2. Traefik proxies to silo. Authelia injects `Remote-User`, `Remote-Groups`,
   `Remote-Email`, `Remote-Name`.
3. **The app re-verifies authorisation itself** (doc 07 §2) — it does not trust
   the header alone, and it refuses requests whose source IP is not the proxy.
4. Operator fills the form, or drops a `.vcf` → parsed server-side into a
   pre-filled, editable form (doc 06). Nothing is committed from a vCard without
   human review.
5. Submit → **plan phase**: engine builds an ordered list of connector actions
   and shows a dry-run preview ("will create in lldap; will add to groups X, Y;
   will call Immich, Vaultwarden, Home Assistant; 4 services need no action; 2
   services need a manual step from you").
6. Operator confirms → **apply phase**:
   - Step 1 (synchronous, blocking): create in lldap. If this fails, nothing
     else runs and the request returns an error. This is the commit point.
   - Step 2..n (asynchronous, per-connector jobs): each connector runs
     independently with its own retry policy. Failures do not block siblings.
7. Browser holds an SSE connection to `/api/runs/{id}/events`; the status grid
   updates per service as jobs settle.
8. On completion, the run ends in `succeeded`, `partial`, or `failed`, and the
   UI shows what needs manual attention.
9. Operator sends the generated one-time enrolment link to the person out of
   band. **No password is ever set or stored by this tool** (doc 07 §3).

## 5. What the tool deliberately does *not* do

- **It does not set passwords.** lldap accounts are created with a
  high-entropy random password that is never displayed; the user sets their own
  via an enrolment flow. This keeps the tool out of the credential business
  entirely and preserves Vaultwarden's zero-knowledge property.
- **It does not proxy logins.** It never holds a user's session.
- **It does not delete.** Offboarding disables and archives; hard deletion is a
  separate, explicitly-confirmed, per-service operation (doc 08 §7).
- **It does not manage containers.** That is Komodo.

## 6. Failure posture

| Failure | Behaviour |
|---|---|
| lldap unreachable | Run refuses to start. Clear error. Nothing partial. |
| Authelia down | Nobody can reach the tool. Correct. |
| One connector's target is down | That service's job retries with backoff, then dead-letters. Run ends `partial`. Reconciler retries later. |
| The tool itself crashes mid-run | Jobs are in Postgres with a lease; on restart the worker reclaims expired leases and resumes. |
| A connector partially applied then failed | Compensation action runs (doc 08 §5). Where a service has no safe compensation, the job is marked `needs_manual` rather than guessed at. |
