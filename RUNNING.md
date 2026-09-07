# Running doser

> ⚠️ **This code has not been executed end to end.** The sandbox it was written
> in has no PyPI access, so FastAPI, SQLAlchemy and vobject could not be
> installed there. What *was* verified is listed in §5. Expect to fix a couple
> of small things on first run — that is normal for code that has never booted,
> and §6 tells you where to look first.

---

## 1. Local development (five minutes)

```powershell
cd C:\Users\jyotirmoyc\Projects\purrBrews-userProvisioning
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

$env:DOSER_DEV_AUTH_BYPASS = "true"  # loopback only; refuses off-loopback
$env:DOSER_DOMAIN           = "purrbrews.test"

uvicorn doser.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. With no credentials configured, every connector
shows as **not configured** on `/connectors` — which is the point: an
unconfigured connector is visibly disabled, never a silent no-op.

You can still walk the whole flow (new user → plan preview → confirm). The
commit will fail at lldap with a clear message, and `commit_failed.html`
explains why nothing was created. That failure path is worth seeing once.

Run the tests:

```powershell
pytest -v
```

## 2. Pointing it at real services

Copy `deploy/local.env.example` to `deploy/local.env`, fill in what you have,
and load it. **Start with lldap alone** — it is the commit point and everything
else is meaningless without it:

```powershell
$env:DOSER_LLDAP_URL            = "http://<sieve-ip>:17170"
$env:DOSER_LLDAP_ADMIN_USER     = "admin"
$env:DOSER_LLDAP_ADMIN_PASSWORD = "<...>"
```

Then `/connectors` → lldap → **Check**. Get that green before adding anything
else. Add connectors one at a time, checking health after each.

Every other service URL is derived as `https://<subdomain>.$DOSER_DOMAIN`,
matching the fleet's split-horizon DNS. Override individually where a service
is somewhere unusual:

```powershell
$env:DOSER_IMMICH_URL = "http://192.168.1.42:2283"
```

**I could not read your Traefik or Pi-hole config** — only the `claude\`
subfolder of purrBrews-infra is shared with this session, not the repo root.
So the subdomains are assumed: `lldap`, `immich`, `nextcloud`, `hass`,
`jellyfin`, `vikunja`, `actual`, `mealie`, `paperless`, `vault`, `mail`.
Check them against your real dynamic.yml and override any that differ.

## 3. Suggested testing order

| Step | Why this order |
|---|---|
| 1. lldap only | The commit point. Nothing works without it, and everything else is a fan-out from it. |
| 2. Add Vaultwarden | Cleanest API in the set — invite-only, unambiguous semantics. Good first real connector. |
| 3. Add Immich | The one where pre-creation genuinely matters (quota + storage label). **Test that OAuth links to the pre-created account by email rather than making a second one.** |
| 4. Turn on the tier A/B services | Nextcloud, Jellyfin, Vikunja, Actual, Paperless, Mealie. No credentials needed — confirm the plan shows them as "no action needed" and that logging in as the new user actually works. |
| 5. Home Assistant last | Leave `hass_mode=oidc_jit`. Only switch to `local_ws` if you specifically want to test that path, and read §4 first. |

## 4. Home Assistant: read before switching modes

`DOSER_HASS_MODE=oidc_jit` (default, decision D5) means doser does nothing for
HA and holds no HA credentials. The account appears on first SSO login via the
HACS OIDC component.

`DOSER_HASS_MODE=local_ws` is **testing only**. It creates the account through
HA's internal WebSocket API — the same one the frontend uses. Three consequences,
all real:

1. That API is undocumented and can change on any HA release.
2. doser has to mint an initial password, which is the *only* place in the
   codebase that breaks the "never handles a password" rule. It is returned once
   in the run result and never stored.
3. It needs `pip install websockets`, which is deliberately not a base
   dependency — so the fragile path cannot be entered by accident.

**Before either mode:** create a local `barista` owner account in HA, put its
password in Vaultwarden, and test logging in with Authelia stopped. Being locked
out of the thing that controls your front door is the failure this guards
against, and an untested break-glass is not a break-glass.

## 5. What has actually been verified

| Verified | How |
|---|---|
| All 23 Python files parse | `ast.parse` across the tree |
| All 8 Jinja templates compile | Jinja2 `Environment.get_template` |
| No missing class/model imports | AST cross-reference pass |
| **Avatar pipeline, 9 cases, all passing** | Executed for real against Pillow 12.3 — see below |
| vCard control-character stripping | Executed: RTL override, nulls, zero-width all stripped |

The avatar tests that ran and passed:

- 512×512 JPEG normalisation; PNG-with-alpha flattening; stable content hash
- **EXIF fully discarded** — Model/Make/DateTime present in input, absent in
  output, `getexif()` empty. This is what stops a home GPS coordinate
  propagating to Immich, Nextcloud and Jellyfin.
- ZIP renamed `.jpg` rejected by magic bytes, not extension
- 144-megapixel decompression bomb rejected (would have OOM'd an 8 GB node)
- Oversized upload rejected before any decode
- Output stays under the 200 KB ceiling even on incompressible noise
- **Appended `<?php` payload does not survive re-encoding** — the bytes shipped
  downstream are bytes doser generated

**Not verified:** anything requiring FastAPI, SQLAlchemy or a live service. That
includes the whole request path, the job queue, and every connector's actual
HTTP call.

## 6. Where problems will most likely be

Honest triage list for first run:

1. **Connector API paths.** Every one is marked ⚠️ in `docs/12`. Immich in
   particular has moved endpoints between versions; the connector tries
   `/api/admin/users` then falls back to `/api/users`, but your version may
   differ again.
2. **lldap GraphQL schema.** `CreateUserInput` field names and the
   `/auth/simple/login` response shape should be checked against your pinned
   tag's `schema.graphql`. Also **docs/12 §1.5**: if lldap has no real disable
   primitive, `disable()` here is weaker than it looks and offboarding needs
   rethinking.
3. **SQLAlchemy async session details** in a few of the less-travelled paths
   (the SSE stream, the reconciler) — these are the parts no test has touched.
4. **`Job` claim on SQLite.** `FOR UPDATE SKIP LOCKED` is applied only on
   Postgres. On SQLite the single worker plus the status transition is
   sufficient, but do not run two SQLite workers.

## 7. Deploying to silo

```bash
# on roastery — no live coding on fleet nodes
docker build -t ghcr.io/purrmonster/doser:0.1.0 .
docker push ghcr.io/purrmonster/doser:0.1.0

# on silo
cd /opt/purrbrews/stacks/silo/doser
cp local.env.example local.env && chmod 0600 local.env   # then fill it in
docker compose up -d
```

Before routing it through Traefik:

- [ ] Set `DOSER_TRUSTED_PROXY_CIDRS` to the real `silo_proxy` subnet
      (`docker network inspect silo_proxy`). This is the check that stops
      header-forged fleet-admin access. **Do not leave it at the default.**
- [ ] `DOSER_DEV_AUTH_BYPASS=false`.
- [ ] Authelia rule for `provision.${DOMAIN}` set to **`two_factor`** with
      `group:purrbrews_infra_admins`.
- [ ] **No** Cloudflare Zero Trust hostname route (decision D7). Verify from a
      device off the LAN and off the tailnet that it does not answer.
- [ ] Create dedicated `svc-doser` accounts on lldap, Vaultwarden and Immich
      rather than reusing `barista`'s credentials — D6 declined encryption at
      rest, so surgical revocation is the compensating control that matters.
- [ ] Add Alembic before this instance holds any real account. It currently uses
      `create_all`, which is fine while the schema moves and wrong afterwards.
