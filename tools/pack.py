#!/usr/bin/env python3
"""
Packs the built catalog into the files the app downloads.

The tree under catalog/ is one document per set, which is the right shape for a human
reading a diff and the wrong shape for a phone: two hundred and eighteen requests to
open a browse screen is the problem this whole repository exists to remove. So a pack
is one gzipped document, versioned, and small enough to be unremarkable.

  dist/catalog-v<schema>.json.gz   Every card, without prices. Downloaded once and kept.

Prices are not packed here and never will be. This repository catalogs what a card *is*,
and what a card is was fixed the day it was printed; what a card is *worth* changes every
day and belongs to whoever is quoting it. Mixing the two would give the immutable half an
expiry date it has no reason to have, so the app reads prices live from the source
instead.

`schema` is in the filename rather than only inside the document because the app has to
decide whether it can read a file *before* parsing it, and because a URL that stops
meaning what it meant is worse than one that 404s. An app built for v1 asks for v1
forever; publishing v2 does not reach back and break installs that predate it.

Usage:
    python pack.py            # the catalog
    python pack.py --static   # the same thing, said explicitly
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

import variants

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
DIST = ROOT / "dist"
OVERRIDES = CATALOG / "overrides.json"

# Bumped when the shape changes in a way an older app cannot read. Adding a field is
# not that; removing or re-meaning one is.
SCHEMA = 1

# What the app actually reads off a card.
#
# The test for membership here is not "is it static" -- everything in the tree is static
# -- but "does the app draw it". The tree keeps far more: attacks, abilities, weaknesses,
# resistances, retreat cost, the detailed variant breakdown. Those stay in the repository
# where they cost nothing, because a phone downloading a card-effect corpus it never
# renders is paying for the privilege, and because having them on disk means adding one
# to a future build is an edit here rather than twenty-three thousand requests.
#
# hp, types and description are in the list because the app genuinely renders all three
# -- the type chip on every collection row comes from `types` -- and until they were
# packed the app had to fetch a live card document to draw them. That made adding a card
# to a binder a network round trip for data already sitting on the device, and made it
# fail outright with no connection.
#
# `special` is not in this list because it is not a field of the pulled card: it is derived
# from `variants_detailed` by variants.py and written after the override, so a correction
# to a card cannot quietly delete the stamped printings beside it.
CARD_FIELDS = (
    "id", "localId", "name", "rarity", "illustrator", "category",
    "image", "imageAlt", "imageAltSource", "variants",
    "hp", "types", "description",
)

SET_FIELDS = (
    "id", "name", "logo", "symbol", "releaseDate", "cardCount", "serie", "abbreviation",
)

# The part of a set a person can correct in the editor. Keep in step with SET_EDITABLE in
# editor/server.py. `cardCount`, `serie` and `abbreviation` are structure the pull derives
# and the app joins on, not wording, so they are not open to a hand edit.
SET_OVERRIDABLE = ("name", "logo", "symbol", "releaseDate")


def write_gz(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separators without spaces, because this is read by a parser and not a person, and
    # the whitespace is a measurable fraction of a twenty-megabyte document.
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as fh:
        fh.write(raw)
    print(f"  {path.name:28} {len(raw) / 1048576:6.2f} MiB raw"
          f"  ->{path.stat().st_size / 1048576:6.2f} MiB gzipped")


def load_overrides() -> tuple[dict[str, dict], dict[str, dict]]:
    """
    Hand corrections: (cards by card id, sets by set id).

    These exist because `catalog/sets/` is output, not source: the weekly refresh re-pulls
    every set from upstream, so anything typed into those files is overwritten without a
    word. Corrections therefore live in their own document and are laid over the pull
    here, on the way into the shipped file -- which keeps the pull an honest copy of
    upstream and the override an explicit statement that upstream is wrong.

    A field overridden to null is a correction too -- "this card's TCGdex art is the wrong
    picture" -- and is removed from the shipped record rather than written as null.

    Written by editor/server.py. Nothing stops it being edited by hand.
    """
    if not OVERRIDES.exists():
        return {}, {}
    doc = json.loads(OVERRIDES.read_text(encoding="utf-8"))

    def fields(section: str) -> dict[str, dict]:
        return {
            key: entry.get("fields") or {}
            for key, entry in (doc.get(section) or {}).items()
            if entry.get("fields")
        }

    return fields("cards"), fields("sets")


def pack_static() -> None:
    index_path = CATALOG / "index.json"
    if not index_path.exists():
        raise SystemExit("No catalog/index.json. Run pull_catalog.py --static first.")

    overrides, set_overrides = load_overrides()
    applied = 0
    sets_applied = 0

    series: dict[str, dict] = {}
    sets = []
    holes = 0
    filled = 0
    special = 0

    for path in sorted((CATALOG / "sets").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        cards = []
        for card in doc.get("cards") or []:
            trimmed = {k: card[k] for k in CARD_FIELDS if card.get(k) is not None}

            # Laid over the trimmed record rather than the pulled one, so an override can
            # only ever change a field the app actually reads. An override on something
            # outside CARD_FIELDS is a no-op by construction instead of a silent edit to
            # a field that gets dropped two lines later.
            patch = overrides.get(card.get("id"))
            if patch:
                kept = {k: v for k, v in patch.items() if k in CARD_FIELDS}
                if kept:
                    trimmed.update(kept)
                    trimmed = {k: v for k, v in trimmed.items() if v is not None}
                    applied += 1

            # The stamped and pattern printings beside the plain ones: MEP Tyrunt's Pokemon
            # Center stamp, a Prismatic Evolutions common's Poke Ball reverse. The app files
            # and prices each as a variant of its own. See variants.py.
            printings = variants.shipped(card)
            if printings:
                trimmed["special"] = printings
                special += len(printings)

            # Counted after the override, because filling a hole by hand is one of the
            # main things an override is for and a report that ignored them would go on
            # describing gaps that are no longer there.
            if not trimmed.get("image"):
                if trimmed.get("imageAlt"):
                    filled += 1
                else:
                    holes += 1
            cards.append(trimmed)

        entry = {k: doc[k] for k in SET_FIELDS if doc.get(k) is not None}
        set_patch = {k: v for k, v in (set_overrides.get(doc.get("id")) or {}).items()
                     if k in SET_OVERRIDABLE}
        if set_patch:
            entry.update(set_patch)
            entry = {k: v for k, v in entry.items() if v is not None}
            sets_applied += 1
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
          f"{holes} still without art, {special} special printings")
    if overrides:
        stale = len(overrides) - applied
        note = f", {stale} matching no card in the catalog" if stale else ""
        print(f"{applied} hand overrides applied{note}")
    if set_overrides:
        stale = len(set_overrides) - sets_applied
        note = f", {stale} matching no set in the catalog" if stale else ""
        print(f"{sets_applied} set overrides applied{note}")
    write_gz(DIST / f"catalog-v{SCHEMA}.json.gz", payload)


def main() -> None:
    ap = argparse.ArgumentParser()
    # Kept as a no-op flag rather than removed, so the workflows and the muscle memory
    # that say `pack.py --static` keep working now that there is only one half to pack.
    ap.add_argument("--static", action="store_true", help="the only half there is")
    ap.parse_args()
    pack_static()


if __name__ == "__main__":
    main()
