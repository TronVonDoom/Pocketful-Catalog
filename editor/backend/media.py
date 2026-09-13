"""
Pictures: what a valid one is, and where it is kept.

The page does the resizing and encoding -- a canvas is a perfectly good image codec -- so
this server never decodes an image. It only reads the few header bytes that say a file
really is WebP and how big it is, and refuses anything outside the picture standard in
docs/database.md. Trusting the page's word for the size would let one bad upload through
into a published set.
"""

from __future__ import annotations

import hashlib
import re

# The largest a picture may be, by role. Card fronts and backs follow the picture standard;
# logos and symbols are not card-shaped and only need to stay reasonable.
MAX_SIZE = {
    "front": (734, 1024),
    "back": (734, 1024),
    "logo": (1200, 600),
    "symbol": (256, 256),
}
THUMB_SIZE = (245, 342)
# Below this a card picture looks soft next to the rest; it is kept, and marked.
SOFT_BELOW = (600, 825)
MAX_BYTES = 2 * 1024 * 1024
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024

ORIGINAL_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}

# Pictures are named after the thing they show, then a short hash of their bytes, so a
# replaced picture is always a new address and nothing can keep serving the old one.
SAFE_NAME = re.compile(r"[^a-z0-9._-]")


class MediaError(ValueError):
    pass


def webp_size(data: bytes) -> tuple[int, int]:
    """Width and height from a WebP header, or MediaError if it is not WebP."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MediaError("not a WebP picture")
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    elif chunk == b"VP8L":
        if data[20] != 0x2F:
            raise MediaError("damaged WebP picture")
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise MediaError("damaged WebP picture")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
    else:
        raise MediaError("unrecognised WebP picture")
    return width, height


def check(data: bytes, limit: tuple[int, int], what: str) -> tuple[int, int]:
    if len(data) > MAX_BYTES:
        raise MediaError(f"{what} is {len(data) // 1024} KB; the most is {MAX_BYTES // 1024} KB")
    width, height = webp_size(data)
    if width > limit[0] or height > limit[1]:
        raise MediaError(f"{what} is {width}×{height}; the most is {limit[0]}×{limit[1]}")
    return width, height


def is_soft(role: str, width: int, height: int) -> bool:
    return role in ("front", "back") and (width < SOFT_BELOW[0] or height < SOFT_BELOW[1])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def picture_key(role: str, subject_kind: str, subject_id: str, set_id: str | None, digest: str) -> str:
    """Where a picture lives in the public bucket."""
    name = SAFE_NAME.sub("-", subject_id.lower())
    stem = f"{name}.{digest[:6]}"
    if role == "front":
        return f"images/cards/{SAFE_NAME.sub('-', (set_id or 'loose').lower())}/{stem}.webp"
    return f"images/{role}s/{stem}.webp"


def thumb_key(key: str) -> str:
    return key[: -len(".webp")] + ".thumb.webp"


def original_key(digest: str, content_type: str) -> str:
    ext = ORIGINAL_TYPES.get(content_type)
    if not ext:
        raise MediaError(f"originals are kept as PNG, JPEG, WebP or GIF, not {content_type}")
    return f"{digest[:2]}/{digest}.{ext}"
