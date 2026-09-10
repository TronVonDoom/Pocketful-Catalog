#!/usr/bin/env python3
"""
Packs the built catalog into the files the app downloads.

The tree under catalog/ is one document per set, which is the right shape for a human
reading a diff and the wrong shape for a phone: two hundred and eighteen requests to
open a browse screen is the problem this whole repository exists to remove. So a pack
is one gzipped document per half, versioned, and small enough to be unremarkable.

  dist/catalog-v<schema>.json.gz   The immutable half. Downloaded once and kept.
  dist/prices-v<schema>.json.gz    The volatile half. Downloaded on a TTL.

`schema` is in the filename rather than only inside the document because the app has to
decide whether it can read a file *before* parsing it, and because a URL that stops
meaning what it meant is worse than one that 404s. An app built for v1 asks for v1
forever; publishing v2 does not reach back and break installs that predate it.

Usage:
    python pack.py            # both halves
    python pack.py --static   # just the catalog
    python pack.py --prices   # just the prices
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
DIST = ROOT / "dist"

# Bumped when the shape changes in a way an older app cannot read. Adding a field is
# not that; removing or re-meaning one is.
SCHEMA = 1

# What the app actually reads off a card. The tree keeps more than this -- attacks,
# abilities, weaknesses, resistances, the detailed variant breakdown -- because it costs
# nothing on disk and re-fetching it later would cost thousands of requests. It is
# dropped here because a phone downloading a card-effect corpus it never renders is
# paying for the privilege.
CARD_FIELDS = (
    "id", "localId", "name", "rarity", "illustrator", "category",
    "image", "imageAlt", "imageAltSource", "variants",
)

SET_FIELDS = (
    "id", "name", "logo", "symbol", "releaseDate", "cardCount", "serie", "abbreviation",
)


def write_gz(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separators without spaces, because this is read by a parser and not a person, and
    # the whitespace is a measurable fraction of a twenty-megabyte document.
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as fh:
        fh.write(raw)
    print(f"  {path.name:28} {len(raw) / 1048576:6.2f} MiB raw"
          f"  ->{path.stat().st_size / 1048576:6.2f} MiB gzipped")


def pack_static() -> None:
    index_path = CATALOG / "index.json"
    if not index_path.exists():
        raise SystemExit("No catalog/index.json. Run pull_catalog.py --static first.")

    series: dict[str, dict] = {}
    sets = []
    holes = 0
    filled = 0

    for path in sorted((CATALOG / "sets").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        cards = []
        for card in doc.get("cards") or []:
            trimmed = {k: card[k] for k in CARD_FIELDS if card.get(k) is not None}
            if not card.get("image"):
                if card.get("imageAlt"):
                    filled += 1
                else:
                    holes += 1
            cards.append(trimmed)

        entry = {k: doc[k] for k in SET_FIELDS if doc.get(k) is not None}
        entry["cards"] = cards
        sets.append(entry)

        serie = doc.get("serie") or {}
        if serie.get("id"):
            series.setdefault(serie["id"], {"id": serie["id"], "name": serie.get("name")})

    payload = {
        "schema": SCHEMA,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "series": sorted(series.values(), key=lambda s: s["id"]),
        "sets": sets,
    }
    total = sum(len(s["cards"]) for s in sets)
    print(f"{len(sets)} sets, {total} cards, {filled} filled from a second source, "
          f"{holes} still without art")
    write_gz(DIST / f"catalog-v{SCHEMA}.json.gz", payload)


def pack_prices() -> None:
    directory = CATALOG / "prices"
    if not directory.is_dir():
        raise SystemExit("No catalog/prices. Run pull_catalog.py --prices first.")

    quotes: dict[str, dict] = {}
    oldest = None
    for path in sorted(directory.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        stamp = doc.get("fetchedAt")
        if stamp and (oldest is None or stamp < oldest):
            oldest = stamp
        for card in doc.get("cards") or []:
            if card.get("usd_cents"):
                quotes[card["id"]] = card["usd_cents"]

    payload = {
        "schema": SCHEMA,
        # The *oldest* stamp in the pack, not the newest. A document is only as fresh as
        # its stalest row, and reporting the newest would let one set pulled a minute ago
        # vouch for two hundred pulled last week.
        "fetchedAt": oldest or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "tcgplayer",
        "unit": "usd_cents",
        "cards": quotes,
    }
    print(f"{len(quotes)} priced cards, oldest quote {oldest}")
    write_gz(DIST / f"prices-v{SCHEMA}.json.gz", payload)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--prices", action="store_true")
    args = ap.parse_args()
    both = not (args.static or args.prices)

    if args.static or both:
        pack_static()
    if args.prices or both:
        pack_prices()


if __name__ == "__main__":
    main()
