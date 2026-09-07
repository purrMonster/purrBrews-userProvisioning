"""vCard import.

The security-critical rule in this file, stated once and enforced once:

    A ``PHOTO`` given as a URI is **never fetched.**

If it were, anyone who could hand you a `.vcf` could make this server — which
sits inside the LAN holding admin credentials for the whole fleet — issue
arbitrary GET requests to arbitrary hosts. ``PHOTO;VALUE=uri:http://10.0.0.5:8080/...``
and you have SSRF from an unusually valuable position. Allowlists and
private-IP filters do not close it (DNS rebinding, redirects); *not fetching*
does. Inline base64 is accepted; a URI is flagged for the operator to handle.

The same applies to LOGO, SOUND, and any other URI-valued property.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CARDS = 50
MAX_FIELD_CHARS = 1024
MAX_EMAILS_SHOWN = 5

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")

RESERVED = {
    "admin", "administrator", "root", "barista", "postgres", "nobody", "daemon",
    "bin", "sys", "www-data", "systemd", "mail", "news", "operator", "guest",
    "doser", "lldap", "authelia", "traefik", "immich", "nextcloud",
}

# Control characters, zero-width marks, and bidirectional overrides.  An RTL
# override in a display name can make "bubbles<U+202E>nimda" render as something
# else entirely — in your audit log and in every downstream service's UI.
_STRIP = re.compile(
    "["
    "\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f-\\u009f"  # C0 / C1 controls
    "\\u200b-\\u200f"                                       # zero-width + marks
    "\\u202a-\\u202e\\u2066-\\u2069"                        # bidi overrides/isolates
    "\\u2060-\\u2064\\ufeff"                                # word joiner, BOM
    "]"
)


class VCardError(ValueError):
    pass


@dataclass
class Issue:
    code: str
    detail: str
    blocking: bool = False


@dataclass
class Candidate:
    index: int
    username: str
    email: str
    first_name: str
    last_name: str
    display_name: str
    avatar_b64: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(i.blocking for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "username": self.username,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "display_name": self.display_name,
            "has_avatar": bool(self.avatar_b64),
            "raw": self.raw,
            "blocked": self.blocked,
            "issues": [{"code": i.code, "detail": i.detail, "blocking": i.blocking} for i in self.issues],
        }


def clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _STRIP.sub("", str(text))
    text = unicodedata.normalize("NFC", text).strip()
    return text[:MAX_FIELD_CHARS]


def decode(raw: bytes) -> tuple[str, str]:
    """Decode, saying which encoding worked rather than producing mojibake."""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise VCardError("Could not decode that file as text.")


def suggest_username(first: str, last: str, nickname: str, taken: set[str]) -> tuple[str, bool]:
    """Return (username, was_generated). Always editable by the operator."""
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9._-]", "", s)

    f, l, n = norm(first), norm(last), norm(nickname)
    for cand in (n, f"{f}.{l}" if f and l else "", f"{f[:1]}{l}" if f and l else "",
                 f"{f}{l[:1]}" if f and l else "", f):
        if cand and USERNAME_RE.match(cand) and cand not in RESERVED and cand not in taken:
            return cand, True
    base = (f"{f[:1]}{l}" or f or "user")[:28]
    for i in range(2, 100):
        cand = f"{base}{i}"
        if USERNAME_RE.match(cand) and cand not in taken:
            return cand, True
    return "", False


def parse(raw: bytes, filename: str, taken: Optional[set[str]] = None) -> list[Candidate]:
    if len(raw) > MAX_FILE_BYTES:
        raise VCardError(
            f"File is {len(raw) // 1024} KB; the limit is {MAX_FILE_BYTES // 1024} KB."
        )
    text, encoding = decode(raw)
    if not text.lstrip().upper().startswith("BEGIN:VCARD"):
        raise VCardError(
            f"{filename} does not start with BEGIN:VCARD — is it really a contact file?"
        )

    try:
        import vobject
    except ImportError as exc:  # pragma: no cover
        raise VCardError("vobject is not installed") from exc

    taken = set(taken or ())
    candidates: list[Candidate] = []

    for index, card in enumerate(vobject.readComponents(text)):
        if index >= MAX_CARDS:
            break
        candidates.append(_one(card, index, taken, encoding))
        if candidates[-1].username:
            taken.add(candidates[-1].username)

    if not candidates:
        raise VCardError("No contacts found in that file.")
    return candidates


def _one(card: Any, index: int, taken: set[str], encoding: str) -> Candidate:
    issues: list[Issue] = []
    if encoding != "utf-8":
        issues.append(Issue("encoding", f"Decoded as {encoding}, not UTF-8 — check for odd characters."))

    # --- name: prefer the structured N over splitting FN ------------------
    first = last = ""
    fn = clean(getattr(card, "fn", None).value if hasattr(card, "fn") else "")
    if hasattr(card, "n") and card.n.value:
        first = clean(getattr(card.n.value, "given", ""))
        last = clean(getattr(card.n.value, "family", ""))
    if not (first or last) and fn:
        parts = fn.rsplit(" ", 1)
        first = clean(parts[0])
        last = clean(parts[1]) if len(parts) > 1 else ""
        issues.append(
            Issue("name_split_guessed", f"No structured name; split {fn!r} on the last space.")
        )
    display = fn or f"{first} {last}".strip()
    if not (first or last):
        issues.append(Issue("no_name", "No usable name in this card.", blocking=True))

    # --- email: multiple is the norm --------------------------------------
    emails: list[str] = []
    for e in card.contents.get("email", []) or []:
        val = clean(getattr(e, "value", ""))
        if val:
            emails.append(val)
    chosen = ""
    if emails:
        pref = [
            clean(e.value)
            for e in card.contents.get("email", [])
            if "PREF" in str(getattr(e, "params", {}).get("TYPE", "")).upper()
        ]
        home = [
            clean(e.value)
            for e in card.contents.get("email", [])
            if "HOME" in str(getattr(e, "params", {}).get("TYPE", "")).upper()
        ]
        chosen = (pref or home or emails)[0]
        if len(emails) > 1:
            issues.append(
                Issue(
                    "multiple_emails",
                    f"{len(emails)} addresses found; using {chosen}. Others: "
                    + ", ".join(emails[:MAX_EMAILS_SHOWN]),
                )
            )
    else:
        issues.append(Issue("no_email", "No email address in this card.", blocking=True))

    # --- photo: inline only, URIs are flagged and NEVER fetched -----------
    avatar_b64 = None
    photo = card.contents.get("photo", [])
    if photo:
        p = photo[0]
        params = {k.upper(): v for k, v in (getattr(p, "params", {}) or {}).items()}
        value_type = str(params.get("VALUE", "")).upper()
        val = getattr(p, "value", None)
        if "URI" in value_type or (isinstance(val, str) and val.strip()[:4].lower() == "http"):
            issues.append(
                Issue(
                    "photo_is_uri",
                    "The photo is a link, not embedded data. It was NOT downloaded "
                    "(that would let a contact file make this server fetch arbitrary "
                    "URLs). Add a picture manually if you want one.",
                )
            )
        elif isinstance(val, bytes):
            avatar_b64 = base64.b64encode(val).decode()
        elif isinstance(val, str) and val:
            try:
                base64.b64decode(val, validate=True)
                avatar_b64 = val
            except Exception:  # noqa: BLE001
                issues.append(Issue("photo_unreadable", "Embedded photo could not be decoded."))

    nickname = ""
    if card.contents.get("nickname"):
        nickname = clean(card.contents["nickname"][0].value)

    username, ok = suggest_username(first, last, nickname, taken)
    if not ok or not username:
        issues.append(Issue("no_username", "Could not derive a username — enter one.", blocking=True))
    elif username in taken:
        issues.append(Issue("username_collision", f"{username!r} is already in use."))

    # Deliberately NOT imported: TEL, ADR, ORG, BDAY, NOTE. A provisioning tool
    # has no reason to persist someone's phone number and home address.
    raw_view = {
        "FN": fn,
        "N": f"{last};{first}",
        "EMAIL": emails[:MAX_EMAILS_SHOWN],
        "NICKNAME": nickname,
        "has_photo": bool(photo),
    }

    return Candidate(
        index=index,
        username=username,
        email=chosen,
        first_name=first,
        last_name=last,
        display_name=display or f"{first} {last}".strip(),
        avatar_b64=avatar_b64,
        raw=raw_view,
        issues=issues,
    )
