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

A card's special printings are priced as well (variants.py: the Pokemon Center stamp, the
Poke Ball pattern, the 1st Edition), under `special` rather than `cards`, keyed by card and
then by `<type>~<printing>`. They are kept apart so an app that predates them reads `cards`
exactly as it always has, and can never take a stamped copy's price for the plain one's.
Stamped cards are mostly filed outside their set's own group, so every group is fetched,
not only the linked ones.

With `--history DIR`, each set's price history (price_history.py) is read from DIR, today
is recorded into it and it is written back; the price file then also carries `previous`,
every figure from the last day before this one, so the app can say how far a card moved
without downloading any history. The card-to-product match is written to
dist/resolution.json either way, which is what a history backfill prices the past through.

Usage:
    python tools/pull_prices.py              # every set
    python tools/pull_prices.py --history dist/history
    python tools/pull_prices.py --sets mep base1
    python tools/pull_prices.py --offline    # from catalog/.tcgcsv only, asking nothing
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import json
import time
from pathlib import Path

import requests

import price_history
from tcgplayer import (
    CACHE, GROUPS, POKEMON, TCGCSV, card_number, finish_key, load_card_links, load_groups,
    plain_quotes, resolve, shared_groups, special_quotes, unlinked_context,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
SETS = CATALOG / "sets"
DIST = ROOT / "dist"
USER_AGENT = "Pocketful-catalog-builder/0.4 (+https://github.com/TronVonDoom/Pocketful)"

# Bumped when the shape changes in a way an older app cannot read. Tracked separately from
# the catalog's schema: the two documents version independently and always have. Adding
# `special` is not that -- an older app ignores it -- so this is still 1.
SCHEMA = 1

# TCGCSV asks for a pause between requests. A full run is about 440 of them: two per group,
# for every group, since a stamped reprint is often filed where no set is linked.
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


class Source:
    """
    TCGplayer's groups, products and prices, from TCGCSV or from the local cache.

    `--offline` exists so the matching can be checked end to end as often as it is changed,
    without spending a second of TCGCSV's daily allowance on it. What the cache lacks is
    simply absent, which under-prices a local run and never mis-prices one.
    """

    def __init__(self, offline: bool) -> None:
        self.offline = offline
        self.s = None if offline else session()

    def _cached(self, name: str) -> list[dict]:
        path = CACHE / name
        if not path.exists():
            return []
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc.get("results") or [] if isinstance(doc, dict) else doc

    def _get(self, suffix: str, name: str) -> list[dict]:
        if self.offline:
            return self._cached(name)
        doc = get_json(self.s, f"{TCGCSV}/{POKEMON}/{suffix}")
        time.sleep(PAUSE)
        return (doc or {}).get("results") or []

    def groups(self) -> list[dict]:
        return self._get("groups", "groups.json")

    def products(self, group_id: int) -> list[dict]:
        return self._get(f"{group_id}/products", f"products-{group_id}.json")

    def prices(self, group_id: int) -> list[dict]:
        return self._get(f"{group_id}/prices", f"prices-{group_id}.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", help="only these catalog set ids")
    ap.add_argument("--offline", action="store_true", help="read catalog/.tcgcsv instead of TCGCSV")
    ap.add_argument("--history", type=Path, help="price history directory to record today into")
    args = ap.parse_args()

    if not GROUPS.exists():
        raise SystemExit("No catalog/tcgplayer-groups.json. Run map_groups.py first.")
    mapping = load_groups()
    links = load_card_links()
    shared = shared_groups(mapping)
    source = Source(args.offline)

    groups = source.groups()
    if not groups:
        raise SystemExit("TCGCSV returned no groups; refusing to publish an empty price file.")
    context = unlinked_context(groups, mapping)

    # Every group, once. Product ids are unique across TCGplayer, so one quote table serves
    # all of them.
    products_of: dict[int, list[dict]] = {}
    quotes: dict[int, dict[str, int]] = {}
    unpriced: set[int] = set()
    for g in groups:
        group_id = g["groupId"]
        products_of[group_id] = source.products(group_id)
        rows = source.prices(group_id)
        if not rows:
            unpriced.add(group_id)
        for row in rows:
            market = row.get("marketPrice")
            key = finish_key(row.get("subTypeName"))
            if not market or market <= 0 or not key:
                continue
            quotes.setdefault(row["productId"], {})[key] = round(market * 100)

    by_number: dict[str, list[dict]] = {}
    for products in products_of.values():
        for product in products:
            number = card_number(product)
            if number:
                by_number.setdefault(number, []).append(product)

    def usable(product: dict) -> bool:
        # "Has a quote", so a plain product nobody has sold does not shadow a stamped one
        # that has a price. Offline, a group whose prices were never cached is taken on
        # trust instead, so the match can still be inspected; it has no quotes to publish.
        if args.offline and product.get("groupId") in unpriced:
            return True
        return product.get("productId") in quotes

    cards: dict[str, dict[str, int]] = {}
    special: dict[str, dict[str, dict[str, int]]] = {}
    matched_sets = skipped_sets = unmatched_cards = total_cards = by_hand = specials_priced = 0
    resolution: dict[str, dict[str, dict]] = {}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    previous_cards: dict[str, dict[str, int]] = {}
    previous_special: dict[str, dict[str, dict[str, int]]] = {}
    previous_dates: set[str] = set()

    for path in sorted(SETS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        set_cards = doc.get("cards") or []
        total_cards += len(set_cards)
        set_id = doc["id"]
        if args.sets and set_id not in args.sets:
            continue

        group_id = (mapping.get(set_id) or {}).get("groupId")
        ids = {c.get("id") for c in set_cards}
        linked = sum(1 for key in links if key.split("~", 1)[0] in ids)
        # A set with no group can still hold cards someone linked one at a time -- a Trainer
        # Kit card TCGplayer happens to sell under a different product line -- and cards
        # whose stamped printings are filed in some other group entirely.
        if not group_id and not linked and not any(c.get("variants_detailed") for c in set_cards):
            skipped_sets += 1
            continue

        found = 0
        set_cards_priced: dict[str, dict[str, int]] = {}
        set_special_priced: dict[str, dict[str, dict[str, int]]] = {}
        set_resolution: dict[str, dict] = {}
        for card in set_cards:
            answer = resolve(
                card, doc, mapping=mapping, links=links, shared=shared, context=context,
                products=lambda gid: products_of.get(gid) or [],
                elsewhere=lambda number: by_number.get(number) or [],
                usable=usable,
            )
            product = answer["product"]
            plain = plain_quotes(quotes.get(product["productId"]) or {}) if product else {}
            matched = {}
            if product:
                matched["product"] = product["productId"]
            if plain:
                cards[card["id"]] = plain
                set_cards_priced[card["id"]] = plain
                found += 1
                by_hand += answer["via"] == "link"
            for printing in answer["special"]:
                if not printing["product"]:
                    continue
                key = f"{printing['type']}~{printing['key']}"
                matched.setdefault("special", {})[key] = {
                    "product": printing["product"]["productId"], "via": printing["via"],
                }
                priced = special_quotes(printing, quotes.get(printing["product"]["productId"]))
                if priced:
                    special.setdefault(card["id"], {})[key] = priced
                    set_special_priced.setdefault(card["id"], {})[key] = priced
                    specials_priced += 1
            if matched:
                set_resolution[card["id"]] = matched
        resolution[set_id] = set_resolution
        if args.history:
            history = price_history.load(args.history, set_id)
            when, before_cards, before_special = price_history.previous(history, today)
            if when:
                previous_dates.add(when)
                previous_cards.update(before_cards)
                previous_special.update(before_special)
            price_history.record(history, today, set_cards_priced, set_special_priced)
            price_history.thin(history, datetime.date.fromisoformat(today))
            price_history.save(args.history, history)
        unmatched_cards += len(set_cards) - found
        matched_sets += 1
        print(f"  {set_id:12} {found}/{len(set_cards)} priced"
              + (f" ({linked} linked by hand)" if linked else ""))

    payload = {
        "schema": SCHEMA,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "tcgplayer",
        "via": "tcgcsv.com",
        "unit": "usd_cents",
        "cards": dict(sorted(cards.items())),
        "special": dict(sorted(special.items())),
    }
    if previous_dates:
        # Every figure from the last recorded day before this one. Normally that is
        # yesterday; after a missed run it is the day before the gap, which is still the
        # honest thing to measure a change against.
        payload["previous"] = {
            "date": max(previous_dates),
            "cards": dict(sorted(previous_cards.items())),
            "special": dict(sorted(previous_special.items())),
        }

    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "resolution.json").write_text(
        json.dumps({"date": today, "sets": resolution}, separators=(",", ":")), encoding="utf-8")
    out = DIST / f"prices-v{SCHEMA}.json.gz"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(out, "wb", compresslevel=9) as fh:
        fh.write(raw)

    print(
        f"\n{len(cards)} of {total_cards} cards priced across {matched_sets} sets "
        f"({skipped_sets} sets skipped, {unmatched_cards} cards unmatched, "
        f"{by_hand} priced from a link made by hand), plus {specials_priced} special printings"
    )
    print(f"  {out.name:28} {len(raw) / 1048576:6.2f} MiB raw"
          f"  ->{out.stat().st_size / 1048576:6.2f} MiB gzipped")


if __name__ == "__main__":
    main()
