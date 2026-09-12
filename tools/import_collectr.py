#!/usr/bin/env python3
"""
Turns a Collectr portfolio export into a file Pocketful can import.

Collectr exports one CSV row per (portfolio, card, printing, condition) with a quantity.
Pocketful stores one row per *physical card*, because two copies of the same card can sit
in different pockets, in different condition, bought for different money. So a row with a
quantity of nine becomes nine copies -- which is the first thing this script does and the
reason the output is much longer than the input.

The join is the interesting part. Collectr names sets the way TCGplayer does; the catalog
names them the way TCGdex does; and nothing says the two are the same set. So a row is
resolved in three steps:

    "SV: 151"  ->  TCGplayer group  ->  TCGdex set  ->  card by printed number

The middle step is `catalog/tcgplayer-groups.json`, which map_groups.py already works out
for the price build. This script gets it for free, which is the whole argument for having
written that mapping down rather than deriving it inline.

What cannot be imported is reported rather than dropped silently. A Collectr portfolio
holds things Pokemon TCG cards are not -- sealed Elite Trainer Boxes, Magic singles,
Japanese printings the English catalog has never heard of -- and a converter that quietly
lost 12,000 dollars of sealed product would be worse than one that says so.

Usage:
    python tools/import_collectr.py export.csv -o pocketful-import.json
    python tools/import_collectr.py export.csv --report
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
GROUPS = CATALOG / "tcgplayer-groups.json"
TCGCSV_CACHE = CATALOG / ".tcgcsv" / "groups.json"

EXPORT_MARKER = "pocketful"
SAVE_SCHEMA = 1

# Collectr's condition names, in the app's vocabulary.
CONDITIONS = {
    "mint": "MINT",
    "near mint": "NEAR_MINT",
    "lightly played": "LIGHTLY_PLAYED",
    "moderately played": "MODERATELY_PLAYED",
    "heavily played": "HEAVILY_PLAYED",
    "damaged": "DAMAGED",
}

# Collectr's "Variance" column, which conflates two things the app keeps apart: how a card
# was *printed* (the finish) and which *run* it came from (the edition). "1st Edition
# Holofoil" is both, and the pair is what makes a variant id.
VARIANCE = {
    "normal": ("NON_HOLO", "UNLIMITED"),
    "holofoil": ("HOLO", "UNLIMITED"),
    "reverse holofoil": ("REVERSE_HOLO", "UNLIMITED"),
    "foil": ("HOLO", "UNLIMITED"),
    "unlimited": ("NON_HOLO", "UNLIMITED"),
    "unlimited holofoil": ("HOLO", "UNLIMITED"),
    "1st edition": ("NON_HOLO", "FIRST_EDITION"),
    "1st edition holofoil": ("HOLO", "FIRST_EDITION"),
    # The 151 sets print a card three ways and Collectr names the pattern rather than the
    # finish. All three are reverse holos as far as the app is concerned; the pattern is a
    # distinction it does not model, so the commoner one is the honest mapping.
    "poke ball reverse holo": ("REVERSE_HOLO", "UNLIMITED"),
    "master ball reverse holo": ("REVERSE_HOLO", "UNLIMITED"),
    "poke ball pattern": ("REVERSE_HOLO", "UNLIMITED"),
    "master ball pattern": ("REVERSE_HOLO", "UNLIMITED"),
    # Topps printed trading cards, not Pokemon TCG cards. Kept so the row reports as an
    # unmatched *card* rather than an unreadable finish.
    "base chrome": ("OTHER", "UNLIMITED"),
}

# Storage that is obviously not a binder. Everything else with "binder" in its name gets
# pages; anything left over becomes a box, which is what Collectr's flat list really is.
SEALED_HINTS = ("sealed", "shelf")


def norm_set(name: str) -> str:
    """A set name with the decoration both catalogs add to it taken back off."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"^[a-z]{1,7}[0-9]*(\.[0-9]+)?[a-z]?\s*[:-]\s*", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"\((unlimited|japanese|jp|cn)\)", " ", text)
    text = re.sub(r"\b(pokemon|tcg|the|cards|base set|promo cards)\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_number(value: str) -> str:
    """
    The printed number, in the one form both catalogs agree on.

    Collectr writes "014/089" where the catalog writes "014", and neither pads
    consistently across eras -- so everything is reduced to the part before the slash with
    leading zeros gone.
    """
    text = (value or "").strip()
    if not text:
        return ""
    head = text.split("/")[0].strip()
    # Promo numbering carries a letter prefix that is part of the number: SWSH039, XY72.
    return head.lstrip("0") or "0"


def slug(text: str, fallback: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return out or fallback


def norm_name(text: str) -> str:
    """
    A card name reduced to what both catalogs agree on.

    Collectr qualifies a name with everything that distinguishes one printing from
    another -- "Pikachu (Cosmos Holo)", "Charizard ex - 161", "Mew (8)" -- because its set
    column is often a bucket rather than a set. All of that comes off; the number carries
    the distinction instead.
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"\([^)]*\)", " ", t)       # (Cosmos Holo), (Prerelease), (JP)
    t = re.sub(r"\[[^\]]*\]", " ", t)      # [W Stamped], [2000]
    t = re.sub(r"\s+-\s+.*$", " ", t)      # "Charizard ex - 161"
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_catalog():
    """Every card, indexed by set and by printed number -- and globally by name."""
    if not SETS.is_dir():
        raise SystemExit("No catalog/sets. Run pull_catalog.py --static --all first.")
    sets = {}
    # The fallback index. Collectr files a great many cards under buckets that are not
    # sets at all -- "Miscellaneous Cards & Products", "Blister Exclusives" -- where the
    # printed number belongs to whichever real set the card came from. For those the set
    # is useless and the name plus the number is the only handle there is.
    everywhere = defaultdict(list)
    for path in sorted(SETS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        by_number = defaultdict(list)
        for card in doc.get("cards") or []:
            n = norm_number(card.get("localId"))
            by_number[n].append(card)
            everywhere[(norm_name(card.get("name")), n)].append((doc["id"], card))
        sets[doc["id"]] = {"doc": doc, "by_number": by_number}
    return sets, everywhere


def set_resolver():
    """Collectr's set name to a TCGdex set id, by way of the TCGplayer group mapping."""
    if not GROUPS.exists():
        raise SystemExit("No catalog/tcgplayer-groups.json. Run map_groups.py first.")
    mapping = json.loads(GROUPS.read_text(encoding="utf-8"))["sets"]
    group_to_set = {v["groupId"]: sid for sid, v in mapping.items() if v.get("groupId")}

    names = {}
    for sid, v in mapping.items():
        if not v.get("groupId"):
            continue
        for candidate in (v.get("tcgplayerName"), v.get("name")):
            if candidate:
                names.setdefault(norm_set(candidate), sid)
    if TCGCSV_CACHE.exists():
        for g in json.loads(TCGCSV_CACHE.read_text(encoding="utf-8")):
            sid = group_to_set.get(g["groupId"])
            if sid:
                names.setdefault(norm_set(g["name"]), sid)

    keys = [k for k in names if k]
    cache: dict[str, str | None] = {}

    def resolve(raw: str) -> str | None:
        if raw in cache:
            return cache[raw]
        n = norm_set(raw)
        hit = names.get(n)
        if hit is None:
            # Containment, longest first: "151" is inside "scarlet and violet 151", and
            # the longer key is the more specific claim.
            inside = [k for k in keys if k in n or n in k]
            if inside:
                hit = names[max(inside, key=len)]
        if hit is None:
            close = difflib.get_close_matches(n, keys, n=1, cutoff=0.84)
            if close:
                hit = names[close[0]]
        cache[raw] = hit
        return hit

    return resolve


def variant_id(card_id: str, finish: str, edition: str) -> str:
    suffix = finish.lower()
    if edition != "UNLIMITED":
        suffix += "-" + edition.lower()
    return f"tcgdex-{card_id}-{suffix}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="a Collectr portfolio export")
    ap.add_argument("-o", "--out", default="pocketful-import.json")
    ap.add_argument("--report", action="store_true", help="only say what would happen")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    sets, everywhere = load_catalog()
    resolve_set = set_resolver()

    price_column = next(
        (c for c in (rows[0].keys() if rows else []) if c.startswith("Market Price")), None
    )

    copies: list[dict] = []
    cards: dict[str, dict] = {}
    printings: dict[str, dict] = {}
    variants: dict[str, dict] = {}
    by_portfolio: dict[str, list[str]] = defaultdict(list)

    skipped = Counter()
    skipped_value = Counter()
    skipped_examples: dict[str, str] = {}
    matched_rows = 0
    seq = 0

    def money(text: str) -> int | None:
        try:
            cents = round(float(text) * 100)
        except (TypeError, ValueError):
            return None
        return cents if cents > 0 else None

    for row in rows:
        qty = int(float(row.get("Quantity") or 0) or 0)
        value = (money(row.get(price_column) or "") or 0) * qty / 100 if price_column else 0

        def drop(reason: str) -> None:
            skipped[reason] += qty
            skipped_value[reason] += value
            skipped_examples.setdefault(reason, f"{row.get('Product Name')} ({row.get('Set')})")

        if qty <= 0:
            continue
        if (row.get("Category") or "").strip() != "Pokemon":
            drop(f"not Pokemon ({row.get('Category')})")
            continue
        if not (row.get("Card Number") or "").strip():
            drop("sealed product (no card number)")
            continue

        number = norm_number(row.get("Card Number") or "")
        set_id = resolve_set(row.get("Set") or "")
        card = None

        if set_id and set_id in sets:
            found = sets[set_id]["by_number"].get(number) or []
            # A number can carry several cards only when the catalog itself duplicates it;
            # the first in printed order is the plain one.
            if found:
                card = found[0]

        if card is None:
            # Name and number, across the whole catalog. Accepted only when it is
            # unambiguous: two different sets printing the same name at the same number is
            # exactly the case where guessing puts the wrong card in somebody's binder.
            hits = everywhere.get((norm_name(row.get("Product Name")), number)) or []
            distinct = {sid for sid, _ in hits}
            if len(hits) == 1 or len(distinct) == 1:
                set_id, card = hits[0]
            elif not set_id or set_id not in sets:
                drop("set not in catalog")
                continue
            else:
                drop("card number not in that set")
                continue

        finish, edition = VARIANCE.get((row.get("Variance") or "").strip().lower(), (None, None))
        if finish is None:
            drop(f"unknown printing ({row.get('Variance')})")
            continue

        condition = CONDITIONS.get((row.get("Card Condition") or "").strip().lower(), "NEAR_MINT")
        paid = money(row.get("Average Cost Paid") or "")
        acquired = (row.get("Date Added") or "").strip() or None
        notes = (row.get("Notes") or "").strip(" ;\t") or None

        card_id = f"tcgdex-{card['id']}"
        printing_id = card_id
        vid = variant_id(card["id"], finish, edition)
        doc = sets[set_id]["doc"]

        cards.setdefault(card_id, {
            "id": card_id,
            "name": card.get("name") or "",
            "supertype": {"trainer": "TRAINER", "energy": "ENERGY"}.get(
                (card.get("category") or "").lower(), "POKEMON"),
            **({"hp": card["hp"]} if card.get("hp") else {}),
            **({"flavorText": card["description"]} if card.get("description") else {}),
        })
        printings.setdefault(printing_id, {
            "id": printing_id,
            "cardId": card_id,
            "setCode": set_id,
            "setName": doc.get("name") or set_id,
            "number": card.get("localId") or number,
            "setTotal": str((doc.get("cardCount") or {}).get("official") or "") or None,
            "rarity": card.get("rarity"),
            "illustrator": card.get("illustrator"),
            "releaseYear": int(doc["releaseDate"][:4]) if (doc.get("releaseDate") or "")[:4].isdigit() else None,
            **({"imageUrl": card["image"]} if card.get("image") else {}),
            **({"imageAltUrl": card["imageAlt"]} if card.get("imageAlt") else {}),
        })
        variants.setdefault(vid, {
            "id": vid, "printingId": printing_id, "finish": finish, "edition": edition,
        })

        matched_rows += 1
        for _ in range(qty):
            seq += 1
            copy_id = f"copy-import-{seq:05d}"
            copies.append({
                "id": copy_id,
                "variantId": vid,
                **({"condition": condition} if condition != "NEAR_MINT" else {}),
                **({"acquiredPrice": paid} if paid else {}),
                **({"acquiredDate": acquired} if acquired else {}),
                **({"notes": notes} if notes else {}),
            })
            by_portfolio[(row.get("Portfolio Name") or "Imported").strip()].append(copy_id)

    # ---- storage ---------------------------------------------------------------
    binders: list[dict] = []
    containers: list[dict] = []
    located: dict[str, dict] = {}

    palette = [0xFF3B82F6, 0xFFEF4444, 0xFF22C55E, 0xFFF59E0B,
               0xFFA855F7, 0xFF14B8A6, 0xFFEC4899, 0xFF64748B]

    for index, (name, copy_ids) in enumerate(sorted(by_portfolio.items())):
        colour = palette[index % len(palette)]
        lower = name.lower()
        if "binder" in lower and not any(h in lower for h in SEALED_HINTS):
            binder_id = f"binder-{slug(name, str(index))}"
            # Nine pockets a page, enough sheets to hold what is in it, and a little room
            # to grow -- a binder imported exactly full has nowhere to put the next card.
            sheets = max(1, math.ceil(len(copy_ids) / 18) + 1)
            binders.append({
                "id": binder_id,
                "name": name,
                "subtitle": "Imported from Collectr",
                "layout": {"cols": 3, "rows": 3},
                "sheetCount": sheets,
                "spineColor": colour,
                "slots": [{"type": "filled", "copyId": c} for c in copy_ids],
            })
            for ordinal, c in enumerate(copy_ids):
                located[c] = {"type": "binder", "binderId": binder_id, "ordinal": ordinal}
        else:
            container_id = f"container-{slug(name, str(index))}"
            containers.append({
                "id": container_id,
                "name": name,
                "subtitle": "Imported from Collectr",
                "kind": "SEALED" if any(h in lower for h in SEALED_HINTS) else "BOX",
                "color": colour,
                "copyIds": copy_ids,
            })
            for c in copy_ids:
                located[c] = {"type": "container", "containerId": container_id}

    for copy in copies:
        where = located.get(copy["id"])
        if where:
            copy["location"] = where

    document = {
        "app": EXPORT_MARKER,
        "schema": SAVE_SCHEMA,
        "exportedOn": time.strftime("%Y-%m-%d"),
        "collection": {
            "schema": SAVE_SCHEMA,
            "copies": copies,
            "binders": binders,
            "containers": containers,
        },
        "catalog": {
            "schema": SAVE_SCHEMA,
            "fetchedAtEpochSeconds": 0,
            "cards": list(cards.values()),
            "printings": list(printings.values()),
            "variants": list(variants.values()),
        },
    }

    print(f"read {len(rows)} rows from {Path(args.csv).name}")
    print(f"  matched   {matched_rows} rows -> {len(copies)} physical cards")
    print(f"  catalog   {len(cards)} cards, {len(printings)} printings, {len(variants)} variants")
    print(f"  storage   {len(binders)} binders, {len(containers)} containers")
    if skipped:
        total = sum(skipped.values())
        print(f"\n  not imported: {total} cards"
              f"  (${sum(skipped_value.values()):,.2f} of listed value)")
        for reason, n in skipped.most_common():
            print(f"    {n:5}  {reason:36} ${skipped_value[reason]:>10,.2f}"
                  f"   e.g. {skipped_examples[reason][:44]}")

    if args.report:
        return
    out = Path(args.out)
    out.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nWrote {out}  ({out.stat().st_size / 1048576:.2f} MiB)")


if __name__ == "__main__":
    main()
