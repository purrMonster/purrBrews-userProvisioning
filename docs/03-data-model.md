# 03 — Data Model

Postgres 16, dedicated instance (`doser_db`), per the fleet's per-app DB
isolation rule. Alembic migrations. All timestamps `timestamptz`, UTC.

## 1. Design principle

**lldap is the source of truth for identity. This database is the source of
truth for *provisioning state*.**

That distinction drives everything. This DB does not store a duplicate user
directory — it stores *what we tried to do, to which service, and how it went*.
If this database were lost entirely, no identity would be lost; you would lose
history and the reconciler would rebuild current state from lldap plus a
discovery pass.

Consequences:
- No password column. Ever. (doc 07 §3)
- `users` here is a thin shadow keyed to the lldap UID, holding only what's
  needed to drive and audit provisioning.
- Every write to a target service is recorded as a `provisioning_job` row
  *before* the network call, so a crash mid-call is recoverable.

---

## 2. Schema

### 2.1 `users` — provisioning subjects

```sql
CREATE TABLE users (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lldap_uid           text UNIQUE,              -- NULL until lldap create succeeds
    username            citext NOT NULL UNIQUE,   -- the requested uid
    email               citext NOT NULL,
    first_name          text NOT NULL,
    last_name           text NOT NULL,
    display_name        text NOT NULL,
    avatar_sha256       text,                     -- FK-ish into avatars
    profile             text NOT NULL DEFAULT 'household'
                        REFERENCES provisioning_profiles(name),
    status              user_status NOT NULL DEFAULT 'draft',
    source              user_source NOT NULL DEFAULT 'manual',
    source_ref          text,                     -- e.g. vcf import batch id
    created_by          text NOT NULL,            -- Remote-User of the operator
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    disabled_at         timestamptz,
    notes               text
);

CREATE TYPE user_status AS ENUM (
    'draft',        -- form filled / vcf parsed, not yet committed
    'provisioning', -- lldap write done, connector jobs in flight
    'active',       -- all required connectors settled successfully
    'partial',      -- lldap ok, ≥1 connector failed or needs manual work
    'disabled',     -- offboarded, not deleted
    'failed'        -- lldap write itself failed; nothing exists
);

CREATE TYPE user_source AS ENUM ('manual', 'vcard', 'api', 'reconciler');
```

`citext` for `username` and `email` — case-insensitive uniqueness is not
optional here. Two accounts differing only in case is a real incident.

**Why `email` is not `UNIQUE`:** shared household mailboxes are plausible, and
more importantly a hard constraint here would block re-provisioning a user after
a failed run. Uniqueness is enforced as a *warning* in the plan phase, not a
DB constraint.

### 2.1a `provisioning_profiles` — least-privilege by default (D8)

```sql
CREATE TABLE provisioning_profiles (
    name            text PRIMARY KEY,
    description     text NOT NULL,
    connectors      text[] NOT NULL,     -- which connectors are in scope
    lldap_groups    text[] NOT NULL,     -- groups granted on creation
    is_privileged   boolean NOT NULL DEFAULT false
);

INSERT INTO provisioning_profiles VALUES
 ('household',
  'Day-to-day household member. Media, photos, documents, tasks, passwords.',
  ARRAY['lldap','vaultwarden','immich','nextcloud','jellyfin','mealie',
        'paperless','vikunja','actual','freshrss','homeassistant'],
  ARRAY['purrbrews_users'],
  false),
 ('infra_admin',
  'Fleet operator. Everything in household, plus admin surfaces.',
  ARRAY['lldap','vaultwarden','immich','nextcloud','jellyfin','mealie',
        'paperless','vikunja','actual','freshrss','homeassistant',
        'n8n','headscale','komodo'],
  ARRAY['purrbrews_users','purrbrews_infra_admins'],
  true);
```

**Why this exists.** Without profiles, the plan preview lists every service every
time and least-privilege depends on the operator remembering to deselect things
while tired. With them, the safe path is the default path.

Concretely: `bubbles` gets `household` and therefore never gets an n8n account
(she is not authoring automations), never gets Headscale (unless she has a device
joining the tailnet — see doc 00, still open), and **never gets near Komodo** —
which is exactly the least-privilege posture decided on 2026-08-29, now enforced
by the data model rather than by discipline.

Rules:

- `profile` is chosen on the create form and is visible on the plan screen.
- Selecting a profile where `is_privileged = true` triggers the second explicit
  confirmation and the distinct audit event described in doc 07 §2.3.
- Changing a user's profile queues an `update` run: connectors added by the new
  profile are provisioned; connectors *removed* are **disabled, never deleted**,
  and surfaced as a review list rather than acted on silently.
- A connector can still be individually added or removed for one user; the
  profile sets the default, it is not a cage.

### 2.2 `service_accounts` — per-user, per-service state

```sql
CREATE TABLE service_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connector           text NOT NULL,            -- 'immich', 'vaultwarden', ...
    tier                service_tier NOT NULL,
    state               account_state NOT NULL DEFAULT 'not_started',
    external_id         text,                     -- the service's own user id
    external_ref        jsonb NOT NULL DEFAULT '{}',  -- invite urls, quotas, etc.
    last_verified_at    timestamptz,
    last_error          text,
    manual_instruction  text,                     -- populated for tier D
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, connector)
);

CREATE TYPE service_tier AS ENUM ('source', 'ldap', 'oidc', 'api', 'manual', 'none');

CREATE TYPE account_state AS ENUM (
    'not_started',
    'not_applicable',      -- tier E: nothing to do
    'pending_first_login', -- tier A/B: waiting for the user to show up
    'provisioned',         -- confirmed present in the target
    'needs_manual',        -- tier D, or a failure with no safe auto-fix
    'failed',
    'disabled',
    'drifted'              -- reconciler found a mismatch
);
```

`pending_first_login` is the state that makes tier A/B honest. Without it, the
UI would have to lie — showing either a green tick for an account that does not
exist yet, or a red cross for a working configuration.

`external_ref` as `jsonb` carries things like n8n's `inviteAcceptUrl`, Immich's
storage label, Vaultwarden's invite timestamp. Keeping it schemaless avoids a
migration every time a connector learns a new field.

### 2.3 `provisioning_runs` — one per operator action

```sql
CREATE TABLE provisioning_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id),
    kind            run_kind NOT NULL,
    status          run_status NOT NULL DEFAULT 'planning',
    plan            jsonb NOT NULL,     -- the dry-run preview, frozen at confirm
    requested_by    text NOT NULL,
    started_at      timestamptz,
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE run_kind AS ENUM ('provision','update','disable','enable','offboard','reconcile');
CREATE TYPE run_status AS ENUM ('planning','awaiting_confirmation','running','succeeded','partial','failed','cancelled');
```

The `plan` jsonb is **frozen at the moment the operator confirms**. If the
connector registry changes between plan and apply, the run executes the plan
that was shown and approved — never a silently different one. This matters more
than it sounds: "the preview said one thing and it did another" is the failure
mode that destroys trust in a tool like this.

### 2.4 `provisioning_jobs` — the queue

```sql
CREATE TABLE provisioning_jobs (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(id),
    connector       text NOT NULL,
    action          text NOT NULL,          -- 'create' | 'update' | 'disable' | 'verify' | 'compensate'
    payload         jsonb NOT NULL,
    idempotency_key text NOT NULL,
    status          job_status NOT NULL DEFAULT 'queued',
    attempts        int NOT NULL DEFAULT 0,
    max_attempts    int NOT NULL DEFAULT 5,
    run_after       timestamptz NOT NULL DEFAULT now(),
    leased_until    timestamptz,
    leased_by       text,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    UNIQUE (idempotency_key)
);

CREATE TYPE job_status AS ENUM ('queued','leased','succeeded','failed','dead_letter','skipped','needs_manual');

CREATE INDEX ON provisioning_jobs (status, run_after) WHERE status = 'queued';
```

**Queue semantics.** The worker claims with:

```sql
UPDATE provisioning_jobs SET status='leased', leased_until=now()+interval '5 minutes',
       leased_by=$1, attempts=attempts+1
WHERE id IN (
    SELECT id FROM provisioning_jobs
    WHERE status='queued' AND run_after <= now()
    ORDER BY id FOR UPDATE SKIP LOCKED LIMIT $2
) RETURNING *;
```

Plus a sweeper that returns jobs whose `leased_until` has passed to `queued`.
That is a correct at-least-once queue in Postgres with no broker — which is why
`idempotency_key` is `UNIQUE` and non-negotiable (doc 08 §4).

Backoff: `run_after = now() + interval '30 seconds' * 2^attempts`, jittered,
capped at 1 hour. After `max_attempts`, status becomes `dead_letter` and the
run is marked `partial`.

### 2.5 `avatars` — content-addressed image store

```sql
CREATE TABLE avatars (
    sha256          text PRIMARY KEY,
    mime            text NOT NULL,          -- always 'image/jpeg' post-normalisation
    width           int NOT NULL,
    height          int NOT NULL,
    bytes           bytea NOT NULL,
    original_mime   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
```

Content-addressed so re-uploading the same picture is free and so an avatar
change is a pointer swap with the old image still present for rollback.
Deliberately `bytea` in Postgres, not the filesystem: these are ≤ 200 KB after
normalisation, there are single digits of them, and keeping them in the DB means
one backup artefact rather than two. (This would be the wrong call at scale.)

### 2.6 `audit_log` — append-only

```sql
CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,          -- Remote-User, or 'system:reconciler'
    actor_groups    text[],
    actor_ip        inet,
    action          text NOT NULL,
    subject_user    uuid,
    connector       text,
    run_id          uuid,
    outcome         text NOT NULL,          -- 'ok' | 'error' | 'denied'
    detail          jsonb NOT NULL DEFAULT '{}'
);

REVOKE UPDATE, DELETE ON audit_log FROM doser_app;
```

Append-only enforced at the grant level, not just by convention. The app role
can `INSERT` and `SELECT`; nothing else. A separate migration role owns DDL.

**Redaction rule:** `detail` is written through a scrubber that drops any key
matching `password|token|secret|key|authorization|cookie`. Enforced in one place
(the audit writer), tested, and asserted on in CI — because an audit log that
accidentally logs bearer tokens is worse than no audit log.

### 2.7 `connector_config` — runtime-visible, secret-free

```sql
CREATE TABLE connector_config (
    connector       text PRIMARY KEY,
    enabled         boolean NOT NULL DEFAULT false,
    tier            service_tier NOT NULL,
    base_url        text,
    required        boolean NOT NULL DEFAULT false,  -- does failure make the run 'partial'?
    default_payload jsonb NOT NULL DEFAULT '{}',     -- quotas, group ids, library ids
    updated_at      timestamptz NOT NULL DEFAULT now()
);
```

**No credentials in this table.** Secrets come from the environment only
(doc 07 §4). This table holds the operationally-tunable, non-sensitive half so
you can enable/disable a connector or change a default quota without a redeploy.

`required` is important: Vaultwarden failing should make a run `partial` and
demand attention; Speedtest Tracker failing should not.

### 2.8 `vcard_imports` — batch staging

```sql
CREATE TABLE vcard_imports (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        text NOT NULL,
    uploaded_by     text NOT NULL,
    card_count      int NOT NULL,
    staged          jsonb NOT NULL,   -- array of parsed, normalised candidates
    resolved        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL DEFAULT now() + interval '24 hours'
);
```

Staged, never auto-committed. Expires so half-finished imports containing
personal data don't linger. A nightly job hard-deletes expired unresolved rows.

---

## 3. State machines

### 3.1 User

```
draft ──confirm──▶ provisioning ──all required ok──▶ active
  │                     │                              │
  │                     ├──≥1 required failed──▶ partial ──retry/fix──▶ active
  │                     └──lldap write failed──▶ failed
  │
  └──discard──▶ (deleted)

active ──disable run──▶ disabled ──enable run──▶ active
partial ──disable run──▶ disabled
```

### 3.2 Service account

```
not_started
  ├─ tier E ──────────────────────────▶ not_applicable
  ├─ tier A/B ────────────────────────▶ pending_first_login ──reconciler sees it──▶ provisioned
  ├─ tier C ──job succeeds────────────▶ provisioned
  │           └──job dead-letters─────▶ failed ──manual fix + verify──▶ provisioned
  └─ tier D ──────────────────────────▶ needs_manual ──operator marks done──▶ provisioned

provisioned ──reconciler finds mismatch──▶ drifted ──repair run──▶ provisioned
provisioned ──disable run──▶ disabled
```

`drifted` deserves a note: the reconciler *reports* drift, it does not silently
repair it by default. Auto-repair is opt-in per connector, because "the tool
quietly recreated an account an operator had deliberately removed" is a bad
surprise.

---

## 4. Retention

| Data | Retention | Rationale |
|---|---|---|
| `audit_log` | Indefinite | Small, and it's the record of who did what to whose account |
| `provisioning_jobs` | 180 days, then delete succeeded rows; keep dead-letters | Payloads may contain names/emails |
| `vcard_imports` | 24 h unresolved, deleted on resolve | Contains third-party personal data from address books — the most sensitive thing this tool touches |
| `avatars` | Until user is hard-deleted | |
| `users` (disabled) | Indefinite unless explicitly purged | Offboarding ≠ forgetting; you need to know the account existed |

---

## 5. Backup

The DB is small (megabytes). Nightly `pg_dump` into the DATA_DIR path that
already flows to the household NAS and cellar's archive mirror. **The dump
contains personal data and avatar images — it must land in the same encrypted /
access-controlled tier as Vaultwarden's backups, not the general media share.**
Losing this DB is recoverable (lldap survives); leaking it is not.
