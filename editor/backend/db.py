"""
The catalog database, through Supabase's REST API.

Supabase puts PostgREST in front of the database, so every table is a URL and every filter a
query parameter: `cards?set_id=eq.ptcg-en-base01&order=sort`. This is a thin client for
exactly that, and nothing more -- the rules (IDs, locks, the publish gate) live in the
database itself, so the editor cannot get them wrong and does not try to restate them.

When the database refuses something, it says why in plain words ("card ... has been
published, so its ID cannot change"). DbError carries that message through to the page
untouched, because it is already the best explanation there is.

The admin key never leaves this process. The page talks to the editor's own server, and
only the server talks to Supabase.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "Pocketful-editor/2.0 (+https://github.com/TronVonDoom/Pocketful-Catalog)"

# Supabase answers at most this many rows per request unless told otherwise, so anything
# that could be longer is read in pages of this size.
PAGE = 1000


class DbError(Exception):
    def __init__(self, status: int, message: str, hint: str | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint
        self.code = code

    def explain(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class Db:
    def __init__(self, rest_url: str, key: str):
        self.rest_url = rest_url.rstrip("/")
        self.key = key

    # -- requests ------------------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 body: Any = None, prefer: str | None = None,
                 headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        url = f"{self.rest_url}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe=",.*()")
        request_headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if prefer:
            request_headers["Prefer"] = prefer
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                payload = json.loads(raw) if raw.strip() else None
                return payload, dict(response.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                detail = json.loads(raw)
            except ValueError:
                raise DbError(e.code, raw.decode("utf-8", "replace")[:300] or e.reason) from None
            raise DbError(e.code, detail.get("message") or str(detail), detail.get("hint"),
                          detail.get("code")) from None
        except urllib.error.URLError as e:
            raise DbError(503, f"Could not reach the database: {e.reason}") from None

    # -- reading -------------------------------------------------------------------------

    def get(self, table: str, params: dict[str, str] | None = None) -> list[dict]:
        """Every matching row, fetched a page at a time."""
        rows: list[dict] = []
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params.setdefault("select", "*")
            page_params["limit"] = str(PAGE)
            page_params["offset"] = str(offset)
            page, _ = self._request("GET", table, page_params)
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            offset += PAGE

    def get_in(self, table: str, column: str, values: list[str],
               params: dict[str, str] | None = None) -> list[dict]:
        """Rows whose `column` is any of `values`, asked in chunks so no URL grows too long."""
        rows: list[dict] = []
        unique = list(dict.fromkeys(values))
        for i in range(0, len(unique), 80):
            rows += self.get(table, {**(params or {}), column: in_list(unique[i:i + 80])})
        return rows

    def one(self, table: str, params: dict[str, str]) -> dict | None:
        rows, _ = self._request("GET", table, {"select": "*", **params, "limit": "1"})
        return rows[0] if rows else None

    # -- writing -------------------------------------------------------------------------

    def insert(self, table: str, rows: dict | list[dict]) -> list[dict]:
        payload, _ = self._request("POST", table, body=rows, prefer="return=representation")
        return payload or []

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> list[dict]:
        payload, _ = self._request("POST", table, {"on_conflict": on_conflict}, body=rows,
                                   prefer="resolution=merge-duplicates,return=representation")
        return payload or []

    def update(self, table: str, filters: dict[str, str], patch: dict) -> list[dict]:
        if not filters:
            raise ValueError("an update needs a filter")
        payload, _ = self._request("PATCH", table, filters, body=patch, prefer="return=representation")
        return payload or []

    def delete(self, table: str, filters: dict[str, str]) -> list[dict]:
        if not filters:
            raise ValueError("a delete needs a filter")
        payload, _ = self._request("DELETE", table, filters, prefer="return=representation")
        return payload or []

    def rpc(self, function: str, args: dict) -> Any:
        payload, _ = self._request("POST", f"rpc/{function}", body=args)
        return payload


def eq(value: str) -> str:
    return f"eq.{value}"


def in_list(values: list[str]) -> str:
    """A PostgREST `in` filter, with every value quoted so commas and dots inside IDs are safe."""
    quoted = ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values)
    return f"in.({quoted})"
