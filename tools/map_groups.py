#!/usr/bin/env python3
"""
Works out which TCGplayer set each TCGdex set is, once, and writes it down.

Prices come from TCGplayer by way of TCGCSV, which files everything under its own set ids
("groups"). Nothing in either catalog says that TCGdex's `mep` and TCGplayer's group 24451
are the same 89 cards -- and they are not obviously the same thing to look at either, since
one is called "MEP Black Star Promos" and the other "ME: Mega Evolution Promo".

Three ways of asking, cheapest first:

  1. **The name**, normalised. Gets the ordinary case: "Surging Sparks" is "SV08: Surging
     Sparks" with a code bolted on the front.
  2. **The abbreviation** either side carries. Gets the promos, where the names diverge
     completely but both agree the set is called MEP.
  3. **A card**, when neither works. TCGdex's REST card document carries the TCGplayer
     product id for that exact card, and every product id belongs to exactly one group --
     so one card is a definitive answer for the whole set. This costs a request and is
     therefore the last resort, but it is the only one of the three that cannot be wrong.

Sets that fail all three are recorded with a null rather than dropped, so the file is a
complete census of what is mapped and what is not. Most of those are Pokemon TCG Pocket,
which TCGplayer does not sell at all: a set with no group is the correct answer there, not
a gap to close.

A person can overrule any of it. An entry with `"via": "manual"` was linked by hand in the
editor -- including a deliberate null, "TCGplayer does not sell this set" -- and is kept as
written by every run, `--recheck` included. Re-deriving it would put back exactly the
answer somebody already looked at and rejected.

The output is committed. It changes when a set is released, and a diff of it is a thing a
person can actually check.

Usage:
    python tools/map_groups.py              # fill in what is missing
    python tools/map_groups.py --recheck    # re-derive every set from scratch
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from difflib import get_close_matches
from pathlib import Path

import requests

from tcgplayer import normalise

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
OUT = CATALOG / "tcgplayer-groups.json"
CACHE = CATALOG / ".tcgcsv"

TCGCSV = "https://tcgcsv.com/tcgplayer"
TCGDEX = "https://api.tcgdex.net/v2/en"
POKEMON = 3

USER_AGENT = "Pocketful-catalog-builder/0.3 (+https://github.com/TronVonDoom/Pocketful)"

# TCGCSV asks for a pause between requests and a real User-Agent, and says a full sync
# should need no more than ten thousand requests. This whole script is about 450.
PAUSE = 0.12


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get_json(s: requests.Session, url: str, tries: int = 3):
    for attempt in range(tries):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            # Throttled. The published penalty is ten minutes, so backing off hard is
            # cheaper than being shut out for the rest of the run.
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


def groups(s: requests.Session) -> list[dict]:
    """Every Pokemon set TCGplayer knows about, cached so a re-run is free."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "groups.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    doc = get_json(s, f"{TCGCSV}/{POKEMON}/groups") or {}
    result = doc.get("results") or []
    path.write_text(json.dumps(result), encoding="utf-8")
    return result


def products(s: requests.Session, group_id: int) -> list[dict]:
    """One group's products, cached. The cache is what keeps a re-run off the network."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"products-{group_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    doc = get_json(s, f"{TCGCSV}/{POKEMON}/{group_id}/products") or {}
    result = doc.get("results") or []
    path.write_text(json.dumps(result), encoding="utf-8")
    time.sleep(PAUSE)
    return result


def tcgplayer_id_for_set(s: requests.Session, doc: dict, limit: int = 6) -> int | None:
    """
    A TCGplayer product id for any one card in this set.

    Tried in printed order and stopped at the first hit, because every card in a set
    belongs to the same group -- one answer settles the whole set. `variants_detailed` is
    REST-only, which is why this is not simply read off the catalog on disk.
    """
    for card in (doc.get("cards") or [])[:limit]:
        card_id = card.get("id")
        if not card_id:
            continue
        live = get_json(s, f"{TCGDEX}/cards/{card_id}", tries=2)
        time.sleep(PAUSE)
        for variant in (live or {}).get("variants_detailed") or []:
            product = (variant.get("thirdParty") or {}).get("tcgplayer")
            if product:
                return int(product)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recheck", action="store_true", help="re-derive every set")
    args = ap.parse_args()

    if not SETS.is_dir():
        raise SystemExit("No catalog/sets. Run pull_catalog.py --static --all first.")

    s = session()
    all_groups = groups(s)
    if not all_groups:
        raise SystemExit("TCGCSV returned no groups. Try again later.")

    by_name: dict[str, list[dict]] = {}
    by_abbr: dict[str, list[dict]] = {}
    for g in all_groups:
        by_name.setdefault(normalise(g.get("name")), []).append(g)
        abbr = (g.get("abbreviation") or "").strip().lower()
        if abbr:
            by_abbr.setdefault(abbr, []).append(g)
    names = list(by_name)

    existing = {}
    if OUT.exists():
        existing = json.loads(OUT.read_text(encoding="utf-8")).get("sets") or {}
        if args.recheck:
            existing = {k: v for k, v in existing.items() if v.get("via") == "manual"}

    # Built lazily: the product index is only needed for sets that reach step 3, and
    # loading it costs a request per group the first time.
    index: dict[int, int] = {}

    def group_of_product(product_id: int) -> int | None:
        if not index:
            print("  building product index across all groups ...", file=sys.stderr)
            for g in all_groups:
                for p in products(s, g["groupId"]):
                    index[p["productId"]] = g["groupId"]
        return index.get(product_id)

    mapped: dict[str, dict] = {}
    counts = {"kept": 0, "manual": 0, "name": 0, "abbreviation": 0, "card": 0, "none": 0}

    for path in sorted(SETS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        set_id = doc["id"]

        prior = existing.get(set_id)
        if prior and prior.get("via") == "manual":
            mapped[set_id] = prior
            counts["manual"] += 1
            continue
        if prior and prior.get("groupId"):
            mapped[set_id] = prior
            counts["kept"] += 1
            continue

        serie = ((doc.get("serie") or {}).get("id") or "").lower()
        # TCGplayer sells printed cards. Pocket is a phone game whose cards do not exist
        # as objects, so there is nothing to look for and no point spending a request.
        if serie == "tcgp":
            mapped[set_id] = {"groupId": None, "via": "not-sold", "name": doc.get("name")}
            counts["none"] += 1
            continue

        hit = None
        via = None

        key = normalise(doc.get("name"))
        if len(by_name.get(key, [])) == 1:
            hit, via = by_name[key][0], "name"

        if hit is None:
            abbr = ((doc.get("abbreviation") or {}).get("official") or "").strip().lower()
            for candidate in (set_id.lower(), abbr):
                if candidate and len(by_abbr.get(candidate, [])) == 1:
                    hit, via = by_abbr[candidate][0], "abbreviation"
                    break

        if hit is None:
            close = get_close_matches(key, names, n=1, cutoff=0.9)
            if close and len(by_name[close[0]]) == 1:
                hit, via = by_name[close[0]][0], "name"

        if hit is None:
            product = tcgplayer_id_for_set(s, doc)
            group_id = group_of_product(product) if product else None
            if group_id:
                hit = next(g for g in all_groups if g["groupId"] == group_id)
                via = "card"

        if hit is None:
            mapped[set_id] = {"groupId": None, "via": None, "name": doc.get("name")}
            counts["none"] += 1
            print(f"  {set_id:12} UNMATCHED  {doc.get('name')}")
        else:
            mapped[set_id] = {
                "groupId": hit["groupId"],
                "via": via,
                "name": doc.get("name"),
                "tcgplayerName": hit.get("name"),
            }
            counts[via] += 1

    OUT.write_text(
        json.dumps(
            {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sets": mapped},
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    total = len(mapped)
    resolved = sum(1 for v in mapped.values() if v.get("groupId"))
    print(
        f"\n{resolved}/{total} sets mapped "
        f"(kept {counts['kept']}, by hand {counts['manual']}, by name {counts['name']}, "
        f"by abbreviation {counts['abbreviation']}, by card {counts['card']}, "
        f"none {counts['none']})"
    )
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
