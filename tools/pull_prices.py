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
group a card is found by the number printed on it. The matching rules themselves live in
tcgplayer.py, shared with the editor, along with the two ways a person can overrule them.

Usage:
    python tools/pull_prices.py            # every mapped set
    python tools/pull_prices.py --sets mep base1
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

import requests

from tcgplayer import (
    GROUPS, POKEMON, TCGCSV, finish_key, load_card_links, load_groups, normalise_local,
    pick_by_number,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
DIST = ROOT / "dist"
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", help="only these catalog set ids")
    args = ap.parse_args()

    if not GROUPS.exists():
        raise SystemExit("No catalog/tcgplayer-groups.json. Run map_groups.py first.")
    mapping = load_groups()
    links = load_card_links()

    s = session()

    # One fetch per group per run. A card linked by hand to a product in some other group
    # costs that group's two requests once, however many cards point into it.
    fetched: dict[int, tuple[list[dict], dict[int, dict[str, int]]]] = {}

    def group(group_id: int) -> tuple[list[dict], dict[int, dict[str, int]]]:
        if group_id not in fetched:
            products = (get_json(s, f"{TCGCSV}/{POKEMON}/{group_id}/products") or {}).get("results") or []
            time.sleep(PAUSE)
            prices = (get_json(s, f"{TCGCSV}/{POKEMON}/{group_id}/prices") or {}).get("results") or []
            time.sleep(PAUSE)
            by_product: dict[int, dict[str, int]] = {}
            for row in prices:
                market = row.get("marketPrice")
                key = finish_key(row.get("subTypeName"))
                if not market or market <= 0 or not key:
                    continue
                by_product.setdefault(row["productId"], {})[key] = round(market * 100)
            fetched[group_id] = (products, by_product)
        return fetched[group_id]

    cards: dict[str, dict[str, int]] = {}
    matched_sets = 0
    skipped_sets = 0
    unmatched_cards = 0
    total_cards = 0
    by_hand = 0

    for path in sorted(SETS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        set_cards = doc.get("cards") or []
        total_cards += len(set_cards)
        set_id = doc["id"]
        if args.sets and set_id not in args.sets:
            continue

        group_id = (mapping.get(set_id) or {}).get("groupId")
        linked = sum(1 for c in set_cards if c.get("id") in links)
        # A set with no group can still hold cards someone linked one at a time -- a
        # Trainer Kit card TCGplayer happens to sell under a different product line.
        if not group_id and not linked:
            skipped_sets += 1
            continue

        by_number: dict[str, dict] = {}
        by_product: dict[int, dict[str, int]] = {}
        if group_id:
            products, by_product = group(group_id)
            if not products:
                print(f"  {set_id:12} no products for group {group_id}", file=sys.stderr)
            by_number = pick_by_number(products, lambda p: p.get("productId") in by_product)

        found = 0
        for card in set_cards:
            link = links.get(card.get("id"))
            if link is not None:
                # Beats the number match outright, and a null product is an answer too:
                # "this card is not sold", which must not fall back to guessing by number.
                quotes = None
                if link.get("productId") and link.get("groupId"):
                    quotes = group(link["groupId"])[1].get(link["productId"])
            else:
                product = by_number.get(normalise_local(card.get("localId")) or "")
                quotes = by_product.get(product["productId"]) if product else None
            if quotes:
                cards[card["id"]] = quotes
                found += 1
                by_hand += link is not None
        unmatched_cards += len(set_cards) - found
        matched_sets += 1
        print(f"  {set_id:12} {found}/{len(set_cards)} priced" + (f" ({linked} linked by hand)" if linked else ""))

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

    print(
        f"\n{len(cards)} of {total_cards} cards priced across {matched_sets} sets "
        f"({skipped_sets} sets have no TCGplayer group, {unmatched_cards} cards unmatched, "
        f"{by_hand} priced from a link made by hand)"
    )
    print(f"  {out.name:28} {len(raw) / 1048576:6.2f} MiB raw"
          f"  ->{out.stat().st_size / 1048576:6.2f} MiB gzipped")


if __name__ == "__main__":
    main()
