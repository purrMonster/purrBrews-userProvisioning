"""Avatar normalisation.

Every step here earns its place against a specific failure:

* **Magic-byte sniffing** — the filename and Content-Type are attacker-supplied.
* **Pixel cap** — a 200 KB PNG can expand to 40 GB of pixels and OOM an 8 GB
  node instantly. Pillow only *warns* about this by default; we promote it.
* **Full decode and re-encode** — the single most valuable step. The bytes that
  reach lldap and every downstream service are bytes *we* generated, so
  polyglot files and embedded payloads do not survive the trip.
* **Discarding EXIF** — profile photos routinely carry GPS coordinates. A photo
  taken at home, propagated to Immich, Nextcloud and Jellyfin, is a location
  leak that costs nothing to avoid. Orientation is applied first, then all
  metadata goes.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
TARGET = 512
MAX_OUTPUT_BYTES = 200 * 1024

# Promote Pillow's decompression-bomb warning to an exception.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"RIFF": "image/webp",  # verified further below
}


class AvatarError(ValueError):
    pass


@dataclass
class NormalisedAvatar:
    sha256: str
    data: bytes
    width: int
    height: int
    mime: str
    original_mime: str


def sniff(data: bytes) -> str:
    """Identify the real type from magic bytes. Never trust the extension."""
    for magic, mime in _MAGIC.items():
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    # HEIC/HEIF carry an ftyp box a few bytes in.
    if len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1"):
        return "image/heic"
    raise AvatarError(
        "That file is not a JPEG, PNG, WebP or HEIC image "
        "(checked the file's own contents, not its name)."
    )


def normalise(data: bytes) -> NormalisedAvatar:
    if not data:
        raise AvatarError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarError(
            f"Image is {len(data) // 1024} KB; the limit is {MAX_UPLOAD_BYTES // 1024} KB."
        )

    original_mime = sniff(data)

    try:
        with Image.open(io.BytesIO(data)) as img:
            # Guard before decoding: a huge declared size is the bomb signature.
            if img.width * img.height > MAX_PIXELS:
                raise AvatarError(
                    f"Image is {img.width}×{img.height}, which is too large to process safely."
                )
            img = ImageOps.exif_transpose(img)  # apply orientation...
            img = img.convert("RGB")  # ...then drop EXIF, ICC, alpha
            img = ImageOps.fit(img, (TARGET, TARGET), method=Image.Resampling.LANCZOS)
            out = _encode(img)
    except AvatarError:
        raise
    except Image.DecompressionBombError as exc:
        raise AvatarError("Image rejected: decompression bomb.") from exc
    except Exception as exc:  # noqa: BLE001
        raise AvatarError(f"Could not read that image: {exc}") from exc

    return NormalisedAvatar(
        sha256=hashlib.sha256(out).hexdigest(),
        data=out,
        width=TARGET,
        height=TARGET,
        mime="image/jpeg",
        original_mime=original_mime,
    )


def _encode(img: Image.Image) -> bytes:
    """JPEG at descending quality until it fits the size ceiling."""
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        # No exif= argument: metadata is deliberately not carried over.
        img.save(buf, format="JPEG", quality=quality, progressive=True, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_OUTPUT_BYTES:
            return out
    return out  # best effort at the lowest quality tried
