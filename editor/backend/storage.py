"""
Where published files and pictures go.

In real use that is Cloudflare R2 (tools/r2.py): a public bucket the app downloads from and
a private one for untouched originals. The tests use a folder instead, served back by the
editor itself, so the whole editor can be exercised without an account anywhere.

Both answer the same four questions, which is all the editor asks of storage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import r2  # noqa: E402


class R2Store:
    kind = "r2"

    def __init__(self) -> None:
        self.r2 = r2.load()
        self.public_url = self.r2.public_url

    def put_public(self, key: str, body: bytes, content_type: str, cache_control: str) -> None:
        self.r2.put(self.r2.public_bucket, key, body, content_type, cache_control)

    def put_private(self, key: str, body: bytes, content_type: str) -> None:
        self.r2.put(self.r2.private_bucket, key, body, content_type)

    def exists_public(self, key: str) -> bool:
        return self.r2.exists(self.r2.public_bucket, key)

    def get_public(self, key: str) -> bytes:
        return self.r2.get(self.r2.public_bucket, key)


class LocalStore:
    """A folder standing in for both buckets, for tests."""

    kind = "local"

    def __init__(self, root: Path, public_url: str) -> None:
        self.root = root
        self.public_url = public_url.rstrip("/")

    def _path(self, bucket: str, key: str) -> Path:
        path = (self.root / bucket / key).resolve()
        if not str(path).startswith(str((self.root / bucket).resolve())):
            raise ValueError(f"not a storage key: {key}")
        return path

    def put_public(self, key: str, body: bytes, content_type: str, cache_control: str) -> None:
        path = self._path("public", key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def put_private(self, key: str, body: bytes, content_type: str) -> None:
        path = self._path("private", key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def exists_public(self, key: str) -> bool:
        return self._path("public", key).is_file()

    def get_public(self, key: str) -> bytes:
        return self._path("public", key).read_bytes()
