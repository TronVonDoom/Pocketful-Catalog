#!/usr/bin/env python3
"""
Reads one era of the catalog the way a person would check it.

audit.py answers "is anything missing that nobody signed off on". This answers the two
questions that come after: **is every card's picture as good as every other card's**,
and **does every card carry the same kind of information as its neighbours**.

Neither is a hole, so neither fails the audit, and both are exactly what someone
notices when they open a binder. A card filled from a low-resolution source looks soft
next to the one beside it. A card with no illustrator sitting among fifty that have one
is not missing artwork -- it is missing a line the app draws.

Resolution is judged from the URL, offline, because every source here serves one
predictable rendition:

    TCGdex      <stem>/high.png             600x825   the reference
    pokemontcg  <id>_hires.png              734x1024  larger
    TCGplayer   fit-in/874x874/<id>.jpg    ~620x874   larger
    TCGplayer   fit-in/437x437/<id>.jpg    ~310x437   half-size, not acceptable

Field consistency is judged within a category rather than across the set, because a
Trainer having no `hp` is not a gap and a Pokemon having none is. A field carried by
most of a category and missing from a few is the signal worth reading; a field carried
by none of it is simply not applicable.

Usage:
    python series_report.py                       # every era, oldest first
    python series_report.py --serie base          # one era
    python series_report.py --verbose             # name the offending cards
    python series_report.py --check-upstream 20   # ask TCGdex: did we drop it, or do
                                                  # they not have it either?
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"

USER_AGENT = "Pocketful-catalog-builder/0.2 (+https://github.com/TronVonDoom/Pocketful)"

# How a stored image URL maps to the rendition it will serve, and whether that is good
# enough to sit beside a native TCGdex card.
RENDITIONS = [
    ("_hires.png", "pokemontcg 734x1024", True),
    ("fit-in/874x874", "tcgplayer ~620x874", True),
    ("fit-in/437x437", "tcgplayer ~310x437", False),
]

# A field is worth reporting as inconsistent only if most of its category carries it.
# Below this it is not a gap, it is a field that category does not use.
EXPECTED_AT = 0.80

# Fields a card may carry. Checked per category, so the report says "17 of 102 Pokemon
# have no illustrator" rather than counting Trainers that were never going to have one.
# `image` is deliberately absent. Artwork is already answered above, by rendition(),
# which knows that a card carrying `imageAlt` has a picture -- and a plain `image` check
# here does not, so the two halves of one report disagreed about the same cards. pop6
# read "2 of 17 Pokemon carry no image" beside a rendition line saying both were filled
# at 620x874. A report that contradicts itself is worse than one that says less.
FIELDS = (
    "rarity", "illustrator", "localId", "name", "category",
    "hp", "types", "stage", "description", "retreat", "variants",
    "attacks", "weaknesses", "dexId",
)


def load() -> list[dict]:
    directory = CATALOG / "sets"
    if not directory.is_dir():
        raise SystemExit(f"No catalog at {directory}.")
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def rendition(card: dict) -> tuple[str, bool]:
    """What this card's picture will actually be, and whether that is good enough."""
    if card.get("image"):
        return "tcgdex 600x825", True
    url = card.get("imageAlt")
    if not url:
        return "none", False
    for marker, label, ok in RENDITIONS:
        if marker in url:
            return label, ok
    return "unknown", False


def report_set(doc: dict, verbose: bool) -> dict:
    cards = doc.get("cards") or []
    listed = doc.get("cardsListed")
    official = (doc.get("cardCount") or {}).get("official") or 0

    renditions: Counter = Counter()
    soft: list[str] = []
    for card in cards:
        label, ok = rendition(card)
        renditions[label] += 1
        if not ok and label != "none":
            soft.append(card["id"])

    # Field coverage, per category.
    by_category: dict[str, list[dict]] = defaultdict(list)
    for card in cards:
        by_category[card.get("category") or "Unknown"].append(card)

    inconsistent: list[tuple[str, str, int, int]] = []
    for category, group in by_category.items():
        if len(group) < 4:
            # Too few to say what "usual" looks like. Three Energy cards disagreeing is
            # not a pattern, it is three cards.
            continue
        for field in FIELDS:
            have = sum(1 for c in group if c.get(field) not in (None, "", [], {}))
            share = have / len(group)
            if EXPECTED_AT <= share < 1.0:
                inconsistent.append((category, field, len(group) - have, len(group)))

    flags = []
    if listed is not None and len(cards) < listed:
        flags.append(f"LOST {listed - len(cards)}")
    if official and len(cards) < official:
        flags.append(f"upstream short {official - len(cards)}")
    if soft:
        flags.append(f"{len(soft)} soft images")
    if renditions.get("none"):
        flags.append(f"{renditions['none']} no art")

    head = f"  {doc['id']:12} {doc.get('name', '')[:28]:28} {len(cards):4} cards"
    print(f"{head}  {'  '.join(flags) if flags else 'clean'}")

    if verbose and soft:
        print(f"      soft: {', '.join(soft[:8])}{' ...' if len(soft) > 8 else ''}")
    if inconsistent:
        for category, field, missing, total in sorted(inconsistent, key=lambda x: -x[2]):
            print(f"      {missing} of {total} {category} carry no {field}")
            if verbose:
                group = by_category[category]
                names = [c["id"] for c in group if c.get(field) in (None, "", [], {})]
                print(f"         {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")

    flagged: list[tuple[str, str]] = []
    for category, field, _missing, _total in inconsistent:
        flagged += [
            (c["id"], field) for c in by_category[category]
            if c.get(field) in (None, "", [], {})
        ]

    return {
        "cards": len(cards),
        "renditions": renditions,
        "soft": len(soft),
        "inconsistent": len(inconsistent),
        "flagged": flagged,
        "summary": {
            "serie": (doc.get("serie") or {}).get("id"),
            "name": doc.get("name"),
            "releaseDate": doc.get("releaseDate"),
            "cards": len(cards),
            "listed": listed,
            "official": official or None,
            "renditions": dict(renditions),
            "soft": soft,
            "fields": [
                {"category": c, "field": f, "missing": m, "total": t}
                for c, f, m, t in sorted(inconsistent, key=lambda x: -x[2])
            ],
        },
    }


def check_upstream(cards: list[tuple[str, str]], limit: int) -> None:
    """
    Asks TCGdex whether a field this catalog lacks is one *they* lack too.

    The question every finding in this report raises is the same one: did the pull drop
    this, or was it never there? Answering it by hand means opening the API three times
    and reading JSON, which is exactly the sort of thing that stops happening after the
    first era. So it is a flag.

    Samples rather than exhausts. A field is either systematically absent upstream or it
    is not, and twenty cards settle that as well as a thousand while costing TCGdex
    twenty requests instead of a thousand.
    """
    import random
    import urllib.request

    random.seed(0)
    picked = random.sample(cards, min(limit, len(cards)))
    print(f"\n  Asking upstream about {len(picked)} of {len(cards)} flagged cards:")
    ours = 0
    for card_id, field in picked:
        url = f"https://api.tcgdex.net/v2/en/cards/{urllib.parse.quote(card_id, safe='-._~')}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            doc = json.loads(urllib.request.urlopen(request, timeout=25).read())
        except Exception as exc:
            print(f"    {card_id:18} {field:12} could not ask ({exc})")
            continue
        upstream = doc.get(field)
        verdict = "upstream has none either" if upstream in (None, "", [], {}) else \
                  f"UPSTREAM HAS IT: {str(upstream)[:40]}"
        if upstream not in (None, "", [], {}):
            ours += 1
        print(f"    {card_id:18} {field:12} {verdict}")
    print(f"\n  {ours} of {len(picked)} are gaps this pull introduced." if ours
          else f"\n  None of {len(picked)} were dropped here -- the gaps are upstream's.")


def write_summary(sets: dict[str, dict]) -> None:
    """
    Writes what the viewer cannot work out for itself.

    Series-level health -- how many cards an era holds, how many have no artwork, how
    many are soft -- needs every set in that era read at once. The viewer loads one set
    at a time on purpose, because loading 218 files to draw a sidebar is not a sidebar.
    So the number is computed here, where every set is already open, and the viewer reads
    one small file instead of the whole catalog.

    Derived, never authored: delete it and the next report rebuilds it. Committed
    anyway, unlike dist/, because its diff is the interesting part -- a weekly refresh
    PR that drops a set's art coverage or adds a field gap shows up here as a few
    changed numbers, which is a far easier thing to review than 23,000 changed cards.
    """
    import time

    eras: dict[str, dict] = {}
    for set_id, entry in sets.items():
        serie = entry["serie"] or "?"
        era = eras.setdefault(serie, {
            "id": serie, "sets": 0, "cards": 0, "art": 0, "filled": 0,
            "holes": 0, "soft": 0, "fields": 0, "from": "9999",
        })
        renditions = entry["renditions"]
        era["sets"] += 1
        era["cards"] += entry["cards"]
        era["art"] += renditions.get("tcgdex 600x825", 0)
        era["filled"] += sum(n for label, n in renditions.items()
                             if label not in ("tcgdex 600x825", "none"))
        era["holes"] += renditions.get("none", 0)
        era["soft"] += len(entry["soft"])
        era["fields"] += len(entry["fields"])
        if entry["releaseDate"] and entry["releaseDate"] < era["from"]:
            era["from"] = entry["releaseDate"]

    path = CATALOG / "summary.json"
    path.write_text(json.dumps({
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "series": eras,
        "sets": sets,
    }, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"\nWrote {path.relative_to(ROOT)} "
          f"({path.stat().st_size / 1024:.0f} KiB, {len(sets)} sets, {len(eras)} eras)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serie", help="only this era")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--check-upstream", type=int, metavar="N", default=0,
                    help="ask TCGdex about N flagged cards: did we drop it, or do they lack it?")
    ap.add_argument("--write-summary", action="store_true",
                    help="write catalog/summary.json for the viewer to read")
    args = ap.parse_args()

    # The summary is what the viewer reads for every era's health, so it has to describe
    # the whole catalog. A filtered run used to write a filtered summary straight over
    # the top of it -- `--serie base --write-summary` silently replaced 218 sets with 7,
    # and the viewer simply lost the other twenty eras with nothing saying why.
    if args.write_summary and args.serie:
        raise SystemExit(
            "--write-summary describes the whole catalog and --serie would write only "
            f"{args.serie!r} over it. Run them separately."
        )

    docs = load()
    eras: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        eras[(doc.get("serie") or {}).get("id") or "?"].append(doc)

    # Oldest first, by the earliest release in the era -- the order someone checking the
    # catalog by hand would actually walk it.
    ordered = sorted(
        eras.items(),
        key=lambda kv: min((d.get("releaseDate") or "9999") for d in kv[1]),
    )
    if args.serie:
        ordered = [kv for kv in ordered if kv[0] == args.serie]
        if not ordered:
            raise SystemExit(f"No era {args.serie!r}. Known: {', '.join(sorted(eras))}")

    grand: Counter = Counter()
    soft_total = 0
    flagged: list[tuple[str, str]] = []
    summaries: dict[str, dict] = {}
    for serie_id, sets in ordered:
        sets.sort(key=lambda d: (d.get("releaseDate") or "9999", d["id"]))
        name = ((sets[0].get("serie") or {}).get("name")) or serie_id
        span = f"{min((d.get('releaseDate') or '?')[:4] for d in sets)}"
        cards = sum(len(d.get("cards") or []) for d in sets)
        print(f"\n{name}  ({serie_id})  {len(sets)} sets, {cards} cards, from {span}")
        print("  " + "-" * 74)
        for doc in sets:
            result = report_set(doc, args.verbose)
            grand.update(result["renditions"])
            soft_total += result["soft"]
            flagged += result["flagged"]
            summaries[doc["id"]] = result["summary"]

    if args.write_summary:
        write_summary(summaries)

    if args.check_upstream and flagged:
        check_upstream(flagged, args.check_upstream)

    total = sum(grand.values())
    if not total:
        return
    print(f"\n{'=' * 78}\nPictures across everything reported:")
    for label, n in grand.most_common():
        print(f"  {label:24} {n:6}  {100 * n / total:5.1f}%")
    good = total - grand.get("none", 0) - soft_total
    print(f"\n  {good} of {total} cards ({100 * good / total:.1f}%) carry a picture at "
          f"600x825 or better.")


if __name__ == "__main__":
    main()
