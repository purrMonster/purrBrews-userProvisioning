# 09 — Deployment

Target node: **silo** (rationale in doc 01 §3). Follows the established fleet
conventions: single git repo as source of truth, `stacks/<node>/<app>/` layout,
bind mounts under `DATA_DIR`, `compose.sh` wrapper, `envsubst` config templating,
no live coding on fleet nodes.

## 1. Repository layout

```
purrBrews-userProvisioning/
├── README.md
├── docs/                          ← this plan
├── src/doser/
│   ├── main.py                    FastAPI app factory
│   ├── config.py                  pydantic-settings, env only
│   ├── auth.py                    forward-auth verification, peer check
│   ├── db/                        models, session, alembic/
│   ├── engine/                    runner, queue, reconciler, saga
│   ├── connectors/
│   │   ├── base.py                Protocol + tier base classes
│   │   ├── lldap.py               ← source of truth, build first
│   │   ├── vaultwarden.py  immich.py  n8n.py  headscale.py
│   │   ├── komodo.py              tier D — manual-instruction stub only
│   │   ├── jit.py                 tiers A & B, config-driven
│   │   └── none.py                tier E, config-driven
│   │   #  no homeassistant.py (D5) · no ntfy.py (D12) · no shares (D9)
│   ├── ingest/                    vcard.py, avatar.py
│   ├── api/                       v1 routers
│   ├── web/                       Jinja templates, static css/js
│   └── tasks/                     reconciler, expiry sweeper
├── tests/
│   ├── fixtures/                  the 12-case corpus (doc 06 §4)
│   ├── contract/                  the connector contract suite
│   └── integration/               compose-based, throwaway targets
├── deploy/
│   ├── docker-compose.yml
│   ├── local.env.example          ⚠ NOT ".env.local.example" — see §6
│   ├── traefik-dynamic.yml.template
│   └── authelia-access-control.snippet.yml
├── Dockerfile
├── pyproject.toml
└── .pre-commit-config.yaml        secret scanning, ruff, mypy
```

## 2. Compose stack

Two containers. Sketch, not final — align with silo's actual network names:

```yaml
services:
  doser:
    image: ghcr.io/purrmonster/doser:${DOSER_TAG}
    restart: unless-stopped
    env_file: [ ./local.env ]
    environment:
      DATABASE_URL: postgresql+asyncpg://doser:${DOSER_DB_PASSWORD}@doser-db:5432/doser
      TRUSTED_PROXY_CIDRS: ${TRUSTED_PROXY_CIDRS}
      REQUIRED_GROUP: purrbrews_infra_admins
      LLDAP_URL: http://lldap.sieve.internal:17170
    depends_on:
      doser-db: { condition: service_healthy }
    networks: [ silo_proxy, doser_internal ]
    # NO ports: — deliberately. See doc 07 §2.2.
    read_only: true
    tmpfs: [ /tmp ]
    cap_drop: [ ALL ]
    security_opt: [ "no-new-privileges:true" ]
    mem_limit: 512m
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s

  doser-db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: doser
      POSTGRES_USER: doser
      POSTGRES_PASSWORD: ${DOSER_DB_PASSWORD}
    command: >
      postgres -c shared_buffers=64MB -c max_connections=20
               -c effective_cache_size=192MB
    volumes:
      - ${DATA_DIR}/doser/pgdata:/var/lib/postgresql/data
    networks: [ doser_internal ]
    mem_limit: 256m
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U doser"]

networks:
  silo_proxy: { external: true }
  doser_internal: { internal: true }   # no egress from the DB
```

Points that are not incidental:

- **No `ports:`.** The single most important line in the file is the one that
  isn't there. Doc 07 §2.2.
- **`doser_internal` is `internal: true`** — the database cannot reach the
  network at all.
- **`read_only: true` + `cap_drop: ALL`** — the app writes nothing to disk;
  avatars live in Postgres precisely so this is possible.
- **`mem_limit`** — an 8 GB node with Komodo on it should not be at the mercy of
  a runaway image decode. Combined with the pixel cap in doc 06 §3.2.
- **Pinned tag**, never `latest`. Diun already watches image tags on this fleet —
  point it at this image.

## 3. Traefik and Authelia

Router on **sieve's** Traefik (external) and/or silo's, per the fleet's hybrid
Traefik + split-horizon DNS design — same public hostname resolving to silo's LAN
IP internally and via the Cloudflare Tunnel externally.

```yaml
# traefik-dynamic.yml.template
http:
  routers:
    doser:
      rule: "Host(`provision.${DOMAIN}`)"
      service: doser
      middlewares: [ authelia@file, security-headers@file ]
      tls: { certResolver: cloudflare }
  services:
    doser:
      loadBalancer:
        servers: [ { url: "http://${SILO_LAN_IP}:8000" } ]
```

```yaml
# authelia-access-control.snippet.yml
access_control:
  default_policy: deny
  rules:
    - domain: provision.${DOMAIN}
      policy: two_factor          # ← not one_factor
      subject: [ "group:purrbrews_infra_admins" ]
```

**`two_factor`, deliberately.** This service can create fleet-admin identities.
Even while Authelia is still on its first-bring-up defaults (`one_factor`,
filesystem notifier, per the 2026-08-28 notes), this route should be the one that
justifies finishing the 2FA setup. If 2FA isn't ready, that's a reason to delay
routing this service — not a reason to weaken the rule.

### 3.1 ✅ Not exposed through the Cloudflare Tunnel (D7)

**Decided: LAN + Headscale tailnet only.** `provision.${DOMAIN}` must **not**
appear in the Cloudflare Zero Trust dashboard's hostname routing.

Because the fleet uses split-horizon DNS with the same public domain both ways,
this needs an explicit action rather than an omission:

- Pi-hole's split-horizon config resolves `provision.${DOMAIN}` to silo's LAN IP.
  ✓ That's the intended path.
- **No** public hostname route is created in the Cloudflare Zero Trust dashboard
  for it. Since the tunnel is token-based and remotely managed, this is a
  dashboard-side omission — nothing in the repo prevents someone adding it later,
  so note it in the runbook.
- Remote access, when genuinely needed, goes over Headscale, which puts the
  device on the tailnet and therefore on the LAN path.

**Verify after deploy:** resolve and curl `provision.${DOMAIN}` from a device
that is off the LAN and off the tailnet. It must not answer. This is a two-minute
check that confirms the most valuable single control in the whole design.

## 4. Configuration

All config from environment. `pydantic-settings`, fail-fast on startup if a
required value is missing — never silently degrade.

| Var | Purpose |
|---|---|
| `DATABASE_URL` | |
| `TRUSTED_PROXY_CIDRS` | Peer-IP allowlist for forward-auth (doc 07 §2.2) |
| `REQUIRED_GROUP` | `purrbrews_infra_admins` |
| `LLDAP_URL`, `LLDAP_ADMIN_USER`, `LLDAP_ADMIN_PASSWORD` | Source of truth |
| `IMMICH_URL`, `IMMICH_API_KEY` | |
| `VAULTWARDEN_URL`, `VAULTWARDEN_ADMIN_TOKEN` | |
| `N8N_URL`, `N8N_API_KEY` | |
| `HEADSCALE_*` | |
| ~~`HASS_URL`, `HASS_TOKEN`~~ | **Removed by D5.** `doser` holds no Home Assistant credentials at all |
| `ENROLMENT_SIGNING_KEY` | Signs one-time enrolment tokens |
| `RECONCILE_INTERVAL_SECONDS` | Default 3600 |
| `NTFY_URL`, `NTFY_TOPIC` | Alerts |

**A connector with no credential configured is automatically disabled and shown
as such on `/connectors`.** It must not silently no-op — a connector that
quietly does nothing is how you discover in six months that nobody was ever
provisioned into Vaultwarden.

## 5. Build and release

Matches the "no live coding on fleet nodes" rule:

1. Develop on roastery/americano.
2. Build multi-stage image, push to GHCR (private).
3. Commit the pinned tag to the repo.
4. On silo: `git pull` (the 06:15 cron does this, ⚠️ once the known
   `barista`-has-no-git-credentials blocker in the fleet backlog is fixed) then
   `./compose.sh doser up -d`.
5. Alembic migrations run on startup, guarded by a Postgres advisory lock so
   concurrent starts can't race.

## 6. Secrets file naming

⚠️ Per the fleet note: the device bridge tooling refuses filenames matching
`*.env.local*`. Use **`local.env.example`** for the tracked template and
`local.env` for the gitignored real file, mirroring what the sieve stack already
does. Add `deploy/local.env` to `.gitignore` and confirm the pre-commit secret
scanner covers this repo.

✅ **D6: plain `local.env` at mode 0600 is the decided pattern** — SOPS+age is
not being adopted. This file will still hold more high-value credentials than any
other single file in PurrBrews, so the compensating controls in doc 07 §4.2–4.4
(dedicated `svc-doser` accounts, a rehearsed rotation runbook, egress
restriction) are the actual mitigation and should be treated as part of the
deployment, not as follow-up work. Full risk statement in doc 00.

## 7. Backup and restore

- Nightly `pg_dump -Fc` to `${DATA_DIR}/doser/backups/`, into the tier that
  already flows to the household NAS and cellar's archive mirror.
- ⚠️ **The dump contains names, emails, and avatar images.** It belongs in the
  same access-controlled tier as Vaultwarden's backups, not the general media
  share. Confirm the destination before enabling the job.
- Restore drill: this DB is *not* load-bearing for identity. If it were lost
  entirely, lldap still holds every account; the reconciler's "unmanaged"
  discovery pass (doc 08 §6) rebuilds the picture. **Rehearse this once** — it is
  a genuinely reassuring property and worth confirming rather than assuming.

## 8. Resource budget on silo

| Component | Steady | Peak |
|---|---|---|
| doser app | ~120 MB | ~350 MB during image processing |
| doser-db | ~120 MB | ~256 MB |
| **Total** | **~240 MB** | **~600 MB** |

Acceptable on an 8 GB node alongside Komodo and the monitoring stack. If silo
turns out to be tighter than expected once Komodo is really running, the fallback
is SQLite + a single worker, dropping the DB container entirely (~120 MB total) —
adequate at ≤ 20 users, at the cost of departing from the fleet's
Postgres-everywhere convention. **Measure silo's actual headroom before
committing.**
