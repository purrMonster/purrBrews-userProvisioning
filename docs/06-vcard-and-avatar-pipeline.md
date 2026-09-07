# 06 — vCard Import & Avatar Pipeline

Two input paths that both accept **untrusted, attacker-influenceable data from
outside the fleet**. A `.vcf` typically comes from someone's phone; a profile
picture is an arbitrary binary. Both get parsed by libraries with a history of
CVEs. This document is as much a security document as a feature spec.

---

## 1. Shared rules for both paths

1. **Parse server-side only.** Client-side parsing is a convenience, never a
   trust boundary.
2. **Nothing is committed without human review.** Both paths stage first.
3. **Re-generate rather than pass through.** Text is normalised and stripped;
   images are decoded and re-encoded. The bytes that leave this tool are bytes
   this tool produced.
4. **Fail loudly and specifically.** "Couldn't read that file" is useless; the
   operator needs to know *which card*, *which field*, and *what to do*.

---

## 2. vCard import

### 2.1 Format reality

`.vcf` files in the wild are vCard 2.1, 3.0 (RFC 2426), or 4.0 (RFC 6350), often
mislabelled, sometimes in Latin-1 despite claiming UTF-8, frequently with
Apple's `item1.EMAIL` grouping syntax and `X-` extensions. Parse with
**`vobject`** (handles 2.1/3.0/4.0 and the Apple quirks) and treat every field
as optional.

### 2.2 Field mapping

| vCard property | Maps to | Rule |
|---|---|---|
| `N` (structured name) | `last_name`, `first_name` | `N:Family;Given;Additional;Prefix;Suffix`. **Preferred source** — it's structured, unlike `FN`. |
| `FN` (formatted name) | `display_name`; fallback for names | If `N` is absent, split `FN` on the last space and flag `name_split_guessed`. Never split silently. |
| `EMAIL` | `email` | Multiple is the norm. Preference order: `TYPE=PREF` → `TYPE=HOME` → `TYPE=INTERNET` → first. **Always flag `multiple_emails` and show all of them** so the operator can pick. |
| `NICKNAME` | username candidate | First choice for username if it matches the username regex |
| `PHOTO` | avatar | **See §2.4 — this is the dangerous one** |
| `ORG`, `TITLE`, `TEL`, `ADR`, `BDAY`, `NOTE` | — | **Not imported.** Shown in the raw view for context, then discarded. There is no reason for a provisioning tool to persist someone's phone number and home address. |
| `UID` | `source_ref` | Useful for de-duplicating re-imports of the same card |

Username generation, in order, first that passes validation and is free:
`NICKNAME` → `first.last` → `flast` → `firstl` → `flast2`, `flast3`…
Always shown, always editable, never auto-committed.

### 2.3 Encoding and structural hardening

- Decode as UTF-8; on failure, try UTF-8-sig, then Latin-1, and **flag which was
  used** rather than mojibake-ing silently.
- Unfold RFC 6350 line folding before parsing.
- Strip C0/C1 control characters, zero-width characters, and Unicode
  bidirectional overrides (`U+202A`–`U+202E`, `U+2066`–`U+2069`) from every
  imported string. A display name containing an RTL override can make
  `bubbles‮nimda` render as something else entirely in your audit log and in
  every downstream service's UI. This is a real spoofing vector, cheap to close.
- NFC-normalise all text.
- Cap: 2 MB file, 50 cards, 1 KB per text field, 5 emails surfaced per card.
- Reject files whose first non-whitespace line is not `BEGIN:VCARD`
  (case-insensitive) — cheap, catches mis-uploads before the parser sees them.

### 2.4 `PHOTO` — the SSRF vector

vCard `PHOTO` comes in two forms:

```
PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABA…      ← inline base64
PHOTO;VALUE=uri:https://someserver.example/me.jpg  ← a URL
```

**Inline base64 is accepted. A URI is never fetched.**

If this tool fetched `PHOTO` URIs, anyone who could hand you a `.vcf` could make
your provisioning server — which sits on silo, inside the LAN, holding admin
credentials for the entire fleet — issue arbitrary GET requests to arbitrary
hosts. `PHOTO;VALUE=uri:http://192.168.x.x:9120/api/…` or a link-local metadata
address. Classic SSRF, and this host is an unusually valuable place to have it.

The mitigation is not an allowlist or a private-IP filter (both are bypassable
via DNS rebinding and redirects). The mitigation is: **do not fetch.** Flag it:
`photo_is_uri — remote image not downloaded; add a picture manually if you want
one.` The operator can always download it themselves and upload it, which puts a
human in the loop where one belongs.

The same rule covers `LOGO`, `SOUND`, and any other URI-valued property.

### 2.5 Staging

Parsed cards go to `vcard_imports.staged` (doc 03 §2.8), never straight to
`users`. Two gates before anything is created: select rows → commit → then a
per-user provisioning run. Unresolved imports expire in 24 h and are hard-deleted
by a nightly job.

---

## 3. Avatar pipeline

### 3.1 Threat

Image parsers are a perennial source of memory-safety bugs, and "decompression
bomb" images (a 200 KB PNG that expands to 40 GB of pixels) will OOM an 8 GB
node instantly. Both are handled below.

### 3.2 Pipeline

```
upload (≤ 8 MB)
  → sniff real type from magic bytes (do NOT trust Content-Type or extension)
  → reject anything not JPEG / PNG / WebP / HEIC
  → Pillow: set MAX_IMAGE_PIXELS = 40_000_000, decompression-bomb warning = error
  → open, apply EXIF orientation, then DISCARD all EXIF/XMP/ICC metadata
  → convert to sRGB, flatten alpha onto white
  → centre-crop to square (or use the operator's crop box)
  → resize to 512×512 (Lanczos)
  → re-encode as JPEG q85, progressive
  → assert output ≤ 200 KB (step quality down if not)
  → sha256 → store in `avatars`
```

Every one of those steps earns its place:

- **Magic-byte sniffing**: filename and `Content-Type` are attacker-controlled.
- **`MAX_IMAGE_PIXELS`**: the decompression-bomb guard. Pillow warns by default;
  promote it to an exception.
- **Re-encoding, not passing through**: the single most valuable step. The bytes
  that reach lldap and every downstream service are bytes *this tool generated*,
  not bytes an attacker supplied. Polyglot files and embedded payloads do not
  survive a decode/re-encode cycle.
- **Discarding EXIF**: profile photos routinely carry GPS coordinates. A photo
  taken at home, propagated to Immich, Nextcloud, and Jellyfin, is a location
  leak that is trivially avoided. Orientation is applied first so the picture
  isn't sideways, then all metadata goes.
- **512×512**: large enough for every consumer, small enough that base64-encoding
  it into a GraphQL mutation is not absurd.

Run the decode in a subprocess with an RLIMIT on address space if you want belt
and braces. At household scale, the pixel cap is probably sufficient.

### 3.3 Distribution to services

Avatar propagation is a **separate connector capability** (`supports_avatar`),
not part of account creation, and it is best-effort — an avatar failure must
never mark a run `partial`.

| Target | Mechanism | Notes |
|---|---|---|
| **lldap** | `avatar` field in `createUser` / `updateUser`, base64 JPEG | ⚠️ **The `avatar` field is deprecated in favour of custom attributes, and if both are set the attribute takes precedence.** Decide which you're using and be consistent — setting both is how you end up with an avatar that won't update. Check your lldap version's `schema.graphql`. |
| **Nextcloud** | Inherits from LDAP if `user_ldap` avatar attribute mapping is configured | ⚠️ Verify the mapping (`jpegPhoto`) is set, or avatars won't flow |
| **Jellyfin** | `POST /Users/{id}/Images/Primary` | ⚠️ Separate call, needs the Jellyfin user id — only available after the user has logged in once under LDAP. Queue as a deferred job. |
| **Immich** | Profile image upload endpoint | ⚠️ Verify path for your version |
| **Everything else** | Not attempted | Most either read LDAP or don't show avatars |

Because several of these only work *after* first login, avatar propagation is
naturally a **reconciler job**, not a provisioning job: when the reconciler
observes a `pending_first_login` account become real, it queues the avatar push.
That's a nice fit and avoids a pile of retries against accounts that don't exist
yet.

### 3.4 Size sanity

`512×512 JPEG q85` ≈ 40–80 KB → ~55–110 KB base64 in a GraphQL mutation. Fine.
If lldap ever complains about entry size, drop to 256×256; nothing downstream
displays avatars larger than that anyway.

---

## 4. Test corpus to build before writing the parser

Keep these in `tests/fixtures/` and treat them as the spec:

1. vCard 2.1 from an old Android export, Latin-1, quoted-printable.
2. vCard 3.0 from Google Contacts, multiple emails, no `NICKNAME`.
3. vCard 4.0 from an iPhone, Apple `item1.EMAIL;type=pref` grouping, inline PHOTO.
4. Multi-card `.vcf` with 50 entries.
5. A card with `PHOTO;VALUE=uri:http://127.0.0.1:8080/admin` → must be flagged,
   **never fetched** (assert no outbound request is made).
6. A card with `FN` but no `N` → must flag `name_split_guessed`.
7. A card with no `EMAIL` → must be shown, disabled, with the reason.
8. A card with an RTL-override in `FN` → must be stripped.
9. A 4 GB-expansion PNG decompression bomb → must be rejected, not OOM.
10. A JPEG with GPS EXIF → output must contain no EXIF (assert on the bytes).
11. A file named `contacts.vcf` that is actually a ZIP → rejected by magic bytes.
12. A 3 MB `.vcf` → rejected by size before parsing.

These twelve cases are most of the risk in this feature. If they pass, the
import path is in good shape.
