#!/usr/bin/env python3
"""
Checks the local catalog for holes, and fails when a new one appears.

Why this exists: a card with no artwork and a card that was never fetched look
identical once they are on disk. That ambiguity is the bug behind every "some sets
are missing images" report -- not the missing image itself, which is often a real
absence upstream and nobody's fault, but the fact that nothing could tell the two
apart. So this makes "hole" a recorded state rather than an inferred one.

Every hole is either **known** -- written down in catalog/holes.json with a reason,
and therefore fine -- or **new**, which fails the run. A known hole is not a bug. An
unknown one is, whether it turns out to be a broken pull or a genuine gap upstream,
because either way nobody decided it.

Deliberately offline. It reads what pull_catalog.py wrote and asks the network
nothing, so it costs TCGdex nothing to run it on every commit, and a run that fails
means the catalog is wrong rather than that the API was having an afternoon.

Usage:
    python audit.py                 # report, exit 1 on a new hole
    python audit.py --verbose       # list every offending card, not just counts
    python audit.py --accept        # record today's holes as the baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
HOLES = CATALOG / "holes.json"

# Characters a card id may contain and still survive being pasted into a URL path and
# into a GraphQL string literal. TcgDex.variantsInSet filters on the same set, so a
# card outside it is silently dropped from every batched lookup the app makes -- which
# is worth knowing about here rather than discovering as a missing holo pocket.
SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")

# What can be wrong with a card, and how it reads in the report. Kept as an explicit
# table so a new check has to be named before it can be counted.
KINDS = {
    "no-image": "no artwork",
    "no-name": "no name",
    "no-rarity": "no rarity",
    "no-local-id": "no printed number",
    "unsafe-id": "id unusable in a URL or GraphQL literal",
    "incomplete-pull": "fewer cards than upstream lists -- this pull lost some",
    "short-set": "fewer cards than the set claims to hold -- upstream is missing them",
    "duplicate-local-id": "two cards share a printed number",
}


def load_sets() -> dict[str, dict]:
    """Every set document on disk, keyed by id."""
    directory = CATALOG / "sets"
    if not directory.is_dir():
        sys.exit(f"No catalog at {directory}. Run pull_catalog.py first.")
    out = {}
    for path in sorted(directory.glob("*.json")):
        try:
            out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            sys.exit(f"{path.name} is not readable JSON: {exc}")
    return out


def load_holes() -> dict:
    if not HOLES.exists():
        return {"sets": {}}
    return json.loads(HOLES.read_text(encoding="utf-8"))


def inspect(set_id: str, doc: dict) -> tuple[Counter, dict[str, list[str]]]:
    """
    One set's holes, counted by kind and itemised by card.

    Set-level findings are counted once and attributed to the set rather than to a
    card, because "this set is missing eleven cards" is one fact about the set and
    not eleven facts about cards nobody has.
    """
    counts: Counter = Counter()
    offenders: dict[str, list[str]] = defaultdict(list)
    cards = doc.get("cards") or []

    for card in cards:
        cid = card.get("id") or "<no id>"
        if not card.get("image"):
            counts["no-image"] += 1
            offenders["no-image"].append(cid)
        if not (card.get("name") or "").strip():
            counts["no-name"] += 1
            offenders["no-name"].append(cid)
        if not card.get("rarity"):
            counts["no-rarity"] += 1
            offenders["no-rarity"].append(cid)
        if not str(card.get("localId") or "").strip():
            counts["no-local-id"] += 1
            offenders["no-local-id"].append(cid)
        if cid and not set(cid) <= SAFE_ID_CHARS:
            counts["unsafe-id"] += 1
            offenders["unsafe-id"].append(cid)

    seen = Counter(str(c.get("localId")) for c in cards)
    for local_id, n in seen.items():
        if n > 1:
            counts["duplicate-local-id"] += 1
            offenders["duplicate-local-id"].append(f"{set_id}-{local_id} x{n}")

    # Two different questions, and conflating them hid a real bug for a whole afternoon.
    #
    # `total` is how many cards upstream actually lists for this set, secret rares
    # included, so falling short of it means *this pull* lost cards. `official` is how
    # many the set claims to have been printed with, so falling short of that means
    # *upstream* is missing cards nobody here can supply.
    #
    # Checking only against `official` misses the first case entirely whenever a set has
    # secret rares: swsh1 lists 216 cards and claims 202, so a pull that silently dropped
    # ten still cleared the bar. That is exactly how a 187-card loss in swshp went
    # unnoticed until the counts were compared by hand.
    count = doc.get("cardCount") or {}
    total = count.get("total") or 0
    official = count.get("official") or 0

    if total and len(cards) < total:
        counts["incomplete-pull"] += total - len(cards)
        offenders["incomplete-pull"].append(f"{len(cards)} of {total} listed upstream")

    if official and len(cards) < official:
        counts["short-set"] += official - len(cards)
        offenders["short-set"].append(f"{len(cards)} of {official} printed")

    return counts, offenders


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true", help="list offending cards")
    ap.add_argument("--accept", action="store_true", help="record today's holes as the baseline")
    args = ap.parse_args()

    sets = load_sets()
    known = load_holes()
    baseline = known.get("sets", {})

    index_path = CATALOG / "index.json"
    indexed = set()
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        indexed = {s["id"] for s in index.get("sets", [])}

    total_cards = sum(len(d.get("cards") or []) for d in sets.values())
    print(f"{len(sets)} sets, {total_cards} cards, from {CATALOG}")
    if indexed:
        unpulled = indexed - set(sets)
        print(f"index lists {len(indexed)} sets; {len(unpulled)} not pulled")

    findings: dict[str, Counter] = {}
    details: dict[str, dict[str, list[str]]] = {}
    for set_id, doc in sorted(sets.items()):
        counts, offenders = inspect(set_id, doc)
        if counts:
            findings[set_id] = counts
            details[set_id] = offenders

    if args.accept:
        recorded = {
            set_id: {
                "reason": baseline.get(set_id, {}).get("reason", "TODO: say why this is a hole"),
                **{kind: n for kind, n in sorted(counts.items())},
            }
            for set_id, counts in findings.items()
        }
        HOLES.write_text(
            json.dumps(
                {
                    "note": (
                        "Holes that are known and accepted. audit.py fails on anything "
                        "worse than this. Each entry needs a reason a human wrote -- an "
                        "accepted hole with no explanation is just an unnoticed one."
                    ),
                    "sets": recorded,
                },
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nRecorded {len(recorded)} sets into {HOLES.relative_to(ROOT)}.")
        print("Now write a reason for each -- that is the half this cannot do for you.")
        return

    regressions: list[str] = []
    improvements: list[str] = []
    totals: Counter = Counter()

    for set_id, counts in findings.items():
        allowed = baseline.get(set_id, {})
        for kind, n in sorted(counts.items()):
            totals[kind] += n
            permitted = allowed.get(kind, 0)
            if n > permitted:
                regressions.append(
                    f"  {set_id:12} {KINDS.get(kind, kind):48} {n} (allowed {permitted})"
                )
            elif n < permitted:
                improvements.append(f"  {set_id:12} {KINDS.get(kind, kind):48} {n} < {permitted}")

    # A hole recorded against a set that is no longer pulled is stale bookkeeping, and
    # left unreported it quietly widens what the baseline permits.
    for set_id in baseline:
        if set_id not in sets:
            improvements.append(f"  {set_id:12} recorded as holed, but is not in the catalog")

    print("\nHoles by kind:")
    if not totals:
        print("  none")
    for kind, n in totals.most_common():
        pct = f"{100 * n / total_cards:.1f}%" if total_cards and kind != "short-set" else ""
        print(f"  {KINDS.get(kind, kind):48} {n:6} {pct}")

    if args.verbose:
        print("\nBy set:")
        for set_id, counts in sorted(findings.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {set_id}  {sets[set_id].get('name')}")
            for kind, n in sorted(counts.items()):
                sample = ", ".join(details[set_id][kind][:6])
                more = "" if len(details[set_id][kind]) <= 6 else " ..."
                print(f"    {KINDS.get(kind, kind):46} {n:5}  {sample}{more}")

    if improvements:
        print(f"\nBetter than recorded ({len(improvements)}) -- re-run with --accept to tighten:")
        for line in improvements[:20]:
            print(line)

    if regressions:
        print(f"\nNEW HOLES ({len(regressions)}):")
        for line in regressions:
            print(line)
        print(
            "\nEach of these is either a bad pull or a real gap upstream. Fix it, or "
            f"record it in {HOLES.relative_to(ROOT)} with a reason."
        )
        sys.exit(1)

    print("\nNo holes beyond the ones already written down.")


if __name__ == "__main__":
    main()
