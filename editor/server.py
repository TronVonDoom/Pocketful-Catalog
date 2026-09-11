#!/usr/bin/env python3
"""
The card editor: a local web app for correcting what the catalog says about a card.

Why an overrides file instead of editing the catalog
----------------------------------------------------
`catalog/sets/*.json` is not a source file. It is the *output* of
`pull_catalog.py --static`, and the weekly Catalog workflow re-runs that over every set
and opens a pull request with the result. Anything typed directly into those files is
gone the next time upstream is pulled -- not flagged, not conflicted, just quietly
overwritten by whatever TCGdex said that morning.

So edits live in `catalog/overrides.json`, and `pack.py` lays them over the pulled data
on its way into the shipped file. The pull stays a faithful copy of upstream, the
override stays a deliberate statement of "upstream is wrong about this", and the two
never fight. Reverting an edit means deleting one entry rather than remembering what a
field used to say.

What an entry records
---------------------
Each override keeps three things:

    fields      what you want the catalog to say
    upstream    what it said when you overrode it
    editedAt    when

`upstream` is the one that earns its place. Six months from now TCGdex may fix the
typo you worked around, and without a record of what you were correcting there is no way
to tell a still-needed override from a stale one -- both just look like a value that
disagrees with upstream. With it, the editor can show you "upstream has changed since
you overrode this" and let you drop the entry. See `stale` in the API below.

Everything is stdlib. The catalog tooling is deliberately dependency-free so it cannot
be broken by a network having a bad day, and an editor that needed a package manager to
fix one Pokemon's name would be a worse tool than a text file.

Usage:
    python editor/server.py                 # localhost:8766, opens a browser
    python editor/server.py --port 9100 --no-open
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
OVERRIDES = CATALOG / "overrides.json"
HERE = Path(__file__).resolve().parent

# The shape of the overrides document, bumped if an older reader could not cope. Kept
# separate from the catalog's own SCHEMA: they version different things and there is no
# reason for a catalog field addition to invalidate everyone's edits.
SCHEMA = 1

# What may be overridden, and how each value is checked.
#
# A whitelist rather than "anything the card has", for two reasons. A field that pack.py
# does not ship is a field the app will never draw, so accepting an edit to it would be
# accepting an edit that silently does nothing. And a typed check here is the only thing
# between a slip in a text box and a catalog that fails to parse on a phone -- the app
# reads `hp` as an integer, so a string in that slot is a card that does not load.
#
# Keep in step with CARD_FIELDS in tools/pack.py.
FIELDS: dict[str, str] = {
    "name": "str",
    "localId": "str",
    "rarity": "str",
    "illustrator": "str",
    "category": "str",
    "image": "str",
    "imageAlt": "str",
    "imageAltSource": "str",
    "hp": "int",
    "types": "list[str]",
    "description": "str",
    "variants": "variants",
}

VARIANT_KEYS = ("normal", "holo", "reverse", "firstEdition", "wPromo")

# A card id has to survive being put in a URL and in a GraphQL string literal, which is
# the same rule tools/pull_catalog.py applies. Anything else is not a card id we issued.
CARD_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# --------------------------------------------------------------------------- data


class Catalog:
    """
    The catalog in memory, reloaded when the files under it change.

    Reloaded rather than held, because the normal way to use this editor is beside a
    pull: fix three cards, re-run the pull, look again. A server that cached the tree at
    startup would spend the rest of the session describing a catalog that no longer
    exists. The check is the newest mtime in the directory, which is one stat per set --
    cheap enough to do on every request and exact enough to never miss a rebuild.
    """

    def __init__(self) -> None:
        self.sets: list[dict] = []
        self.by_card: dict[str, tuple[dict, dict]] = {}
        self._stamp: float = -1.0

    def _newest(self) -> float:
        if not SETS.is_dir():
            return 0.0
        return max((p.stat().st_mtime for p in SETS.glob("*.json")), default=0.0)

    def load(self) -> None:
        stamp = self._newest()
        if stamp == self._stamp:
            return
        sets, by_card = [], {}
        for path in sorted(SETS.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            sets.append(doc)
            for card in doc.get("cards") or []:
                if card.get("id"):
                    by_card[card["id"]] = (doc, card)
        self.sets, self.by_card, self._stamp = sets, by_card, stamp


CACHE = Catalog()


def read_overrides() -> dict:
    if not OVERRIDES.exists():
        return {"schema": SCHEMA, "cards": {}}
    try:
        doc = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Refused rather than replaced. This file is hand-editable and lives in git, so
        # a syntax error in it is a thing to fix, not a reason to start again empty and
        # throw away every correction in it.
        raise SystemExit(f"{OVERRIDES} is not valid JSON ({exc}). Fix or delete it.")
    doc.setdefault("cards", {})
    return doc


def write_overrides(doc: dict) -> None:
    """
    Writes via a temporary file and one rename.

    The rename is atomic, so a crash or a Ctrl-C mid-write leaves the old file intact
    rather than a half-written one. The alternative is losing every correction ever made
    to a power cut during the save of the next one.
    """
    doc["schema"] = SCHEMA
    doc["cards"] = dict(sorted(doc.get("cards", {}).items()))
    tmp = OVERRIDES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OVERRIDES)


def coerce(field: str, value):
    """
    One field, checked and normalised, or ValueError.

    Empty means "no opinion" and comes back as None, which the caller drops from the
    patch. That is what makes clearing a box in the UI the same gesture as never having
    touched it -- an override that says `"illustrator": ""` would be a claim that the
    card has no illustrator, which is not the same as declining to correct one.
    """
    kind = FIELDS.get(field)
    if kind is None:
        raise ValueError(f"{field} is not an overridable field")

    if kind == "str":
        text = str(value).strip()
        return text or None

    if kind == "int":
        if value in (None, "", []):
            return None
        try:
            return int(str(value).strip())
        except ValueError:
            raise ValueError(f"{field} must be a whole number")

    if kind == "list[str]":
        if isinstance(value, str):
            value = [p.strip() for p in value.split(",")]
        if not isinstance(value, list):
            raise ValueError(f"{field} must be a list")
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or None

    if kind == "variants":
        if not isinstance(value, dict):
            raise ValueError("variants must be an object")
        # Written whole rather than merged. The five flags are one statement about how a
        # card was pressed, and a partial one -- "holo is true, no idea about the rest" --
        # is not something the app can draw a variant picker from.
        return {k: bool(value.get(k)) for k in VARIANT_KEYS}

    raise ValueError(f"no rule for {field}")


def merged(card: dict, entry: dict | None) -> dict:
    out = dict(card)
    if entry:
        out.update(entry.get("fields") or {})
    return out


def is_stale(card: dict, entry: dict) -> bool:
    """
    True when upstream has moved since the override was written.

    Not the same as "the override is wrong" -- upstream may have changed to something
    else wrong -- so it is surfaced and never acted on. It is the difference between a
    correction that is still doing work and one that is quietly duplicating a fix
    somebody else already made.
    """
    was = entry.get("upstream") or {}
    return any(card.get(k) != v for k, v in was.items())


# --------------------------------------------------------------------------- API


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args) -> None:
        line = fmt % args
        if " /api/set/" in line or " /api/index" in line:
            return
        super().log_message(fmt, *args)

    # -- plumbing

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)

        if not path.startswith("/api/"):
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()

        try:
            CACHE.load()
        except (OSError, json.JSONDecodeError) as exc:
            return self._fail(500, f"catalog unreadable: {exc}")

        over = read_overrides()

        if path == "/api/index":
            return self._json({
                "sets": [
                    {
                        "id": s.get("id"),
                        "name": s.get("name"),
                        "serie": (s.get("serie") or {}).get("name"),
                        "releaseDate": s.get("releaseDate"),
                        "cards": len(s.get("cards") or []),
                        "edited": sum(
                            1 for c in (s.get("cards") or []) if c.get("id") in over["cards"]
                        ),
                    }
                    for s in CACHE.sets
                ],
                "overrides": len(over["cards"]),
                "stale": sum(
                    1 for cid, e in over["cards"].items()
                    if cid in CACHE.by_card and is_stale(CACHE.by_card[cid][1], e)
                ),
                "orphans": sorted(c for c in over["cards"] if c not in CACHE.by_card),
            })

        if path.startswith("/api/set/"):
            set_id = urllib.parse.unquote(path[len("/api/set/"):])
            doc = next((s for s in CACHE.sets if s.get("id") == set_id), None)
            if doc is None:
                return self._fail(404, f"no set {set_id}")
            return self._json({
                "id": doc.get("id"),
                "name": doc.get("name"),
                "logo": doc.get("logo"),
                "cards": [self._card_payload(c, over) for c in (doc.get("cards") or [])],
            })

        if path == "/api/search":
            needle = (query.get("q") or [""])[0].strip().lower()
            if len(needle) < 2:
                return self._json({"cards": []})
            hits = []
            for set_doc, card in CACHE.by_card.values():
                name = (card.get("name") or "").lower()
                if needle in name or needle == (card.get("localId") or "").lower() \
                        or needle in (card.get("id") or "").lower():
                    hits.append(self._card_payload(card, over, set_doc))
                    if len(hits) >= 200:
                        break
            hits.sort(key=lambda c: (not c["card"]["name"].lower().startswith(needle),
                                     c["card"]["name"]))
            return self._json({"cards": hits})

        if path == "/api/overrides":
            rows = []
            for cid, entry in sorted(over["cards"].items()):
                found = CACHE.by_card.get(cid)
                rows.append({
                    "id": cid,
                    "fields": entry.get("fields") or {},
                    "upstream": entry.get("upstream") or {},
                    "editedAt": entry.get("editedAt"),
                    "setName": found[0].get("name") if found else None,
                    "orphan": found is None,
                    "stale": bool(found) and is_stale(found[1], entry),
                })
            return self._json({"rows": rows})

        return self._fail(404, "no such endpoint")

    def do_PUT(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if not url.path.startswith("/api/override/"):
            return self._fail(404, "no such endpoint")

        card_id = urllib.parse.unquote(url.path[len("/api/override/"):])
        if not CARD_ID.match(card_id):
            return self._fail(400, "that is not a card id")

        CACHE.load()
        found = CACHE.by_card.get(card_id)
        if not found:
            return self._fail(404, f"no card {card_id} in the catalog")
        _, card = found

        try:
            patch = (self._body() or {}).get("fields") or {}
        except json.JSONDecodeError:
            return self._fail(400, "body was not JSON")

        fields, upstream = {}, {}
        try:
            for key, raw in patch.items():
                value = coerce(key, raw)
                if value is None:
                    continue
                # An "override" that agrees with upstream is not one. Dropping it here
                # keeps the file to things that actually change the shipped catalog, so
                # its length stays a true count of how far it departs from the pull.
                if value == card.get(key):
                    continue
                fields[key] = value
                upstream[key] = card.get(key)
        except ValueError as exc:
            return self._fail(400, str(exc))

        doc = read_overrides()
        if not fields:
            doc["cards"].pop(card_id, None)
            write_overrides(doc)
            return self._json({"ok": True, "removed": True})

        doc["cards"][card_id] = {
            "fields": fields,
            "upstream": upstream,
            "editedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        write_overrides(doc)
        return self._json({"ok": True, "entry": doc["cards"][card_id]})

    def do_DELETE(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if not url.path.startswith("/api/override/"):
            return self._fail(404, "no such endpoint")
        card_id = urllib.parse.unquote(url.path[len("/api/override/"):])
        doc = read_overrides()
        existed = doc["cards"].pop(card_id, None) is not None
        write_overrides(doc)
        return self._json({"ok": True, "removed": existed})

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/api/pack":
            return self._fail(404, "no such endpoint")
        # Repacking from the editor because the alternative is a second terminal and a
        # remembered command. It is the same script the publish workflow runs, so what
        # you check here is what ships.
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "pack.py"), "--static"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        return self._json({
            "ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr).strip(),
        })

    # -- shaping

    def _card_payload(self, card: dict, over: dict, set_doc: dict | None = None) -> dict:
        entry = over["cards"].get(card.get("id"))
        payload = {
            "card": merged(card, entry),
            "upstream": card,
            "override": entry,
            "stale": bool(entry) and is_stale(card, entry),
        }
        if set_doc is not None:
            payload["setId"] = set_doc.get("id")
            payload["setName"] = set_doc.get("name")
        return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if not SETS.is_dir():
        raise SystemExit(
            f"No {SETS}. Run `python tools/pull_catalog.py --static --all` first -- "
            "there is nothing to edit until the catalog has been pulled."
        )

    socketserver.TCPServer.allow_reuse_address = True
    # Bound to loopback rather than 0.0.0.0. This server writes to the repository on an
    # unauthenticated PUT, which is entirely reasonable for a tool only you can reach and
    # not something to put on a network.
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        CACHE.load()
        over = read_overrides()
        print(f"Card editor:  {url}")
        print(f"{len(CACHE.by_card)} cards, {len(over['cards'])} overrides in "
              f"{OVERRIDES.relative_to(ROOT)}")
        print("Ctrl-C to stop.")
        if not args.no_open:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
