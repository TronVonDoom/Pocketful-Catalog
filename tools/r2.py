"""
Cloudflare R2, where everything the app downloads lives.

Two buckets: a public one holding the published index, set files, pictures and prices,
served to the app from its public address, and a private one holding untouched originals.
The database stays in Supabase and is never exposed; this is the only half the app sees.

Credentials live outside every repository -- by default `~/keystores/pocketful-r2.json`,
or wherever POCKETFUL_R2 points:

    {
      "endpoint": "https://<account id>.r2.cloudflarestorage.com",
      "access_key_id": "...",
      "secret_access_key": "...",
      "public_bucket": "pocketful",
      "private_bucket": "pocketful-originals",
      "public_url": "https://pub-<id>.r2.dev"
    }

The token behind them needs only Object Read & Write on those two buckets.

R2 speaks the S3 API. This signs its requests (AWS Signature Version 4) with hashlib and
hmac rather than pulling in boto3, for the same reason the rest of the tooling is stdlib
only: the editor imports it, and a tool for fixing one card should not need a package
manager.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

KEY_FILE = Path(os.environ.get("POCKETFUL_R2") or Path.home() / "keystores" / "pocketful-r2.json")
REGION = "auto"
USER_AGENT = "Pocketful-Catalog (+https://github.com/TronVonDoom/Pocketful-Catalog)"


class R2Error(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"R2 answered {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class R2:
    endpoint: str
    access_key_id: str
    secret_access_key: str
    public_bucket: str
    private_bucket: str
    public_url: str

    # -- the public address -------------------------------------------------------------

    def public_address(self, key: str) -> str:
        return f"{self.public_url}/{quote_key(key)}"

    # -- objects ---------------------------------------------------------------------------

    def put(self, bucket: str, key: str, body: bytes, content_type: str,
            cache_control: str | None = None) -> None:
        headers = {"content-type": content_type}
        if cache_control:
            headers["cache-control"] = cache_control
        self._request("PUT", bucket, key, body=body, headers=headers)

    def get(self, bucket: str, key: str) -> bytes:
        return self._request("GET", bucket, key)

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self._request("HEAD", bucket, key)
            return True
        except R2Error as e:
            if e.status == 404:
                return False
            raise

    def delete(self, bucket: str, key: str) -> None:
        self._request("DELETE", bucket, key)

    def list(self, bucket: str, prefix: str = "") -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._request("GET", bucket, "", query=query))
            ns = {"s3": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            find = (lambda el, tag: el.findall(f"s3:{tag}", ns)) if ns else (lambda el, tag: el.findall(tag))
            keys += [c.findtext("s3:Key" if ns else "Key", namespaces=ns) for c in find(root, "Contents")]
            truncated = root.findtext("s3:IsTruncated" if ns else "IsTruncated", namespaces=ns)
            token = root.findtext("s3:NextContinuationToken" if ns else "NextContinuationToken", namespaces=ns)
            if truncated != "true" or not token:
                return keys

    # -- signing ---------------------------------------------------------------------------

    def _request(self, method: str, bucket: str, key: str, body: bytes = b"",
                 headers: dict[str, str] | None = None, query: dict[str, str] | None = None) -> bytes:
        host = urllib.parse.urlparse(self.endpoint).netloc
        path = f"/{bucket}/{quote_key(key)}" if key else f"/{bucket}"
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted((query or {}).items()))

        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        signed = {k.lower(): v.strip() for k, v in (headers or {}).items()}
        signed.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
        names = sorted(signed)
        canonical_request = "\n".join([
            method, path, canonical_query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names), payload_hash,
        ])
        scope = f"{date}/{REGION}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signing_key = f"AWS4{self.secret_access_key}".encode()
        for part in (date, REGION, "s3", "aws4_request"):
            signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

        request_headers = {n: signed[n] for n in names if n != "host"}
        request_headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}")
        request_headers["user-agent"] = USER_AGENT

        url = f"{self.endpoint.rstrip('/')}{path}" + (f"?{canonical_query}" if canonical_query else "")
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace") if method != "HEAD" else ""
            raise R2Error(e.code, detail or e.reason) from None


def quote_key(key: str) -> str:
    """An object key as it goes in a URL: every segment escaped, the slashes between them kept."""
    return "/".join(urllib.parse.quote(segment, safe="-_.~") for segment in key.split("/"))


ENV_NAMES = {"endpoint": "POCKETFUL_R2_ENDPOINT", "access_key_id": "POCKETFUL_R2_ACCESS_KEY_ID",
             "secret_access_key": "POCKETFUL_R2_SECRET_ACCESS_KEY", "public_bucket": "POCKETFUL_R2_PUBLIC_BUCKET",
             "private_bucket": "POCKETFUL_R2_PRIVATE_BUCKET", "public_url": "POCKETFUL_R2_PUBLIC_URL"}


def load() -> R2:
    # GitHub Actions has no key file; its secrets arrive as environment variables instead.
    from_env = {field: os.environ.get(name, "") for field, name in ENV_NAMES.items()}
    if from_env["endpoint"] and from_env["access_key_id"]:
        raw = {**from_env, "private_bucket": from_env["private_bucket"] or "pocketful-originals"}
    elif not KEY_FILE.exists():
        raise SystemExit(f"No R2 credentials at {KEY_FILE}. See tools/r2.py.")
    else:
        raw = json.loads(KEY_FILE.read_text(encoding="utf-8"))
    missing = [k for k in ("endpoint", "access_key_id", "secret_access_key",
                           "public_bucket", "private_bucket", "public_url") if not raw.get(k)]
    if missing:
        raise SystemExit(f"{KEY_FILE} is missing: {', '.join(missing)}")
    endpoint = urllib.parse.urlparse(raw["endpoint"])
    if not endpoint.netloc.endswith(".r2.cloudflarestorage.com"):
        raise SystemExit(f"{KEY_FILE}: endpoint should look like https://<account id>.r2.cloudflarestorage.com")
    return R2(
        # The dashboard shows the endpoint with a bucket on the end at times; only the origin is wanted.
        endpoint=f"{endpoint.scheme}://{endpoint.netloc}",
        access_key_id=raw["access_key_id"].strip(),
        secret_access_key=raw["secret_access_key"].strip(),
        public_bucket=raw["public_bucket"].strip(),
        private_bucket=raw["private_bucket"].strip(),
        public_url=raw["public_url"].strip().rstrip("/"),
    )
