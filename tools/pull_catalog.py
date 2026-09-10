#!/usr/bin/env python3
"""
Builds the local card catalog from TCGdex.

Why this exists: the app currently asks the network every question. A set has not
changed since the day it was printed, so re-fetching Base Set is paying a round trip
for an answer that was already true in 1999. This pulls each set once and writes it
to disk in the shape the app reads.

Two files come out per set, and the split is the whole point:

  sets/<id>.json    Static. Name, number, rarity, art stem, variants, attacks.
                    Immutable once a set is released, so it ships with the app and
                    never expires.
  prices/<id>.json  Volatile. Market prices, stamped with the hour they were taken.
                    Never bundled -- it would be stale before the APK finished
                    uploading. Refreshed on a schedule and published separately.

Mixing the two into one document is the mistake this layout is built to avoid: it
would make the immutable half expire at the speed of the volatile half.

The two halves also cost wildly different amounts to fetch, which is why they are
separate commands rather than one pass:

  --static   GraphQL, 40 cards per POST. The whole catalog is ~590 requests.
  --prices   REST, one request per card. The whole catalog is ~23,500 requests.

That asymmetry is not a choice. TCGdex's GraphQL schema exposes `image`, `rarity`
and `variants`, but carries no `pricing` field and no `thirdParty` ids -- both are
REST-only. So static data is cheap to refresh and prices are not, which is exactly
backwards from how often each one changes, and is the reason prices get their own
schedule instead of riding along with a static pull.

Running --prices daily and serving the result to users is strictly kinder to TCGdex
than the app asking them directly: one client with a User-Agent and a backoff, once
a day, instead of one request per card per user per sync.

Usage:
    python pull_catalog.py --static --all           # every set, static half
    python pull_catalog.py --static --sets base1    # named sets
    python pull_catalog.py --prices --all           # every set, prices half
    python pull_catalog.py --static --first 3       # first N by release date
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from urllib.parse import quote

BASE = "https://api.tcgdex.net/v2"
LANG = "en"

# Six at a time, matching the ceiling CatalogSync already uses against this host.
# Unbounded is a rate limit; serial is an afternoon.
WORKERS = 6

# How many cards go into one GraphQL POST. Big enough that the catalog is hundreds of
# requests rather than tens of thousands, small enough that one dropped batch costs a
# handful of cards and one response stays a readable size.
BATCH = 40

# Characters that could close a GraphQL string literal, and therefore the only ones a
# card id may not contain. Everything else -- "!", "%", "?" -- is inert inside quotes.
LITERAL_BREAKERS = frozenset(['\t', '\n', '\r', '"', '\\'])

OUT = Path(__file__).resolve().parent.parent / "catalog"

# The app introduces itself the same way. TCGdex needs no API key, so this header is
# the only thing telling them who is calling and where to complain.
USER_AGENT = "Pocketful-catalog-builder/0.2 (+https://github.com/TronVonDoom/Pocketful)"

# Fields that describe the card as printed, as GraphQL selects them. Everything here is
# fixed at print time. `variants` matters more than it looks: it is what a master-set
# binder is built from, and fetching it per-card at runtime is the single most expensive
# thing the app does.
STATIC_SELECTION = """
    id localId name rarity illustrator category image hp types stage evolveFrom
    description retreat suffix regulationMark dexId trainerType energyType effect level
    variants { normal holo reverse firstEdition wPromo }
    variants_detailed { type subtype size stamp foil }
    attacks { name cost damage effect }
    abilities { type name effect }
    weaknesses { type value }
    resistances { type value }
"""

# What the REST fallback keeps. Deliberately excludes `pricing` and the third-party ids,
# which belong to the volatile half and must not leak into a file that never expires.
REST_STATIC_KEYS = (
    "id", "localId", "name", "rarity", "illustrator", "category", "image", "hp", "types",
    "stage", "evolveFrom", "description", "retreat", "suffix", "regulationMark", "dexId",
    "trainerType", "energyType", "effect", "level", "variants", "attacks", "abilities",
    "weaknesses", "resistances",
)

SET_INDEX_QUERY = """
{
  sets {
    id name logo symbol releaseDate
    cardCount { official total }
    serie { id name }
  }
}
"""


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get_json(s: requests.Session, url: str, tries: int = 4):
    """GET with backoff. The API answers 503 under load often enough to matter."""
    for attempt in range(tries):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def post_graphql(s: requests.Session, query: str, tries: int = 4):
    """
    POST with backoff, returning `data` or None.

    GraphQL answers 200 with its errors inside the body, so a missing or empty `data`
    is the failure and the status code is not.
    """
    for attempt in range(tries):
        try:
            r = s.post(f"{BASE}/graphql", json={"query": query}, timeout=45)
            if r.status_code == 200:
                body = r.json()
                if body.get("data"):
                    return body["data"]
                if body.get("errors"):
                    # A malformed query fails identically every time; retrying it just
                    # spends someone else's capacity to be told the same thing again.
                    print(
                        f"  !! GraphQL refused the query: "
                        f"{json.dumps(body['errors'])[:200]}",
                        file=sys.stderr,
                    )
                    return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def set_index(s: requests.Session) -> list:
    """Every set, dated and filed under its era. Only GraphQL carries both fields."""
    data = post_graphql(s, SET_INDEX_QUERY)
    if not data or not data.get("sets"):
        raise SystemExit("Could not reach the set index.")
    return data["sets"]


def prune(value):
    """
    Drops nulls and empties, recursively.

    GraphQL returns every field it was asked for, so an unasked-for attack list arrives
    as `null` and an absent type list as `[]`. Writing those out would roughly double
    the catalog with fields that mean "no".
    """
    if isinstance(value, dict):
        cleaned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        cleaned = [prune(v) for v in value]
        return [v for v in cleaned if v not in (None, [], {}, "")]
    return value


def static_batch(s: requests.Session, ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """
    One POST, one aliased `card` query per id.

    Returns the cards that answered *as themselves*, and the ids that did not, which the
    caller re-fetches over REST.

    That second half is not defensive programming, it is a bug this pull already shipped
    once. TCGdex's GraphQL `card(id:)` matches loosely: asked for `xy8-146` it returns
    `xy8-146a`, a different card that happens to share a name. Both exist upstream, both
    are listed in the set, and REST tells them apart correctly -- so a batch that trusted
    the answer wrote `xy8-146a` twice, dropped the real `xy8-146`, and then reported the
    artwork `xy8-146` actually has as a hole. Wrong data that looks right is the worst
    thing this tool can produce, so every row is checked against what was asked for.

    Ids are matched by alias rather than by scanning the response, because the alias is
    the only thing that records what was requested; the row's own `id` is precisely the
    field that cannot be trusted here.
    """
    # These ids are interpolated into a GraphQL string literal unescaped, so the only
    # thing that actually matters is that none of them can close that literal: a quote,
    # a backslash, or a line break.
    #
    # An allowlist of alphanumerics plus "-._" was the obvious way to write this and it
    # was wrong. The EX-era Unown collection numbers two of its cards "!" and "%3F", and
    # both are perfectly safe inside a literal -- excluding them did not prevent an
    # injection, it silently dropped two real cards and then reported the set as short.
    # The app's TcgDex.variantsInSet still filters the same over-strict way, which is why
    # those two Unown have never had a variants lookup.
    safe = [c for c in ids if c and not (set(c) & LITERAL_BREAKERS)]
    unsafe = [c for c in ids if c not in safe]
    if not safe:
        return {}, unsafe

    query = "{ " + " ".join(
        f'c{i}: card(id: "{cid}") {{ {STATIC_SELECTION} }}' for i, cid in enumerate(safe)
    ) + " }"
    data = post_graphql(s, query)
    if not data:
        return {}, ids

    cards: dict[str, dict] = {}
    rejected: list[str] = list(unsafe)
    for i, cid in enumerate(safe):
        row = data.get(f"c{i}")
        if row and row.get("id") == cid:
            cards[cid] = prune(row)
        else:
            rejected.append(cid)
    return cards, rejected


def static_via_rest(s: requests.Session, card_id: str) -> dict | None:
    """
    One card, over REST, trimmed to the same shape the batch produces.

    The slow path, for the handful of cards GraphQL will not answer honestly. REST
    carries more than the static half wants -- prices, third-party ids, the detailed
    variant breakdown with its own pricing -- so it is cut down here rather than letting
    two code paths write two different shapes into the same file.
    """
    # Percent-encoded rather than pasted in raw. One Unown's id is literally the string
    # "exu-%3F"; sent unencoded the server decodes the %3F back into a "?", which ends
    # the path and starts a query string, and the request 404s on a card that exists.
    card = get_json(s, f"{BASE}/{LANG}/cards/{quote(card_id, safe='-._~')}")
    if not card or card.get("id") != card_id:
        return None
    kept = {k: card[k] for k in REST_STATIC_KEYS if card.get(k) is not None}
    # variants_detailed over REST carries thirdParty and pricing; GraphQL's does not.
    # Matching the narrower shape keeps the file uniform and keeps volatile data out of
    # the half that is supposed to never expire.
    if card.get("variants_detailed"):
        kept["variants_detailed"] = [
            {k: v[k] for k in ("type", "subtype", "size", "stamp", "foil") if v.get(k)}
            for v in card["variants_detailed"]
            if isinstance(v, dict)
        ]
    return prune(kept)


def pull_static(s: requests.Session, set_id: str):
    """One set's immutable half, batched over GraphQL."""
    detail = get_json(s, f"{BASE}/{LANG}/sets/{set_id}")
    if not detail:
        print(f"  !! {set_id}: set document unavailable", file=sys.stderr)
        return None

    briefs = detail.get("cards") or []
    ids = [b["id"] for b in briefs]

    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda chunk: static_batch(s, chunk), batches))

    by_id: dict[str, dict] = {}
    stragglers: list[str] = []
    for got, rejected in results:
        by_id.update(got)
        stragglers.extend(rejected)

    # Everything GraphQL would not answer for, or answered wrongly, fetched one at a
    # time. A whole batch that failed lands here too, which is why a flaky minute now
    # costs a set some extra requests rather than costing it a hundred and eighty cards.
    if stragglers:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for card_id, card in zip(stragglers, pool.map(lambda c: static_via_rest(s, c), stragglers)):
                if card:
                    by_id[card_id] = card

    cards = list(by_id.values())

    missing = [c for c in ids if c not in by_id]
    if missing:
        print(
            f"  !! {set_id}: {len(missing)} cards did not answer, over either transport: "
            f"{missing[:5]}",
            file=sys.stderr,
        )

    # Sorted by printed number so the file reads like the set does. localId is not
    # always numeric (promos, "TG01", "SV049"), so sort numerically where possible
    # and lexically otherwise rather than crashing on the exceptions.
    def order(c):
        raw = str(c.get("localId", ""))
        digits = "".join(ch for ch in raw if ch.isdigit())
        return (0, int(digits), raw) if digits else (1, 0, raw)

    cards.sort(key=order)

    return {
        "id": detail["id"],
        "name": detail["name"],
        "serie": detail.get("serie"),
        "releaseDate": detail.get("releaseDate"),
        "cardCount": detail.get("cardCount"),
        "logo": detail.get("logo"),
        "symbol": detail.get("symbol"),
        "abbreviation": detail.get("abbreviation"),
        "legal": detail.get("legal"),
        "cards": cards,
    }


def card_prices(card: dict) -> dict | None:
    """
    One card's TCGplayer quotes, in cents.

    Only TCGplayer is read. The same document carries Cardmarket figures, but those are
    in euros, and the app renders one currency symbol -- quietly filing a EUR number
    under a "$" would be worse than showing no price at all.
    """
    pricing = card.get("pricing") or {}
    tcg = pricing.get("tcgplayer") or {}
    prices = {}
    for finish, quote in tcg.items():
        # 'unit' and 'updated' sit alongside the finishes rather than inside them.
        if not isinstance(quote, dict):
            continue
        market = quote.get("marketPrice")
        if market:
            prices[finish] = round(market * 100)  # cents, matching domain.Money

    # The TCGplayer product id behind each variant. Not a price, but it only ever
    # arrives on this REST document, and it is the one handle on a card whose artwork
    # TCGdex does not have -- so it is captured here rather than paid for again later.
    products = sorted({
        v["thirdParty"]["tcgplayer"]
        for v in (card.get("variants_detailed") or [])
        if isinstance(v, dict) and (v.get("thirdParty") or {}).get("tcgplayer")
    })

    if not prices and not products:
        return None
    out = {"id": card["id"]}
    if prices:
        out["usd_cents"] = prices
    if products:
        out["tcgplayer"] = products
    return out


def pull_prices(s: requests.Session, set_id: str):
    """One set's volatile half. REST, one request per card -- there is no batched path."""
    detail = get_json(s, f"{BASE}/{LANG}/sets/{set_id}")
    if not detail:
        print(f"  !! {set_id}: set document unavailable", file=sys.stderr)
        return None

    briefs = detail.get("cards") or []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        full = list(pool.map(lambda b: get_json(s, f"{BASE}/{LANG}/cards/{b['id']}"), briefs))

    prices = [p for card in full if card for p in [card_prices(card)] if p]

    return {
        "setId": detail["id"],
        "source": "tcgplayer",
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cards": prices,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    half = ap.add_mutually_exclusive_group(required=True)
    half.add_argument("--static", action="store_true", help="the immutable half (GraphQL)")
    half.add_argument("--prices", action="store_true", help="the volatile half (REST)")

    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--sets", nargs="+", help="explicit set ids")
    which.add_argument("--first", type=int, help="first N sets by release date")
    which.add_argument("--all", action="store_true")
    args = ap.parse_args()

    s = session()
    index = set_index(s)
    dated = sorted(
        (x for x in index if x.get("releaseDate")),
        key=lambda x: (x["releaseDate"], x["name"]),
    )

    if args.sets:
        wanted = args.sets
    elif args.first:
        wanted = [x["id"] for x in dated[: args.first]]
    else:
        wanted = [x["id"] for x in dated]

    (OUT / "sets").mkdir(parents=True, exist_ok=True)
    (OUT / "prices").mkdir(parents=True, exist_ok=True)

    # The index itself is worth writing: it is what the browse screen opens on, and
    # it is the one document that legitimately changes when a new set is announced.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (OUT / "index.json").write_text(
        json.dumps({"fetchedAt": stamp, "sets": dated}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )

    started = time.time()
    for n, set_id in enumerate(wanted, 1):
        print(f"[{n}/{len(wanted)}] {set_id} ...", end=" ", flush=True)
        if args.static:
            doc = pull_static(s, set_id)
            if not doc:
                continue
            (OUT / "sets" / f"{set_id}.json").write_text(
                json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
            art = sum(1 for c in doc["cards"] if c.get("image"))
            print(f"{len(doc['cards'])} cards, {art} with art")
        else:
            doc = pull_prices(s, set_id)
            if not doc:
                continue
            (OUT / "prices" / f"{set_id}.json").write_text(
                json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"{len(doc['cards'])} priced")

    print(f"\nDone in {time.time() - started:.0f}s. Now run audit.py.")


if __name__ == "__main__":
    main()
