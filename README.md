# PurrBrews User Provisioning

Planning repository for a self-hosted user-provisioning web application for the
PurrBrews fleet.

**Status:** planning only — no implementation code in this repo yet.
**Owner / decision-maker:** User Penguin (Jyotirmoy).
**Date of this plan:** 2026-09-06.

---

## 1. What this is

A single admin web page that takes a person's details — username, email, first
name, last name, profile picture, or an imported `.vcf` contact card — and turns
that into a working identity across the whole PurrBrews ecosystem: lldap,
Authelia, Home Assistant, Nextcloud, Immich, Jellyfin, Paperless-ngx,
Vaultwarden, Vikunja, and the rest of the container fleet.

It also handles the other half of the lifecycle that people forget until they
need it: **disable, re-enable, update, and offboard**.

## 2. The core architectural claim

> **Provisioning is not fan-out. It is "write the identity once, then reconcile."**

The naive reading of "provision an account across all services" is a loop that
calls 25 APIs. That design is wrong for this fleet, for three reasons:

1. **Most services do not need it.** Roughly two-thirds of the fleet either bind
   to lldap directly or accept OIDC from Authelia. For those, an account
   materialises on first login. Pre-creating one is redundant at best and
   creates a duplicate, unlinked account at worst.
2. **Many services have no user model at all.** Pi-hole, Traefik's dashboard,
   Scrutiny, Diun, NetAlertX, Gatus, Homepage — these are gated entirely by
   Authelia forward-auth. "Provisioning" for them means *group membership*, not
   an account.
3. **The ones that do need an API call cannot be transactional.** There is no
   two-phase commit across Immich, Vaultwarden and Home Assistant. A loop that
   half-succeeds leaves the fleet in an undefined state with no way back.

So the design is:

- **lldap is the single source of truth.** The write to lldap is the commit
  point. Everything downstream is eventually-consistent.
- **Every target service is classified into one of five integration tiers**
  (see [`docs/02-service-integration-matrix.md`](docs/02-service-integration-matrix.md)).
  The tier determines whether the tool calls an API, does nothing, or prints a
  manual step.
- **The engine is a saga with compensation and a reconciliation loop**, not a
  for-loop (see [`docs/08-provisioning-engine.md`](docs/08-provisioning-engine.md)).
- **The UI reports a per-service status grid**, never a single green tick.

## 3. Document set

| # | Document | What it covers |
|---|---|---|
| **00** | **[Decisions log](docs/00-decisions.md)** | **The ten decisions made 2026-09-06. Authoritative — read this first** |
| 01 | [Architecture](docs/01-architecture.md) | Components, stack choice, node placement, request flow |
| 02 | [Service integration matrix](docs/02-service-integration-matrix.md) | Every fleet container, its tier, its provisioning mechanism |
| 03 | [Data model](docs/03-data-model.md) | Postgres schema, state machines, audit log |
| 04 | [API specification](docs/04-api-spec.md) | FastAPI endpoint surface, schemas, SSE stream |
| 05 | [Frontend specification](docs/05-frontend-spec.md) | Screens, form design, live progress, error UX |
| 06 | [vCard & avatar pipeline](docs/06-vcard-and-avatar-pipeline.md) | RFC 6350 parsing, field mapping, image normalisation, SSRF defence |
| 07 | [Security model](docs/07-security-model.md) | Threat model, authz, credential storage, blast radius |
| 08 | [Provisioning engine](docs/08-provisioning-engine.md) | Connector contract, saga, idempotency, reconciliation, drift |
| 09 | [Deployment](docs/09-deployment.md) | Compose stack, config, secrets, Traefik/Authelia wiring |
| 10 | [Roadmap](docs/10-roadmap.md) | Phases, exit criteria, effort shape |
| 11 | [Open questions](docs/11-open-questions.md) | Reasoning behind each decision; the two things still genuinely open |
| 12 | [Verification checklist](docs/12-verification-checklist.md) | Every API claim in this plan that must be confirmed hands-on before building |

**Read 00, then 02, then 12.** Document 00 is what was decided and why. Document
02 is where the real work is. Document 12 is the honest accounting of what in
this plan is researched versus what is inferred and needs checking against your
actual running containers.

## 4. Relationship to existing PurrBrews decisions

This plan is the scoped version of the direction recorded on 2026-08-30 ("a full
scale enterprise management system"). Both prior decisions are now reconciled
(doc 00):

- **Komodo already covers container management.** ✅ D2 scopes this project to
  *identity provisioning only*. If you want a unified admin shell later, that is
  a Homepage tile group linking Komodo, `doser`, and lldap's admin UI — not a
  second control plane to secure and keep current.
- **"Manual provisioning over automated orchestration" was a deliberate choice.**
  Automating *identity* is narrower than automating *infrastructure*: it happens
  after the nodes exist, and its failure mode is a wrong account rather than a
  broken node. It does not contradict the earlier decision, but that distinction
  belongs in the fleet runbook so it reads as scoped rather than forgotten.

**What the 2026-09-06 decisions removed:** four of the seven planned connectors.
Home Assistant (HACS OIDC instead), ntfy, Stirling-PDF, and the cellar file
shares are all out. **Four connectors remain** — Vaultwarden, Immich, n8n, and
Headscale — plus lldap itself and a ~20-line manual-instruction stub for Komodo.

## 5. Name

The deployed service is **`doser`** — the grinder component that measures out a
single dose. Decided 2026-09-06 (D1); used throughout these documents.

## 6. Non-goals

- Not a container manager (that is Komodo).
- Not a replacement for lldap's admin UI — it sits on top of it.
- Not multi-tenant. Household scale: single-digit users, tens over the lifetime.
- Not an SCIM server. SCIM is noted as a possible later interface in the
  roadmap, but nothing in the fleet speaks SCIM today.
- Not self-service. This is an operator tool. Users do not create their own
  accounts; they receive an enrolment link.
