#!/usr/bin/env python3
"""
The card editor: a local app for correcting what the catalog says about a card, giving a
card artwork it does not have, and telling the price pull which TCGplayer product a card
or a set really is.

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

Artwork someone supplied
------------------------
A card with no picture anywhere upstream can be given one by hand. The picture has to
be somewhere a phone can download it, so it is committed under `catalog/art/` and the
override points `imageAlt` at its raw.githubusercontent.com address -- the same field
fill_gaps.py fills from pokemontcg.io and TCGplayer, marked `imageAltSource: manual`.

Files are named by a hash of their bytes. A replaced picture is therefore a new URL, so
no phone and no CDN can go on serving the old one from cache, and a file nothing refers
to any more is deleted on the next save rather than left to accumulate.

The browser does the resizing and encoding (a canvas is a perfectly good image codec) so
this server can stay stdlib: it checks the bytes are an image and writes them down.

TCGplayer links
---------------
Written to `catalog/tcgplayer-groups.json` (a set) and `catalog/tcgplayer-cards.json` (a
card). The rules for what those mean, and the automatic match they overrule, are in
tools/tcgplayer.py -- imported here rather than restated, so the match this editor shows
you is the match the nightly price pull makes.

TCGplayer's product lists come from the same `catalog/.tcgcsv/` cache map_groups.py
keeps, so browsing them is mostly reading files. What is not cached is fetched from
tcgcsv.com politely and then cached too.

Everything is stdlib. The catalog tooling is deliberately dependency-free so it cannot
be broken by a network having a bad day, and an editor that needed a package manager to
fix one Pokemon's name would be a worse tool than a text file.

Usage:
    python editor/server.py                 # localhost:8766, opens a browser tab
    python editor/server.py --app           # its own window; exits when that closes
    python editor/server.py --port 9100 --no-open
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
OVERRIDES = CATALOG / "overrides.json"
ART = CATALOG / "art"
HERE = Path(__file__).resolve().parent
LOG = HERE / "editor.log"

sys.path.insert(0, str(ROOT / "tools"))
import tcgplayer  # noqa: E402  (tools/ is not a package; see tools/tcgplayer.py)

# Where committed artwork is served from once it is pushed. raw.githubusercontent.com
# rather than a CDN in front of it: jsDelivr resolves a branch to a commit and caches that
# for hours, so a picture pushed a minute ago would 404 there until it caught up, where
# raw serves it as soon as the push lands. Hashed filenames make its short cache harmless.
ART_BASE = "https://raw.githubusercontent.com/TronVonDoom/Pocketful-Catalog/main/catalog/art/"

USER_AGENT = "Pocketful-catalog-editor/1.0 (+https://github.com/TronVonDoom/Pocketful)"

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

# Fields that may be overridden to *nothing*, as opposed to left alone.
#
# Only the artwork. An empty text box everywhere else means "no opinion", and that has to
# stay true or clearing a box would start asserting things about cards. But "the picture
# upstream has for this card is the wrong picture" is a real correction with no value to
# put in its place, and the app prefers a TCGdex stem over any fallback -- so replacing a
# card's art means removing the stem, not just adding something beside it.
CLEARABLE = ("image", "imageAlt", "imageAltSource")

# The part of a set that can be corrected. Keep in step with SET_OVERRIDABLE in pack.py.
SET_EDITABLE: dict[str, str] = {
    "name": "str",
    "releaseDate": "date",
    "logo": "str",
    "symbol": "str",
}

VARIANT_KEYS = ("normal", "holo", "reverse", "firstEdition", "wPromo")

# A card id has to survive being put in a URL and in a GraphQL string literal, which is
# the same rule tools/pull_catalog.py applies. Anything else is not a card id we issued.
CARD_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ART_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

# What Publish commits: everything this editor writes, and nothing else. A half-finished
# change to a tool sitting in the same working tree is not the editor's to ship.
PUBLISHABLE = (
    "catalog/overrides.json",
    "catalog/art",
    "catalog/tcgplayer-groups.json",
    "catalog/tcgplayer-cards.json",
)

# Big enough for a phone photo of a card sent as base64, small enough that a mistaken
# drop of a video is refused rather than read into memory.
MAX_BODY = 40 * 1024 * 1024
MAX_IMAGE = 12 * 1024 * 1024

# Run under pythonw, every git or pack call would otherwise flash a console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_git() -> str:
    """
    Git for Windows where it is installed, and whatever `git` is on PATH otherwise.

    Not simply the first `git` on PATH, because a shortcut launched from the Start menu
    gets the machine's PATH, not a terminal's, and on a machine with a toolchain that
    bundles its own MSYS2 (devkitPro does) that can be a git with none of Git for Windows'
    configuration: no line-ending conversion and no credential manager. Committing with it
    once rewrote every line of a 2,500-line JSON file as CRLF, which then collided with the
    price job's one-line change to the same file. The repository's .gitattributes makes line
    endings safe under any git; this keeps the push using the credentials you actually have.
    """
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                 os.path.join(os.environ.get("LocalAppData", ""), "Programs")]
        for root in filter(None, roots):
            candidate = Path(root) / "Git" / "cmd" / "git.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which("git") or "git"


GIT = find_git()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if args and args[0] == "git":
        args = [GIT, *args[1:]]
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=timeout, creationflags=NO_WINDOW, env=env,
    )


def write_json(path: Path, doc: dict) -> None:
    """
    Writes via a temporary file and one rename.

    The rename is atomic, so a crash or a Ctrl-C mid-write leaves the old file intact
    rather than a half-written one. The alternative is losing every correction ever made
    to a power cut during the save of the next one.
    """
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- catalog


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
        self.by_set: dict[str, dict] = {}
        self.by_card: dict[str, tuple[dict, dict]] = {}
        self._stamp: float = -1.0
        self._lock = threading.Lock()

    def _newest(self) -> float:
        if not SETS.is_dir():
            return 0.0
        return max((p.stat().st_mtime for p in SETS.glob("*.json")), default=0.0)

    def load(self) -> None:
        with self._lock:
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
            self.by_set = {s.get("id"): s for s in sets}


CACHE = Catalog()

# One writer at a time. The page can fire a save and a link in quick succession, and two
# threads each reading, patching and renaming the same file would lose one of the edits.
WRITE_LOCK = threading.Lock()


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
    doc.setdefault("sets", {})
    return doc


def write_overrides(doc: dict) -> None:
    out = {"schema": SCHEMA, "cards": dict(sorted(doc.get("cards", {}).items()))}
    # Only written once there is something in it, so a catalog nobody has corrected a
    # set in keeps the file it always had.
    if doc.get("sets"):
        out["sets"] = dict(sorted(doc["sets"].items()))
    write_json(OVERRIDES, out)


def read_card_links() -> dict:
    if not tcgplayer.CARD_LINKS.exists():
        return {"note": tcgplayer.CARD_LINKS_NOTE, "cards": {}}
    doc = json.loads(tcgplayer.CARD_LINKS.read_text(encoding="utf-8"))
    doc.setdefault("cards", {})
    return doc


def write_card_links(doc: dict) -> None:
    write_json(tcgplayer.CARD_LINKS, {
        "note": tcgplayer.CARD_LINKS_NOTE,
        "cards": dict(sorted(doc.get("cards", {}).items())),
    })


def read_groups_doc() -> dict:
    if not tcgplayer.GROUPS.exists():
        return {"sets": {}}
    doc = json.loads(tcgplayer.GROUPS.read_text(encoding="utf-8"))
    doc.setdefault("sets", {})
    return doc


def coerce(field: str, value, rules: dict[str, str] = FIELDS):
    """
    One field, checked and normalised, or ValueError.

    Empty means "no opinion" and comes back as None, which the caller drops from the
    patch. That is what makes clearing a box in the UI the same gesture as never having
    touched it -- an override that says `"illustrator": ""` would be a claim that the
    card has no illustrator, which is not the same as declining to correct one.
    """
    kind = rules.get(field)
    if kind is None:
        raise ValueError(f"{field} is not an overridable field")

    if kind == "str":
        text = str(value if value is not None else "").strip()
        return text or None

    if kind == "date":
        text = str(value if value is not None else "").strip()
        if not text:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"{field} must be written YYYY-MM-DD")
        return text

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


def merged(record: dict, entry: dict | None) -> dict:
    out = dict(record)
    if entry:
        for key, value in (entry.get("fields") or {}).items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = value
    return out


def is_stale(record: dict, entry: dict) -> bool:
    """
    True when upstream has moved since the override was written.

    Not the same as "the override is wrong" -- upstream may have changed to something
    else wrong -- so it is surfaced and never acted on. It is the difference between a
    correction that is still doing work and one that is quietly duplicating a fix
    somebody else already made.
    """
    was = entry.get("upstream") or {}
    return any(record.get(k) != v for k, v in was.items())


def has_art(card: dict) -> bool:
    return bool(card.get("image") or card.get("imageAlt"))


# --------------------------------------------------------------------------- artwork


def sniff(data: bytes) -> str | None:
    """The extension these bytes deserve, judged by their magic number, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def store_art(kind: str, stem: str, payload: dict, only: tuple[str, ...]) -> str:
    """
    Writes one picture under catalog/art/<kind>/ and returns its published URL.

    `payload` is what the page sends: base64 bytes the browser has already resized and
    encoded. They are checked here anyway, because "the page would never send that" is
    not a property of a file that ends up in a release.
    """
    try:
        data = base64.b64decode(payload.get("data") or "", validate=True)
    except (ValueError, TypeError):
        raise ValueError("the picture was not readable")
    if not data:
        raise ValueError("the picture was empty")
    if len(data) > MAX_IMAGE:
        raise ValueError("that picture is too large to ship to a phone; use a smaller one")
    ext = sniff(data)
    if ext not in only:
        raise ValueError(f"expected {' or '.join(only)}, got {ext or 'something that is not an image'}")
    digest = hashlib.sha256(data).hexdigest()[:10]
    name = f"{stem}-{digest}.{ext}"
    folder = ART / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return f"{ART_BASE}{kind}/{name}"


def prune_art(over: dict) -> list[str]:
    """
    Deletes every file under catalog/art/ that no override points at any more.

    Run after each write, while the write lock is held, so it only ever sees a file and
    the override that references it together. A picture replaced twice before publishing
    therefore never reaches the repository at all.
    """
    wanted: set[str] = set()
    for entry in (over.get("cards") or {}).values():
        url = (entry.get("fields") or {}).get("imageAlt")
        if isinstance(url, str) and url.startswith(ART_BASE):
            wanted.add(url[len(ART_BASE):])
    for entry in (over.get("sets") or {}).values():
        stem = (entry.get("fields") or {}).get("logo")
        # A logo is stored as a stem, because the app appends ".png" to draw one.
        if isinstance(stem, str) and stem.startswith(ART_BASE):
            wanted.add(stem[len(ART_BASE):] + ".png")
    removed = []
    if ART.is_dir():
        for path in ART.rglob("*"):
            if path.is_file():
                rel = path.relative_to(ART).as_posix()
                if rel not in wanted:
                    path.unlink()
                    removed.append(rel)
    return removed


def fetch_url(url: str, accept: str = "*/*", limit: int = MAX_IMAGE) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=25) as response:
        kind = response.headers.get("Content-Type") or ""
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("that file is too large")
    return data, kind


# --------------------------------------------------------------------------- TCGplayer


class TcgData:
    """
    TCGplayer's catalog as far as this editor needs it, read from the tcgcsv cache.

    Product lists are what map_groups.py already cached, and a set printed years ago has
    no reason to be asked about again. Two things do move: the list of groups (a new set
    appears) and a recent group's products (a set fills in over its first weeks), so those
    are re-asked when they are old. Prices are cached for most of a day, which is how
    often TCGCSV rebuilds them.

    A failed fetch falls back to whatever is on disk, however old. Picking a product to
    link needs the list, not today's copy of it.
    """

    GROUPS_TTL = 24 * 3600
    RECENT_PRODUCTS_TTL = 3 * 24 * 3600
    PRICES_TTL = 20 * 3600
    PAUSE = 0.12

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._slim: list[dict] | None = None
        self._prices: dict[int, dict[int, dict[str, int]]] = {}

    # -- fetching

    def _get(self, url: str):
        with self._lock:
            wait = self.PAUSE - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                data, _ = fetch_url(url, "application/json", limit=64 * 1024 * 1024)
                return json.loads(data.decode("utf-8"))
            except (OSError, ValueError, urllib.error.URLError):
                return None
            finally:
                self._last = time.time()

    def _cached(self, name: str, url: str, ttl: float | None) -> list[dict]:
        path = tcgplayer.CACHE / name
        fresh = path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl)
        if not fresh:
            doc = self._get(url)
            results = (doc or {}).get("results")
            if results is not None:
                tcgplayer.CACHE.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(results), encoding="utf-8")
                if name.startswith("products-"):
                    self._slim = None
                return results
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return []

    # -- the three documents

    def groups(self) -> list[dict]:
        return self._cached("groups.json", f"{tcgplayer.TCGCSV}/{tcgplayer.POKEMON}/groups",
                            self.GROUPS_TTL)

    def group(self, group_id: int) -> dict | None:
        return next((g for g in self.groups() if g.get("groupId") == group_id), None)

    def products(self, group_id: int) -> list[dict]:
        group = self.group(group_id) or {}
        published = (group.get("publishedOn") or "")[:10]
        recent = published >= time.strftime("%Y-%m-%d", time.gmtime(time.time() - 180 * 86400))
        return self._cached(
            f"products-{group_id}.json",
            f"{tcgplayer.TCGCSV}/{tcgplayer.POKEMON}/{group_id}/products",
            self.RECENT_PRODUCTS_TTL if recent else None,
        )

    def prices(self, group_id: int) -> dict[int, dict[str, int]]:
        rows = self._cached(
            f"prices-{group_id}.json",
            f"{tcgplayer.TCGCSV}/{tcgplayer.POKEMON}/{group_id}/prices",
            self.PRICES_TTL,
        )
        out: dict[int, dict[str, int]] = {}
        for row in rows:
            market = row.get("marketPrice")
            key = tcgplayer.finish_key(row.get("subTypeName"))
            if market and market > 0 and key:
                out.setdefault(row["productId"], {})[key] = round(market * 100)
        return out

    # -- shaping

    @staticmethod
    def slim(product: dict, group: dict | None = None) -> dict:
        extended = {e.get("name"): e.get("value") for e in product.get("extendedData") or []}
        out = {
            "productId": product.get("productId"),
            "groupId": product.get("groupId"),
            "name": product.get("name"),
            "number": extended.get("Number"),
            "rarity": extended.get("Rarity"),
            "thumb": product.get("imageUrl"),
            "photo": tcgplayer.PRODUCT_IMAGE.format(product.get("productId")),
            "url": product.get("url"),
        }
        if group:
            out["groupName"] = group.get("name")
        return out

    def _all(self) -> list[dict]:
        """Every cached product, slimmed, with the matching keys precomputed. Built once."""
        if self._slim is None:
            groups = {g["groupId"]: g for g in self.groups()}
            slim = []
            for path in tcgplayer.CACHE.glob("products-*.json"):
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for product in rows:
                    item = self.slim(product, groups.get(product.get("groupId")))
                    item["_name"] = tcgplayer.normalise(item["name"] or "")
                    item["_number"] = tcgplayer.card_number(product)
                    item["_published"] = (groups.get(product.get("groupId")) or {}).get("publishedOn") or ""
                    slim.append(item)
            self._slim = slim
        return self._slim

    def overlap(self, cards: list[dict]) -> dict[int, float]:
        """
        For each group, the share of these cards it sells under the same number and name.

        This is how the set picker ranks its suggestions, and it is far better evidence
        than the names are. TCGdex's "Sun & Moon" is TCGplayer's "SM Base Set", which no
        amount of string similarity connects -- but 150 of its 172 cards are in there at
        the same numbers, and that is not a coincidence two unrelated sets manage.
        """
        wanted = {
            (tcgplayer.normalise_local(c.get("localId")), tcgplayer.normalise(c.get("name") or ""))
            for c in cards if c.get("localId")
        }
        if not wanted:
            return {}
        hits: dict[int, set] = {}
        for item in self._all():
            key = (item["_number"], item["_name"])
            if key in wanted:
                hits.setdefault(item["groupId"], set()).add(key)
        return {gid: len(keys) / len(wanted) for gid, keys in hits.items()}

    def search(self, needle: str, limit: int = 150) -> list[dict]:
        """
        Every cached product whose name holds every word typed.

        A number typed on its own ("4", "4/102", "#4") matches a printed number instead of
        a name, so "charizard 4" finds Base Set Charizard rather than every Charizard.
        """
        words, numbers = [], []
        for token in needle.lower().split():
            bare = token.lstrip("#")
            if re.fullmatch(r"[a-z]*\d+[a-z]*(/\w+)?", bare) and any(ch.isdigit() for ch in bare):
                numbers.append(bare.split("/")[0].lstrip("0") or "0")
            else:
                words.append(tcgplayer.normalise(token))
        words = [w for w in words if w]
        if not words and not numbers:
            return []

        hits = [
            item for item in self._all()
            if all(w in item["_name"] for w in words)
            and all(n == (item["_number"] or "").lower() for n in numbers)
        ]
        # Newest set first, then names that start with what was typed ahead of names that
        # merely contain it. Two stable sorts, because one key cannot run both directions.
        first = words[0] if words else ""
        hits.sort(key=lambda i: i["_published"], reverse=True)
        hits.sort(key=lambda i: not i["_name"].startswith(first))
        return [{k: v for k, v in i.items() if not k.startswith("_")} for i in hits[:limit]]


TCG = TcgData()


def auto_match(set_doc: dict, card: dict, mapping: dict) -> tuple[dict | None, bool]:
    """
    The product the nightly pull would price this card from, and whether prices were
    available to decide it exactly as the pull does.
    """
    group_id = (mapping.get(set_doc.get("id")) or {}).get("groupId")
    if not group_id:
        return None, True
    products = TCG.products(group_id)
    prices = TCG.prices(group_id)
    usable = (lambda p: p.get("productId") in prices) if prices else (lambda p: True)
    product = tcgplayer.pick_by_number(products, usable).get(
        tcgplayer.normalise_local(card.get("localId")) or "")
    if not product:
        return None, bool(prices)
    return {**TCG.slim(product, TCG.group(group_id)), "prices": prices.get(product["productId"])}, bool(prices)


# --------------------------------------------------------------------------- publishing


def head_json(rel: str) -> dict:
    proc = run(["git", "show", f"HEAD:{rel}"], timeout=30)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def current_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def changed_keys(before: dict, after: dict) -> int:
    return sum(1 for k in set(before) | set(after) if before.get(k) != after.get(k))


def pending_changes() -> dict:
    """What Publish would commit, counted in the units a person thinks in."""
    status = run(["git", "status", "--porcelain", "--untracked-files=all", "--", *PUBLISHABLE], timeout=30)
    if status.returncode != 0:
        return {"error": status.stderr.strip() or "git is not available", "files": []}
    files = [line for line in status.stdout.splitlines() if line.strip()]

    over_head, over_now = head_json("catalog/overrides.json"), current_json(OVERRIDES)
    links_head = head_json("catalog/tcgplayer-cards.json")
    links_now = current_json(tcgplayer.CARD_LINKS)
    groups_head = head_json("catalog/tcgplayer-groups.json")
    groups_now = current_json(tcgplayer.GROUPS)

    art_added = sum(1 for f in files if "catalog/art/" in f and "D" not in f[:2])
    counts = {
        "cards": changed_keys(over_head.get("cards") or {}, over_now.get("cards") or {}),
        "sets": changed_keys(over_head.get("sets") or {}, over_now.get("sets") or {}),
        "cardLinks": changed_keys(links_head.get("cards") or {}, links_now.get("cards") or {}),
        "setLinks": changed_keys(groups_head.get("sets") or {}, groups_now.get("sets") or {}),
        "images": art_added,
    }

    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    parts = []
    corrected = [plural(counts[k], w) for k, w in (("cards", "card"), ("sets", "set")) if counts[k]]
    if corrected:
        parts.append("Correct " + " and ".join(corrected))
    if counts["images"]:
        parts.append(f"add {plural(counts['images'], 'picture')}")
    linked = [plural(counts[k], w) for k, w in (("cardLinks", "card"), ("setLinks", "set")) if counts[k]]
    if linked:
        parts.append("link " + " and ".join(linked) + " to TCGplayer")
    message = ", ".join(parts) if parts else "Catalog edits"
    message = message[:1].upper() + message[1:]

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=15).stdout.strip()
    return {"files": files, "counts": counts, "message": message, "branch": branch}


def git_unfinished() -> str | None:
    """What git is in the middle of in this repository, if anything, in words."""
    git_dir = ROOT / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "a rebase"
    if (git_dir / "MERGE_HEAD").exists():
        return "a merge"
    return None


def unreadable_file() -> tuple[Path, str] | None:
    """The first file this editor writes that no longer parses, and why."""
    for path in (OVERRIDES, tcgplayer.GROUPS, tcgplayer.CARD_LINKS):
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return path, str(exc)
    return None


def explain(exc: BaseException) -> str:
    """
    An error in words a person can act on.

    Any exception in a handler used to drop the connection, which the browser reports as
    "Failed to fetch" -- true, and no help at all when the real problem is a file left full
    of conflict markers by an unfinished rebase.
    """
    broken = unreadable_file()
    if broken:
        path, why = broken
        text = f"{path.relative_to(ROOT).as_posix()} cannot be read ({why})."
        busy = git_unfinished()
        if busy:
            verb = busy.split()[-1]
            text += (f" Git is in the middle of {busy}, which leaves conflict markers in files. "
                     f"Finish or abort it in VS Code or a terminal (git {verb} --abort puts "
                     "everything back as it was), then reload.")
        return text
    return f"{type(exc).__name__}: {exc}"


def publish(message: str) -> dict:
    log: list[str] = []

    def step(args: list[str]) -> bool:
        proc = run(args)
        log.append("$ " + " ".join(args))
        text = (proc.stdout + proc.stderr).strip()
        if text:
            log.append(text)
        return proc.returncode == 0

    busy = git_unfinished()
    if busy:
        return {"ok": False, "output": f"Git is in the middle of {busy} in this repository. "
                                       "Finish or abort it in VS Code or a terminal first."}
    if not pending_changes().get("files"):
        return {"ok": False, "output": "Nothing to publish."}

    if not step(["git", "add", "-A", "--", *PUBLISHABLE]):
        return {"ok": False, "output": "\n".join(log)}
    # Committed by path, so anything else that happens to be staged -- a tool someone is
    # halfway through editing -- stays out of a commit that says it is catalog edits.
    if not step(["git", "commit", "-m", message, "--", *PUBLISHABLE]):
        return {"ok": False, "output": "\n".join(log)}
    # The nightly price job commits newly mapped sets to main, so the branch here may be
    # behind. Rebasing first turns that from a rejected push into a non-event.
    if not step(["git", "pull", "--rebase", "--autostash"]):
        # Never left half-done. A stopped rebase leaves conflict markers inside the very
        # JSON files this editor reads, so the editor itself stops working -- and the person
        # looking at it is the one least likely to want to finish a rebase by hand.
        if git_unfinished() == "a rebase":
            step(["git", "rebase", "--abort"])
        return {"ok": False, "committed": True, "output": "\n".join(log)
                + "\n\nYour edits are committed on this computer, but GitHub has changes to the "
                  "same lines, so the two could not be combined automatically. Nothing was "
                  "pushed and nothing is lost. Resolve it in VS Code or a terminal "
                  "(git pull --rebase), then push."}
    if not step(["git", "push"]):
        return {"ok": False, "committed": True, "output": "\n".join(log)
                + "\n\nCommitted locally, but the push failed. Nothing is lost; push again "
                  "when the problem above is fixed."}
    return {"ok": True, "output": "\n".join(log)}


# --------------------------------------------------------------------------- app window


class Presence:
    """
    Knows whether any editor window is still open, so the app can exit with its window.

    Under `--app` the server runs without a console, so there is nothing to Ctrl-C and a
    server left behind would sit invisibly on the port until the next reboot. Each page
    says hello every half minute and goodbye as it closes. A goodbye starts a short grace
    period rather than stopping outright, because a reload is a goodbye followed a moment
    later by a hello, and that should not kill the app underneath it.

    The half-minute heartbeat is a backstop for a window that closed without managing to
    say goodbye. Its expiry is generous because a browser throttles timers in a minimised
    window to about one a minute, and a minimised editor is not a closed one.
    """

    EXPIRE = 10 * 60
    GRACE = 6
    NOBODY_CAME = 3 * 60

    def __init__(self) -> None:
        self.clients: dict[str, float] = {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.seen = False

    def hello(self, client: str) -> None:
        with self.lock:
            self.clients[client] = time.time()
            self.seen = True

    def bye(self, client: str) -> None:
        with self.lock:
            self.clients.pop(client, None)

    def watch(self, httpd: socketserver.BaseServer) -> None:
        empty_since = None
        while True:
            time.sleep(2)
            with self.lock:
                cutoff = time.time() - self.EXPIRE
                self.clients = {k: v for k, v in self.clients.items() if v >= cutoff}
                open_windows = len(self.clients)
            if not self.seen:
                if time.time() - self.started > self.NOBODY_CAME:
                    print("No window ever connected; exiting.")
                    break
                continue
            if open_windows:
                empty_since = None
                continue
            empty_since = empty_since or time.time()
            if time.time() - empty_since > self.GRACE:
                print("Editor window closed; exiting.")
                break
        threading.Thread(target=httpd.shutdown, daemon=True).start()


PRESENCE = Presence()


def open_window(url: str, app: bool) -> None:
    """
    Opens the editor as its own window where a Chromium browser is installed.

    Edge ships with Windows, and `--app` gives it a window with no tabs or address bar --
    which is the difference between a tool and a web page someone has to find again among
    thirty tabs. Anything else falls back to an ordinary browser tab.
    """
    if app:
        candidates = [
            shutil.which("msedge"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            shutil.which("chrome"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for exe in candidates:
            if exe and os.path.isfile(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,960"],
                                 creationflags=NO_WINDOW, close_fds=True)
                return
    webbrowser.open(url)


# --------------------------------------------------------------------------- API


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args) -> None:
        line = fmt % args
        if any(f" /{p}" in line for p in ("api/set/", "api/index", "api/hello", "art/",
                                             "api/tcgplayer/", "api/changes")):
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

    def _bytes(self, data: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _trusted(self) -> bool:
        """
        Whether a request really came from this editor's own page.

        The server is bound to loopback, which keeps the network out but not the browser:
        any web page open in the same browser can aim a request at 127.0.0.1. Checking
        Host defeats DNS rebinding, and checking Origin stops a cross-site POST, which is
        the one kind a browser sends without asking first -- and this server commits and
        pushes on a POST.
        """
        port = self.server.server_address[1]
        own = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in own:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in own}

    def _safely(self, handler) -> None:
        """Any failure becomes an answer with a reason, never a dropped connection."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (Exception, SystemExit) as exc:  # noqa: BLE001  (read_overrides exits on bad JSON)
            traceback.print_exc()
            try:
                self._fail(500, explain(exc))
            except OSError:
                pass

    @staticmethod
    def _tail(path: str, prefix: str) -> str:
        return urllib.parse.unquote(path[len(prefix):])

    # -- GET

    def do_GET(self) -> None:  # noqa: N802
        self._safely(self._route_get)

    def _route_get(self) -> None:
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)

        if path.startswith("/art/"):
            return self._art(path)

        if not path.startswith("/api/"):
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()

        if not self._trusted():
            return self._fail(403, "not from this editor")

        if path == "/api/ping":
            return self._json({"app": "pocketful-editor"})

        if path == "/api/hello":
            PRESENCE.hello((query.get("c") or [""])[0])
            return self._json({"ok": True})

        try:
            CACHE.load()
        except (OSError, json.JSONDecodeError) as exc:
            return self._fail(500, f"catalog unreadable: {exc}")

        over = read_overrides()

        if path == "/api/index":
            return self._json(self._index(over))

        if path.startswith("/api/set/"):
            set_id = self._tail(path, "/api/set/")
            doc = CACHE.by_set.get(set_id)
            if doc is None:
                return self._fail(404, f"no set {set_id}")
            links = tcgplayer.load_card_links()
            entry = over["sets"].get(set_id)
            return self._json({
                "set": self._set_payload(doc, over),
                "cards": [self._card_payload(c, over, links=links) for c in (doc.get("cards") or [])],
                "override": entry,
            })

        if path == "/api/search":
            needle = (query.get("q") or [""])[0].strip().lower()
            if len(needle) < 2:
                return self._json({"cards": []})
            links = tcgplayer.load_card_links()
            hits = []
            for set_doc, card in CACHE.by_card.values():
                shown = merged(card, over["cards"].get(card.get("id")))
                name = (shown.get("name") or "").lower()
                if needle in name or needle == (card.get("localId") or "").lower() \
                        or needle in (card.get("id") or "").lower():
                    hits.append(self._card_payload(card, over, set_doc, links))
                    if len(hits) >= 200:
                        break
            hits.sort(key=lambda c: (not (c["card"].get("name") or "").lower().startswith(needle),
                                     c["card"].get("name") or ""))
            return self._json({"cards": hits})

        if path == "/api/no-art":
            links = tcgplayer.load_card_links()
            rows = [
                self._card_payload(card, over, set_doc, links)
                for set_doc, card in CACHE.by_card.values()
                if not has_art(merged(card, over["cards"].get(card.get("id"))))
            ]
            return self._json({"cards": rows})

        if path == "/api/linked":
            links = tcgplayer.load_card_links()
            rows = [
                self._card_payload(CACHE.by_card[cid][1], over, CACHE.by_card[cid][0], links)
                for cid in sorted(links) if cid in CACHE.by_card
            ]
            return self._json({"cards": rows})

        if path == "/api/overrides":
            links = tcgplayer.load_card_links()
            rows = []
            for cid, entry in sorted(over["cards"].items()):
                found = CACHE.by_card.get(cid)
                rows.append({
                    "id": cid,
                    "card": merged(found[1], entry) if found else None,
                    "fullUpstream": found[1] if found else None,
                    "link": links.get(cid),
                    "fields": entry.get("fields") or {},
                    "upstream": entry.get("upstream") or {},
                    "editedAt": entry.get("editedAt"),
                    "setId": found[0].get("id") if found else None,
                    "setName": found[0].get("name") if found else None,
                    "orphan": found is None,
                    "stale": bool(found) and is_stale(found[1], entry),
                })
            return self._json({"rows": rows})

        if path.startswith("/api/tcgplayer/card/"):
            card_id = self._tail(path, "/api/tcgplayer/card/")
            found = CACHE.by_card.get(card_id)
            if not found:
                return self._fail(404, f"no card {card_id}")
            set_doc, card = found
            mapping = tcgplayer.load_groups()
            link = tcgplayer.load_card_links().get(card_id)
            auto, exact = auto_match(set_doc, card, mapping)
            linked = None
            if link and link.get("productId") and link.get("groupId"):
                product = next((p for p in TCG.products(link["groupId"])
                                if p.get("productId") == link["productId"]), None)
                if product:
                    linked = {**TCG.slim(product, TCG.group(link["groupId"])),
                              "prices": TCG.prices(link["groupId"]).get(link["productId"])}
                else:
                    linked = {"productId": link["productId"], "groupId": link["groupId"],
                              "name": link.get("tcgplayerName"), "missing": True}
            return self._json({
                "setGroup": mapping.get(set_doc.get("id")),
                "auto": auto,
                "exact": exact,
                "link": link,
                "linked": linked,
            })

        if path == "/api/tcgplayer/groups":
            mapping = tcgplayer.load_groups()
            used: dict[int, list[str]] = {}
            for sid, entry in mapping.items():
                if entry.get("groupId"):
                    used.setdefault(entry["groupId"], []).append(sid)
            target = CACHE.by_set.get((query.get("for") or [""])[0])
            key = tcgplayer.normalise((target or {}).get("name") or "")
            overlap = TCG.overlap((target or {}).get("cards") or []) if target else {}
            rows = []
            for g in TCG.groups():
                row = {k: g.get(k) for k in ("groupId", "name", "abbreviation", "publishedOn")}
                row["usedBy"] = used.get(g.get("groupId"), [])
                if target:
                    row["cardsMatched"] = round(overlap.get(g.get("groupId"), 0.0), 3)
                    row["nameSimilarity"] = round(difflib.SequenceMatcher(
                        None, key, tcgplayer.normalise(g.get("name") or "")).ratio(), 3)
                rows.append(row)
            # Shared cards first, by a distance; the name only breaks ties, which in practice
            # means it orders the groups that share nothing.
            rows.sort(key=lambda r: (-(r.get("cardsMatched") or 0), -(r.get("nameSimilarity") or 0),
                                     r.get("name") or ""))
            return self._json({"groups": rows})

        if path.startswith("/api/tcgplayer/group/"):
            try:
                group_id = int(self._tail(path, "/api/tcgplayer/group/"))
            except ValueError:
                return self._fail(400, "not a group id")
            group = TCG.group(group_id)
            if group is None:
                return self._fail(404, f"TCGplayer has no group {group_id}")
            prices = TCG.prices(group_id)
            products = [{**TCG.slim(p, group), "prices": prices.get(p.get("productId"))}
                        for p in TCG.products(group_id)]

            # In printed order, so a set reads the way its binder does. Products with no
            # number -- booster boxes, tins -- sink to the bottom.
            def order(p):
                number = (p["number"] or "").split("/")[0].strip()
                digits = re.sub(r"\D", "", number)
                return (not number, int(digits) if digits else 0, number, p["name"] or "")

            products.sort(key=order)
            return self._json({"group": group, "products": products})

        if path == "/api/tcgplayer/search":
            needle = (query.get("q") or [""])[0].strip()
            return self._json({"products": TCG.search(needle) if len(needle) >= 2 else []})

        if path == "/api/changes":
            return self._json(pending_changes())

        return self._fail(404, "no such endpoint")

    def _art(self, path: str) -> None:
        """Committed artwork, served locally so it can be seen before it is pushed."""
        parts = path[len("/art/"):].split("/")
        if len(parts) != 2 or parts[0] not in ("cards", "logos") or not ART_NAME.match(parts[1]):
            return self._fail(404, "no such picture")
        file = ART / parts[0] / parts[1]
        if not file.is_file():
            return self._fail(404, "no such picture")
        kind = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg"}.get(
            file.suffix.lstrip("."), "application/octet-stream")
        return self._bytes(file.read_bytes(), kind)

    # -- PUT

    def do_PUT(self) -> None:  # noqa: N802
        self._safely(self._route_put)

    def _route_put(self) -> None:
        if not self._trusted():
            return self._fail(403, "not from this editor")
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._fail(400, "body was not JSON")
        CACHE.load()

        routes = {
            "/api/override/": self._put_card,
            "/api/set-override/": self._put_set,
            "/api/link/card/": self._put_card_link,
            "/api/link/set/": self._put_set_link,
        }
        for prefix, handler in routes.items():
            if path.startswith(prefix):
                key = self._tail(path, prefix)
                if not CARD_ID.match(key):
                    return self._fail(400, "that is not an id")
                try:
                    with WRITE_LOCK:
                        return handler(key, body)
                except ValueError as exc:
                    return self._fail(400, str(exc))
        return self._fail(404, "no such endpoint")

    def _put_card(self, card_id: str, body: dict) -> None:
        found = CACHE.by_card.get(card_id)
        if not found:
            return self._fail(404, f"no card {card_id} in the catalog")
        _, card = found

        patch = body.get("fields") or {}
        clear = body.get("clear") or []
        fields, upstream = {}, {}

        for key, raw in patch.items():
            if key in clear:
                continue
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

        for key in clear:
            if key not in CLEARABLE:
                raise ValueError(f"{key} cannot be cleared")
            fields.pop(key, None)
            if card.get(key) is not None:
                fields[key] = None
                upstream[key] = card.get(key)

        if body.get("art"):
            url = store_art("cards", card_id, body["art"], ("webp", "jpg", "png"))
            fields["imageAlt"], upstream["imageAlt"] = url, card.get("imageAlt")
            fields["imageAltSource"], upstream["imageAltSource"] = "manual", card.get("imageAltSource")
            # The app draws a TCGdex stem in preference to any fallback, so a picture
            # supplied for a card that already has one is only seen if the stem goes.
            if card.get("image"):
                fields["image"], upstream["image"] = None, card.get("image")
            else:
                fields.pop("image", None)
                upstream.pop("image", None)

        doc = read_overrides()
        if not fields:
            doc["cards"].pop(card_id, None)
            write_overrides(doc)
            prune_art(doc)
            return self._json({"ok": True, "removed": True})

        doc["cards"][card_id] = {"fields": fields, "upstream": upstream, "editedAt": now()}
        write_overrides(doc)
        prune_art(doc)
        return self._json({"ok": True, "entry": doc["cards"][card_id]})

    def _put_set(self, set_id: str, body: dict) -> None:
        set_doc = CACHE.by_set.get(set_id)
        if not set_doc:
            return self._fail(404, f"no set {set_id} in the catalog")

        fields, upstream = {}, {}
        for key, raw in (body.get("fields") or {}).items():
            value = coerce(key, raw, SET_EDITABLE)
            if value is None or value == set_doc.get(key):
                continue
            fields[key], upstream[key] = value, set_doc.get(key)

        if body.get("logo"):
            url = store_art("logos", set_id, body["logo"], ("png",))
            stem = url[: -len(".png")]
            fields["logo"], upstream["logo"] = stem, set_doc.get("logo")

        doc = read_overrides()
        if not fields:
            doc["sets"].pop(set_id, None)
            write_overrides(doc)
            prune_art(doc)
            return self._json({"ok": True, "removed": True})

        doc["sets"][set_id] = {"fields": fields, "upstream": upstream, "editedAt": now()}
        write_overrides(doc)
        prune_art(doc)
        return self._json({"ok": True, "entry": doc["sets"][set_id]})

    def _put_card_link(self, card_id: str, body: dict) -> None:
        found = CACHE.by_card.get(card_id)
        if not found:
            return self._fail(404, f"no card {card_id} in the catalog")
        set_doc, card = found
        product_id = body.get("productId")
        group_id = body.get("groupId")

        entry: dict = {"groupId": None, "productId": None, "linkedAt": now()}
        if product_id is not None:
            try:
                product_id, group_id = int(product_id), int(group_id)
            except (TypeError, ValueError):
                raise ValueError("a link needs a product and the group it is in")
            product = next((p for p in TCG.products(group_id) if p.get("productId") == product_id), None)
            if product is None:
                raise ValueError(f"TCGplayer group {group_id} has no product {product_id}")
            auto, _ = auto_match(set_doc, card, tcgplayer.load_groups())
            doc = read_card_links()
            # The same rule as an override that agrees with upstream: pointing a card at
            # the product it already matches is not a link, so none is written.
            if auto and auto.get("productId") == product_id:
                doc["cards"].pop(card_id, None)
                write_card_links(doc)
                return self._json({"ok": True, "removed": True})
            slim = TCG.slim(product, TCG.group(group_id))
            entry.update({
                "groupId": group_id, "productId": product_id,
                "tcgplayerName": slim["name"], "number": slim["number"],
            })
            doc["cards"][card_id] = entry
        else:
            doc = read_card_links()
            doc["cards"][card_id] = entry
        write_card_links(doc)
        return self._json({"ok": True, "entry": entry})

    def _put_set_link(self, set_id: str, body: dict) -> None:
        set_doc = CACHE.by_set.get(set_id)
        if not set_doc:
            return self._fail(404, f"no set {set_id} in the catalog")
        group_id = body.get("groupId")
        group = None
        if group_id is not None:
            try:
                group_id = int(group_id)
            except (TypeError, ValueError):
                raise ValueError("not a group id")
            group = TCG.group(group_id)
            if group is None:
                raise ValueError(f"TCGplayer has no group {group_id}")

        doc = read_groups_doc()
        prior = doc["sets"].get(set_id) or {}
        auto = prior.get("auto") if prior.get("via") == "manual" else (prior or None)

        if auto is not None and auto.get("groupId") == group_id:
            doc["sets"][set_id] = auto
            write_json(tcgplayer.GROUPS, doc)
            return self._json({"ok": True, "removed": True, "entry": auto})

        entry = {
            "groupId": group_id,
            "via": "manual",
            "name": set_doc.get("name"),
            "tcgplayerName": (group or {}).get("name"),
            "linkedAt": now(),
            "auto": auto,
        }
        doc["sets"][set_id] = entry
        write_json(tcgplayer.GROUPS, doc)
        return self._json({"ok": True, "entry": entry})

    # -- DELETE

    def do_DELETE(self) -> None:  # noqa: N802
        self._safely(self._route_delete)

    def _route_delete(self) -> None:
        if not self._trusted():
            return self._fail(403, "not from this editor")
        path = urllib.parse.urlparse(self.path).path

        with WRITE_LOCK:
            if path.startswith("/api/override/"):
                doc = read_overrides()
                existed = doc["cards"].pop(self._tail(path, "/api/override/"), None) is not None
                write_overrides(doc)
                prune_art(doc)
                return self._json({"ok": True, "removed": existed})

            if path.startswith("/api/set-override/"):
                doc = read_overrides()
                existed = doc["sets"].pop(self._tail(path, "/api/set-override/"), None) is not None
                write_overrides(doc)
                prune_art(doc)
                return self._json({"ok": True, "removed": existed})

            if path.startswith("/api/link/card/"):
                doc = read_card_links()
                existed = doc["cards"].pop(self._tail(path, "/api/link/card/"), None) is not None
                write_card_links(doc)
                return self._json({"ok": True, "removed": existed})

            if path.startswith("/api/link/set/"):
                set_id = self._tail(path, "/api/link/set/")
                doc = read_groups_doc()
                prior = doc["sets"].get(set_id)
                if not prior or prior.get("via") != "manual":
                    return self._json({"ok": True, "removed": False})
                # Put back what the automatic match said. If it never said anything,
                # the entry goes, and map_groups.py works it out on its next run.
                if prior.get("auto"):
                    doc["sets"][set_id] = prior["auto"]
                else:
                    doc["sets"].pop(set_id, None)
                write_json(tcgplayer.GROUPS, doc)
                return self._json({"ok": True, "removed": True})

        return self._fail(404, "no such endpoint")

    # -- POST

    def do_POST(self) -> None:  # noqa: N802
        self._safely(self._route_post)

    def _route_post(self) -> None:
        if not self._trusted():
            return self._fail(403, "not from this editor")
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)

        if path == "/api/bye":
            PRESENCE.bye((query.get("c") or [""])[0])
            return self._json({"ok": True})

        # Everything below changes something, so it has to be a JSON request. A browser
        # will not send one cross-site without a preflight this server never answers.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._fail(415, "expected JSON")
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._fail(400, "body was not JSON")

        if path == "/api/pack":
            # Repacking from the editor because the alternative is a second terminal and a
            # remembered command. It is the same script the publish workflow runs, so what
            # you check here is what ships.
            proc = run([sys.executable, str(ROOT / "tools" / "pack.py"), "--static"])
            return self._json({
                "ok": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr).strip(),
            })

        if path == "/api/fetch-image":
            # The page cannot read a picture off another site itself -- the browser taints
            # a canvas drawn from a cross-origin image -- so the bytes come through here.
            target = str(body.get("url") or "").strip()
            if urllib.parse.urlparse(target).scheme not in ("http", "https"):
                return self._fail(400, "that is not a web address")
            try:
                data, kind = fetch_url(target, "image/*")
            except urllib.error.HTTPError as exc:
                return self._fail(502, f"that site answered {exc.code}. Try right-clicking the "
                                       "picture, choosing Copy image, and pasting it here instead.")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                return self._fail(502, f"could not download it ({exc})")
            if not kind.startswith("image/"):
                return self._fail(415, "that address is a web page, not a picture. Right-click the "
                                       "picture itself and choose Copy image address.")
            return self._bytes(data, kind.split(";")[0])

        if path == "/api/publish":
            message = str(body.get("message") or "").strip()
            if not message:
                return self._fail(400, "a commit needs a message")
            with WRITE_LOCK:
                return self._json(publish(message))

        return self._fail(404, "no such endpoint")

    # -- shaping

    def _index(self, over: dict) -> dict:
        mapping = tcgplayer.load_groups()
        links = tcgplayer.load_card_links()
        sets, no_art = [], 0
        for s in CACHE.sets:
            cards = s.get("cards") or []
            missing = sum(1 for c in cards if not has_art(merged(c, over["cards"].get(c.get("id")))))
            no_art += missing
            shown = merged(s, over["sets"].get(s.get("id")))
            sets.append({
                "id": s.get("id"),
                "name": shown.get("name"),
                "serie": s.get("serie") or {},
                "releaseDate": shown.get("releaseDate"),
                "logo": shown.get("logo"),
                "cards": len(cards),
                "edited": sum(1 for c in cards if c.get("id") in over["cards"]),
                "setEdited": s.get("id") in over["sets"],
                "noArt": missing,
                "linked": sum(1 for c in cards if c.get("id") in links),
                "tcgplayer": mapping.get(s.get("id")),
            })
        unlinked = sum(
            1 for s in sets
            if not (s["tcgplayer"] or {}).get("groupId")
            and (s["tcgplayer"] or {}).get("via") not in ("not-sold", "manual")
        )
        return {
            "sets": sets,
            "overrides": len(over["cards"]),
            "setOverrides": len(over["sets"]),
            "stale": sum(
                1 for cid, e in over["cards"].items()
                if cid in CACHE.by_card and is_stale(CACHE.by_card[cid][1], e)
            ),
            "orphans": sorted(c for c in over["cards"] if c not in CACHE.by_card),
            "noArt": no_art,
            "cardLinks": len(links),
            "unlinkedSets": unlinked,
            "artBase": ART_BASE,
            "app": self.server.app_mode,
            "gitUnfinished": git_unfinished(),
        }

    def _set_payload(self, doc: dict, over: dict) -> dict:
        entry = over["sets"].get(doc.get("id"))
        upstream = {k: doc.get(k) for k in ("id", "name", "logo", "symbol", "releaseDate", "serie", "abbreviation")}
        return {
            "set": merged(upstream, entry),
            "upstream": upstream,
            "override": entry,
            "stale": bool(entry) and is_stale(doc, entry),
            "tcgplayer": tcgplayer.load_groups().get(doc.get("id")),
            "cardCount": len(doc.get("cards") or []),
        }

    def _card_payload(self, card: dict, over: dict, set_doc: dict | None = None,
                      links: dict | None = None) -> dict:
        entry = over["cards"].get(card.get("id"))
        payload = {
            "card": merged(card, entry),
            "upstream": card,
            "override": entry,
            "stale": bool(entry) and is_stale(card, entry),
            "link": (links or {}).get(card.get("id")),
        }
        if set_doc is not None:
            payload["setId"] = set_doc.get("id")
            payload["setName"] = set_doc.get("name")
        return payload


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Off on Windows, where SO_REUSEADDR does not mean "reuse a port in TIME_WAIT" but "let
    # two servers bind the same port" -- a second editor would start without complaint and
    # the two would take turns answering. already_running() handles a second launch there.
    allow_reuse_address = os.name != "nt"
    app_mode = False


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "pocketful-editor"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--app", action="store_true",
                    help="open in its own window and exit when it is closed")
    args = ap.parse_args()

    if args.app and (sys.stdout is None or not sys.stdout.isatty()):
        # Under pythonw there is no console to print to, and http.server logs to stderr,
        # which would be None. A log file is also the only way anyone finds out why the
        # window never appeared.
        stream = open(LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        print(f"\n--- {now()} starting")

    url = f"http://127.0.0.1:{args.port}/"

    # A second launch opens another window onto the editor that is already running,
    # instead of failing on a port the first one holds.
    if already_running(args.port):
        print(f"Already running at {url}" + ("." if args.no_open else "; opening a window onto it."))
        if not args.no_open:
            open_window(url, args.app)
        return

    if not SETS.is_dir():
        raise SystemExit(
            f"No {SETS}. Run `python tools/pull_catalog.py --static --all` first -- "
            "there is nothing to edit until the catalog has been pulled."
        )

    # Bound to loopback rather than 0.0.0.0. This server writes to the repository on an
    # unauthenticated PUT, which is entirely reasonable for a tool only you can reach and
    # not something to put on a network.
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        httpd.app_mode = args.app
        CACHE.load()
        over = read_overrides()
        print(f"Card editor:  {url}")
        print(f"{len(CACHE.by_card)} cards, {len(over['cards'])} overrides in "
              f"{OVERRIDES.relative_to(ROOT)}")
        if args.app:
            threading.Thread(target=PRESENCE.watch, args=(httpd,), daemon=True).start()
        else:
            print("Ctrl-C to stop.")
        if not args.no_open:
            threading.Timer(0.4, lambda: open_window(url, args.app)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
