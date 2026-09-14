"""
Publishing a set: the only way anything reaches the app.

1. Ask the database what stands in the way (`publish_problems`). Anything at all, and the
   publish stops there with the list.
2. Build the set file from the database: every card and printing that is not withdrawn,
   with the paths of their chosen pictures. Pictures are already in storage -- they went
   there when they were chosen -- so nothing but the file is uploaded.
3. Upload it under its version number. A version is never overwritten, so an app that has
   version 2 keeps a file that says exactly what version 2 said.
4. `record_publish`, which checks everything again inside the database, locks what went
   out and bumps the version.
5. Rewrite `catalog/index.json`, the one file the app reads first.

If the index upload fails after the database has recorded the publish, the set is
published and the index is behind. Publishing the index again fixes that, and every
publish rewrites it anyway.
"""

from __future__ import annotations

import datetime
import gzip
import json
import re

from . import media
from .db import Db, eq, together

SCHEMA = 2
IMMUTABLE = "public, max-age=31536000, immutable"
FRESH = "public, max-age=60, must-revalidate"


class PublishRefused(Exception):
    def __init__(self, problems: list[dict]):
        super().__init__(f"{len(problems)} thing(s) stand in the way of publishing")
        self.problems = problems


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def natural(number: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.findall(r"\d+|\D+", number or ""))


def compact(value):
    """Drops empty values so the file stays small, keeping zero and false."""
    if isinstance(value, dict):
        cleaned = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def chosen_images(db: Db, column: str, ids: list[str]) -> dict[tuple[str, str], dict]:
    if not ids:
        return {}
    rows = db.get_in("images", column, ids, {"chosen": "is.true"})
    return {(row[column], row["role"]): row for row in rows}


def build_set_document(db: Db, the_set: dict, version: int) -> dict:
    set_id = eq(the_set["id"])
    cards, printings, words, set_images, card_images, printing_images = together(
        lambda: db.get("cards", {"set_id": set_id, "withdrawn": "is.false"}),
        lambda: db.get("printings", {"select": "*,cards!inner()", "cards.set_id": set_id,
                                     "cards.withdrawn": "is.false", "withdrawn": "is.false"}),
        lambda: {w["word"]: w for w in db.get("variant_words")},
        lambda: chosen_images(db, "set_id", [the_set["id"]]),
        lambda: {(row["card_id"], row["role"]): row for row in db.get(
            "images", {"select": "*,cards!inner()", "cards.set_id": set_id, "chosen": "is.true"})},
        lambda: {(row["printing_id"], row["role"]): row for row in db.get(
            "images", {"select": "*,printings!inner(cards!inner())", "printings.cards.set_id": set_id, "chosen": "is.true"})},
    )
    cards.sort(key=lambda c: (c["sort"] is None, c["sort"] or 0, natural(c["number"])))

    by_card: dict[str, list[dict]] = {}
    for p in printings:
        by_card.setdefault(p["card_id"], []).append(p)

    def word_sort(word: str | None) -> int:
        return (words.get(word) or {}).get("sort", 0) if word else 0

    # Plainest first, the order the app lists a card's printings in: unlimited before other
    # editions (in the word list's order), plain before patterned or stamped, then by finish.
    def printing_order(p: dict) -> tuple:
        extras = (1 if p["pattern"] else 0) + len(p["stamps"] or []) + (1 if p["error"] else 0)
        return (p["edition"] is not None, word_sort(p["edition"]), extras, word_sort(p["finish"]), p["variant"])

    def image_fields(image: dict | None) -> dict:
        return {"image": image["path"], "thumb": image.get("thumb_path")} if image else {}

    doc_cards = []
    for c in cards:
        doc_cards.append({
            "id": c["id"],
            "number": c["number"],
            "printedNumber": c["printed_number"],
            "numberAssigned": c["number_assigned"] or None,
            "section": c["section"],
            "name": c["name"],
            "nameEn": c["name_en"],
            "category": c["category"],
            "subtypes": c["subtypes"],
            "hp": c["hp"],
            "types": c["types"],
            "evolvesFrom": c["evolves_from"],
            "retreat": c["retreat"],
            "rarity": c["rarity"],
            "regulationMark": c["regulation_mark"],
            "illustrator": c["illustrator"],
            "dexNumbers": c["dex_numbers"],
            "flavorText": c["flavor_text"],
            **image_fields(card_images.get((c["id"], "front"))),
            "printings": [
                {
                    "id": p["id"],
                    "variant": p["variant"],
                    "edition": p["edition"],
                    "pattern": p["pattern"],
                    "finish": p["finish"],
                    "stamps": p["stamps"],
                    "error": p["error"],
                    "identify": p["identify"],
                    **image_fields(printing_images.get((p["id"], "front"))),
                }
                for p in sorted(by_card.get(c["id"], []), key=printing_order)
            ],
        })

    logo = set_images.get((the_set["id"], "logo"))
    symbol = set_images.get((the_set["id"], "symbol"))
    return compact({
        "schema": SCHEMA,
        "id": the_set["id"],
        "version": version,
        "publishedAt": now(),
        "series": the_set["series_id"],
        "code": the_set["code"],
        "name": the_set["name"],
        "nameEn": the_set["name_en"],
        "kind": the_set["kind"],
        "releaseDate": the_set["release_date"],
        "printedTotal": the_set["printed_total"],
        "abbreviation": the_set["abbreviation"],
        "logo": logo["path"] if logo else None,
        "symbol": symbol["path"] if symbol else None,
        "cards": doc_cards,
    })


def index_entry(doc: dict, key: str, digest: str) -> dict:
    return compact({
        "id": doc["id"], "code": doc["code"], "name": doc["name"], "nameEn": doc.get("nameEn"),
        "kind": doc["kind"], "releaseDate": doc.get("releaseDate"), "printedTotal": doc.get("printedTotal"),
        "abbreviation": doc.get("abbreviation"), "logo": doc.get("logo"), "symbol": doc.get("symbol"),
        "version": doc["version"], "publishedAt": doc["publishedAt"], "file": key, "sha256": digest,
        "cards": len(doc["cards"]), "printings": sum(len(c.get("printings", [])) for c in doc["cards"]),
    })


def publish_set(db: Db, store, set_id: str) -> dict:
    the_set = db.one("sets", {"id": eq(set_id)})
    if not the_set:
        raise PublishRefused([{"subject": set_id, "problem": "no such set"}])
    problems = db.rpc("publish_problems", {"target": set_id}) or []
    if problems:
        raise PublishRefused(problems)

    version = the_set["version"] + 1
    doc = build_set_document(db, the_set, version)
    body = gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    digest = media.sha256(body)
    key = f"catalog/sets/{set_id}.v{version}.json.gz"
    store.put_public(key, body, "application/gzip", IMMUTABLE)

    record = db.rpc("record_publish", {"target": set_id, "next_version": version,
                                        "file_path": key, "file_sha256": digest})
    entry = index_entry(doc, key, digest)
    db.update("publishes", {"set_id": eq(set_id), "version": eq(str(version))}, {"summary": entry})
    index = publish_index(db, store)
    return {"publish": record, "file": key, "cards": entry["cards"], "printings": entry["printings"],
            "index": index}


def build_index(db: Db, public_url: str) -> dict:
    catalogs, series, sets, word_rows, term_rows = together(
        lambda: sorted(db.get("catalogs"), key=lambda c: (c["sort"], c["id"])),
        lambda: db.get("series", {"status": eq("published")}),
        lambda: db.get("sets", {"version": "gt.0"}),
        lambda: db.get("variant_words"),
        lambda: db.get("terms"))
    publish_rows, catalog_images, series_images, set_images = together(
        lambda: db.get_in("publishes", "set_id", [s["id"] for s in sets]) if sets else [],
        lambda: chosen_images(db, "catalog_id", [c["id"] for c in catalogs]),
        lambda: chosen_images(db, "series_id", [s["id"] for s in series]),
        lambda: chosen_images(db, "set_id", [s["id"] for s in sets]))
    publishes = {(p["set_id"], p["version"]): p for p in publish_rows}

    def path(images: dict, subject: str, role: str) -> str | None:
        image = images.get((subject, role))
        return image["path"] if image else None

    sets_by_series: dict[str, list[dict]] = {}
    for s in sets:
        record = publishes.get((s["id"], s["version"]))
        if not record:
            continue
        entry = record.get("summary") or compact({
            "id": s["id"], "code": s["code"], "name": s["name"], "nameEn": s["name_en"], "kind": s["kind"],
            "releaseDate": s["release_date"], "printedTotal": s["printed_total"], "abbreviation": s["abbreviation"],
            "logo": path(set_images, s["id"], "logo"), "symbol": path(set_images, s["id"], "symbol"),
            "version": s["version"], "publishedAt": record["published_at"], "file": record["file"],
            "sha256": record["sha256"], "cards": record["cards"], "printings": record["printings"],
        })
        sets_by_series.setdefault(s["series_id"], []).append({**entry, "sort": s["sort"]})

    out_catalogs = []
    for c in catalogs:
        out_series = []
        for s in sorted((s for s in series if s["catalog_id"] == c["id"]), key=lambda s: (s["sort"], s["code"])):
            entries = sorted(sets_by_series.get(s["id"], []), key=lambda e: (e["sort"], e.get("releaseDate") or "", e["code"]))
            if not entries:
                continue
            out_series.append(compact({
                "id": s["id"], "code": s["code"], "name": s["name"], "nameEn": s["name_en"],
                "logo": path(series_images, s["id"], "logo"), "back": path(series_images, s["id"], "back"),
                "sets": [{k: v for k, v in e.items() if k != "sort"} for e in entries],
            }))
        if out_series:
            out_catalogs.append(compact({
                "id": c["id"], "game": c["game_id"], "language": c["language"], "name": c["name"],
                "nativeName": c["native_name"], "back": path(catalog_images, c["id"], "back"),
                "series": out_series,
            }))

    words = {w["word"]: {"kind": w["kind"], "label": w["label"], "sort": w["sort"]} for w in word_rows}
    terms: dict[str, dict] = {}
    for t in term_rows:
        terms.setdefault(t["kind"], {})[t["code"]] = t["labels"]
    return {"schema": SCHEMA, "generatedAt": now(), "publicUrl": public_url,
            "catalogs": out_catalogs, "words": words, "terms": terms}


def publish_index(db: Db, store) -> dict:
    index = build_index(db, store.public_url)
    body = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    store.put_public("catalog/index.json", body, "application/json", FRESH)
    return {"file": "catalog/index.json", "sets": sum(len(s["sets"]) for c in index["catalogs"] for s in c["series"])}
