# 05 — Frontend Specification

Jinja2 templates + HTMX + SSE. No build step. Progressive enhancement: every
form works without JavaScript; HTMX makes it live.

## 1. Design brief

This is an **operator tool used rarely**. That single fact should drive the whole
UI:

- Someone will use this three times a year. They will not remember how it works.
  **Every screen must be self-explanatory on a cold read** — no learned
  shortcuts, no ambiguous icons, no jargon without an inline definition.
- The dangerous actions are irreversible-ish and fan out across seven systems.
  **The dry-run preview is the most important screen in the app**, not the form.
- Errors are the normal case, not the exception. A run that ends `partial` is
  routine. The UI must make partial success legible and actionable rather than
  alarming.

Given the project's stated purpose — supporting executive function — the UI
should also assume the operator may be interrupted mid-task and return later.
Nothing should be lost on refresh; drafts persist server-side; a half-finished
run is resumable and says clearly what remains.

## 2. Screens

### 2.1 `/` — Users

Table: username, display name, status pill, service summary (`5 ✓ · 1 ⚠ · 1 ⏳`),
created, last run. Filters by status. Primary button: **New user**.

Status pills use text *and* colour (never colour alone) — `active`,
`partial ⚠`, `pending`, `disabled`.

### 2.2 `/users/new` — Create

Two tabs on one page: **Enter details** | **Import a contact card (.vcf)**.

**Form fields**

| Field | Widget | Behaviour |
|---|---|---|
| First name | text | required |
| Last name | text | required |
| Username | text | Auto-suggested from names (`first.last` → `flast` → `flast2`), live-validated against lldap with a debounced `hx-get`. Editable. Shows the exact rule on failure, not just "invalid". |
| Email | email | required, live duplicate warning (warning, not block) |
| Display name | text | auto-filled `First Last`, editable |
| Profile picture | drag-drop + file input | Live preview, client-side crop to square, server-side normalisation. Shows resulting size. Clearly optional. |
| Groups | checkbox list from lldap | `purrbrews_infra_admins` is visually separated, red-bordered, and requires a second explicit "I understand this grants fleet admin access" checkbox |
| Notes | textarea | free text, goes to the audit trail |

Submit → **plan screen**. Not straight to provisioning.

### 2.3 `/users/new` (vCard tab) — Import

1. Drop `.vcf` → `POST /api/v1/vcard/parse`.
2. **Review table**, one row per card, each with a checkbox (default *unchecked* —
   opt-in, not opt-out).
3. Each row is inline-editable and shows its `issues` badges: `multiple emails`,
   `photo skipped (remote URI)`, `username taken`, `no email`.
4. Rows with unresolvable issues (no email, no name) are shown disabled with the
   reason, not silently dropped — the operator should see what the file
   contained.
5. **Commit selected** → creates drafts → lands on the users list with the new
   drafts highlighted. Each still needs its own run.

Copy at the top of this tab, verbatim-ish: *"Contact cards usually come from
someone's address book and contain other people's personal data. Nothing here
is created until you select it and confirm. Anything you don't select is
discarded within 24 hours."*

### 2.4 `/runs/{id}/plan` — **The plan screen**

The most important screen. Layout:

```
Provision  bubbles  (Bubbles Example · bubbles@example.invalid)

  COMMIT STEP — this happens first and must succeed
  ┌────────────────────────────────────────────────────────────┐
  │ lldap    Create uid=bubbles, add to: purrbrews_users        │
  │          If this fails, nothing else runs.                  │
  └────────────────────────────────────────────────────────────┘

  WILL BE CALLED  (3)                                  [expand all]
   ▸ Immich        create account, 50 GB quota, label 'bubbles'
   ▸ Vaultwarden   send invitation (user sets own master password)
   ▸ n8n           create + capture invite link

  NO ACTION NEEDED  (17)                                  [expand]
   ▸ Nextcloud, Jellyfin, Mealie   account appears on first LDAP login
   ▸ Paperless, Vikunja, Actual    account appears on first SSO login
   ▸ Pi-hole, Traefik, Scrutiny…   no accounts — access via group only

  ⚠ YOU WILL NEED TO DO THIS BY HAND  (2)
   • Komodo — after their first login, open Komodo → Users → Enable
     and set permissions.  (Komodo cannot pre-create OIDC accounts.)
   • Jellyfin — library access is NOT granted automatically. Set it in
     Jellyfin → Users → bubbles → Library Access.

  ⓘ Home Assistant connector is disabled. See docs/02 §9.

           [ Cancel ]              [ Confirm and provision ]
```

The "no action needed" group is collapsed but **counted and expandable**. The
count is what builds confidence that the tool knows about all 20-odd services
rather than having quietly forgotten them.

### 2.5 `/runs/{id}` — Live progress

The status grid, one card per connector, updated over SSE:

- `⏳ queued` · `⟳ running` · `✓ provisioned` · `⏳ awaiting first login` ·
  `✋ needs you` · `✕ failed (retry 2/5, next in 1m)` · `– not applicable`

Failed cards expand to show the error, the attempt count, and buttons: **Retry
now**, **Skip with reason**, **Mark done manually**.

When the run settles, a summary banner and — for `partial` — a persistent
"unfinished business" list that follows the user record around until resolved.

**No modal dialogs for progress.** The page must survive a refresh, a closed
laptop, and being reopened an hour later.

### 2.6 `/users/{id}` — Detail

Identity summary, avatar, the service grid (same component as 2.5, now showing
steady state), run history, audit entries for this user, and the lifecycle
actions: **Update**, **Disable**, **Generate enrolment link**, **Offboard**.

Enrolment link UI: a button that reveals the URL once, with a copy control, a
visible countdown to expiry, and the note *"shown once — regenerate if lost"*.
Never rendered into the page on load.

### 2.7 `/connectors` — Registry

One row per connector: name, tier badge, enabled toggle, required toggle,
**Health** button with last-check result and latency, and the default payload
editor (quota, group IDs, library IDs) as a small JSON form.

This screen is also the documentation surface — each connector row expands to
show, in plain language, what it does and what it cannot do. That is where the
tier-D caveats live so they're read at configuration time, not discovered at
3am.

### 2.8 `/audit` — Log

Filterable by actor, user, connector, outcome, date. Read-only. Export as CSV.

## 3. Interaction mechanics

| Need | Implementation |
|---|---|
| Live username validation | `hx-get="/api/v1/users/check-username" hx-trigger="keyup changed delay:400ms"` swapping a small validity fragment |
| Live run progress | `<div hx-ext="sse" sse-connect="/api/v1/runs/{id}/events" sse-swap="job">` |
| Avatar preview + crop | ~40 lines of vanilla JS on a `<canvas>`; degrades to plain upload with no JS |
| Confirm-destructive | Native `<dialog>` requiring the username to be typed for `offboard` only |
| No-JS fallback | Every `hx-*` route also handles a normal form POST and returns a full page |

## 4. Accessibility and clarity

- Status never encoded in colour alone.
- All form errors announced via `aria-live="polite"` and associated with their
  input by `aria-describedby`.
- Keyboard-complete: the whole provisioning flow is doable without a mouse.
- Target contrast ≥ 4.5:1. Respect `prefers-reduced-motion` on the progress grid.
- Plain-language errors: "Immich refused the request — the API key may have been
  revoked. Check Connectors → Immich → Health." Not `HTTP 401`.

## 5. Styling

One hand-written CSS file, custom properties, light/dark via
`prefers-color-scheme`. No framework, no CDN — the fleet is CGNAT'd and may be
reached over the tunnel from a phone on bad signal; a self-contained ~8 KB
stylesheet is the right answer. System font stack.

## 6. Explicitly out of scope for v1

- Mobile-optimised layouts (responsive, but not designed phone-first).
- Bulk editing across users.
- Any real-time collaboration.
- Theming to match Homepage.
