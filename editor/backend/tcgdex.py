"""
Importing one set from TCGdex, only ever when you start it.

Nothing here runs on its own. An import is one TCGdex set, fetched because you pressed the
button for it, and what comes back is stored untouched in `source_records` as TCGdex's
word on the matter. It becomes a card only when you accept it, and every card it becomes
starts unreviewed.

Turning TCGdex's shape into the catalog's is the other half of this module: its "Stage2"
into the `stage-2` term, its shadowless holo with a 1st Edition stamp into the
`1st-edition-holo` printing. Whatever does not map -- a rarity the terms do not have yet, a
stamp with no word -- is not guessed at. It is left out and reported, so it is fixed in the
word lists once and every later import agrees.

Being a good guest: English card documents come over GraphQL, forty to a request, so a
200-card set is five requests rather than two hundred. TCGdex's GraphQL matches card IDs
loosely (asked for xy8-146 it can answer with xy8-146a), so every answer is checked against
what was asked for and anything wrong is fetched again over REST. See tools/pull_catalog.py,
where both lessons were learned.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .db import Db, chunked, each, eq, together

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from variants import common_stamps  # noqa: E402

BASE = "https://api.tcgdex.net/v2"
USER_AGENT = "Pocketful-editor/2.0 (+https://github.com/TronVonDoom/Pocketful-Catalog)"
SOURCE = "tcgdex"

# The catalog's language codes, and TCGdex's for the same language.
LANGUAGES = {"en": "en", "jp": "ja", "cht": "zh-tw", "chs": "zh-cn"}

BATCH = 40
WORKERS = 4
LITERAL_BREAKERS = frozenset(['\t', '\n', '\r', '"', '\\'])

CARD_SELECTION = """
    id localId name rarity illustrator category image hp types stage evolveFrom
    description retreat suffix regulationMark dexId trainerType energyType effect level
    variants { normal holo reverse firstEdition wPromo }
    variants_detailed { type subtype size stamp foil }
    attacks { name cost damage effect }
    abilities { type name effect }
    weaknesses { type value }
    resistances { type value }
"""
REST_KEYS = (
    "id", "localId", "name", "rarity", "illustrator", "category", "image", "hp", "types",
    "stage", "evolveFrom", "description", "retreat", "suffix", "regulationMark", "dexId",
    "trainerType", "energyType", "effect", "level", "variants", "attacks", "abilities",
    "weaknesses", "resistances",
)

CATEGORIES = {"pokemon": "pokemon", "trainer": "trainer", "energy": "energy"}
STAGES = {
    "basic": "basic", "stage1": "stage-1", "stage2": "stage-2", "baby": "baby",
    "restored": "restored", "level-up": "level-up", "break": "break", "mega": "mega",
    "vmax": "vmax", "vstar": "vstar", "v-union": "v-union",
}
TRAINER_TYPES = {
    "supporter": "supporter", "item": "item", "tool": "pokemon-tool", "stadium": "stadium",
    "rocket's secret machine": "rockets-secret-machine", "technical machine": "technical-machine",
}
ENERGY_TYPES = {"normal": "basic-energy", "special": "special-energy"}
# Case matters here: "ex" and "EX" are different mechanics a decade apart.
SUFFIXES = {
    "ex": "ex", "EX": "ex-uppercase", "GX": "gx", "TAG TEAM-GX": "tag-team-gx", "V": "v",
    "SP": "sp", "Prime": "prime", "Legend": "legend",
}
RARITY_SYNONYMS = {"rare holo": "holo-rare", "rare holo lv.x": "holo-rare-lv-x"}

# TCGdex subtypes that are an edition in the catalog's words, and the ones that are a
# misprint. Anything else is reported rather than filed somewhere plausible.
EDITION_SUBTYPES = {"shadowless", "1999-2000-copyright", "1999-copyright", "no-e-reader",
                    "blue-border", "gold-border", "glossy", "peelable-ditto"}
STAMP_ERRORS = {"1st-edition-error", "1st-edition-scratch-error", "d-edition-error"}
# Where TCGdex uses one word for two things, the catalog gave each its own.
RENAMED_STAMPS = {"pokeball": "pokeball-stamp"}
RENAMED_PATTERNS = {"professor-program": "professor-program-foil"}

SPELLED_NUMBERS = {"?": "question", "!": "exclamation"}


class SourceError(Exception):
    pass


# ------------------------------------------------------------------------------ fetching


class Client:
    """TCGdex over the network, or recorded answers from a folder (for tests)."""

    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures

    def _get(self, path: str):
        if self.fixtures:
            file = self.fixtures / (urllib.parse.unquote(path).strip("/") + ".json")
            return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
        for attempt in range(4):
            try:
                request = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
            except (urllib.error.URLError, TimeoutError, ValueError):
                pass
            time.sleep(1.5 * (attempt + 1))
        raise SourceError(f"TCGdex did not answer for {path}")

    def _graphql(self, query: str):
        body = json.dumps({"query": query}).encode()
        for attempt in range(4):
            try:
                request = urllib.request.Request(f"{BASE}/graphql", data=body, method="POST", headers={
                    "User-Agent": USER_AGENT, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    payload = json.loads(response.read())
                if payload.get("data"):
                    return payload["data"]
                if payload.get("errors"):
                    return None
            except (urllib.error.URLError, TimeoutError, ValueError):
                pass
            time.sleep(1.5 * (attempt + 1))
        return None

    def sets(self, language: str) -> list[dict]:
        return self._get(f"{LANGUAGES[language]}/sets") or []

    def set_detail(self, language: str, key: str) -> dict | None:
        return self._get(f"{LANGUAGES[language]}/sets/{urllib.parse.quote(key, safe='-._~')}")

    def cards(self, language: str, ids: list[str]) -> dict[str, dict]:
        lang = LANGUAGES[language]
        found: dict[str, dict] = {}
        stragglers = list(ids)
        if lang == "en" and not self.fixtures:
            safe = [i for i in ids if not set(i) & LITERAL_BREAKERS]
            batches = [safe[i:i + BATCH] for i in range(0, len(safe), BATCH)]
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for chunk, data in zip(batches, pool.map(self._batch_query, batches)):
                    for n, card_id in enumerate(chunk):
                        row = (data or {}).get(f"c{n}")
                        if row and row.get("id") == card_id:
                            found[card_id] = prune(row)
            stragglers = [i for i in ids if i not in found]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for card_id, card in zip(stragglers, pool.map(lambda i: self._rest_card(lang, i), stragglers)):
                if card:
                    found[card_id] = card
        return found

    def _batch_query(self, chunk: list[str]):
        query = "{ " + " ".join(f'c{n}: card(id: "{cid}") {{ {CARD_SELECTION} }}'
                                for n, cid in enumerate(chunk)) + " }"
        return self._graphql(query)

    def _rest_card(self, lang: str, card_id: str) -> dict | None:
        card = self._get(f"{lang}/cards/{urllib.parse.quote(card_id, safe='-._~')}")
        if not card or card.get("id") != card_id:
            return None
        kept = {k: card[k] for k in REST_KEYS if card.get(k) is not None}
        if card.get("variants_detailed"):
            kept["variants_detailed"] = [
                {k: v[k] for k in ("type", "subtype", "size", "stamp", "foil") if v.get(k)}
                for v in card["variants_detailed"] if isinstance(v, dict)
            ]
        return prune(kept)


def prune(value):
    if isinstance(value, dict):
        cleaned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        cleaned = [prune(v) for v in value]
        return [v for v in cleaned if v not in (None, [], {}, "")]
    return value


# ------------------------------------------------------------------------------ vocabulary


class Vocabulary:
    def __init__(self, db: Db):
        words, terms = together(lambda: db.get("variant_words"), lambda: db.get("terms"))
        self.words = {w["word"]: w["kind"] for w in words}
        self.terms: dict[str, set[str]] = {}
        self.rarity_by_label: dict[str, str] = {}
        for t in terms:
            self.terms.setdefault(t["kind"], set()).add(t["code"])
            if t["kind"] == "rarity":
                self.rarity_by_label[(t["labels"] or {}).get("en", "").lower()] = t["code"]
                self.rarity_by_label[t["code"]] = t["code"]
        self.rarity_by_label.update(RARITY_SYNONYMS)

    def has_term(self, kind: str, code: str | None) -> bool:
        return bool(code) and code in self.terms.get(kind, set())

    def is_word(self, word: str | None, kind: str) -> bool:
        return word is None or self.words.get(word) == kind


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ------------------------------------------------------------------------------ mapping


def number_from(local_id: str) -> str | None:
    raw = urllib.parse.unquote(str(local_id)).strip()
    if raw in SPELLED_NUMBERS:
        return SPELLED_NUMBERS[raw]
    cleaned = re.sub(r"[^a-z0-9]", "", raw.lower())
    return cleaned or None


def printed_number(local_id: str, official: int | None, set_kind: str) -> str:
    raw = urllib.parse.unquote(str(local_id)).strip()
    if raw.isdigit() and official and set_kind != "promo":
        total = str(official)
        return f"{raw}/{total.zfill(len(raw))}"
    return raw


def suggest_set(detail: dict) -> dict:
    count = detail.get("cardCount") or {}
    return {
        "name": detail.get("name"),
        "release_date": detail.get("releaseDate"),
        "printed_total": count.get("official"),
        "abbreviation": (detail.get("abbreviation") or {}).get("official"),
        "logo_url": f"{detail['logo']}.webp" if detail.get("logo") else None,
        "symbol_url": f"{detail['symbol']}.webp" if detail.get("symbol") else None,
        "cards_listed": len(detail.get("cards") or []),
    }


def suggest_card(data: dict, detail: dict, set_kind: str, vocab: Vocabulary) -> dict:
    """What TCGdex says a card is, in the catalog's own fields, with what did not map."""
    problems: list[str] = []

    def term(kind: str, code: str | None, said: str | None) -> str | None:
        if code is None:
            return None
        if vocab.has_term(kind, code):
            return code
        problems.append(f"TCGdex says {kind.replace('_', ' ')} \"{said}\", which is not a term yet")
        return None

    number = number_from(data.get("localId", ""))
    if not number:
        problems.append(f"TCGdex's number \"{data.get('localId')}\" has nothing usable in it")

    subtypes = []
    if data.get("stage"):
        subtypes.append(term("subtype", STAGES.get(data["stage"].lower(), slug(data["stage"])), data["stage"]))
    if data.get("trainerType"):
        subtypes.append(term("subtype", TRAINER_TYPES.get(data["trainerType"].lower(), slug(data["trainerType"])),
                             data["trainerType"]))
    if data.get("energyType"):
        subtypes.append(term("subtype", ENERGY_TYPES.get(data["energyType"].lower(), slug(data["energyType"])),
                             data["energyType"]))
    if data.get("suffix"):
        subtypes.append(term("subtype", SUFFIXES.get(data["suffix"], slug(data["suffix"])), data["suffix"]))

    def types(values) -> list[str]:
        return [t for t in (term("type", slug(v), v) for v in values or []) if t]

    rarity = None
    if data.get("rarity") and data["rarity"] != "None":
        code = vocab.rarity_by_label.get(data["rarity"].lower()) or slug(data["rarity"])
        rarity = term("rarity", code, data["rarity"])

    abilities = []
    for a in data.get("abilities") or []:
        kind = term("ability_kind", slug(a.get("type", "")), a.get("type"))
        if kind:
            abilities.append({"name": a.get("name"), "kind": kind, "text": a.get("effect")})

    attacks = [{"name": a.get("name"), "cost": types(a.get("cost")), "damage": str(a["damage"]) if a.get("damage") is not None else None,
                "text": a.get("effect")} for a in data.get("attacks") or []]

    def weakness_list(values):
        out = []
        for w in values or []:
            t = term("type", slug(w.get("type", "")), w.get("type"))
            if t:
                out.append({"type": t, "value": w.get("value")})
        return out

    regulation = str(data.get("regulationMark") or "").strip().upper()
    fields = {
        "number": number,
        "printed_number": printed_number(data.get("localId", ""), (detail.get("cardCount") or {}).get("official"), set_kind),
        "name": data.get("name"),
        "category": CATEGORIES.get(str(data.get("category", "")).lower()),
        "subtypes": [s for s in subtypes if s],
        "hp": data.get("hp") if isinstance(data.get("hp"), int) else None,
        "types": types(data.get("types")),
        "evolves_from": data.get("evolveFrom"),
        "abilities": abilities,
        "attacks": attacks,
        "weaknesses": weakness_list(data.get("weaknesses")),
        "resistances": weakness_list(data.get("resistances")),
        "retreat": data.get("retreat") if isinstance(data.get("retreat"), int) else None,
        "rules": [data["effect"]] if data.get("effect") else [],
        "flavor_text": data.get("description"),
        "illustrator": data.get("illustrator"),
        "rarity": rarity,
        "regulation_mark": regulation if re.fullmatch(r"[A-Z]", regulation) else None,
        "dex_numbers": [n for n in data.get("dexId") or [] if isinstance(n, int)],
    }
    if not fields["category"]:
        problems.append(f"TCGdex's category \"{data.get('category')}\" is not pokemon, trainer or energy")

    printings = suggest_printings(data, vocab, problems)
    return {
        "fields": fields,
        "printings": printings,
        "image_url": f"{data['image']}/high.webp" if data.get("image") else None,
        "problems": problems,
    }


def suggest_printings(data: dict, vocab: Vocabulary, problems: list[str]) -> list[dict]:
    detailed = [v for v in data.get("variants_detailed") or []
                if isinstance(v, dict) and v.get("type") and (v.get("size") or "standard") == "standard"]
    shared = common_stamps(data)
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(finish, edition=None, pattern=None, stamps=(), error=None):
        parts = {"edition": edition, "pattern": pattern, "finish": finish, "stamps": sorted(set(stamps)), "error": error}
        for kind in ("edition", "pattern", "finish", "error"):
            if not vocab.is_word(parts[kind], kind):
                problems.append(f"TCGdex has a printing with {kind} \"{parts[kind]}\", which is not a word yet")
                return
        for stamp in parts["stamps"]:
            if not vocab.is_word(stamp, "stamp"):
                problems.append(f"TCGdex has a printing stamped \"{stamp}\", which is not a word yet")
                return
        key = (edition, pattern, finish, tuple(parts["stamps"]), error)
        if key not in seen:
            seen.add(key)
            out.append(parts)

    if detailed:
        for v in detailed:
            stamps = set(v.get("stamp") or []) - shared
            edition = None
            error = None
            if "1st-edition" in stamps:
                stamps.discard("1st-edition")
                edition = "1st-edition"
            for s in list(stamps & STAMP_ERRORS):
                stamps.discard(s)
                error = s
            subtype = v.get("subtype")
            if subtype == "shadowless-red-cheek":
                edition = edition or "shadowless"
                error = "red-cheeks"
            elif subtype in EDITION_SUBTYPES:
                # A 1st Edition Base Set card is shadowless by definition, so the stamp says it all.
                if not (edition == "1st-edition" and subtype == "shadowless"):
                    if edition:
                        problems.append(f"TCGdex has a printing that is both {edition} and {subtype}")
                        continue
                    edition = subtype
            elif subtype and subtype != "unlimited":
                error = subtype
            pattern = v.get("foil")
            pattern = RENAMED_PATTERNS.get(pattern, pattern)
            add(v["type"], edition, pattern, [RENAMED_STAMPS.get(s, s) for s in stamps], error)
    else:
        flags = data.get("variants") or {}
        for finish in ("normal", "holo", "reverse"):
            if flags.get(finish):
                add(finish)
        if flags.get("firstEdition"):
            add("holo" if flags.get("holo") else "normal", edition="1st-edition")
        if flags.get("wPromo"):
            add("holo" if flags.get("holo") else "normal", stamps=["w-promo"])
    return out


# ------------------------------------------------------------------------------ import


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def import_set(db: Db, client: Client, our_set: dict, catalog: dict, key: str) -> dict:
    """Fetch one TCGdex set and store what it says. Creates no series, set or card."""
    language = catalog["language"]
    if language not in LANGUAGES:
        raise SourceError(f"TCGdex has no {catalog['name']} catalog")
    key = key.strip()
    detail = client.set_detail(language, key)
    if not detail or not detail.get("id"):
        raise SourceError(f"TCGdex has no set \"{key}\" in {catalog['name']}")
    ids = [c["id"] for c in detail.get("cards") or [] if c.get("id")]
    cards = client.cards(language, ids)
    fetched = now()

    # One import per set: importing a different TCGdex set replaces the old link.
    db.update("source_records", {"source_id": eq(SOURCE), "kind": eq("set"), "matched": eq(our_set["id"]),
                                 "key": f"neq.{detail['id']}"}, {"matched": None, "matched_hash": None})
    set_row = {"source_id": SOURCE, "language": language, "kind": "set", "key": detail["id"],
               "data": prune(detail), "fetched_at": fetched, "matched": our_set["id"]}
    stored_set = db.upsert("source_records", [set_row], "source_id,language,kind,key")
    if stored_set and not stored_set[0].get("matched_hash"):
        db.update("source_records", {"id": eq(str(stored_set[0]["id"]))}, {"matched_hash": stored_set[0]["data_hash"]})

    rows = [{"source_id": SOURCE, "language": language, "kind": "card", "key": card_id,
             "data": data, "fetched_at": fetched} for card_id, data in cards.items()]
    each(lambda chunk: db.upsert("source_records", chunk, "source_id,language,kind,key"), chunked(rows, 100), workers=4)

    missing = [i for i in ids if i not in cards]
    return {"key": detail["id"], "name": detail.get("name"), "cards": len(cards), "missing": missing}


def set_import(db: Db, our_set: dict) -> dict | None:
    return db.one("source_records", {"source_id": eq(SOURCE), "kind": eq("set"), "matched": eq(our_set["id"])})


def candidates(db: Db, our_set: dict, vocab: Vocabulary | None = None) -> dict:
    record = set_import(db, our_set)
    if not record:
        return {"import": None, "candidates": []}
    detail = record["data"]
    listed = [c["id"] for c in detail.get("cards") or []]
    given = vocab
    vocab, found, ours = together(
        lambda: given or Vocabulary(db),
        lambda: db.get_in("source_records", "key", listed,
                          {"source_id": eq(SOURCE), "language": eq(record["language"]), "kind": eq("card")}),
        lambda: db.get("cards", {"set_id": eq(our_set["id"]), "select": "id,number,name,review"}))
    records = {r["key"]: r for r in found}
    by_number = {c["number"]: c for c in ours}
    by_id = {c["id"]: c for c in ours}

    out = []
    for card_id in listed:
        r = records.get(card_id)
        if not r:
            out.append({"key": card_id, "status": "missing", "problems": ["TCGdex did not answer for this card"]})
            continue
        suggestion = suggest_card(r["data"], detail, our_set["kind"], vocab)
        number = suggestion["fields"]["number"]
        if r.get("matched") and r["matched"] in by_id:
            status = "changed" if r.get("changed") else "accepted"
        elif number and number in by_number:
            status = "number-taken"
        elif not number or not suggestion["fields"]["category"]:
            status = "unusable"
        else:
            status = "new"
        out.append({
            "key": card_id,
            "status": status,
            "matched": r.get("matched") if r.get("matched") in by_id else None,
            "number": number,
            "printed_number": suggestion["fields"]["printed_number"],
            "name": suggestion["fields"]["name"],
            "rarity": suggestion["fields"]["rarity"],
            "printings": [variant_name(p) for p in suggestion["printings"]],
            "image_url": suggestion["image_url"],
            "problems": suggestion["problems"],
        })
    return {
        "import": {"source": SOURCE, "key": record["key"], "language": record["language"],
                   "fetched_at": record["fetched_at"], "changed": record.get("changed"),
                   "suggestion": suggest_set(detail)},
        "candidates": out,
    }


def variant_name(p: dict) -> str:
    return "-".join(x for x in [p["edition"], p["pattern"], p["finish"], *p["stamps"], p["error"]] if x)


CARD_COLUMNS = ("number", "printed_number", "name", "category", "subtypes", "hp", "types", "evolves_from",
                "abilities", "attacks", "weaknesses", "resistances", "retreat", "rules", "flavor_text",
                "illustrator", "rarity", "regulation_mark", "dex_numbers")


def accept(db: Db, our_set: dict, keys: list[str]) -> dict:
    """Make cards (and their printings) from accepted suggestions. Every one starts unreviewed."""
    record = set_import(db, our_set)
    if not record:
        raise SourceError("this set has not been imported from TCGdex")
    vocab = Vocabulary(db)
    listing, found = together(
        lambda: {c["key"]: c for c in candidates(db, our_set, vocab)["candidates"]},
        lambda: db.get_in("source_records", "key", keys,
                          {"source_id": eq(SOURCE), "language": eq(record["language"]), "kind": eq("card")}))
    records = {r["key"]: r for r in found}

    skipped: dict[str, str] = {}
    made = []
    for key in keys:
        entry = listing.get(key)
        if not entry or key not in records:
            skipped[key] = "not part of this import"
            continue
        if entry["status"] != "new":
            skipped[key] = {"accepted": "already a card", "changed": "already a card",
                            "number-taken": "a card with that number already exists",
                            "unusable": "its number or category could not be read",
                            "missing": "TCGdex did not answer for it"}.get(entry["status"], entry["status"])
            continue
        suggestion = suggest_card(records[key]["data"], record["data"], our_set["kind"], vocab)
        note = f"Imported from TCGdex {key} on {now()[:10]}."
        if suggestion["problems"]:
            note += " Not carried over: " + "; ".join(suggestion["problems"]) + "."
        row = {"set_id": our_set["id"], **{c: suggestion["fields"][c] for c in CARD_COLUMNS}, "notes": note}
        made.append((key, row, suggestion["printings"]))

    created = [card for batch in each(lambda chunk: db.insert("cards", [row for _, row, _ in chunk]),
                                      chunked(made, 50), workers=4) for card in batch]
    by_number = {c["number"]: c["id"] for c in created}

    printing_rows = []
    matched_rows = []
    for key, row, printings in made:
        card_id = by_number.get(row["number"])
        if not card_id:
            continue
        printing_rows += [{"card_id": card_id, **p} for p in printings]
        r = records[key]
        matched_rows.append({"source_id": SOURCE, "language": r["language"], "kind": "card", "key": key,
                             "data": r["data"], "matched": card_id, "matched_hash": r["data_hash"]})
    together(
        lambda: each(lambda chunk: db.insert("printings", chunk), chunked(printing_rows, 200), workers=4),
        lambda: each(lambda chunk: db.upsert("source_records", chunk, "source_id,language,kind,key"),
                     chunked(matched_rows, 100), workers=4))

    return {"created": len(created), "printings": len(printing_rows), "skipped": skipped}


def card_sources(db: Db, card: dict, our_set: dict) -> list[dict]:
    """What each import said about this card, in the catalog's own fields, for comparing."""
    records, set_record, vocab = together(
        lambda: [r for r in db.get("source_records", {"matched": eq(card["id"]), "kind": eq("card")})
                 if r["source_id"] == SOURCE],
        lambda: set_import(db, our_set),
        lambda: Vocabulary(db))
    if not set_record:
        return []
    out = []
    for r in records:
        suggestion = suggest_card(r["data"], set_record["data"], our_set["kind"], vocab)
        out.append({"source": r["source_id"], "key": r["key"], "fetched_at": r["fetched_at"],
                    "changed": r.get("changed"), **suggestion,
                    "printings": [{**p, "variant": variant_name(p)} for p in suggestion["printings"]]})
    return out


def mark_reviewed(db: Db, card_ids: list[str]) -> None:
    """The cards' current source data is what was reviewed, so it stops counting as changed."""
    changed = db.get_in("source_records", "matched", card_ids,
                        {"kind": eq("card"), "changed": "is.true", "select": "id,data_hash"})
    each(lambda r: db.update("source_records", {"id": eq(str(r["id"]))}, {"matched_hash": r["data_hash"]}), changed)
