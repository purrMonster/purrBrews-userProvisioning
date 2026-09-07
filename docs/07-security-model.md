# 07 — Security Model

## 1. The uncomfortable framing

Be clear about what is being built: **a single web application that holds
administrative credentials for every service in the fleet and can create
identities in all of them.**

Compromise of this service is equivalent to compromise of the fleet — arguably
worse, because it can create *legitimate-looking* accounts that survive
remediation of the original entry point. It is a higher-value target than any
individual app it manages, and its security posture should reflect that rather
than the posture of "an internal admin page nobody else can reach."

Everything below follows from that.

---

## 2. Authentication and authorisation

### 2.1 Layers

1. **Network** — the container publishes no host port. It is reachable only on
   the docker network that silo's/sieve's Traefik uses. A ufw rule scopes it.
2. **Traefik + Authelia forward-auth** — `access_control` requires
   `group:purrbrews_infra_admins` for `provision.${DOMAIN}`, consistent with the
   Pi-hole and Traefik-dashboard rules already decided on 2026-08-29.
3. **In-application authorisation** — the app re-checks `Remote-Groups` itself
   and denies if `purrbrews_infra_admins` is absent.
4. **Source verification** — the app rejects any request whose peer address is
   not the expected proxy, and strips/ignores `Remote-*` headers that did not
   arrive from it.

### 2.2 Why layer 3 and 4 exist even though layer 2 already gates it

This is the single most important control in the document, and it is the one
most commonly skipped.

`Remote-User` and `Remote-Groups` are **just HTTP headers.** If anything can
ever reach the app container directly — a misconfigured Traefik router, a
compose file that gains a `ports:` line during debugging, a second service on the
same docker network that gets popped, an `extra_hosts` shortcut like the one
already used for Pi-hole — then *anyone who can reach the container can set
`Remote-Groups: purrbrews_infra_admins` and become a fleet administrator.*

Header-forged auth behind a reverse proxy is one of the most reliably
catastrophic homelab misconfigurations, precisely because it looks correct right
up until the network assumption quietly stops holding. The fleet already has one
place where a service is reachable off its intended path (Pi-hole on 8080 via
`host.docker.internal`), which is proof the assumption is not free.

Mitigations, all cheap:

- Bind the app to the proxy network only; never publish a host port.
- Verify the peer IP is Traefik's; reject otherwise, log loudly.
- Additionally verify Authelia's session independently — either by validating a
  signed header/JWT from Authelia if your version emits one, or by making the app
  an **OIDC relying party of Authelia in its own right** rather than relying on
  forward-auth headers at all. ⚠️ Authelia 4.39+ is a full OIDC provider; making
  this app a proper confidential client is the strongest option and is
  **recommended for v1.1**. Forward-auth + peer check is acceptable for v1.
- Assert in an automated test that a request with forged headers from an
  unexpected source IP returns 403.

### 2.3 Privilege within the app

There is one role: fleet admin. That is honest — there is no meaningful
lesser-privileged use of a tool that creates accounts everywhere.

`barista` is the intended operator identity, per the 2026-08-29 model.
`penguin` and `bubbles` are deliberately not in `purrbrews_infra_admins` and must
not gain access to this tool.

**Guard against self-escalation:** the app must refuse to add *its own operator*
to `purrbrews_infra_admins`, and adding anyone to that group must require the
second explicit confirmation described in doc 05 §2.2 plus a distinct audit
event. Not because it stops a determined attacker, but because it stops an
accidental one and creates an unambiguous log line.

---

## 3. Credentials this tool does **not** handle

A deliberate, structural choice: **the tool never sets, stores, transmits, or
sees a user's password.**

The flow:

1. lldap account is created with a 32-byte CSPRNG password that is generated,
   used once in the mutation, and never persisted or logged.
2. The operator generates a one-time enrolment link (72 h TTL, single use,
   audit-logged on generation *and* on redemption).
3. The user redeems it and sets their own password through Authelia's own
   reset flow.

Why this matters concretely:

- **Vaultwarden's entire security model depends on it.** Vaultwarden can only
  ever *invite*; a password or key set by an administrator would break the
  zero-knowledge property. Building the rest of the tool around the same
  constraint means Vaultwarden isn't a weird exception.
- It removes the highest-value target from the database. A dump of this DB
  contains names, emails and avatars — bad, but not account takeover.
- It removes a whole class of bug: no password ever transits a log, an SSE
  event, a plan preview, or a job payload, because there is never one to leak.

**There is now no exception.** The only one that existed was Home Assistant
Option B, which would have required `doser` to generate and transmit an initial
HA password. Decision D5 chose the HACS OIDC path instead, so **this rule holds
absolutely: no code path in `doser` ever produces, stores, transmits, or logs a
user password.** That is a property worth defending — any future change that
would introduce one should be treated as a design regression, not a feature.

The one adjacent credential that does exist is the HA **break-glass** local owner
account (doc 02 §9). It is created by hand, stored in Vaultwarden, and `doser`
neither knows nor manages it.

---

## 4. Service credentials (the real crown jewels)

The tool holds, at minimum: an lldap admin bind/JWT, an Immich admin API key, a
Vaultwarden `ADMIN_TOKEN`, an n8n owner API key, possibly a Home Assistant
long-lived token, possibly Headscale gRPC access.

### 4.1 Storage

Follow the pattern already decided for sieve on 2026-08-28: **secrets generated
at setup time, kept as plain gitignored `*.env.local` files, never committed.**
Injected as environment variables via `--env-file`.

✅ **Decided (D6): plain gitignored `local.env`, mode 0600.** SOPS+age is not
adopted for silo. This is a consciously accepted risk, recorded in full in
doc 00 — `doser`'s env file will hold the largest concentration of high-value
credentials on the fleet, unencrypted at rest.

The reasoning that makes it defensible: it matches sieve, it needs no new
tooling, and **D7 removes the internet-facing path** that would make remote
exfiltration the likely attack. The residual threat is physical access to silo
or a compromise that already has filesystem read as the service user — against
which a file the service must be able to decrypt at runtime buys less than it
looks like.

Because the encryption control was declined, the compensating controls in §4.2
and §4.3 stop being nice-to-haves and become the actual mitigation. **Do them.**
In particular, dedicated `svc-doser` accounts per target mean a leaked env file
is revocable service-by-service without touching `barista`'s own credentials.

Rules regardless of pattern:

- File mode `0600`, owned by the service user.
- `docker-compose.yml` references `env_file`, never inline values.
- Pre-commit secret-scanning hooks (already in the repo) must cover this repo too.
- No secret in `connector_config` (doc 03 §2.7), in the DB, in a log, in an audit
  `detail`, in an SSE event, or in a plan preview. The audit scrubber (doc 03
  §2.6) is the backstop; the primary defence is never putting them there.

### 4.2 Least privilege per connector

Where the target supports scoped credentials, use them. Where it does not
(Vaultwarden's `ADMIN_TOKEN` is all-or-nothing; HA long-lived tokens inherit the
owner's rights), **record that in the connector's documentation row so the blast
radius is visible on the `/connectors` screen**, not buried.

Create a dedicated service account per target where possible (`svc-doser`)
rather than reusing `barista`'s own credentials — so revocation is surgical and
the audit trail on the *target* side distinguishes tool actions from human ones.

### 4.3 Rotation

Rotation must be a documented, rehearsed runbook step, not an aspiration. Each
connector's health check verifies its credential; a rotation is: update
`.env.local` → `compose.sh doser up -d` → run health checks → confirm all
green. Put it in the fleet runbook with a target cadence.

### 4.4 Egress

The app makes outbound connections to a known, finite set of hosts. Constrain
them: a docker network per target group where practical, or ufw egress rules from
silo. This is what turns an SSRF or a compromised dependency from "reach anything
on the LAN" into "reach the seven hosts it already talks to." Given that this
service parses attacker-supplied files (doc 06), egress control is worth the
effort here specifically.

---

## 5. Input handling

Covered in detail in doc 06. Summary of the security-relevant rules:

| Input | Control |
|---|---|
| vCard `PHOTO;VALUE=uri` | **Never fetched.** SSRF prevention. |
| vCard text fields | Control chars, zero-width, and RTL overrides stripped; NFC-normalised; length-capped |
| vCard file | 2 MB, 50 cards, magic-byte check, `BEGIN:VCARD` check |
| Avatar image | Magic-byte sniff, pixel cap (decompression bomb), full decode + re-encode, all metadata discarded |
| Username | Strict regex, reserved-name deny list, case-insensitive collision check against lldap |
| Everything into LDAP | Escaped per RFC 4514/4515 — no string-concatenated DNs or filters |
| Everything into HTML | Jinja2 autoescape on; no `\|safe` on user data, ever |
| Everything into SQL | Parameterised (SQLAlchemy). No f-string SQL. |

**LDAP injection deserves a specific mention:** a username or display name
containing `)`, `*`, `\`, or a null byte, concatenated into a search filter, is a
filter-injection bug. Use the library's escaping helpers; never build filters by
formatting.

---

## 6. Web application hardening

- **CSRF**: double-submit token on every mutating form; `SameSite=Strict` on the
  session cookie. HTMX must send the token (`hx-headers`).
- **CSP**: `default-src 'self'; img-src 'self' data:; script-src 'self'`. All CSS
  and JS are local — no CDN — so a strict CSP costs nothing. (This also means the
  CDN-fetching habit must be resisted at build time.)
- Standard headers: `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  same-origin`, `X-Frame-Options: DENY`, HSTS at Traefik.
- **Rate limiting**: per-operator limits on run creation, vCard parse, and avatar
  upload — mostly to bound resource use on an 8 GB node, secondarily to slow a
  compromised session.
- Request body limits enforced at Traefik *and* in the app.
- No stack traces to the client. Correlation ID in the response, full detail in
  the server log.
- Dependencies pinned with hashes; `pip-audit` in CI; base image rebuilt on a
  schedule (Diun already watches image tags — point it at this image too).

---

## 7. Audit

Every action that changes state or reveals a secret-adjacent value gets a row:
who (Remote-User + groups + source IP), what, to whom, which connector, outcome,
timestamp. Append-only at the grant level (doc 03 §2.6). Redaction scrubber on
the way in.

Specifically audited beyond the obvious:
- Every enrolment-link generation **and redemption**.
- Every `ack-manual` (an operator asserting a manual step was done — the tool
  cannot verify it, so the assertion itself is the record).
- Every `skip with reason`.
- Every group change, with `purrbrews_infra_admins` changes at a distinct level.
- Every denied authorisation attempt, with the source IP — this is the signal
  that layer 4 (§2.2) has caught something.

Consider forwarding denied-auth and admin-group events to **ntfy on ristretto**.
ristretto is deliberately isolated and observes when other nodes fail, which
makes it the right place for "something is wrong with the thing that controls
identity" to surface.

---

## 8. Threat model summary

| Threat | Likelihood | Impact | Control |
|---|---|---|---|
| Header-forged auth via direct container access | Low but plausible | **Total fleet compromise** | §2.2 — peer check, no host port, OIDC client in v1.1 |
| Malicious `.vcf` → SSRF | Low | High (internal reach from a credentialed host) | §5 — URIs never fetched |
| Malicious image → RCE / OOM | Low | High / medium | §5 — sniff, cap, re-encode |
| Secrets leaked via logs or audit | **Medium** (easy mistake) | High | §4.1 scrubber + never-log discipline, tested |
| Compromised Python dependency | Low | Total | Pinned hashes, `pip-audit`, egress control (§4.4) |
| Operator error creating an over-privileged account | **Medium** — the most likely one | Medium–high | Plan preview, double confirmation on admin group, audit |
| Half-applied run leaves an inconsistent identity | **High** — this is routine | Low–medium | Saga + compensation + reconciler (doc 08) |
| Stolen enrolment link | Low | Medium | Single-use, 72 h TTL, audited on redemption |

The two entries in bold at the bottom are the ones that will actually happen.
The design spends most of its complexity budget there — on the plan preview and
the reconciler — rather than on exotic attacks.

---

## 9. Recommended pre-build security tasks

1. ✅ ~~Decide the silo secrets pattern~~ — D6: plain `local.env`. Now do the
   compensating controls: `svc-doser` accounts (§4.2), rotation runbook (§4.3),
   egress restriction (§4.4). These are load-bearing, not optional.
2. ✅ ~~Decide Home Assistant Option A vs B~~ — D5: Option A. §3 holds absolutely.
   **Create and test the HA break-glass account before enabling OIDC.**
3. Confirm Authelia's `access_control` rule for `provision.${DOMAIN}` before the
   service is ever routed, not after — and that it is **not** published through
   the Cloudflare Tunnel (D7).
4. **Write the forged-header test first.** It is the single test most worth
   having, and D7 does not remove the need for it — a LAN-only service is still
   trivially reachable by anything already inside the LAN.
