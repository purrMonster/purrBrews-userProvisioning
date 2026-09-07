# 04 — API Specification

FastAPI. All routes under `/api/v1`. HTML views are a separate router (`/`)
rendering over the same service layer — the JSON API is not an afterthought
bolted on later, it is the primary interface and the HTML is a second renderer.

Auth on every route: Authelia forward-auth headers, re-verified in-process
(doc 07 §2). No API keys for humans. A single optional machine token exists for
the reconciler's internal calls and for future automation.

---

## 1. Conventions

- Errors: RFC 9457 `application/problem+json`.
- All mutating routes accept an `Idempotency-Key` header; replaying a key within
  24 h returns the original response rather than acting twice.
- Pagination: cursor-based (`?after=<id>&limit=`), never offset.
- Times: ISO 8601 with offset.
- The API never returns a secret, token, or password. `inviteAcceptUrl` values
  from n8n and enrolment links are the one exception, and they are one-time,
  short-TTL, and audit-logged on every read.

---

## 2. Users

### `POST /api/v1/users` — create a draft

Creates a `draft` user. **Does not provision anything.**

```jsonc
// request
{
  "username": "bubbles",
  "email": "bubbles@example.invalid",
  "first_name": "…",
  "last_name": "…",
  "display_name": "…",           // optional; defaults to "First Last"
  "avatar_id": "sha256:ab12…",   // optional; from POST /avatars
  "groups": ["purrbrews_users"], // lldap groups
  "notes": "…"
}
```

```jsonc
// 201
{ "id": "…uuid…", "status": "draft", "warnings": [
    { "code": "email_in_use", "detail": "…", "severity": "warning" }
] }
```

Validation, all server-side:

| Field | Rule |
|---|---|
| `username` | `^[a-z][a-z0-9._-]{1,31}$`. Lowercase only. Rejected if it collides with an existing lldap uid, a reserved name (`admin`, `root`, `barista`, `postgres`, `nobody`, `_apt`, …), or a system-ish prefix. Case-insensitive check. |
| `email` | RFC 5322 shape + deliverable-domain sanity. **No MX lookup** — that leaks the address to DNS and this fleet runs its own resolver, so it's an unnecessary side channel. |
| `first_name` / `last_name` | 1–64 chars, Unicode allowed, control chars and RTL-override stripped |
| `groups` | Must all exist in lldap; membership in `purrbrews_infra_admins` requires an extra confirmation flag |

**Username collision is checked against lldap live, not just the local DB**, since
lldap is the source of truth and may have entries this tool never created.

### `GET /api/v1/users` / `GET /api/v1/users/{id}`

Returns the user plus its `service_accounts` array — the per-service status grid
the UI renders.

### `PATCH /api/v1/users/{id}`

Updates the draft, or for an active user queues an `update` run (name/email/avatar
changes must propagate to lldap and to any connector that copied them).

### `DELETE /api/v1/users/{id}`

Only valid on `draft` and `failed`. Active users are *disabled*, not deleted.
Returns `409` otherwise with a pointer to the disable endpoint. This is
deliberate friction.

---

## 3. Provisioning runs

### `POST /api/v1/users/{id}/runs` — plan a run

```jsonc
{ "kind": "provision", "connectors": null }   // null = all enabled
```

Returns `202` with a **plan**, and the run in `awaiting_confirmation`:

```jsonc
{
  "run_id": "…",
  "status": "awaiting_confirmation",
  "plan": {
    "commit_step": { "connector": "lldap", "action": "create_user",
                     "summary": "Create uid=bubbles, add to purrbrews_users" },
    "steps": [
      { "connector": "immich",      "tier": "api",    "action": "create",
        "summary": "POST /api/admin/users, quota 50 GB, storage label 'bubbles'",
        "reversible": true },
      { "connector": "vaultwarden", "tier": "api",    "action": "invite",
        "summary": "POST /admin/invite — user sets own master password",
        "reversible": true },
      { "connector": "n8n",         "tier": "api",    "action": "invite",
        "summary": "POST /api/v1/users, capture inviteAcceptUrl",
        "reversible": true },
      { "connector": "nextcloud",   "tier": "ldap",   "action": "none",
        "summary": "No action — account appears on first LDAP login",
        "reversible": true },
      { "connector": "jellyfin",    "tier": "ldap",   "action": "none",
        "summary": "No action — LDAP-Auth plugin. ⚠ library grants are NOT automatic",
        "reversible": true },
      { "connector": "komodo",      "tier": "manual", "action": "manual",
        "summary": "MANUAL: after first OIDC login, Enable User + assign permissions",
        "reversible": true },
      { "connector": "pihole",      "tier": "none",   "action": "none",
        "summary": "No user model — Authelia group access only",
        "reversible": true }
    ],
    "manual_tasks": [ "…" ],
    "warnings": [ "Home Assistant connector disabled — see docs/02 §9" ]
  }
}
```

**The plan is the product.** An operator should be able to read it and know
exactly what is about to happen to seven different systems, including what will
*not* happen. This screen is where mistakes get caught.

### `POST /api/v1/runs/{run_id}/confirm`

Freezes the plan, writes to lldap **synchronously**, enqueues the rest. Returns
`200` with `status: running` or `400` if the lldap write failed (in which case
nothing was enqueued and the user is `failed`).

### `POST /api/v1/runs/{run_id}/cancel`

Valid while `awaiting_confirmation`, or during `running` — cancels *queued*
jobs only. Already-applied steps are not rolled back automatically; the response
tells you which ones stand and offers a compensating run.

### `GET /api/v1/runs/{run_id}`

### `GET /api/v1/runs/{run_id}/events` — SSE

```
event: job
data: {"connector":"immich","status":"succeeded","external_id":"…","at":"…"}

event: job
data: {"connector":"vaultwarden","status":"failed","error":"connection refused","attempts":2,"next_retry_at":"…"}

event: run
data: {"status":"partial","succeeded":5,"failed":1,"manual":1,"not_applicable":14}
```

Heartbeat comment every 15 s so Traefik doesn't reap the connection. Client
reconnects with `Last-Event-ID`; the server replays from the job log, so a
dropped connection never loses progress.

---

## 4. Lifecycle

| Route | Effect |
|---|---|
| `POST /api/v1/users/{id}/disable` | Queues a `disable` run: lldap account disabled/locked, per-connector disable where supported, sessions revoked where possible |
| `POST /api/v1/users/{id}/enable` | Reverse |
| `POST /api/v1/users/{id}/offboard` | `disable` + emits the full manual checklist (data ownership transfer, shared Vikunja projects, Nextcloud shares, Immich albums, Actual budget files) |
| `POST /api/v1/users/{id}/enrolment-link` | Generates a one-time, 72 h enrolment token. Audit-logged. Returns the URL exactly once. |

**On offboarding and data.** The tool never deletes a departing user's data.
It produces a checklist, because "who owns the shared photo album now" is a
human decision, not an API call. Doc 08 §7.

---

## 5. Service accounts

| Route | Effect |
|---|---|
| `POST /api/v1/users/{id}/services/{connector}/retry` | Requeue a failed/dead-lettered job |
| `POST /api/v1/users/{id}/services/{connector}/verify` | Run the connector's `verify()` now |
| `POST /api/v1/users/{id}/services/{connector}/ack-manual` | Operator marks a tier-D manual task done. Audit-logged with the actor. |
| `POST /api/v1/users/{id}/services/{connector}/skip` | Explicitly skip, with a required reason |

---

## 6. vCard import

### `POST /api/v1/vcard/parse` — multipart, field `file`

Parses server-side, returns staged candidates. **Never creates anything.**

```jsonc
{
  "import_id": "…",
  "card_count": 3,
  "candidates": [
    {
      "index": 0,
      "suggested": { "username":"jdoe","email":"…","first_name":"Jane",
                     "last_name":"Doe","display_name":"Jane Doe",
                     "avatar_id":"sha256:…" },
      "raw": { "FN":"Jane Doe","N":"Doe;Jane;;;","EMAIL":["…","…"],
               "NICKNAME":"jd","ORG":"…","TEL":["…"] },
      "issues": [
        {"code":"multiple_emails","detail":"3 addresses found; first HOME used"},
        {"code":"photo_is_uri","detail":"PHOTO is a URI, not inline data — SKIPPED, not fetched"},
        {"code":"username_collision","detail":"'jdoe' already exists in lldap"}
      ]
    }
  ]
}
```

Limits: 2 MB file, 50 cards, `text/vcard` only, content sniffed not trusted.

### `POST /api/v1/vcard/{import_id}/commit`

Takes an array of `{index, overrides}` — only the indices the operator selected,
with their edits. Creates drafts. Still requires a per-user run to provision.

**Two confirmation gates for vCard, one for manual entry.** Address-book data is
messy and belongs to third parties; it should not be one click from becoming
seven accounts.

---

## 7. Avatars

### `POST /api/v1/avatars` — multipart

Normalises (doc 06 §3) and returns `{"avatar_id":"sha256:…","width":512,"height":512,"bytes":48210}`.
Limits: 8 MB in, JPEG/PNG/WebP/HEIC in, **always JPEG out**.

### `GET /api/v1/avatars/{sha256}`

`Cache-Control: private, immutable, max-age=31536000` — safe because the URL is
the content hash.

---

## 8. Admin / introspection

| Route | Purpose |
|---|---|
| `GET /api/v1/connectors` | Registry: name, tier, enabled, required, health |
| `PATCH /api/v1/connectors/{name}` | Toggle enabled/required, edit `default_payload` |
| `POST /api/v1/connectors/{name}/health` | Live connectivity + credential check |
| `POST /api/v1/reconcile` | Trigger a reconciliation pass |
| `GET /api/v1/audit` | Filtered audit log |
| `GET /healthz` | Liveness — no auth, no data |
| `GET /readyz` | Readiness: DB + lldap reachable. **Not** every connector — a down Immich must not make this service unready |

`/metrics` (Prometheus) is listed in the roadmap as optional; nothing in the
fleet currently scrapes, since monitoring is Gatus + ntfy on ristretto.

---

## 9. What is deliberately absent

- **No user-facing self-service registration.** Not a public app.
- **No password endpoints.** No set, no reset, no rotate. Authelia and lldap own
  that; this tool issues an enrolment link and steps back.
- **No bulk-provision-everything endpoint.** vCard commit creates drafts; each
  still needs its own reviewed run. Bulk provisioning without per-user review is
  how you create eight accounts with the wrong group membership at once.
- **No SCIM.** Nothing in the fleet speaks it. Revisit only if that changes.
