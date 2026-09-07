# 11 — Open Questions

**Status as of 2026-09-06: mostly resolved.** Ten decisions were made — see
[`00-decisions.md`](00-decisions.md), which is authoritative. This document keeps
the reasoning behind each, because the *why* matters when revisiting them later,
and lists the two things genuinely still open.

---

## Part 1 — Still open

### 1.1 What is the SMB/NFS share auth model on cellar? *(D9: deferred)*

Decided to defer, because the answer changes the shape of the connector
completely:

- **Samba bound to lldap** → tier A, near-zero work, accounts appear from the
  LDAP bind exactly like Nextcloud and Jellyfin. **Best case — check this first.**
- **Samba with local Unix users** → `useradd` + `smbpasswd` on cellar's *host*,
  not in a container. This would give `doser` host-level account-creation powers
  on a node holding your backup mirror, breaking the "only talks to container
  APIs" property that keeps its blast radius bounded.
- **NFS with UID mapping** → `doser` would have to own a stable cross-node UID
  namespace it otherwise never touches.

**Action:** find out whether Samba can bind to lldap. If yes, adopt it and shares
become free. If no, leave shares out of `doser` permanently rather than granting
host-level powers to reach them.

### 1.2 Does `bubbles` need a Headscale account?

Depends entirely on whether she has devices joining the tailnet. The `household`
profile (D8) excludes Headscale by default, so **this answers itself at
provisioning time** — add the connector for that one user if and when a device
needs it. No design work required now.

---

## Part 2 — Resolved, with reasoning retained

### 2.1 Does this overlap Komodo? → **D2: identity provisioning only**

The 2026-08-30 direction described "a full scale enterprise management system"
covering container management *and* user provisioning. Komodo on silo is already
the designated fleet container manager.

Scoping to identity avoids months of work reaching parity with something you are
deploying anyway, and avoids a second high-privilege control plane to secure,
back up, and keep current. If the underlying want is *one place to go*, that is a
Homepage tile group linking Komodo, `doser`, and lldap's admin UI.

### 2.2 Does this contradict "manual provisioning over orchestration"?

That decision was about *infrastructure* — building nodes and deploying stacks —
and its reasoning was learning, control, and blast radius during fleet bring-up.

Identity automation is a different thing: repetitive, error-prone, happens after
the infrastructure exists, and its failure mode is a wrong account rather than a
broken node. **It does not contradict the earlier decision, but the distinction
should be written into the fleet runbook** so future-you knows it was considered
and scoped rather than forgotten.

### 2.3 Home Assistant auth → **D5: HACS OIDC + break-glass**

Full analysis in doc 02 §9. The chosen path gives real SSO, removes the most
complex and most fragile connector from the project entirely, and lets doc 07 §3's
"never handles a password" rule hold with no exception.

The objection it had to answer — *a community component in the auth path of my
door locks* — is addressed by the local `barista` owner account in Vaultwarden.
**That account is a standing obligation, not a one-time setup step:** test it
after every HA core upgrade, since upgrade breakage is precisely the risk.

### 2.4 Secrets → **D6: plain gitignored `local.env`, mode 0600**

Consistent with sieve's 2026-08-28 pattern; no new tooling. SOPS+age not adopted
despite an unused age key existing on sieve.

Accepted risk, stated in full in doc 00: this file will hold the largest
concentration of high-value credentials on the fleet, unencrypted at rest. D7
removes the remote-exfiltration path that would be the likely attack, and the
residual threat is one where runtime-decryptable encryption buys little.

**Because the encryption control was declined, the compensating controls become
the mitigation rather than extras** — dedicated `svc-doser` accounts per target,
a rehearsed rotation runbook, and egress restriction from silo (doc 07 §4.2–4.4).

### 2.5 Network exposure → **D7: LAN + Headscale tailnet only**

Provisioning a household member is never an emergency phone task, so excluding
`doser` from the Cloudflare Tunnel removes the entire internet-facing attack
surface on the one service that can mint fleet-admin identities, at essentially
zero cost.

⚠️ Because the fleet uses split-horizon DNS with the same public domain both
ways, this needs an **explicit omission** in the Cloudflare Zero Trust dashboard,
which lives outside the repo. Nothing in git prevents someone adding the route
later. Noted in doc 09 §3.1, with an off-LAN verification step.

### 2.6 Provisioning profiles → **D8: `household` and `infra_admin`**

Without profiles, least-privilege depends on the operator remembering to
deselect services while tired. With them, the safe path is the default path.

Concretely, `bubbles` on `household` never gets an n8n account, never gets
Headscale unless a device needs it, and **never gets near Komodo** — enforcing
the least-privilege model decided on 2026-08-29 in the data model rather than by
discipline. Schema in doc 03 §2.1a.

### 2.7 Smaller items, all settled

| Question | Decision |
|---|---|
| Homepage or Homarr? | **D10: Homepage.** No user model, tier E, no connector |
| Stirling-PDF users? | **D11: no login, Authelia only.** Tier E. Per-user document history given up deliberately |
| ntfy per-user topics? | **D12: shared household topic.** No connector — and `doser` therefore never reaches ristretto, preserving that node's deliberate isolation |
| Onboarding delivery? | **D13: one-time enrolment links, in person.** No SMTP, sidesteps Authelia's unconfigured notifier |
| Service name? | **D1: `doser`** |
| Node placement? | **D3: silo.** Doc 01 §3 |
| Stack? | **D4: Python + FastAPI + HTMX + Postgres** |

---

## Part 3 — Deliberately not decided yet

These do not need answers before Phase 1, and deciding them early would be
guessing:

| Question | When to decide |
|---|---|
| Speedtest Tracker: single admin or per-user? | Phase 0 verification. Almost certainly tier E |
| Does Authelia session invalidation on disable actually work? | Phase 0 — doc 12 §11.1. **The most important verification in the plan** |
| Prometheus `/metrics`? | Only if the fleet ever gains a scraper. Monitoring is Gatus + ntfy; don't add a stack for one service |
| Should `doser` manage lldap **groups**, not just membership? | Phase 6 at the earliest. Groups change rarely and are easy to get wrong |
| Migrate to SOPS+age later? | If the fleet adopts it for other reasons, `doser` should be the first service to move |
