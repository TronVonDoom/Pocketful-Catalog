#!/usr/bin/env python3
"""
Finds artwork for the cards TCGdex has none for.

Roughly 7% of the catalog -- around 1,700 cards across 67 sets -- carries no image at
all: whole Trainer Kits, Shining Fates' Shiny Vault, Crown Zenith's Galarian Gallery,
the McDonald's sets, Ancient Mew. This is not a fetching bug and no amount of retrying
fixes it. TCGdex derives a card's image from whether the asset exists on their CDN,
and for these cards it does not, in any language.

Three tiers, best first, because no single source covers them all:

  1. pokemontcg.io      Proper scans. Covers the big whole-set holes -- Shiny Vault,
                        Crown Zenith Gallery, Shining Legends, the SM promos. Its set
                        ids differ from TCGdex's ("sma" vs "swsh4.5sv"), which is what
                        SET_ALIASES is for. Flaky under load, hence the retries.
  2. TCGplayer product  A product photo, keyed by the id TCGdex hands out in
                        variants_detailed[].thirdParty.tcgplayer. Lower fidelity than a
                        scan but real, and it reaches the oddities the card databases
                        never filed -- Ancient Mew has no pokemontcg.io entry at all.
                        The ids are REST-only, so they are fetched per holed card --
                        only for the cards that actually need one.
  3. Nothing            Recorded as a hole with a reason, and drawn as a real "no art"
                        placeholder rather than an empty pocket. Mostly Trainer Kits,
                        which are not sold as singles so no product photo exists.

Writes `imageAlt` beside `image` rather than filling `image` in. Keeping them apart
means a later TCGdex pull that *does* have the art wins automatically, and nobody has
to remember which stems were invented here.

Nothing is downloaded. This resolves URLs and records where the art lives; fetching
and re-hosting it is pack.py's job and a separate decision.

Usage:
    python fill_gaps.py                 # every set with a hole
    python fill_gaps.py --sets miscp    # named sets
    python fill_gaps.py --tier2-only    # skip pokemontcg.io
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"

PTCGIO = "https://api.pokemontcg.io/v2"
TCGDEX = "https://api.tcgdex.net/v2"
# 874x874 rather than the 437x437 this used to ask for.
#
# These are a resizer's bounding box, not a stored size: a card fits it as ~620x874, which
# is slightly larger than TCGdex's own high.png at 600x825. The 437 box produced ~310x437,
# roughly half the linear resolution of every other card in the catalog -- fine in a
# 132px grid tile and visibly soft the moment anyone opened one full size. A filled card
# should be indistinguishable from a native one at any size the app draws.
TCGPLAYER_IMAGE = "https://product-images.tcgplayer.com/fit-in/874x874/{}.jpg"

USER_AGENT = "Pocketful-catalog-builder/0.2 (+https://github.com/TronVonDoom/Pocketful)"
WORKERS = 4

# TCGdex set id -> pokemontcg.io set id, where they disagree.
#
# Hand-checked rather than derived. A name-and-count match gets most of these right and
# gets a few catastrophically wrong, and a wrong mapping does not fail loudly -- it
# quietly puts another card's picture on this card, which is worse than a blank pocket
# and much harder to notice. Add to this table only after looking at both sets.
SET_ALIASES = {
    "swsh4.5sv": "sma",          # Shining Fates Shiny Vault
    "sm3.5": "sm35",             # Shining Legends
    "swsh12.5gg": "swsh12pt5gg",  # Crown Zenith Galarian Gallery
    "sm7.5": "sm75",             # Dragon Majesty
    "swsh9tg": "swsh9tg",        # Brilliant Stars Trainer Gallery
    "smp": "smp",                # SM Black Star Promos
    "svp": "svp",                # SVP Black Star Promos
}


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get_json(s: requests.Session, url: str, tries: int = 5, timeout: int = 30):
    """
    GET with backoff.

    pokemontcg.io answers 500 and 502 under load often enough that a single attempt is
    not a measurement of whether a card exists there. Five tries with widening gaps is
    the difference between "this card has no scan" and "I asked at a bad moment", and
    those two must not be confused -- one of them gets written down as a permanent hole.
    """
    for attempt in range(tries):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1.2 * (attempt + 1))
    return None


def head_ok(s: requests.Session, url: str, tries: int = 3) -> bool:
    """Whether a URL actually serves an image, rather than a 404 or a placeholder."""
    for attempt in range(tries):
        try:
            r = s.head(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                # A few hundred bytes is an error page or a spacer, not a card.
                return int(r.headers.get("Content-Length") or 0) > 3000
            if r.status_code == 404:
                return False
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    return False


def ptcgio_set(s: requests.Session, set_id: str) -> dict[str, str]:
    """Every card in one pokemontcg.io set, as {number: image url}."""
    data = get_json(s, f"{PTCGIO}/cards?q=set.id:{set_id}&pageSize=250")
    if not data:
        return {}
    out = {}
    for card in data.get("data", []):
        image = (card.get("images") or {}).get("large") or (card.get("images") or {}).get("small")
        if image:
            out[str(card.get("number", "")).strip()] = image
    return out


def number_keys(local_id: str) -> list[str]:
    """
    The forms one printed number might take on the other side.

    TCGdex zero-pads where pokemontcg.io does not -- "SV001" against "SV1", "004"
    against "4" -- so a direct key lookup misses most of the cards it should hit.
    """
    raw = str(local_id).strip()
    keys = [raw]
    digits = "".join(ch for ch in raw if ch.isdigit())
    letters = "".join(ch for ch in raw if not ch.isdigit())
    if digits:
        stripped = digits.lstrip("0") or "0"
        keys += [stripped, f"{letters}{stripped}", digits]
    return list(dict.fromkeys(k for k in keys if k))


def tcgplayer_ids(s: requests.Session, card_ids: list[str]) -> dict[str, list[int]]:
    """
    TCGplayer product ids for the cards named, keyed by card id.

    Fetched per card, and only for cards that actually have a hole. The ids live only
    on the REST card document -- GraphQL exposes neither `pricing` nor `thirdParty` --
    so this is the one place in the repository that pays the REST cost, and it pays it
    for seventeen hundred cards rather than twenty-three thousand.

    A set with two missing images costs two requests.
    """

    def one(card_id: str) -> tuple[str, list[int]]:
        card = get_json(s, f"{TCGDEX}/en/cards/{card_id}", tries=3)
        if not card:
            return card_id, []
        return card_id, sorted({
            v["thirdParty"]["tcgplayer"]
            for v in (card.get("variants_detailed") or [])
            if isinstance(v, dict) and (v.get("thirdParty") or {}).get("tcgplayer")
        })

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return {cid: ids for cid, ids in pool.map(one, card_ids) if ids}


def fill_set(s: requests.Session, path: Path, skip_tier1: bool) -> tuple[int, int, int]:
    """Resolves one set's holes in place. Returns (tier1, tier2, still missing)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    set_id = doc["id"]
    holes = [c for c in doc.get("cards", []) if not c.get("image") and not c.get("imageAlt")]
    if not holes:
        return 0, 0, 0

    tier1 = tier2 = 0

    scans: dict[str, str] = {}
    if not skip_tier1:
        scans = ptcgio_set(s, SET_ALIASES.get(set_id, set_id))

    products = tcgplayer_ids(s, [c["id"] for c in holes if not c.get("imageAlt")])

    for card in holes:
        for key in number_keys(card.get("localId", "")):
            if key in scans:
                card["imageAlt"] = scans[key]
                card["imageAltSource"] = "pokemontcg.io"
                tier1 += 1
                break
        if card.get("imageAlt"):
            continue

        for product in products.get(card["id"], []):
            url = TCGPLAYER_IMAGE.format(product)
            if head_ok(s, url):
                card["imageAlt"] = url
                card["imageAltSource"] = "tcgplayer"
                tier2 += 1
                break

    still = sum(1 for c in holes if not c.get("imageAlt"))
    if tier1 or tier2:
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return tier1, tier2, still


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", help="explicit set ids")
    ap.add_argument("--tier2-only", action="store_true", help="skip pokemontcg.io")
    args = ap.parse_args()

    directory = CATALOG / "sets"
    if not directory.is_dir():
        sys.exit(f"No catalog at {directory}. Run pull_catalog.py --static first.")

    paths = (
        [directory / f"{sid}.json" for sid in args.sets]
        if args.sets
        else sorted(directory.glob("*.json"))
    )
    paths = [p for p in paths if p.exists()]

    holed = []
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if any(not c.get("image") and not c.get("imageAlt") for c in doc.get("cards", [])):
            holed.append(p)

    if not holed:
        print("No unfilled holes.")
        return

    print(f"{len(holed)} sets with holes. Resolving...")
    s = session()
    totals = [0, 0, 0]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for path, (a, b, c) in zip(holed, pool.map(lambda p: fill_set(s, p, args.tier2_only), holed)):
            totals[0] += a
            totals[1] += b
            totals[2] += c
            if a or b or c:
                print(f"  {path.stem:12} pokemontcg.io {a:4}   tcgplayer {b:4}   unfilled {c:4}")

    print(f"\npokemontcg.io {totals[0]}   tcgplayer {totals[1]}   still no art {totals[2]}")
    print("Run audit.py --accept to record what is left as known holes, then write reasons.")


if __name__ == "__main__":
    main()
