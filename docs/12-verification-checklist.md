# 12 — Verification Checklist

**Read this before you write any code.**

This plan was assembled from research on 2026-09-06 plus general knowledge of
these projects. It has **not** been tested against your running fleet, and no
session has had SSH access to your nodes. Self-hosted projects change their auth
and API surfaces frequently, and several claims below are version-dependent.

Everything here is a claim that could be wrong for *your* pinned image tags.
Work through it in Phase 0. Record the actual request and response shapes as you
go — that record becomes the connector implementation spec.

**Legend:** ✅ researched and confirmed 2026-09-06 · ⚠️ inferred, verify ·
🔍 verify against your specific version/config

---

## 1. lldap — the foundation

| # | Claim | Status | How to verify |
|---|---|---|---|
| 1.1 | GraphQL `createUser` mutation exists with `CreateUserInput { id, email, displayName, firstName, lastName, avatar, attributes }` | ✅ | Fetch `/api/graphql` schema; compare to `lldap/lldap` `schema.graphql` at your tag |
| 1.2 | `avatar` accepts base64 JPEG but is **deprecated in favour of attributes**, and the attribute wins if both are set | ✅ | Set both, observe which renders. **Decide which you use and be consistent.** |
| 1.3 | Auth via `POST /auth/simple/login` returning a JWT usable on `/api/graphql` | ⚠️ | curl it |
| 1.4 | `addUserToGroup` / `removeUserFromGroup` mutations | ⚠️ | Schema |
| 1.5 | A user can be **disabled** without deletion | 🔍 | **Important.** If lldap has no disable primitive at your version, offboarding must be implemented as group removal + password scramble. This changes doc 08 §7 materially. |
| 1.6 | Duplicate-uid create returns a distinguishable error | ⚠️ | Needed for idempotency (doc 08 §4) |

---

## 2. Authelia

| # | Claim | Status | How to verify |
|---|---|---|---|
| 2.1 | Emits `Remote-User`, `Remote-Groups`, `Remote-Email`, `Remote-Name` on forward-auth | ⚠️ | Echo headers from a test container |
| 2.2 | Is a usable **OIDC provider** for Immich/Paperless/Vikunja/Actual/Komodo | 🔍 | Authelia 4.39+ has a mature OP. Confirm your version and that the OIDC config is actually enabled — the sieve build notes say Authelia came up on first-bring-up defaults |
| 2.3 | Disabling a user in lldap invalidates existing Authelia sessions | 🔍 | **Verify explicitly.** If sessions survive in Redis until TTL, offboarding needs a Redis session flush step. Test: log in, disable in lldap, refresh. |
| 2.4 | `two_factor` policy is actually usable (notifier working) | 🔍 | Notes say filesystem notifier from bring-up. Doc 09 §3 depends on this |
| 2.5 | An OIDC group claim can be emitted for downstream authorisation | 🔍 | Needed if you want Paperless/Immich roles mapped automatically |

---

## 3. Home Assistant — highest uncertainty

**D5 chose the HACS OIDC path**, so rows 3.3 and 3.4 are moot — there is no HA
connector and `doser` holds no HA credentials. What remains is setup
verification, and it is the highest-stakes item in this document because getting
it wrong locks you out of your own home.

| # | Claim | Status | How to verify |
|---|---|---|---|
| 3.1 | HA core still has **no native OIDC RP support** as of 2026.9 | ✅ | Confirmed 2026-09-06 |
| 3.2 | A HACS OIDC component works against your Authelia version | 🔍 | **Test on a throwaway HA instance, never your live one.** Read the source before installing — it sits in the auth path of your door locks |
| 3.3 | **Break-glass local owner (`barista`) still logs in with OIDC enabled** | 🔍 | **Create and test this BEFORE installing the component.** Test properly: stop Authelia, then log in. A break-glass you haven't exercised is not a break-glass |
| 3.4 | Break-glass survives an HA core upgrade | 🔍 | **Re-test after every HA upgrade.** Component breakage on upgrade is the exact risk being managed. Add to the fleet runbook next to the upgrade procedure |
| 3.5 | OIDC-created HA users land with sane default permissions | 🔍 | A JIT user who arrives as an admin is a least-privilege failure. Check what a first login actually produces |
| ~~3.6~~ | ~~WS `config/auth/create` internal API~~ | **N/A** | Removed by D5 |
| 3.7 | `config/auth/list` over the internal WebSocket API also works as a **read-only** check, separate from the (removed) write path | ⚠️ | Implemented 2026-09-07: `HomeAssistantConnector.verify()` uses it to confirm a JIT login automatically, but only if `DOSER_HASS_TOKEN` is set — D5 still holds by default (no token, no HA credentials). Same undocumented-API caveat as 3.6 applies: it can break on any HA release, so a failure here degrades to "pending_first_login", never a hard error |

---

## 4. Nextcloud

| # | Claim | Status | How to verify |
|---|---|---|---|
| 4.1 | `user_ldap` binds to lldap and creates users on first login | ⚠️ | Standard, but the lldap↔Nextcloud attribute mapping needs care |
| 4.2 | OCS `POST /ocs/v2.php/cloud/users` with `OCS-APIRequest: true` | ⚠️ | Only for the non-LDAP path |
| 4.3 | **Combining `user_ldap` with OCS-created local users causes shadowing** | ⚠️ | Pick one path. Verify by trying both on a test instance |
| 4.4 | Avatars flow from LDAP `jpegPhoto` | 🔍 | Requires explicit mapping config; often missed |
| 4.5 | Quota can be set at/near creation | 🔍 | |
| 4.6 | `GET /ocs/v2.php/cloud/users/{uid}` works read-only against an LDAP-backed account too, once Nextcloud has provisioned it internally on first bind | ⚠️ | Implemented 2026-09-07: `NextcloudConnector.verify()` uses this in `ldap` mode when `DOSER_NEXTCLOUD_ADMIN_USER/_PASSWORD` are set, purely to confirm the login — never to create or modify anything |

---

## 5. Immich

| # | Claim | Status | How to verify |
|---|---|---|---|
| 5.1 | `POST /api/admin/users` (`createUserAdmin`) exists | ✅ | api.immich.app — but **confirm the path for your version**, Immich has moved endpoints before |
| 5.2 | OAuth `autoRegister` links to a pre-created account **by email** rather than creating a duplicate | ⚠️ | **Test this specifically.** It is the whole basis of the pre-create-then-OIDC strategy in doc 02 §6 |
| 5.3 | Storage quota and storage label settable at creation | ⚠️ | The main reason to pre-create at all |
| 5.4 | Profile image upload endpoint | ⚠️ | Doc 06 §3.3 |
| 5.5 | Soft-delete for compensation | ⚠️ | Doc 08 §5 |

---

## 6. Vaultwarden

| # | Claim | Status | How to verify |
|---|---|---|---|
| 6.1 | Admin page + `ADMIN_TOKEN` enables the invite API | ✅ | Confirmed 2026-09-06 |
| 6.2 | `POST /admin/invite` with an email invites a user | ⚠️ | Read `vividboarder/vaultwarden_ldap` — it does exactly this and is the best available reference |
| 6.3 | `INVITATIONS_ALLOWED` must not be `false` | ✅ | Check your compose env |
| 6.4 | Invite can be revoked while unaccepted (compensation) | ⚠️ | |
| 6.5 | Only invitation is possible — never a password or key | ✅ | Zero-knowledge by design. This is correct behaviour, not a limitation |

---

## 7. Jellyfin

| # | Claim | Status | How to verify |
|---|---|---|---|
| 7.1 | Official LDAP-Auth plugin binds to lldap | ⚠️ | |
| 7.2 | `POST /Users/New` with an admin API key | ⚠️ | Non-LDAP path only |
| 7.3 | **Library access is NOT granted automatically** on user creation | ⚠️ | **Verify.** If true, a new user may see everything or nothing — both are wrong defaults. This is a least-privilege issue, not cosmetic |
| 7.4 | `POST /Users/{id}/Images/Primary` for avatars | ⚠️ | Needs the Jellyfin user id, only available post-first-login |

---

## 8. Paperless-ngx

| # | Claim | Status | How to verify |
|---|---|---|---|
| 8.1 | OIDC via django-allauth, **v2.5.0+ required** | ✅ | Check your pinned tag |
| 8.2 | `PAPERLESS_SOCIAL_AUTO_SIGNUP` auto-creates on first login | ⚠️ | |
| 8.3 | `/api/users/` supports programmatic creation | ⚠️ | **Not clearly documented — assume no.** Prefer JIT |
| 8.5 | `GET /api/users/` (DRF, paginated `{"results": [...]}`) lists users when called with a valid Token | ⚠️ | Implemented 2026-09-07 as `PaperlessConnector.verify()`, read-only, only when `DOSER_PAPERLESS_TOKEN` is set. Confirm the response shape against your version — code degrades to "pending_first_login" on any unexpected shape rather than guessing |
| 8.4 | OIDC group claims map to Paperless permissions | 🔍 | If not, every user needs manual permission assignment — a tier-D manual task |

---

## 9. Vikunja / Mealie / Actual / n8n / FreshRSS

| # | Claim | Status | How to verify |
|---|---|---|---|
| 9.1 | Vikunja OIDC via `auth.openid.providers`, JIT create | ⚠️ | |
| 9.2 | Vikunja local registration can be fully disabled | ⚠️ | Right posture once SSO works |
| 9.3 | Vikunja LDAP support at your version | 🔍 | Would make it tier A |
| 9.4 | Mealie `LDAP_AUTH_ENABLED` / `OIDC_AUTH_ENABLED` env var names | 🔍 | **These have changed across Mealie major versions.** Check your tag's docs |
| 9.5 | Actual Budget: `ACTUAL_USER_CREATION_MODE=login` is required for OIDC auto-create | ✅ | Confirmed 2026-09-06 |
| 9.6 | Actual has **no** REST user-creation API — and, checked 2026-09-07, no public API for **listing** users either (only an npm package for its own sync client, plus an admin-only `/admin/oauth-clients` API unrelated to user accounts) | ✅ | `ActualConnector.verify()` deliberately always returns `pending_first_login` rather than guess at an internal admin-UI route. Confirm in Actual → Settings → User Directory by hand |
| 9.7 | Actual roles + budget-file access are manual post-creation | ✅ | Tier-D manual tasks |
| 9.11 | Vikunja `GET /api/v1/users?s=<query>` (the same search used for project/team sharing) also matches on exact username, when called with a personal API token | ⚠️ | Implemented 2026-09-07 as `VikunjaConnector.verify()`, read-only, only when `DOSER_VIKUNJA_TOKEN` is set. Confirm against your version at `/api/v1/docs` |
| 9.12 | Mealie `GET /api/admin/users` (paginated `{"items": [...]}`) lists users when called with an admin account's token | ⚠️ | Implemented 2026-09-07 as `MealieConnector.verify()`, read-only, only when `DOSER_MEALIE_TOKEN` is set |
| 9.8 | n8n `POST /api/v1/users` returns `inviteAcceptUrl` | ✅ | Confirmed 2026-09-06 |
| 9.9 | n8n SSO (SAML/OIDC) is **Enterprise-only** | ✅ | Which is why the connector is genuinely needed |
| 9.10 | FreshRSS `AUTH_TYPE=http_auth` **auto-creates** unknown users | 🔍 | **Verify.** If it does not, you still need the `cli/create-user.php` docker-exec connector |

---

## 10. Komodo / Headscale / ntfy

| # | Claim | Status | How to verify |
|---|---|---|---|
| 10.1 | Komodo supports generic OIDC in the open-source edition | ✅ | Confirmed 2026-09-06 |
| 10.2 | Komodo **cannot provision OIDC users**; admin must Enable User after first login | ✅ | Confirmed. This is why it is tier D |
| 10.3 | Komodo v2 replaced passkeys with Ed25519 PKI (April 2026) | ✅ | Check which version you deploy |
| 10.4 | `headscale users create` via CLI/gRPC | ⚠️ | `infra_admin` profile only, and only for users with tailnet devices |
| 10.5 | Headscale pre-auth keys should be short-TTL and on-demand, not at provisioning | ⚠️ | Design decision, not an API claim |
| ~~10.6~~ | ~~ntfy user/ACL management~~ | **N/A** | Removed by D12 — shared household topic, no per-user accounts, no connector reaching ristretto |

---

## 11. Cross-cutting things to verify

| # | Item | Why it matters |
|---|---|---|
| 11.1 | **Does disabling in lldap actually lock the user out of each tier-A/B service, including live sessions?** | Test per service. This is the single most important verification in this document — an offboarding that doesn't offboard is worse than none, because you'll believe it worked |
| 11.2 | Which services match OIDC identities on **email**, which on **username**, which on **subject claim** | Determines whether pre-creation links or duplicates. Get this wrong and you create shadow accounts |
| 11.3 | Actual RAM headroom on silo with Komodo running | Doc 09 §8 — decides Postgres vs SQLite |
| 11.4 | Whether `barista`'s git-credential blocker is fixed | Doc 09 §5 — the 06:15 cron pull currently fails on every node |
| 11.5 | The pre-commit secret scanner covers this repo | Doc 09 §6 |
| 11.6 | Every image tag pinned in doc 09 is current and the API shapes match that tag | The fleet notes already flag that 2026-08-28 pins need re-checking before deploy |

---

## 12. How to work through this

Stand up **throwaway instances** of Immich, Vaultwarden, n8n and Home Assistant
in a scratch compose file on roastery — not the live fleet. Point them at a
scratch lldap. Then curl each claim and write the real request/response into a
`docs/api-notes/<service>.md` alongside this file.

Two hours of curl here will save considerably more debugging later, and will
almost certainly change two or three things in doc 02. **Expect that. It is the
plan working, not failing.**
