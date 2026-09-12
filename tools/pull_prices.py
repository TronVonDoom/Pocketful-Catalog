#!/usr/bin/env python3
"""
Builds the price file the app downloads, from TCGplayer by way of TCGCSV.

Why this exists at all, given that this repository is otherwise about what a card *is*:
because the alternative was worse. TCGdex carries a `pricing.tcgplayer` field and it is
empty for a great deal of the catalog -- every promo not sold as an English single, most
Japanese printings, most of the Trainer Kits. For those cards the app had a choice between
showing nothing and showing the European price converted from euros, and the converted
figure turned out to be badly wrong: MEP Ceruledge converts to about $24 and actually
trades at $14, because scarcity in Europe says nothing about scarcity here.

TCGCSV mirrors TCGplayer's own catalog, and it has all of them. What it does not permit is
being asked per user: its terms are a pull a day, ten thousand requests, and an explicit
"ingest this into your own cache rather than querying it live". One nightly job here is
exactly the arrangement it asks for, and it is the same bargain the catalog itself strikes
-- one client asks, everybody downloads the answer.

Prices keep their own file, their own tag and their own schedule. That separation is the
whole reason this is safe to do: the catalog is a document with no expiry date and must
stay that way, and a price is a fact with a shelf life of about a day. Putting the second
inside the first would give the whole thing the shorter of the two lifetimes.

The join is by set, then by printed number. `catalog/tcgplayer-groups.json` says which
TCGplayer group each set is -- see map_groups.py, which works that out once -- and within a
group a card is found by the number printed on it.

Usage:
    python tools/pull_prices.py            # every mapped set
    python tools/pull_prices.py --sets mep base1
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
GROUPS = CATALOG / "tcgplayer-groups.json"
DIST = ROOT / "dist"

TCGCSV = "https://tcgcsv.com/tcgplayer"
POKEMON = 3
USER_AGENT = "Pocketful-catalog-builder/0.3 (+https://github.com/TronVonDoom/Pocketful)"

# Bumped when the shape changes in a way an older app cannot read. Tracked separately from
# the catalog's schema: the two documents version independently and always have.
SCHEMA = 1

# TCGCSV asks for a pause between requests. A full run is about 370 of them.
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
            if r.status_code == 429:
                # The documented penalty is ten minutes, so waiting is cheaper than being
                # locked out for the rest of the run.
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


def finish_key(sub_type: str | None) -> str | None:
    """
    TCGplayer's printing name, in the spelling the app already looks prices up by.

    The app's key vocabulary came from TCGdex, which uses TCGplayer's own keys in camel
    case -- "reverseHolofoil", "1stEditionHolofoil". TCGCSV spells the same values out
    with spaces, so this is a spelling change rather than a mapping, and the app needs no
    new vocabulary to read this file.
    """
    if not sub_type:
        return None
    text = sub_type.strip()
    if not text:
        return None
    parts = re.split(r"\s+", text)
    head = parts[0].lower()
    rest = "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return head + rest


def card_number(product: dict) -> str | None:
    """
    The number printed on a card, as the catalog writes it.

    TCGplayer stores it as "014/089" where the catalog stores "014", and pads
    inconsistently across eras -- so the comparison is made on the part before the slash
    with leading zeros stripped, which is the only form both agree on.
    """
    for entry in product.get("extendedData") or []:
        if entry.get("name") == "Number":
            value = (entry.get("value") or "").strip()
            if not value:
                return None
            return value.split("/")[0].strip().lstrip("0") or "0"
    return None


def normalise_local(local_id: str | None) -> str | None:
    if not local_id:
        return None
    return str(local_id).strip().lstrip("0") or "0"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", help="only these catalog set ids")
    args = ap.parse_args()

    if not GROUPS.exists():
        raise SystemExit("No catalog/tcgplayer-groups.json. Run map_groups.py first.")
    mapping = json.loads(GROUPS.read_text(encoding="utf-8")).get("sets") or {}

    s = session()
    cards: dict[str, dict[str, int]] = {}
    matched_sets = 0
    skipped_sets = 0
    unmatched_cards = 0

    paths = sorted(SETS.glob("*.json"))
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        set_id = doc["id"]
        if args.sets and set_id not in args.sets:
            continue

        group_id = (mapping.get(set_id) or {}).get("groupId")
        if not group_id:
            skipped_sets += 1
            continue

        products = (get_json(s, f"{TCGCSV}/{POKEMON}/{group_id}/products") or {}).get("results") or []
        time.sleep(PAUSE)
        prices = (get_json(s, f"{TCGCSV}/{POKEMON}/{group_id}/prices") or {}).get("results") or []
        time.sleep(PAUSE)
        if not products:
            print(f"  {set_id:12} no products for group {group_id}", file=sys.stderr)
            continue

        by_product: dict[int, dict[str, int]] = {}
        for row in prices:
            market = row.get("marketPrice")
            key = finish_key(row.get("subTypeName"))
            if not market or market <= 0 or not key:
                continue
            by_product.setdefault(row["productId"], {})[key] = round(market * 100)

        # A printed number can carry several products: the card, its staff stamp, its
        # prerelease stamp. Those are genuinely different objects that trade at genuinely
        # different prices, and the catalog only knows about the plain one -- so the plain
        # one is what gets the number, and a decorated name never displaces it.
        by_number: dict[str, dict[str, int]] = {}
        decorated: dict[str, dict[str, int]] = {}
        for product in products:
            number = card_number(product)
            quotes = by_product.get(product.get("productId"))
            if not number or not quotes:
                continue
            if re.search(r"\[[^\]]+\]", product.get("name") or ""):
                decorated.setdefault(number, quotes)
            else:
                by_number.setdefault(number, quotes)

        found = 0
        for card in doc.get("cards") or []:
            number = normalise_local(card.get("localId"))
            if not number:
                continue
            quotes = by_number.get(number) or decorated.get(number)
            if quotes:
                cards[card["id"]] = quotes
                found += 1
        unmatched_cards += len(doc.get("cards") or []) - found
        matched_sets += 1
        print(f"  {set_id:12} {found}/{len(doc.get('cards') or [])} priced")

    payload = {
        "schema": SCHEMA,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "tcgplayer",
        "via": "tcgcsv.com",
        "unit": "usd_cents",
        "cards": dict(sorted(cards.items())),
    }

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"prices-v{SCHEMA}.json.gz"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(out, "wb", compresslevel=9) as fh:
        fh.write(raw)

    total_cards = sum(len(json.loads(Path(p).read_text(encoding="utf-8")).get("cards") or [])
                      for p in glob.glob(str(SETS / "*.json")))
    print(
        f"\n{len(cards)} of {total_cards} cards priced across {matched_sets} sets "
        f"({skipped_sets} sets have no TCGplayer group, {unmatched_cards} cards unmatched)"
    )
    print(f"  {out.name:28} {len(raw) / 1048576:6.2f} MiB raw"
          f"  ->{out.stat().st_size / 1048576:6.2f} MiB gzipped")


if __name__ == "__main__":
    main()
