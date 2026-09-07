# 00 — Decisions Log

Decisions made by User Penguin on **2026-09-06**, before any code was written.
These are settled. Where a later document still discusses alternatives, this
file wins.

---

| # | Decision | Choice | Consequence |
|---|---|---|---|
| D1 | Service name | **`doser`** | The grinder component that measures out a single dose. Used throughout the docs. |
| D2 | Project scope | **Identity provisioning only** | Komodo on silo remains the container manager. If a single entry point is wanted later, that's a Homepage tile group, not a second control plane. |
| D3 | Node placement | **silo** | Rationale in doc 01 §3. Dedicated Postgres, ~500 MB budget. |
| D4 | Backend stack | **Python 3.12 + FastAPI**, Jinja2 + HTMX + SSE, Postgres 16 | No JS build toolchain. Doc 01 §2. |
| D5 | Home Assistant auth | **HACS OIDC component + break-glass local owner** | HA becomes **tier B** (JIT on first SSO login). **The HA connector is deleted from the plan entirely.** Doc 02 §9. |
| D6 | Secrets storage | **Plain gitignored `local.env`, mode 0600** | Consistent with sieve's 2026-08-28 pattern. SOPS+age not adopted. See "Accepted risk" below. |
| D7 | Network exposure | **LAN + Headscale tailnet only** | Not routed through the Cloudflare Tunnel. Removes the internet-facing surface entirely. |
| D8 | Provisioning profiles | **Two: `household` and `infra_admin`** | Least-privilege becomes the default path. Adds a `profile` column and a profile→connector map to doc 03. |
| D9 | SMB/NFS shares on cellar | **Deferred — auth model unknown** | No share connector in any phase. Investigate whether Samba can bind to lldap; if yes it becomes tier A for free. |
| D10 | Homepage vs Homarr | **Homepage is deployed** | No user model. Tier E. The Homarr row is removed from doc 02. |
| D11 | Stirling-PDF | **No login; Authelia is the only gate** | Tier E. No connector. |
| D12 | ntfy | **Shared household topic, no per-user accounts** | No ntfy connector. Keeps `doser` from needing to reach ristretto at all, preserving that node's deliberate isolation. |
| D13 | Onboarding delivery | **One-time enrolment links, delivered in person** | No SMTP dependency. Sidesteps Authelia's unconfigured notifier. |

---

## What these decisions removed from the project

Four of the seven planned connectors are gone:

| Connector | Removed by | Was going to be |
|---|---|---|
| **Home Assistant** | D5 | The single most complex and most fragile connector — an undocumented WebSocket API that could break on any HA upgrade |
| **ntfy** | D12 | An SSH-based connector against a Pi Zero W with no Docker — a different shape from everything else in the codebase |
| **SMB/NFS shares** | D9 | Potentially `useradd` on cellar's host, breaking the "only talks to container APIs" property |
| **Stirling-PDF** | D11 | A small one, but still a connector |

**Remaining connectors: four.** lldap (the source of truth), Vaultwarden,
Immich, n8n — plus Headscale, which is only needed for users with devices that
join the tailnet, and Komodo as a manual-instruction stub.

D5 is the biggest single simplification. It also means **doc 07 §3's "this tool
never handles a password" rule now holds absolutely**, with no exception —
which was the strongest argument for choosing it.

---

## Accepted risk: D6 (plain `local.env`)

Recorded plainly so it is a known trade rather than an oversight.

`doser`'s `local.env` will hold the lldap admin credential, Vaultwarden's
`ADMIN_TOKEN`, an Immich admin API key, and an n8n owner API key. That is a
larger concentration of high-value credentials than any other single file on the
fleet, and it will sit at rest unencrypted on silo's disk.

The choice is defensible: it is consistent with sieve, it needs no new tooling,
and D7 removes the internet-facing path that would make remote exfiltration the
likely attack. The realistic threat is physical access to silo or a compromise
that already has filesystem read as the service user — at which point an
encrypted file that the service must be able to decrypt at runtime buys less
than it appears to.

Compensating controls, all of which should actually be done:

- File mode `0600`, owned by the service user; never in the repo; pre-commit
  secret scanning confirmed to cover this repo.
- **Dedicated `svc-doser` service accounts** on each target rather than reusing
  `barista`'s credentials, so revocation is surgical and the target's own audit
  log distinguishes tool actions from human ones. (Doc 07 §4.2.)
- A **rehearsed rotation runbook** — not an aspiration. Doc 07 §4.3.
- Egress restriction from silo so a compromised dependency can reach only the
  four hosts `doser` already talks to. Doc 07 §4.4.

Revisit if the fleet ever adopts SOPS+age for other reasons; this service should
be the first to migrate.

---

## Still genuinely open

Only two things, both small and both deferrable past Phase 1:

1. **The cellar share auth model** (D9) — investigate, don't design yet.
2. **Whether `bubbles` needs a Headscale account** — depends on whether she has
   devices joining the tailnet. Answerable at provisioning time via the
   `household` profile, which excludes Headscale by default.

Everything in doc 11 that these decisions settled has been marked resolved
there.
