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

Speed comes from two things. Connections are kept and reused: a fresh HTTPS connection to
Supabase costs about 150 ms of handshaking before the question is even asked, which used
to more than double every request. And `together` asks independent questions at the same
time, so a page that needs seven reads waits for the slowest one rather than all seven.
"""

from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

USER_AGENT = "Pocketful-editor/2.0 (+https://github.com/TronVonDoom/Pocketful-Catalog)"

# Supabase answers at most this many rows per request unless told otherwise, so anything
# that could be longer is read in pages of this size.
PAGE = 1000

# A kept connection is only reused this soon after its last answer, well inside the time
# Supabase's front door waits before closing an idle one. Older ones are closed instead.
IDLE_SECONDS = 20
KEEP_CONNECTIONS = 12

# The errors a kept connection gives when the server closed it while it sat idle, which is
# before the request could arrive, so it is sent again on another connection. Only a kept
# connection is retried: a new one failing this way is a real failure.
STALE = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


def together(*calls: Callable[[], Any]) -> list:
    """Run independent calls at the same time and return their results in order.

    The first call to fail raises its error here, as it would have if they ran one by one.
    Each use gets its own threads, so a call may itself use `together`.
    """
    if len(calls) == 1:
        return [calls[0]()]
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def each(call: Callable[[Any], Any], items: list, workers: int = 8) -> list:
    """`call` on every item, a few at a time, with the results in the items' order."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(call, items))


def chunked(values: list, size: int = 80) -> list[list]:
    """Values in groups small enough to go in one URL as an `in` filter."""
    return [values[i:i + size] for i in range(0, len(values), size)]


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
        parts = urllib.parse.urlsplit(self.rest_url)
        self._secure = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._base = parts.path
        self._idle: list[tuple[http.client.HTTPConnection, float]] = []
        self._lock = threading.Lock()

    # -- connections ---------------------------------------------------------------------

    def _connect(self) -> tuple[http.client.HTTPConnection, bool]:
        """A kept connection if a recent one is free, otherwise a new one. Says which."""
        cutoff = time.monotonic() - IDLE_SECONDS
        stale = []
        found = None
        with self._lock:
            while self._idle:
                connection, last_used = self._idle.pop()
                if last_used >= cutoff:
                    found = connection
                    break
                stale.append(connection)
        for connection in stale:
            connection.close()
        if found:
            return found, True
        kind = http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        return kind(self._host, self._port, timeout=60), False

    def _keep(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            if len(self._idle) < KEEP_CONNECTIONS:
                self._idle.append((connection, time.monotonic()))
                return
        connection.close()

    # -- requests ------------------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 body: Any = None, prefer: str | None = None,
                 headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        target = f"{self._base}/{path}"
        if params:
            target += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe=",.*()")
        request_headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if prefer:
            request_headers["Prefer"] = prefer
        request_headers.update(headers or {})

        while True:
            connection, reused = self._connect()
            try:
                connection.request(method, target, body=data, headers=request_headers)
                response = connection.getresponse()
                raw = response.read()
            except STALE as e:
                connection.close()
                if reused:
                    continue
                raise DbError(503, f"Could not reach the database: {e}") from None
            except (OSError, http.client.HTTPException) as e:
                connection.close()
                raise DbError(503, f"Could not reach the database: {e}") from None
            if response.will_close:
                connection.close()
            else:
                self._keep(connection)
            break

        if (response.getheader("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        if response.status >= 400:
            try:
                detail = json.loads(raw)
            except ValueError:
                raise DbError(response.status, raw.decode("utf-8", "replace")[:300] or response.reason) from None
            if not isinstance(detail, dict):
                raise DbError(response.status, str(detail)) from None
            raise DbError(response.status, detail.get("message") or str(detail), detail.get("hint"),
                          detail.get("code")) from None
        payload = json.loads(raw) if raw.strip() else None
        return payload, dict(response.getheaders())

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
        pages = each(lambda chunk: self.get(table, {**(params or {}), column: in_list(chunk)}),
                     chunked(list(dict.fromkeys(values))))
        return [row for page in pages for row in page]

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
