"""Ingest tests — the two untrusted-input paths.

These are the twelve cases from docs/06 §4, made executable.  Most of the risk
in the vCard and avatar features lives here; if these pass, that surface is in
good shape.

Cases 1-9 (avatar) were run and passing on 2026-09-06.
Cases 10-14 (vCard) need `vobject` installed.
"""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from doser.ingest import avatar as av
from doser.ingest import vcard as vc


def _img(fmt="JPEG", size=(800, 600), mode="RGB", **kw) -> bytes:
    b = io.BytesIO()
    Image.new(mode, size, (200, 120, 60) if mode == "RGB" else (10, 200, 10, 128)).save(
        b, format=fmt, **kw
    )
    return b.getvalue()


# --------------------------------------------------------------- avatars ---
def test_jpeg_normalised_to_512_square():
    n = av.normalise(_img())
    assert (n.width, n.height) == (512, 512)
    assert n.mime == "image/jpeg"


def test_png_alpha_flattened():
    n = av.normalise(_img("PNG", (400, 400), mode="RGBA"))
    assert n.original_mime == "image/png"
    assert n.mime == "image/jpeg"


def test_content_addressed_hash_is_stable():
    data = _img()
    assert av.normalise(data).sha256 == av.normalise(data).sha256


def test_exif_is_discarded():
    """A profile photo routinely carries GPS. It must not reach Immich."""
    img = Image.new("RGB", (600, 600), (90, 90, 190))
    ex = img.getexif()
    ex[0x0110] = "SECRET-CAMERA-MODEL"
    ex[0x010F] = "LEAKY-MAKE"
    b = io.BytesIO()
    img.save(b, format="JPEG", exif=ex.tobytes())
    src = b.getvalue()
    assert b"SECRET-CAMERA-MODEL" in src, "fixture is wrong"

    out = av.normalise(src).data
    assert b"SECRET-CAMERA-MODEL" not in out
    assert b"LEAKY-MAKE" not in out
    assert not Image.open(io.BytesIO(out)).getexif()


def test_zip_named_as_image_rejected_by_magic_bytes():
    with pytest.raises(av.AvatarError):
        av.normalise(b"PK\x03\x04" + b"\x00" * 200)


def test_decompression_bomb_rejected():
    b = io.BytesIO()
    Image.new("RGB", (12000, 12000)).save(b, format="PNG", compress_level=9)
    with pytest.raises(av.AvatarError):
        av.normalise(b.getvalue())


def test_oversized_rejected_before_decode():
    with pytest.raises(av.AvatarError):
        av.normalise(b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024))


def test_output_respects_size_ceiling():
    noise = Image.new("RGB", (1500, 1500))
    noise.putdata([(random.randrange(256),) * 3 for _ in range(1500 * 1500)])
    b = io.BytesIO()
    noise.save(b, format="JPEG", quality=100)
    assert len(av.normalise(b.getvalue()).data) <= av.MAX_OUTPUT_BYTES


def test_appended_payload_does_not_survive_reencode():
    src = _img("JPEG", (300, 300)) + b"<?php system($_GET['c']); ?>"
    assert b"<?php" not in av.normalise(src).data


# ---------------------------------------------------------------- vcard ----
CARD = """BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
N:Doe;Jane;;;
EMAIL;TYPE=HOME:jane@example.invalid
EMAIL;TYPE=WORK:j.doe@work.invalid
NICKNAME:jd
TEL:+441234567890
ADR:;;12 Somewhere St;Town;;AB1 2CD;UK
END:VCARD
"""


def test_rtl_override_is_stripped():
    """An RTL override can make a name render as something else entirely —
    in the audit log and in every downstream service's UI."""
    assert vc.clean("bubbles‮nimda") == "bubblesnimda"


def test_control_and_zero_width_stripped():
    assert vc.clean("ok\x00\x07na​me") == "okname"


def test_photo_uri_is_flagged_and_never_fetched(monkeypatch):
    """The SSRF guard. A contact file must not make this server fetch URLs."""
    pytest.importorskip("vobject")
    card = CARD.replace(
        "NICKNAME:jd", "NICKNAME:jd\nPHOTO;VALUE=uri:http://127.0.0.1:8080/admin"
    )

    called = []
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: called.append(a))

    cands = vc.parse(card.encode(), "t.vcf")
    codes = {i.code for i in cands[0].issues}
    assert "photo_is_uri" in codes
    assert cands[0].avatar_b64 is None
    assert not called, "a PHOTO URI must NEVER be fetched"


def test_multiple_emails_flagged_and_home_preferred():
    pytest.importorskip("vobject")
    c = vc.parse(CARD.encode(), "t.vcf")[0]
    assert c.email == "jane@example.invalid"
    assert "multiple_emails" in {i.code for i in c.issues}


def test_phone_and_address_are_not_imported():
    """A provisioning tool has no reason to persist someone's home address."""
    pytest.importorskip("vobject")
    c = vc.parse(CARD.encode(), "t.vcf")[0]
    blob = str(c.as_dict())
    assert "441234567890" not in blob
    assert "Somewhere St" not in blob


def test_card_without_email_is_blocked_not_dropped():
    pytest.importorskip("vobject")
    card = "BEGIN:VCARD\nVERSION:3.0\nFN:No Email\nN:Email;No;;;\nEND:VCARD\n"
    c = vc.parse(card.encode(), "t.vcf")[0]
    assert c.blocked
    assert "no_email" in {i.code for i in c.issues}


def test_fn_without_n_flags_the_guess():
    pytest.importorskip("vobject")
    card = "BEGIN:VCARD\nVERSION:3.0\nFN:Solo Name\nEMAIL:s@x.invalid\nEND:VCARD\n"
    c = vc.parse(card.encode(), "t.vcf")[0]
    assert "name_split_guessed" in {i.code for i in c.issues}


def test_non_vcard_file_rejected_early():
    with pytest.raises(vc.VCardError, match="BEGIN:VCARD"):
        vc.parse(b"just some text", "notes.vcf")


def test_oversized_vcf_rejected():
    with pytest.raises(vc.VCardError):
        vc.parse(b"BEGIN:VCARD" + b"x" * (3 * 1024 * 1024), "big.vcf")


def test_username_suggestion_avoids_collisions_and_reserved():
    u, ok = vc.suggest_username("Jane", "Doe", "", {"jane.doe", "jdoe"})
    assert ok and u not in ("jane.doe", "jdoe")
    u2, _ = vc.suggest_username("Ad", "Min", "admin", set())
    assert u2 != "admin", "reserved names must not be suggested"
