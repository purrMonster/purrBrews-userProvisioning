# 08 — Provisioning Engine

## 1. Why not a for-loop

The obvious implementation is:

```python
for connector in connectors:
    connector.create_user(user)      # ← wrong
```

It fails on the first thing that goes wrong, and it fails badly:

- **No atomicity.** Immich succeeded, Vaultwarden timed out, n8n never ran. What
  state is the user in? Undefined.
- **No retry.** A container restarting during the run permanently loses a step.
- **Not idempotent.** Running it again duplicates the accounts that worked.
- **Head-of-line blocking.** One slow target stalls all the others.
- **No visibility.** The operator gets one exception, not a per-service picture.

Distributed transactions across seven heterogeneous services do not exist. The
correct pattern is a **saga**: a sequence of local transactions, each with a
compensating action, with the whole thing designed to be re-runnable.

---

## 2. The connector contract

Every connector implements one interface. Adding a service means writing one
class and registering it — no engine changes.

```python
class Connector(Protocol):
    name: str
    tier: ServiceTier          # source | ldap | oidc | api | manual | none
    required: bool             # does failure make the run 'partial'?
    supports_avatar: bool
    supports_disable: bool
    supports_delete: bool

    async def health(self) -> HealthResult:
        """Connectivity + credential validity. No side effects. Safe to call often."""

    async def plan(self, user: User, action: Action) -> list[PlannedStep]:
        """Human-readable description of what apply() would do. NO side effects.
        May return [] (tier E) or a manual-instruction step (tier D)."""

    async def apply(self, user: User, step: PlannedStep,
                    idem_key: str) -> ApplyResult:
        """Do the thing. MUST be idempotent under idem_key."""

    async def verify(self, user: User) -> VerifyResult:
        """Read-only: does this account exist in the target, and does it match?
        Drives 'provisioned' vs 'pending_first_login' vs 'drifted'."""

    async def compensate(self, user: User, step: PlannedStep) -> CompensateResult:
        """Undo a partially-applied step. May return NOT_POSSIBLE."""

    async def disable(self, user: User) -> ApplyResult: ...
    async def reconcile(self, user: User) -> ReconcileResult: ...
```

Notes that matter:

- **`plan()` must have no side effects.** The operator sees its output before
  confirming; if planning mutated anything, the preview would be a lie.
- **`verify()` is separate from `apply()`** and is the workhorse. It is what
  makes tier A/B honest, what detects drift, and what lets a failed job be
  resolved by hand and then confirmed by the tool.
- **`compensate()` is allowed to say no.** Guessing at an undo is worse than
  admitting one isn't available; `NOT_POSSIBLE` puts the step into
  `needs_manual` with an instruction.

### 2.1 Tier defaults

Tiers A/B/E get near-empty base classes, so most of the fleet is a few lines:

```python
class NoUserModelConnector(Connector):     # tier E — Pi-hole, Scrutiny, Traefik…
    async def plan(self, user, action):
        return [PlannedStep(action="none",
                summary="No user model — access via Authelia group membership")]
    async def apply(self, *a, **kw):  return ApplyResult.NOT_APPLICABLE
    async def verify(self, user):     return VerifyResult.NOT_APPLICABLE

class JitConnector(Connector):             # tiers A & B — Nextcloud, Vikunja…
    async def plan(self, user, action):
        return [PlannedStep(action="none",
                summary=f"No action — account appears on first {self.mechanism} login")]
    async def apply(self, *a, **kw):  return ApplyResult.PENDING_FIRST_LOGIN
    async def verify(self, user):     return await self._probe_exists(user)
```

Only ~7 connectors need real `apply()` bodies (doc 02 §8).

---

## 3. Run execution

```
PLAN     every enabled connector's plan() is called concurrently (read-only)
         → assembled into a frozen plan document → shown to the operator
   │
   ▼  operator confirms
COMMIT   lldap.apply() runs SYNCHRONOUSLY, inline, blocking the HTTP request
         │
         ├─ fails → run 'failed', user 'failed', NOTHING enqueued, error returned
         │
         └─ succeeds → this is the commit point. user.lldap_uid is set.
   │
   ▼
FAN-OUT  one provisioning_job row per remaining step, all 'queued'
         worker pool claims with SKIP LOCKED, runs them CONCURRENTLY
         each with its own retry/backoff; failures do not block siblings
   │
   ▼
SETTLE   all jobs terminal → run status:
         succeeded  all required steps ok, no manual tasks outstanding
         partial    ≥1 required step failed OR manual tasks outstanding
         failed     only reachable via lldap failure at COMMIT
```

**Why lldap is synchronous and everything else is not.** lldap is the source of
truth; without it there is no identity and nothing downstream is meaningful. It
is also the only step whose failure should stop the world. Making it inline
means the operator gets an immediate, unambiguous answer to "did this work at
all?", and the asynchronous part carries only steps whose failure is survivable.

Concurrency is capped (default 4) so an 8 GB node isn't hit with seven
simultaneous TLS handshakes and JSON parses. Per-connector concurrency of 1
prevents two runs racing on the same target.

---

## 4. Idempotency

At-least-once delivery from a leased queue means **every `apply()` will
eventually run twice.** This is not an edge case; a lease expiring during a slow
API call guarantees it.

`idempotency_key = sha256(run_id | connector | action | user_id)`, stored
`UNIQUE` on the job row. But the DB constraint only stops duplicate *jobs* — it
does not stop a duplicate *effect* when the first attempt succeeded and the
response was lost. So each connector must also be idempotent at the target:

| Connector | Idempotency strategy |
|---|---|
| lldap | `createUser` errors on duplicate uid → treat "already exists" as success after `verify()` confirms it matches |
| Immich | Search by email first; if present, adopt the existing id rather than creating |
| Vaultwarden | Re-inviting an already-invited address is a no-op / benign error — treat as success |
| n8n | Look up by email; if present, re-read the existing invite state |
| Headscale | `users create` fails on duplicate → check `users list` first |
| Home Assistant | ⚠️ No natural key lookup via the WS API — **must** record the returned user id in `external_id` immediately, and check it before re-creating. This is the weakest link and another argument for doc 02 §9 Option A. |
| FreshRSS CLI | `create-user.php` on an existing user errors → check the user dir first |

**Rule for every connector: read before write.** `verify()` runs first inside
`apply()`; if the account already exists and matches, return success without
writing. This makes every retry safe and makes the whole system re-runnable,
which is what makes the reconciler possible.

---

## 5. Compensation

Compensation runs when a run is explicitly rolled back, or when a step succeeds
and a *dependent* step then fails irrecoverably.

| Connector | Compensating action | Available? |
|---|---|---|
| lldap | Delete the created user | Yes — but only if this run created it |
| Immich | Delete the user (soft-delete) | Yes |
| Vaultwarden | Revoke/delete the pending invitation | ⚠️ Verify — only clean while still unaccepted |
| n8n | Delete the invited user | ⚠️ Verify |
| Headscale | `users destroy` | Yes, if no nodes are attached |
| Home Assistant | Delete the user via WS API | ⚠️ Verify |
| Tier A/B/E | Nothing was created | N/A |

**Compensation is opt-in per run, not automatic.** Default behaviour on partial
failure is to *stop and report*, leaving what succeeded in place, because
"provisioned into 5 of 7 services" is usually the state you want to fix forward
from — not unwind. Automatic rollback would mean a transient Immich timeout
silently deletes an account the operator can see working.

The one automatic case: if the lldap commit succeeds but the operator cancels
before any other job runs, offer immediate lldap compensation as a one-click
"undo" while the window is clean.

---

## 6. Reconciliation

A periodic pass (default hourly, and on demand) that answers: **does reality
still match what we think?**

For each active user, for each enabled connector, call `verify()`:

| Finding | Action |
|---|---|
| `pending_first_login` → account now exists | Promote to `provisioned`. **Queue the deferred avatar push** (doc 06 §3.3). |
| `provisioned` → account missing | Mark `drifted`. Notify. **Do not auto-recreate** unless that connector has `auto_repair=true`. |
| `provisioned` → attributes differ (email, display name) | Mark `drifted` with a field-level diff. Offer a one-click repair run. |
| `disabled` → account is enabled again | Mark `drifted`. This one is security-relevant — notify at a higher level. |
| Account exists in target but no `users` row | Report as **unmanaged**. Do not touch. This is how you discover the accounts that predate the tool. |

**Why drift is reported, not silently fixed.** An account disappearing from
Immich might be a bug — or might be you deleting it deliberately last Tuesday. A
tool that quietly recreates it is fighting its operator. Auto-repair is available
per connector for the cases where you've decided the tool wins; the default is
that the human decides.

The **unmanaged** finding is worth building early: on first run against your
existing fleet it produces the inventory of accounts that already exist, which
is exactly what you need to adopt them into the tool rather than duplicating
them.

---

## 7. Offboarding

Deliberately the least automated part of the system.

**Automated (`disable` run):**
- lldap: disable/lock the account, remove from all groups.
- Authelia: sessions invalidated (⚠️ verify the mechanism — Redis session
  flush, or wait for TTL).
- Per-connector `disable()` where supported: Immich, Jellyfin, n8n, Actual,
  Vaultwarden (revoke access), Headscale (expire nodes).
- Tier A/B: disabling in lldap is usually sufficient, because login is gated by
  the LDAP bind or the OIDC token. ⚠️ **Verify per service that an existing
  local session doesn't outlive it** — several will happily keep a logged-in
  session alive for days.

**Emitted as a manual checklist, never automated:**
- Nextcloud: files owned, shares granted to others, group folders.
- Immich: shared albums, partner sharing, whose photos are whose.
- Vikunja: projects they own that others depend on.
- Actual Budget: budget files they own.
- Paperless-ngx: documents they own, permissions granted.
- Home Assistant: automations they authored, their mobile-app device trackers,
  their presence-detection entities (deleting these breaks automations for
  everyone else — a classic).
- Vaultwarden: **do not delete.** Organisation items may be shared; personal
  items are theirs and are unrecoverable by design.

These are ownership and consequence questions, not API calls. The tool's job is
to make sure none of them are forgotten, then get out of the way.

**Hard deletion** is a separate, per-service, individually-confirmed operation
available only after a user has been `disabled` for a configurable cooling-off
period (default 30 days). Never part of an offboarding run.

---

## 8. Observability

- Structured JSON logs, one line per job attempt, with `run_id` correlation.
- The audit log (doc 03 §2.6) is the durable record; logs are for debugging.
- Alert to **ntfy on ristretto** for: a run ending `failed`; any job reaching
  `dead_letter`; reconciler finding `drifted` on a `disabled` user; any denied
  authorisation attempt.
- The `/connectors` health screen is the first place to look when something
  breaks, and it should be linked from every error message the UI shows.

---

## 9. Testing strategy

| Layer | Approach |
|---|---|
| Connectors | Contract test suite every connector must pass: `plan()` has no side effects; `apply()` twice == once; `verify()` after `apply()` is true; `compensate()` after `apply()` leaves `verify()` false |
| Engine | In-memory fake connectors that fail deterministically — timeout, 500, partial write, lease expiry mid-apply. Assert final run status and DB state for each. |
| Idempotency | Kill the worker mid-`apply()` (fault injection), restart, assert exactly one account |
| Input | The 12-case fixture corpus in doc 06 §4 |
| Security | Forged-header test (doc 07 §9.4); LDAP filter injection; XSS via display name; no-secrets-in-audit assertion |
| Integration | Against **disposable throwaway instances** of Immich/Vaultwarden/n8n in compose — never against the live fleet |

**The one test to write first:** create a user, kill the worker halfway through
the fan-out, restart it, and assert the end state is exactly one account per
service and the run reports honestly. Everything else in this engine exists to
make that test pass.
