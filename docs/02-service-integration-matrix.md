# 02 — Service Integration Matrix

This is the core of the plan. Every container in the fleet, classified by how a
user account actually comes into existence in it.

> ⚠️ **Verification status.** Entries marked ✅ were confirmed by research on
> 2026-09-06 or are stable, long-standing behaviour. Entries marked ⚠️ are
> inferred and **must be checked against your running container and pinned image
> tag before building the connector**. Document 12 is the checklist. Nothing
> here should be treated as fact about your specific deployment until you have
> curl'd it.

---

## 1. The five tiers

| Tier | Meaning | What the tool does |
|---|---|---|
| **A — LDAP-native** | Service binds to lldap directly; account materialises on first successful login | Nothing beyond lldap write + group membership. Optionally pre-warm. |
| **B — OIDC-native (JIT)** | Service is an Authelia OIDC relying party and auto-creates the local account on first login | Nothing at provision time. Track as `pending_first_login`. Some also support pre-creation — see notes. |
| **C — API/CLI required** | No JIT path, or JIT exists but pre-creation is strongly preferable | Call the service's API or run a CLI command in its container. This is where connectors live. |
| **D — Partially manual** | Automated step exists but a human must finish it | Automate what's possible, emit an explicit manual task with instructions. |
| **E — No user model** | Access is entirely Authelia forward-auth | Group membership only. No per-service account exists. |

**Why tier matters:** a service in tier A or B that you *also* pre-create an
account in usually ends up with **two unlinked accounts** — one from your API
call, one from first login — because the services match on different keys
(sometimes username, sometimes email, sometimes provider subject claim). Getting
the tier right is the difference between provisioning and creating a mess.

---

## 2. sieve — gateway

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **lldap** | **SOURCE OF TRUTH** | GraphQL `createUser` mutation | ✅ `CreateUserInput` accepts `id`, `email`, `displayName`, `firstName`, `lastName`, `avatar` (base64 JPEG), `attributes`. **The `avatar` field is deprecated in favour of custom attributes; if both are supplied the attribute wins.** Group membership via `addUserToGroup`. Auth via a JWT obtained from `/auth/simple/login`. |
| **Authelia** | — | No provisioning needed | Users come from lldap. Authelia's own storage holds only 2FA devices and preferences, created on first use. Nothing to pre-create. Authelia is also the **OIDC provider (OP)** that makes tier B possible for everything else. |
| **Pi-hole** | E | Authelia only | Per the 2026-08-29 decision, Pi-hole's own password is disabled (`FTLCONF_webserver_api_password: ""`) and Authelia is the sole gate. There is no per-user account. Requires `purrbrews_infra_admins`. |
| **Traefik dashboard** | E | Authelia only | Requires `purrbrews_infra_admins`. |
| **Headscale** | C | CLI / gRPC: `headscale users create <name>` | ⚠️ Deliberately **not** behind Authelia (tailscale clients can't do interactive login), so this is a genuine separate account namespace. Provisioning creates the Headscale user; the pre-auth key for device enrolment should be generated on demand, not at provisioning time (short TTL). Connector shells into the container or uses the gRPC socket. |
| **Cloudflare Tunnel** | E | n/a | No user model. |
| **Redis (Authelia)** | E | n/a | |

---

## 3. silo — security / QoL / fleet ops

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **Komodo** | **D** | OIDC login, then **manual enable** | ✅ Komodo supports generic OIDC (open-source edition, redirect `<HOST>/auth/oidc/callback`). **But Komodo has no way to provision OIDC users** — the account appears only after first login and an admin must then click *Enable User* and assign permissions. The connector cannot automate this. It emits a manual task: "after $USER's first Komodo login, enable the account and set permissions." Only relevant for `purrbrews_infra_admins` members — `penguin`/`bubbles` should never touch Komodo. |
| **Homepage** | E ✅ *decided D10* | Static dashboard, Authelia-gated | Homepage is what's deployed, not Homarr. No user model at all. Per-user tile visibility, if ever wanted, is a config-file concern, not a provisioning one. |
| **CrowdSec** | E | n/a | Machine/bouncer credentials only, not human users. |
| **Unbound** | E | n/a | |
| **NetAlertX** | E | Authelia only | Single shared password historically; disable it and rely on Authelia, same pattern as Pi-hole. |
| **Speedtest Tracker** | ⚠️ C | Laravel app with its own users; has an admin user creation artisan command | ⚠️ Verify. Likely simplest to leave single-admin and Authelia-gate it (treat as E). |
| **Scrutiny** | E | Authelia only | |
| **Diun** | E | Notification-only | |

---

## 4. cellar — vault / archive

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **Vaultwarden** | **C** | Admin API invite | ✅ Enable the admin page (`ADMIN_TOKEN`), ensure `INVITATIONS_ALLOWED` is not `false`, then `POST /admin/invite` with the email. **Vaultwarden's zero-knowledge design means only an invitation is possible — never a password or key.** The user completes registration and sets their own master password. This is the correct and only model. Reference implementations exist (`vividboarder/vaultwarden_ldap`, `sirtoobii/vaultwarden_ldap_sync`) — read them; do not necessarily deploy them, since this tool subsumes their job. |
| **SMB / NFS shares** | **DEFERRED** ✅ *decided D9* | Auth model not yet known | **Out of scope for every phase.** The auth model determines the connector's shape entirely: Samba bound to lldap would be tier A and free; Samba with local Unix users would mean `useradd` + `smbpasswd` **on cellar's host**, breaking the "only talks to container APIs" property and giving `doser` host-level account powers; NFS with UID mapping would make `doser` own a UID namespace it otherwise wouldn't touch. <br>**Action:** investigate whether Samba can bind to lldap. If yes, this becomes tier A at near-zero cost. If no, keep it deferred rather than granting host-level powers. |

---

## 5. percolator — core stateful apps

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **Home Assistant** | **B** ✅ *decided D5* | HACS OIDC auth provider, JIT on first login | ✅ HA still has no native OIDC RP support in core as of 2026.9. **Decision D5: install a HACS OIDC auth provider** (`christiaangoossens/hass-oidc-auth` or equivalent) pointed at Authelia. HA then creates the account on first SSO login — **no connector needed, no HA credentials held by `doser`**. <br>**Mandatory companion:** keep one local HA owner account (`barista`) with a strong password in Vaultwarden that never goes through OIDC, so an Authelia or component failure can never lock you out of your own house. 🔍 **Test the break-glass path before relying on it.** <br>🔍 Read the component's source before installing — it sits in the auth path of your door locks. Test on a throwaway HA instance first. |
| **Nextcloud** | **A or B** (choose one) | `user_ldap` app, **or** `user_oidc` app, **or** OCS Provisioning API | ✅ Three real options. <br>**Recommended: `user_ldap`** — bind to lldap, users appear on first login, group mapping comes free, and it is the most battle-tested path. Tier A. <br>Alternative: `user_oidc` against Authelia (tier B). <br>Fallback / pre-creation: OCS Provisioning API `POST /ocs/v2.php/cloud/users` with `OCS-APIRequest: true` — but **do not combine this with `user_ldap`**; you will get a local account shadowing the LDAP one. |
| **Paperless-ngx** | **B** | django-allauth OIDC | ✅ OIDC via `PAPERLESS_APPS=allauth.socialaccount.providers.openid_connect` + `PAPERLESS_SOCIALACCOUNT_PROVIDERS`. Requires **v2.5.0+**. Auto-signup from the IdP is supported (`PAPERLESS_SOCIAL_AUTO_SIGNUP`). ⚠️ For pre-creation there is a Django management command (`createsuperuser`) and a `/api/users/` endpoint, but ⚠️ **API user-creation is not clearly documented** — treat pre-creation as unverified and prefer JIT. Permissions/groups still need assigning after first login unless you map them from the OIDC groups claim (⚠️ verify group-claim mapping works in your version). |
| **Postgres ×4, Redis** | E | n/a | Service accounts only, not human users. |
| **Traefik (percolator instance)** | E | n/a | Internal routing. |

---

## 6. mochaPot — media / utility

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **Jellyfin** | **A or C** | LDAP-Auth plugin, SSO-Auth plugin, or `POST /Users/New` | ✅ Three paths. **Recommended: the official LDAP-Auth plugin** bound to lldap → tier A, users appear on first login, and library permissions can be defaulted per-plugin-config. Alternative: SSO-Auth plugin for OIDC (tier B, third-party plugin). Fallback: `POST /Users/New` with an admin API key (tier C) — use only if you are not using LDAP, otherwise you get duplicates. ⚠️ In all cases, **library access grants are separate from account creation** and must be set explicitly, or the new user sees everything. |
| **Immich** | **B + C (both)** | `POST /api/admin/users` **and** OAuth auto-register | ✅ Immich has both a real admin user-creation API (`createUserAdmin`) and OIDC with auto-registration. **Recommended: pre-create via the admin API, then let OIDC link by email.** This is one of the few services where pre-creation is genuinely better, because it lets you set the storage quota and storage label at creation time rather than after the fact. ⚠️ Confirm your Immich version's OAuth `autoRegister` matches on email and does not create a second account. |
| **Vikunja** | **B** | OIDC via `auth.openid.providers` | ⚠️ Vikunja creates the account on first OIDC login. Local registration can be disabled entirely (`auth.local.enabled: false`), which is the right posture once SSO works. ⚠️ Verify whether your version supports LDAP as well; if so, tier A is also available. Team/project membership is not provisionable at creation — emit a manual task if the user needs access to a shared project. |
| **n8n** | **C** (community) | `POST /api/v1/users` | ✅ The public API creates/invites users: `POST /api/v1/users` with email and role (`global:member`); the response includes an `inviteAcceptUrl`. **This is ideal for this tool** — no SMTP needed, the tool captures the invite URL and includes it in the enrolment packet. ⚠️ Note: **SAML/OIDC SSO in n8n is Enterprise-only**, so the community edition genuinely needs this connector — it cannot be tier B. Requires an API key from an owner account. |
| **Mealie** | ⚠️ A / B / C | `LDAP_AUTH_ENABLED`, `OIDC_AUTH_ENABLED`, or `POST /api/admin/users` | ⚠️ Mealie supports all three. Recommend LDAP (tier A) for consistency with Jellyfin/Nextcloud. Verify against your pinned version — Mealie's auth env vars have changed across major versions. |
| **FreshRSS** | **C** | CLI only | ⚠️ FreshRSS has **no REST API for user creation.** Provisioning is `php ./cli/create-user.php --user X --password Y` executed inside the container. Alternatively `AUTH_TYPE=http_auth` makes it trust Authelia's `Remote-User` header and auto-create — which is cleaner and turns it into tier B-ish. **Recommended: `http_auth` behind Authelia**, avoiding a docker-exec connector entirely. ⚠️ Verify that FreshRSS's `http_auth` mode auto-creates unknown users in your version; if it does not, you still need the CLI connector for the first login. |
| **Actual Budget** | **B (conditional)** | OIDC + `ACTUAL_USER_CREATION_MODE=login` | ✅ Multi-user requires an OpenID provider. **Actual does not auto-create OIDC users unless `ACTUAL_USER_CREATION_MODE=login` is set** — set it, and it becomes tier B. Roles (Basic vs Admin) and budget-file access are assigned afterward in the User Directory. ⚠️ No documented REST user-creation API — pre-creation is not available. Emit a manual task for budget-file sharing. |
| **Stirling-PDF** | **E** ✅ *decided D11* | Login disabled; Authelia is the only gate | No local user store in use, so no connector. Per-user document history is given up deliberately — an acceptable trade for a PDF utility. |
| **Roundcube** | **Special** | No user store of its own | ⚠️ Roundcube authenticates against **IMAP** — it has no accounts. "Provisioning a Roundcube user" actually means provisioning a mailbox on whatever mail backend it points at. **The initiation set does not name a mail server.** Until that exists, this is a no-op with a documented note. If a mail backend is later added (Stalwart, Mailcow, Dovecot), that becomes the tier-C connector and Roundcube stays tier E. |
| **Music Assistant** | E | Single-user | No account model. Authelia-gated. |
| **Traefik (mochaPot)** | E | n/a | |

---

## 7. ristretto & roastery

| Service | Tier | Mechanism | Notes |
|---|---|---|---|
| **Gatus** | E | Read-only status page | No users. Deliberately isolated — this tool should have **no dependency on and no credentials for ristretto**. |
| **ntfy** | **E** ✅ *decided D12* | Shared household topic; no per-user accounts | No connector. This preserves ristretto's deliberate isolation — `doser` holds no credentials for it and never reaches it, so ristretto keeps observing the fleet even when the fleet's identity plane is broken. (`doser` still *sends* alerts to ntfy one-way; that's a publish, not provisioning.) Revisit only if per-user notification routing becomes a real need. |
| **Ollama** | E | No auth model | roastery, tailnet-only. |

---

## 8. Summary counts

After the 2026-09-06 decisions (doc 00):

| Tier | Count | Services |
|---|---|---|
| Source of truth | 1 | lldap |
| A — LDAP-native | 3 | Nextcloud, Jellyfin, Mealie |
| B — OIDC JIT | 6 | **Home Assistant** (D5), Paperless-ngx, Vikunja, Actual Budget, Immich (also C), FreshRSS (via `http_auth`) |
| C — API/CLI connector | **4** | Vaultwarden, Immich, n8n, Headscale |
| D — partially manual | 1–2 | Komodo, (Actual Budget role/file grants) |
| E — no user model | 17 | Pi-hole, Traefik ×3, Unbound, CrowdSec, Diun, Scrutiny, NetAlertX, Speedtest, Homepage, Gatus, Music Assistant, Ollama, Stirling-PDF, ntfy |
| Deferred | 1 | SMB/NFS shares (D9) |

**The headline:** out of ~30 containers, **four need a real connector** — five
counting lldap itself. Home Assistant, ntfy, Stirling-PDF and the file shares
were all eliminated by the decisions in doc 00, and Headscale is only needed for
users whose devices join the tailnet.

That is the number the roadmap in doc 10 is built around, and it is why the
"loop over every service" design would have been almost entirely wasted work.

---

## 9. The Home Assistant decision — ✅ RESOLVED (D5: Option A)

**Decided 2026-09-06: Option A — HACS OIDC component, with a break-glass local
owner account.** HA is tier B. There is no HA connector, `doser` holds no HA
credentials, and doc 07 §3's "never handles a password" rule now holds with no
exception.

The reasoning is kept below because the failure mode it guards against is worth
remembering, and because the break-glass account is a standing obligation rather
than a one-time setup step.

**Three things this decision commits you to:**

1. **Create the break-glass account first, before enabling OIDC.** A local HA
   owner (`barista`) with a strong password stored in Vaultwarden, never routed
   through OIDC.
2. **Test the break-glass path before relying on it** — log in with it while
   Authelia is deliberately stopped. An untested break-glass is not a
   break-glass.
3. **Re-test it after every HA core upgrade**, since the risk being managed is
   precisely that a community auth component breaks on an update. Put this in
   the fleet runbook next to the upgrade procedure.

<details>
<summary>Original analysis (superseded)</summary>

**Option A — HACS OIDC component** (`hass-oidc-auth` or similar)
- ✅ Real SSO. One password for the household. Users appear automatically.
- ✅ No fragile internal-API connector to maintain.
- ❌ Your home automation — locks, alarms, heating — depends on a
  community-maintained custom component for authentication. If it breaks after
  an HA core update, you may be locked out of the thing that controls your
  physical space.
- ❌ HACS components are not covered by HA's own security process.

**Option B — provision via HA's WebSocket API, keep local HA passwords**
- ✅ HA's auth path stays 100% stock. No third-party code in the login flow.
- ✅ Works today, no version-gating.
- ❌ Users have a separate HA password. Not real SSO. Exactly the executive-function
  friction the whole project exists to reduce.
- ❌ The API is internal-and-undocumented; it can change without notice.
- ❌ The tool must generate and transmit an initial HA password, which breaks the
  "this tool never handles passwords" rule from doc 01 §5. (Mitigable: generate a
  random one and force change at first login — but HA doesn't have a clean
  force-change mechanism, so it's a manual instruction.)

**Recommendation: Option A, with a documented break-glass.** Keep one local HA
owner account (`barista`) with a strong password stored in Vaultwarden that
never goes through OIDC, so an OIDC or Authelia failure can never lock you out
of your own house. Test the break-glass path before relying on it. Then HA
becomes tier B and needs no connector at all.

This is worth deciding before anything else, because it removes or adds the
single most complex connector in the project.

</details>

---

## 10. Cross-cutting: what pre-creation gets you that JIT does not

Even for tier A/B services, there are three things JIT cannot do, and they are
the reason a couple of pre-creation connectors are still worth building:

1. **Quotas and storage placement** (Immich storage label, Nextcloud quota) —
   must be set before the user uploads anything, or you're migrating files later.
2. **Group and library permissions** — a JIT user typically lands with default
   permissions. If the default is "sees everything," JIT alone is a
   least-privilege failure.
3. **Knowing the account exists.** A JIT-only user is invisible to your fleet
   until they log in for the first time — which means your provisioning tool
   cannot tell you whether onboarding actually completed. This is why the data
   model (doc 03) tracks `pending_first_login` as a first-class state and the
   reconciler (doc 08 §6) polls for it.
