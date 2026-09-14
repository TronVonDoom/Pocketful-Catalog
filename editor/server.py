#!/usr/bin/env python3
"""
The Pocketful Editor: where the catalog is built, reviewed and published.

It is a small web server on this computer and a page that talks to it, opened as a window
of its own. The page never holds a key: it asks this server, and this server talks to the
catalog database in Supabase and to the public files on Cloudflare R2, with the credentials
kept in ~/keystores (see tools/supabase_config.py and tools/r2.py). docs/database.md is the
design everything here follows.

The database keeps its own rules -- IDs built from their parts, published records locked,
words and terms from their lists, the publish gate -- so this server does not restate them.
It passes the page's edits through, and when the database refuses one, the database's own
explanation goes back to the page word for word.

Nothing is imported unless you start it: an import is one set, from TCGdex, from the Import
tab of that set. See backend/tcgdex.py.

Stdlib only, like the rest of the tooling.

Usage:
    python editor/server.py                 # 127.0.0.1:8767, opens a browser tab
    python editor/server.py --app           # its own window; exits when that closes
    python editor/server.py --port 9100 --no-open

For tests, environment variables point it somewhere other than the real services:
POCKETFUL_REST_URL and POCKETFUL_REST_KEY (a database API), POCKETFUL_STORE=local:<folder>
(a folder instead of R2), and POCKETFUL_TCGDEX_FIXTURES / POCKETFUL_TCGCSV_FIXTURES=<folder>
(recorded TCGdex and TCGCSV answers).
"""

from __future__ import annotations

import argparse
import base64
import datetime
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = HERE / "app"
LOG = HERE / "editor.log"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

from backend import media, publish, tcgdex, tcgplayer_links  # noqa: E402
from backend.db import Db, DbError, eq, in_list  # noqa: E402
from backend.storage import LocalStore, R2Store  # noqa: E402
from tcgcsv import Tcgcsv, TcgcsvError  # noqa: E402

APP_NAME = "pocketful-editor-2"
DEFAULT_PORT = 8767
MAX_BODY = 40 * 1024 * 1024
MAX_FETCH = 15 * 1024 * 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
USER_AGENT = "Pocketful-editor/2.0 (+https://github.com/TronVonDoom/Pocketful-Catalog)"

STATIC = {
    "/": (APP / "index.html", "text/html; charset=utf-8"),
    "/app.js": (APP / "app.js", "text/javascript; charset=utf-8"),
    "/app.css": (APP / "app.css", "text/css; charset=utf-8"),
    "/icon.png": (HERE / "icon.png", "image/png"),
}


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- field rules

# What the page may write to each table, and how each value is read. Anything else in a
# request is refused rather than ignored, so a typo in the page cannot quietly lose an edit.
# IDs, locks, statuses and versions are never here: the database builds or guards them.
TEXT, REQUIRED, INT, BOOL, DATE, WORDS, JSON_LIST, JSON_OBJECT, ENUM = (
    "text", "required", "int", "bool", "date", "words", "json-list", "json-object", "enum")

SERIES_FIELDS = {"catalog_id": REQUIRED, "code": REQUIRED, "name": REQUIRED, "name_en": TEXT,
                 "sort": INT, "notes": TEXT}
SET_FIELDS = {"series_id": REQUIRED, "code": REQUIRED, "name": REQUIRED, "name_en": TEXT, "kind": REQUIRED,
              "release_date": DATE, "printed_total": INT, "abbreviation": TEXT, "sort": INT,
              "no_logo": BOOL, "no_symbol": BOOL, "tcgplayer_group": INT, "tcgplayer_via": TEXT, "notes": TEXT}
CARD_FIELDS = {"set_id": REQUIRED, "number": REQUIRED, "printed_number": TEXT, "number_assigned": BOOL,
               "section": TEXT, "sort": INT, "name": REQUIRED, "name_en": TEXT, "category": REQUIRED,
               "subtypes": WORDS, "hp": INT, "types": WORDS, "evolves_from": TEXT, "abilities": JSON_LIST,
               "attacks": JSON_LIST, "weaknesses": JSON_LIST, "resistances": JSON_LIST, "retreat": INT,
               "rules": WORDS, "flavor_text": TEXT, "illustrator": TEXT, "rarity": TEXT,
               "regulation_mark": TEXT, "dex_numbers": JSON_LIST, "no_image": BOOL, "same_as": TEXT,
               "review": REQUIRED, "review_note": TEXT, "withdrawn": BOOL, "notes": TEXT}
PRINTING_FIELDS = {"card_id": REQUIRED, "edition": TEXT, "pattern": TEXT, "finish": REQUIRED, "stamps": WORDS,
                   "error": TEXT, "tcgplayer_product": INT, "tcgplayer_printing": TEXT, "tcgplayer_via": TEXT,
                   "identify": TEXT, "review": REQUIRED, "review_note": TEXT, "withdrawn": BOOL, "notes": TEXT}
WORD_FIELDS = {"word": REQUIRED, "kind": REQUIRED, "label": REQUIRED, "description": TEXT, "sort": INT, "notes": TEXT}
TERM_FIELDS = {"kind": REQUIRED, "code": REQUIRED, "labels": JSON_OBJECT, "sort": INT, "notes": TEXT}

# Fields that are part of what a row is, and set only when it is made.
CREATE_ONLY = {"catalog_id", "set_id", "card_id", "word", "kind", "code"}
EDITABLE_AFTER = {"series": {"code"}, "sets": {"code", "kind"}, "terms": set(), "variant_words": set()}


def read_value(field: str, rule: str, value):
    if rule in (TEXT, REQUIRED, ENUM, DATE):
        if value is None:
            text = ""
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
        else:
            raise ApiError(400, f"{field} should be text")
        if rule == REQUIRED and not text:
            raise ApiError(400, f"{field} cannot be empty")
        if rule == DATE and text and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ApiError(400, f"{field} should be a date like 1999-01-09")
        return text or None
    if rule == INT:
        if value in (None, ""):
            return None
        try:
            return int(str(value).strip())
        except ValueError:
            raise ApiError(400, f"{field} should be a whole number") from None
    if rule == BOOL:
        if not isinstance(value, bool):
            raise ApiError(400, f"{field} should be true or false")
        return value
    if rule == WORDS:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ApiError(400, f"{field} should be a list of text")
        return [v.strip() for v in value if v.strip()]
    if rule == JSON_LIST:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ApiError(400, f"{field} should be a list")
        return value
    if rule == JSON_OBJECT:
        if not isinstance(value, dict):
            raise ApiError(400, f"{field} should be an object")
        return {k: v for k, v in value.items() if isinstance(v, str) and v.strip()}
    raise ApiError(500, f"no rule for {field}")


def read_fields(body: dict, rules: dict[str, str], creating: bool, table: str) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "expected an object")
    unknown = sorted(set(body) - set(rules))
    if unknown:
        raise ApiError(400, f"cannot write {', '.join(unknown)}")
    allowed_after = EDITABLE_AFTER.get(table, set())
    out = {}
    for field, value in body.items():
        if not creating and field in CREATE_ONLY and field not in allowed_after:
            raise ApiError(400, f"{field} is set when the record is made and cannot be changed here")
        out[field] = read_value(field, rules[field], value)
    if creating:
        missing = [f for f, r in rules.items() if r == REQUIRED and f not in out and f not in ("review", "kind")]
        if table in ("variant_words", "terms") and "kind" not in out:
            missing.append("kind")
        if missing:
            raise ApiError(400, f"missing {', '.join(missing)}")
    return out


def friendly(error: DbError) -> ApiError:
    """The database's refusal, with the two cryptic constraint messages put into words."""
    if error.code == "23503":
        if "is still referenced" in error.message:
            return ApiError(409, "Something still belongs to it. Delete or move what is inside it first.")
        return ApiError(400, f"That refers to something that does not exist ({error.message}).")
    if error.code == "23505":
        return ApiError(409, "Something with that ID already exists. " + error.message)
    return ApiError(400 if error.status < 500 else 502, error.explain())


# --------------------------------------------------------------------------- the API


class Api:
    def __init__(self, db: Db, store, client: tcgdex.Client, project: str, tcg: Tcgcsv | None = None):
        self.db = db
        self.store = store
        self.client = client
        self.project = project
        self.tcg = tcg or Tcgcsv()

    # -- helpers --------------------------------------------------------------------------

    def _row(self, table: str, row_id: str, what: str) -> dict:
        row = self.db.one(table, {"id": eq(row_id)})
        if not row:
            raise ApiError(404, f"No {what} \"{row_id}\".")
        return row

    def _images(self, column: str, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        return self.db.get_in("images", column, ids, {"order": "created_at.desc"})

    # -- overview -------------------------------------------------------------------------

    def ping(self, **_) -> dict:
        return {"app": APP_NAME}

    def bootstrap(self, **_) -> dict:
        catalogs = self.db.get("catalogs", {"order": "sort,id"})
        return {
            "project": {"database": self.project, "storage": self.store.kind, "publicUrl": self.store.public_url},
            "catalogs": catalogs,
            "series": self.db.get("series", {"order": "sort,code"}),
            "sets": self.db.get("sets", {"select": "id,series_id,code,name,kind,release_date,status,version,sort,locked",
                                         "order": "sort,release_date.nullslast,code"}),
            "summaries": self.db.get("set_summaries"),
            "words": self.db.get("variant_words", {"order": "kind,sort,word"}),
            "terms": self.db.get("terms", {"order": "kind,sort,code"}),
            "sources": self.db.get("sources", {"order": "id"}),
        }

    def catalog(self, id: str, **_) -> dict:
        catalog = self._row("catalogs", id, "catalog")
        return {"catalog": catalog, "images": self._images("catalog_id", [id])}

    def list_review(self, catalog: str = "", **_) -> dict:
        cards = self.db.get("cards", {"select": "id,set_id,number,name,review,review_note,locked",
                                      "review": "neq.reviewed", "withdrawn": "is.false",
                                      "set_id": f"like.{catalog}-*", "order": "set_id,sort.nullslast,number"})
        return {"cards": cards}

    def list_no_picture(self, catalog: str = "", **_) -> dict:
        published = [s["id"] for s in self.db.get("sets", {"select": "id", "version": "gt.0",
                                                            "id": f"like.{catalog}-*"})]
        cards = self.db.get_in("cards", "set_id", published, {
            "select": "id,set_id,number,name", "no_image": "is.true", "withdrawn": "is.false",
            "order": "set_id,sort.nullslast,number"}) if published else []
        return {"cards": cards}

    # -- series ---------------------------------------------------------------------------

    def series_detail(self, id: str, **_) -> dict:
        series = self._row("series", id, "series")
        sets = self.db.get("sets", {"series_id": eq(id), "order": "sort,release_date.nullslast,code"})
        summaries = self.db.get_in("set_summaries", "set_id", [s["id"] for s in sets]) if sets else []
        return {"series": series, "sets": sets, "summaries": summaries, "images": self._images("series_id", [id])}

    def series_create(self, body: dict, **_) -> dict:
        return self.db.insert("series", read_fields(body, SERIES_FIELDS, True, "series"))[0]

    def series_update(self, id: str, body: dict, **_) -> dict:
        rows = self.db.update("series", {"id": eq(id)}, read_fields(body, SERIES_FIELDS, False, "series"))
        if not rows:
            raise ApiError(404, f"No series \"{id}\".")
        return rows[0]

    def series_delete(self, id: str, **_) -> dict:
        if self.db.get("sets", {"series_id": eq(id), "select": "id", "limit": "1"}):
            raise ApiError(409, "This series still has sets. Delete or move them first.")
        self.db.delete("images", {"series_id": eq(id)})
        if not self.db.delete("series", {"id": eq(id)}):
            raise ApiError(404, f"No series \"{id}\".")
        return {"deleted": id}

    # -- sets -----------------------------------------------------------------------------

    def set_detail(self, id: str, **_) -> dict:
        the_set = self._row("sets", id, "set")
        series = self.db.one("series", {"id": eq(the_set["series_id"])})
        catalog = self.db.one("catalogs", {"id": eq(series["catalog_id"])})
        cards = self.db.get("cards", {"set_id": eq(id), "order": "sort.nullslast,number"})
        cards.sort(key=lambda c: (c["sort"] is None, c["sort"] or 0, publish.natural(c["number"])))
        card_ids = [c["id"] for c in cards]
        printings = self.db.get_in("printings", "card_id", card_ids) if card_ids else []
        images = self._images("set_id", [id]) + [
            i for i in self._images("card_id", card_ids) if i["chosen"]]
        record = tcgdex.set_import(self.db, the_set)
        return {
            "set": the_set, "series": series, "catalog": catalog, "cards": cards, "printings": printings,
            "images": images,
            "summary": self.db.one("set_summaries", {"set_id": eq(id)}),
            "import": {"source": record["source_id"], "key": record["key"], "fetched_at": record["fetched_at"],
                       "changed": record["changed"], "suggestion": tcgdex.suggest_set(record["data"])} if record else None,
            "publishes": self.db.get("publishes", {"set_id": eq(id), "order": "version.desc",
                                                   "select": "version,published_at,cards,printings,file"}),
        }

    def set_create(self, body: dict, **_) -> dict:
        fields = read_fields(body, SET_FIELDS, True, "sets")
        fields.setdefault("kind", "expansion")
        return self.db.insert("sets", fields)[0]

    def set_update(self, id: str, body: dict, **_) -> dict:
        rows = self.db.update("sets", {"id": eq(id)}, read_fields(body, SET_FIELDS, False, "sets"))
        if not rows:
            raise ApiError(404, f"No set \"{id}\".")
        return rows[0]

    def set_delete(self, id: str, **_) -> dict:
        the_set = self._row("sets", id, "set")
        if the_set["locked"]:
            raise ApiError(409, "This set has been published, so it cannot be deleted. Withdraw its cards instead.")
        card_ids = [c["id"] for c in self.db.get("cards", {"set_id": eq(id), "select": "id"})]
        if card_ids:
            printing_ids = [p["id"] for p in self.db.get_in("printings", "card_id", card_ids, {"select": "id"})]
            for i in range(0, len(printing_ids), 80):
                self.db.delete("images", {"printing_id": in_list(printing_ids[i:i + 80])})
            for i in range(0, len(card_ids), 80):
                self.db.delete("images", {"card_id": in_list(card_ids[i:i + 80])})
        self.db.delete("images", {"set_id": eq(id)})
        self.db.update("source_records", {"matched": eq(id), "kind": eq("set")}, {"matched": None, "matched_hash": None})
        self.db.delete("sets", {"id": eq(id)})
        return {"deleted": id}

    def set_problems(self, id: str, **_) -> dict:
        return {"problems": self.db.rpc("publish_problems", {"target": id}) or []}

    def set_publishes(self, id: str, **_) -> dict:
        return {"publishes": self.db.get("publishes", {"set_id": eq(id), "order": "version.desc"})}

    def set_publish(self, id: str, **_) -> dict:
        try:
            return publish.publish_set(self.db, self.store, id)
        except publish.PublishRefused as refused:
            raise ApiError(409, str(refused), problems=refused.problems) from None

    def index_publish(self, **_) -> dict:
        return publish.publish_index(self.db, self.store)

    # -- importing ------------------------------------------------------------------------

    def tcgdex_sets(self, catalog: str = "", **_) -> dict:
        row = self._row("catalogs", catalog, "catalog")
        if row["language"] not in tcgdex.LANGUAGES:
            raise ApiError(400, f"TCGdex has no {row['name']} catalog.")
        try:
            sets = self.client.sets(row["language"])
        except tcgdex.SourceError as e:
            raise ApiError(502, str(e)) from None
        return {"sets": [{"id": s.get("id"), "name": s.get("name"),
                          "cards": (s.get("cardCount") or {}).get("total")} for s in sets]}

    def set_candidates(self, id: str, **_) -> dict:
        return tcgdex.candidates(self.db, self._row("sets", id, "set"))

    def set_import(self, id: str, body: dict, **_) -> dict:
        the_set = self._row("sets", id, "set")
        if body.get("source") != "tcgdex":
            raise ApiError(400, "The only source that can be imported from so far is TCGdex.")
        key = str(body.get("key") or "").strip()
        if not key:
            raise ApiError(400, "Which TCGdex set? Give its TCGdex ID, like base1.")
        series = self.db.one("series", {"id": eq(the_set["series_id"])})
        catalog = self.db.one("catalogs", {"id": eq(series["catalog_id"])})
        try:
            result = tcgdex.import_set(self.db, self.client, the_set, catalog, key)
        except tcgdex.SourceError as e:
            raise ApiError(502, str(e)) from None
        return {"imported": result, **tcgdex.candidates(self.db, the_set)}

    def set_accept(self, id: str, body: dict, **_) -> dict:
        the_set = self._row("sets", id, "set")
        keys = body.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ApiError(400, "Choose at least one card to accept.")
        try:
            result = tcgdex.accept(self.db, the_set, keys)
        except tcgdex.SourceError as e:
            raise ApiError(400, str(e)) from None
        return {"accepted": result, **tcgdex.candidates(self.db, the_set)}

    # -- TCGplayer -------------------------------------------------------------------------

    def _language(self, the_set: dict) -> str:
        series = self.db.one("series", {"id": eq(the_set["series_id"])})
        return self.db.one("catalogs", {"id": eq(series["catalog_id"])})["language"]

    def set_tcgplayer(self, id: str, suggest: str = "", **_) -> dict:
        the_set = self._row("sets", id, "set")
        out = {"group": {"groupId": the_set["tcgplayer_group"], "via": the_set["tcgplayer_via"]}
               if the_set["tcgplayer_group"] else None}
        if suggest:
            try:
                out["suggestions"] = tcgplayer_links.suggest_groups(self.tcg, self.db, the_set, self._language(the_set))
            except TcgcsvError as e:
                raise ApiError(502, str(e)) from None
        return out

    def set_tcgplayer_link(self, id: str, body: dict, **_) -> dict:
        the_set = self._row("sets", id, "set")
        group_id = read_value("group_id", INT, body.get("group_id"))
        if not group_id:
            raise ApiError(400, "Which TCGplayer group? Give its group ID.")
        try:
            return tcgplayer_links.link_set(self.tcg, self.db, the_set, self._language(the_set), group_id,
                                            by_hand=bool(body.get("by_hand")))
        except TcgcsvError as e:
            raise ApiError(502, str(e)) from None
        except ValueError as e:
            raise ApiError(400, str(e)) from None

    # -- cards ----------------------------------------------------------------------------

    def card_detail(self, id: str, **_) -> dict:
        card = self._row("cards", id, "card")
        the_set = self.db.one("sets", {"id": eq(card["set_id"])})
        printings = self.db.get("printings", {"card_id": eq(id), "order": "created_at"})
        images = self._images("card_id", [id]) + self._images("printing_id", [p["id"] for p in printings])
        order = self.db.get("cards", {"set_id": eq(card["set_id"]), "select": "id,number,sort"})
        order.sort(key=lambda c: (c["sort"] is None, c["sort"] or 0, publish.natural(c["number"])))
        ids = [c["id"] for c in order]
        at = ids.index(id)
        return {
            "card": card, "set": the_set, "printings": printings, "images": images,
            "sources": tcgdex.card_sources(self.db, card, the_set),
            "previous": ids[at - 1] if at > 0 else None,
            "next": ids[at + 1] if at + 1 < len(ids) else None,
            "position": at + 1, "count": len(ids),
        }

    def card_create(self, body: dict, **_) -> dict:
        fields = read_fields(body, CARD_FIELDS, True, "cards")
        return self.db.insert("cards", self._review_stamp(fields))[0]

    def card_update(self, id: str, body: dict, **_) -> dict:
        fields = self._review_stamp(read_fields(body, CARD_FIELDS, False, "cards"))
        rows = self.db.update("cards", {"id": eq(id)}, fields)
        if not rows:
            raise ApiError(404, f"No card \"{id}\".")
        if fields.get("review") == "reviewed":
            tcgdex.mark_reviewed(self.db, rows[0]["id"])
        return rows[0]

    def card_delete(self, id: str, **_) -> dict:
        card = self._row("cards", id, "card")
        if card["locked"]:
            raise ApiError(409, "This card has been published, so it cannot be deleted. Withdraw it instead.")
        printing_ids = [p["id"] for p in self.db.get("printings", {"card_id": eq(id), "select": "id"})]
        if printing_ids:
            self.db.delete("images", {"printing_id": in_list(printing_ids)})
        self.db.delete("images", {"card_id": eq(id)})
        self.db.update("source_records", {"matched": eq(id)}, {"matched": None, "matched_hash": None})
        self.db.delete("cards", {"id": eq(id)})
        return {"deleted": id}

    @staticmethod
    def _review_stamp(fields: dict) -> dict:
        if "review" in fields:
            fields["reviewed_at"] = now() if fields["review"] == "reviewed" else None
            if fields["review"] != "flagged" and "review_note" not in fields:
                fields["review_note"] = None
        return fields

    # -- printings ------------------------------------------------------------------------

    def printing_create(self, body: dict, **_) -> dict:
        return self.db.insert("printings", self._review_stamp(read_fields(body, PRINTING_FIELDS, True, "printings")))[0]

    def printing_update(self, id: str, body: dict, **_) -> dict:
        rows = self.db.update("printings", {"id": eq(id)},
                              self._review_stamp(read_fields(body, PRINTING_FIELDS, False, "printings")))
        if not rows:
            raise ApiError(404, f"No printing \"{id}\".")
        return rows[0]

    def printing_delete(self, id: str, **_) -> dict:
        printing = self._row("printings", id, "printing")
        if printing["locked"]:
            raise ApiError(409, "This printing has been published, so it cannot be deleted. Withdraw it instead.")
        self.db.delete("images", {"printing_id": eq(id)})
        self.db.delete("printings", {"id": eq(id)})
        return {"deleted": id}

    # -- pictures -------------------------------------------------------------------------

    SUBJECTS = {"catalog": ("catalogs", "catalog_id"), "series": ("series", "series_id"),
                "set": ("sets", "set_id"), "card": ("cards", "card_id"), "printing": ("printings", "printing_id")}

    def image_create(self, body: dict, **_) -> dict:
        kind = body.get("subject_kind")
        subject_id = str(body.get("subject_id") or "")
        role = body.get("role")
        if kind not in self.SUBJECTS:
            raise ApiError(400, "A picture belongs to a catalog, series, set, card or printing.")
        if role not in media.MAX_SIZE:
            raise ApiError(400, "A picture is a front, back, logo or symbol.")
        table, column = self.SUBJECTS[kind]
        subject = self._row(table, subject_id, kind)

        set_id = None
        if kind == "card":
            set_id = subject["set_id"]
        elif kind == "printing":
            set_id = self._row("cards", subject["card_id"], "card")["set_id"]

        try:
            data = base64.b64decode(body.get("image") or "", validate=True)
            width, height = media.check(data, media.MAX_SIZE[role], "The picture")
            thumb = base64.b64decode(body.get("thumb") or "", validate=True) if body.get("thumb") else None
            if role in ("front", "back") and not thumb:
                raise media.MediaError("a card picture needs its thumbnail")
            if thumb:
                media.check(thumb, media.THUMB_SIZE, "The thumbnail")
            original = base64.b64decode(body.get("original") or "", validate=True) if body.get("original") else None
            if original and len(original) > media.MAX_ORIGINAL_BYTES:
                raise media.MediaError("the original is larger than 20 MB")
        except (ValueError, media.MediaError) as e:
            raise ApiError(400, f"That picture cannot be used: {e}") from None

        digest = media.sha256(data)
        key = media.picture_key(role, kind, subject_id, set_id, digest)
        source_id = body.get("source_id") or "upload"
        source_url = body.get("source_url") or None

        # Files first, then the database: if an upload fails, the picture already chosen stays chosen.
        existing = self.db.one("images", {"path": eq(key)})
        if existing:
            self.db.update("images", {column: eq(subject_id), "role": eq(role), "chosen": "is.true"}, {"chosen": False})
            row = self.db.update("images", {"id": eq(existing["id"])}, {"chosen": True})[0]
        else:
            original_path = None
            original_type = str(body.get("original_type") or "")
            if original and not source_url:
                try:
                    original_path = media.original_key(media.sha256(original), original_type)
                except media.MediaError as e:
                    raise ApiError(400, str(e)) from None
            self.store.put_public(key, data, "image/webp", publish.IMMUTABLE)
            thumb_path = None
            if thumb:
                thumb_path = media.thumb_key(key)
                self.store.put_public(thumb_path, thumb, "image/webp", publish.IMMUTABLE)
            if original_path:
                self.store.put_private(original_path, original, original_type)
            self.db.update("images", {column: eq(subject_id), "role": eq(role), "chosen": "is.true"}, {"chosen": False})
            row = self.db.insert("images", {
                column: subject_id, "role": role, "chosen": True, "path": key, "thumb_path": thumb_path,
                "width": width, "height": height, "bytes": len(data), "sha256": digest,
                "below_standard": media.is_soft(role, width, height), "source_id": source_id,
                "source_url": source_url, "original_path": original_path,
            })[0]
        if kind == "card" and subject.get("no_image"):
            self.db.update("cards", {"id": eq(subject_id)}, {"no_image": False})
        return row

    def image_update(self, id: str, body: dict, **_) -> dict:
        image = self._row("images", id, "picture")
        if set(body) - {"chosen", "notes"}:
            raise ApiError(400, "Only whether a picture is chosen, and its notes, can change.")
        patch = {}
        if "notes" in body:
            patch["notes"] = read_value("notes", TEXT, body["notes"])
        if "chosen" in body:
            chosen = read_value("chosen", BOOL, body["chosen"])
            if chosen:
                if not image["path"]:
                    raise ApiError(400, "That picture was never stored, so it cannot be chosen.")
                column = next(c for _, c in self.SUBJECTS.values() if image.get(c))
                self.db.update("images", {column: eq(image[column]), "role": eq(image["role"]),
                                          "chosen": "is.true"}, {"chosen": False})
            patch["chosen"] = chosen
        return self.db.update("images", {"id": eq(id)}, patch)[0] if patch else image

    def image_delete(self, id: str, **_) -> dict:
        if not self.db.delete("images", {"id": eq(id)}):
            raise ApiError(404, f"No picture \"{id}\".")
        return {"deleted": id}

    def fetch_image(self, body: dict, **_) -> tuple[bytes, str]:
        url = str(body.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise ApiError(400, "Give a picture's web address, starting with http:// or https://.")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
            with urllib.request.urlopen(request, timeout=30) as response:
                kind = response.headers.get_content_type()
                data = response.read(MAX_FETCH + 1)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise ApiError(502, f"Could not get that picture: {getattr(e, 'reason', e)}") from None
        if len(data) > MAX_FETCH:
            raise ApiError(400, "That picture is larger than 15 MB.")
        if not kind.startswith("image/"):
            raise ApiError(400, f"That address is not a picture (it is {kind}).")
        return data, kind

    # -- vocabulary -----------------------------------------------------------------------

    def word_create(self, body: dict, **_) -> dict:
        return self.db.insert("variant_words", read_fields(body, WORD_FIELDS, True, "variant_words"))[0]

    def word_update(self, id: str, body: dict, **_) -> dict:
        rows = self.db.update("variant_words", {"word": eq(id)}, read_fields(body, WORD_FIELDS, False, "variant_words"))
        if not rows:
            raise ApiError(404, f"No word \"{id}\".")
        return rows[0]

    def word_delete(self, id: str, **_) -> dict:
        if not self.db.delete("variant_words", {"word": eq(id)}):
            raise ApiError(404, f"No word \"{id}\".")
        return {"deleted": id}

    def term_create(self, body: dict, **_) -> dict:
        return self.db.insert("terms", read_fields(body, TERM_FIELDS, True, "terms"))[0]

    def term_update(self, kind: str, code: str, body: dict, **_) -> dict:
        rows = self.db.update("terms", {"kind": eq(kind), "code": eq(code)}, read_fields(body, TERM_FIELDS, False, "terms"))
        if not rows:
            raise ApiError(404, f"No {kind} \"{code}\".")
        return rows[0]

    def term_delete(self, kind: str, code: str, **_) -> dict:
        if not self.db.delete("terms", {"kind": eq(kind), "code": eq(code)}):
            raise ApiError(404, f"No {kind} \"{code}\".")
        return {"deleted": f"{kind}:{code}"}


SEGMENT = r"(?P<{}>[^/]+)"
ROUTES = [(method, re.compile("^" + pattern.format(id=SEGMENT.format("id"), kind=SEGMENT.format("kind"),
                                                   code=SEGMENT.format("code")) + "$"), name)
          for method, pattern, name in [
    ("GET", "/api/ping", "ping"),
    ("GET", "/api/bootstrap", "bootstrap"),
    ("GET", "/api/catalogs/{id}", "catalog"),
    ("GET", "/api/lists/review", "list_review"),
    ("GET", "/api/lists/no-picture", "list_no_picture"),
    ("GET", "/api/series/{id}", "series_detail"),
    ("POST", "/api/series", "series_create"),
    ("PATCH", "/api/series/{id}", "series_update"),
    ("DELETE", "/api/series/{id}", "series_delete"),
    ("GET", "/api/sets/{id}", "set_detail"),
    ("POST", "/api/sets", "set_create"),
    ("PATCH", "/api/sets/{id}", "set_update"),
    ("DELETE", "/api/sets/{id}", "set_delete"),
    ("GET", "/api/sets/{id}/problems", "set_problems"),
    ("GET", "/api/sets/{id}/publishes", "set_publishes"),
    ("POST", "/api/sets/{id}/publish", "set_publish"),
    ("GET", "/api/sets/{id}/candidates", "set_candidates"),
    ("POST", "/api/sets/{id}/import", "set_import"),
    ("POST", "/api/sets/{id}/accept", "set_accept"),
    ("GET", "/api/sets/{id}/tcgplayer", "set_tcgplayer"),
    ("POST", "/api/sets/{id}/tcgplayer", "set_tcgplayer_link"),
    ("POST", "/api/publish-index", "index_publish"),
    ("GET", "/api/tcgdex/sets", "tcgdex_sets"),
    ("GET", "/api/cards/{id}", "card_detail"),
    ("POST", "/api/cards", "card_create"),
    ("PATCH", "/api/cards/{id}", "card_update"),
    ("DELETE", "/api/cards/{id}", "card_delete"),
    ("POST", "/api/printings", "printing_create"),
    ("PATCH", "/api/printings/{id}", "printing_update"),
    ("DELETE", "/api/printings/{id}", "printing_delete"),
    ("POST", "/api/images", "image_create"),
    ("PATCH", "/api/images/{id}", "image_update"),
    ("DELETE", "/api/images/{id}", "image_delete"),
    ("POST", "/api/fetch-image", "fetch_image"),
    ("POST", "/api/words", "word_create"),
    ("PATCH", "/api/words/{id}", "word_update"),
    ("DELETE", "/api/words/{id}", "word_delete"),
    ("POST", "/api/terms", "term_create"),
    ("PATCH", "/api/terms/{kind}/{code}", "term_update"),
    ("DELETE", "/api/terms/{kind}/{code}", "term_delete"),
]]


# --------------------------------------------------------------------------- app window


class Presence:
    """
    Knows whether any editor window is still open, so the app can exit with its window.

    Under --app the server runs without a console, so there is nothing to Ctrl-C and a server
    left behind would sit invisibly on the port until the next reboot. Each page says hello
    every half minute and goodbye as it closes. A goodbye starts a short grace period, because
    a reload is a goodbye followed a moment later by a hello.
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
    """Edge's app mode where it exists: a window with no tabs or address bar."""
    if app:
        for exe in [
            shutil.which("msedge"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            shutil.which("chrome"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]:
            if exe and os.path.isfile(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,960"],
                                 creationflags=NO_WINDOW, close_fds=True)
                return
    webbrowser.open(url)


# --------------------------------------------------------------------------- HTTP


class Handler(http.server.BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, fmt, *args) -> None:
        line = fmt % args
        if "/api/hello" in line or " /api/bootstrap" in line:
            return
        sys.stderr.write(f"{self.log_date_time_string()} {line}\n")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _trusted(self) -> bool:
        """
        Whether a request really came from this editor's own page.

        The server is bound to loopback, which keeps the network out but not the browser: any
        web page open in the same browser can aim a request at 127.0.0.1. Checking Host
        defeats DNS rebinding, and checking Origin stops a cross-site request -- and this
        server writes to the catalog database.
        """
        port = self.server.server_address[1]
        own = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in own:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in own}

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._route(method)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ApiError as e:
            self._json({"error": e.message, **e.extra}, e.status)
        except DbError as e:
            error = friendly(e)
            self._json({"error": error.message}, error.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._json({"error": f"The editor hit a problem: {e}"}, 500)
            except OSError:
                pass

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if not self._trusted():
            raise ApiError(403, "Requests must come from the editor's own page.")

        if method == "GET" and path in STATIC:
            file, kind = STATIC[path]
            self._send(200, file.read_bytes(), kind)
            return
        if method == "GET" and path.startswith("/_store/") and isinstance(self.server.api.store, LocalStore):
            key = urllib.parse.unquote(path[len("/_store/"):])
            try:
                self._send(200, self.server.api.store.get_public(key),
                           "image/webp" if key.endswith(".webp") else "application/octet-stream")
            except (OSError, ValueError):
                self._send(404, b"", "text/plain")
            return
        if path == "/api/hello":
            PRESENCE.hello(query.get("c", "?"))
            self._json({"ok": True})
            return
        if path == "/api/bye" and method == "POST":
            PRESENCE.bye(query.get("c", "?"))
            self._json({"ok": True})
            return

        for route_method, pattern, name in ROUTES:
            match = pattern.match(path)
            if not match or route_method != method:
                continue
            args = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
            if method in ("POST", "PATCH"):
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    raise ApiError(415, "Writes must be JSON.")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ApiError(413, "That request is too large.")
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    args["body"] = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    raise ApiError(400, "That request is not valid JSON.") from None
            result = getattr(self.server.api, name)(**args, **{k: v for k, v in query.items() if k not in args})
            if isinstance(result, tuple):
                data, kind = result
                self._send(200, data, kind)
            else:
                self._json(result, 201 if method == "POST" and name.endswith("_create") else 200)
            return
        if any(p.match(path) for _, p, _ in ROUTES):
            raise ApiError(405, f"{method} is not allowed here.")
        raise ApiError(404, "Nothing here.")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Off on Windows, where SO_REUSEADDR lets two servers bind one port and take turns.
    allow_reuse_address = os.name != "nt"
    api: Api


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_NAME
    except (OSError, ValueError, urllib.error.URLError):
        return False


def make_api(port: int) -> Api:
    rest_url = os.environ.get("POCKETFUL_REST_URL")
    if rest_url:
        db = Db(rest_url, os.environ.get("POCKETFUL_REST_KEY", ""))
        project = "local"
    else:
        import supabase_config
        config = supabase_config.load()
        db = Db(config.url + "/rest/v1", config.secret_key)
        project = config.ref

    store_spec = os.environ.get("POCKETFUL_STORE", "")
    if store_spec.startswith("local:"):
        store = LocalStore(Path(store_spec[len("local:"):]), f"http://127.0.0.1:{port}/_store")
    else:
        store = R2Store()

    fixtures = os.environ.get("POCKETFUL_TCGDEX_FIXTURES")
    tcgcsv_fixtures = os.environ.get("POCKETFUL_TCGCSV_FIXTURES")
    return Api(db, store, tcgdex.Client(Path(fixtures) if fixtures else None), project,
               Tcgcsv(fixtures=Path(tcgcsv_fixtures)) if tcgcsv_fixtures else Tcgcsv())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--app", action="store_true", help="open in its own window and exit when it is closed")
    args = ap.parse_args()

    if args.app and (sys.stdout is None or not sys.stdout.isatty()):
        # Under pythonw there is no console, and http.server logs to stderr, which would be
        # None. A log file is also the only way anyone finds out why the window never appeared.
        stream = open(LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        print(f"\n--- {now()} starting")

    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):
        print(f"Already running at {url}" + ("." if args.no_open else "; opening a window onto it."))
        if not args.no_open:
            open_window(url, args.app)
        return

    api = make_api(args.port)
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        httpd.api = api
        print(f"Pocketful Editor: {url}  (database {api.project}, files on {api.store.kind})", flush=True)
        if args.app:
            threading.Thread(target=PRESENCE.watch, args=(httpd,), daemon=True).start()
        else:
            print("Ctrl-C to stop.", flush=True)
        if not args.no_open:
            threading.Timer(0.4, lambda: open_window(url, args.app)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
