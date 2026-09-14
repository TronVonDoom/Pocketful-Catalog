"""
Which TCGplayer product, and which of its printings, each printing of a set is.

Run only when you ask, from a set's Details tab: it finds the set's TCGplayer group, then
links every printing it can and reports the rest. A link made here is marked `auto`; one set
by hand is `manual` and is never overwritten. The nightly price job prices a published
printing from exactly this link and nothing else, so what you see here is what is priced.

The matching rules for a product are the old catalog's (tools/tcgplayer.py), learned the hard
way over twenty thousand cards: by printed number within the set's group, by name where the
number is not enough, and a stamped or patterned printing by what TCGplayer writes in brackets
after the name, searched in the set's group and then in TCGplayer's promo groups. What is new
is which of a product's *printings* a printing is, and that is decided by its words:

    normal / holo / reverse        Normal, Holofoil, Reverse Holofoil (or Unlimited ...)
    1st-edition                    1st Edition, 1st Edition Holofoil -- a printing of the
                                   plain product, or of the same card in a sibling group
    shadowless                     the same card in a group named "... (Shadowless)"

A printing whose words have no TCGplayer equivalent -- a copyright line, a misprint -- is left
unlinked for a person to link or leave.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

from .db import Db, eq

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import tcgplayer  # noqa: E402
from tcgcsv import CATEGORY_BY_LANGUAGE, Tcgcsv  # noqa: E402

# TCGplayer printing names to try, best first, for a finish in an unmarked and a 1st Edition printing.
UNLIMITED_NAMES = {
    "normal": ("Normal", "Unlimited", "Unlimited Normal"),
    "holo": ("Holofoil", "Unlimited Holofoil"),
    "reverse": ("Reverse Holofoil",),
}
FIRST_EDITION_NAMES = {
    "normal": ("1st Edition", "1st Edition Normal"),
    "holo": ("1st Edition Holofoil",),
    "reverse": (),
}
# The catalog's pattern words, as TCGdex (and so tools/tcgplayer.py) spelled them.
PATTERN_AS_TCGDEX = {"professor-program-foil": "professor-program"}
STAMP_AS_TCGDEX = {"pokeball-stamp": "pokeball"}
# Groups worth searching for a stamped reprint: TCGplayer's promo buckets.
PROMO_WORDS = (" promo", "calendar", "championship", "prize pack", "league", "staff", "prerelease",
               "trainer kit", "mcdonald", "burger king", "nintendo", "black star", "wotc", "jumbo")


def plain(name: str) -> str:
    """A group or set name as lowercase words, keeping every word (unlike tcgplayer.normalise, which drops "base set")."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def suggest_groups(tcg: Tcgcsv, db: Db, the_set: dict, language: str) -> list[dict]:
    """TCGplayer groups this set could be, best first, each with how many of its cards it holds."""
    category = CATEGORY_BY_LANGUAGE.get(language, tcgplayer.POKEMON)
    groups = tcg.groups(category)
    target = plain(the_set["name"])
    loose_target = tcgplayer.normalise(the_set["name"])
    abbreviation = (the_set.get("abbreviation") or "").strip().lower()

    scored = []
    for g in groups:
        name = plain(g.get("name") or "")
        loose = tcgplayer.normalise(g.get("name") or "")
        score = 0
        if name == target:
            score = 100
        elif loose and loose == loose_target:
            score = 95
        elif abbreviation and (g.get("abbreviation") or "").strip().lower() == abbreviation:
            score = 90
        elif loose and loose_target and (loose in loose_target or loose_target in loose):
            score = 50
        if score:
            scored.append((score, g))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1].get("name") or "")))

    cards = db.get("cards", {"set_id": eq(the_set["id"]), "withdrawn": "is.false",
                             "select": "id,number,printed_number,name"})
    out = []
    for score, g in scored[:5]:
        products = tcg.products(g["groupId"], category)
        held = sum(1 for c in cards if tcgplayer.pick_for_card(products, _as_card(c, [])) is not None)
        out.append({"groupId": g["groupId"], "name": g.get("name"), "abbreviation": g.get("abbreviation"),
                    "score": score, "cards": len(cards), "matched": held})
    out.sort(key=lambda s: (-s["matched"], -s["score"]))
    return out


def link_set(tcg: Tcgcsv, db: Db, the_set: dict, language: str, group_id: int, by_hand: bool) -> dict:
    """Link every printing of the set that is not linked by hand. Returns what was found for each."""
    category = CATEGORY_BY_LANGUAGE.get(language, tcgplayer.POKEMON)
    group_id = int(group_id)
    groups = tcg.groups(category)
    own = next((g for g in groups if g["groupId"] == group_id), None)
    if own is None:
        raise ValueError(f"TCGplayer has no group {group_id}")
    db.update("sets", {"id": eq(the_set["id"])},
              {"tcgplayer_group": group_id, "tcgplayer_via": "manual" if by_hand else "auto"})

    # Groups TCGplayer files the same set's other printings under: "Base Set (Shadowless)" beside
    # "Base Set". A sibling is named for the set with something in brackets after it, which is
    # what keeps "Base Set 2", a different set, out.
    own_name = plain(own.get("name") or "")
    siblings = [g for g in groups if g["groupId"] != group_id and "(" in (g.get("name") or "")
                and plain(g.get("name") or "").startswith(own_name + " ")]
    shadowless = [g for g in siblings if "shadowless" in (g.get("name") or "").lower()]
    promos = [g for g in groups if g["groupId"] != group_id
              and any(word in f" {(g.get('name') or '').lower()}" for word in PROMO_WORDS)]
    linked_groups = {s["tcgplayer_group"] for s in db.get("sets", {"select": "tcgplayer_group",
                                                                    "tcgplayer_group": "not.is.null"})}
    context = {g["groupId"]: tcgplayer.words(g.get("name") or "") for g in promos + siblings
               if g["groupId"] not in linked_groups}

    products: dict[int, list[dict]] = {}
    prices: dict[int, dict[int, dict[str, int]]] = {}

    def products_of(gid: int) -> list[dict]:
        if gid not in products:
            products[gid] = tcg.products(gid, category)
        return products[gid]

    def prices_of(gid: int) -> dict[int, dict[str, int]]:
        if gid not in prices:
            prices[gid] = tcg.prices(gid, category)
        return prices[gid]

    cards = db.get("cards", {"set_id": eq(the_set["id"]), "withdrawn": "is.false"})
    printings = db.get_in("printings", "card_id", [c["id"] for c in cards], {"withdrawn": "is.false"}) if cards else []
    by_card: dict[str, list[dict]] = {}
    for p in printings:
        by_card.setdefault(p["card_id"], []).append(p)

    results = []
    linked = 0
    for card in cards:
        own_printings = by_card.get(card["id"], [])
        as_card = _as_card(card, own_printings)
        for p in own_printings:
            if p.get("tcgplayer_via") == "manual":
                results.append(_result(p, None, None, None, "linked by hand, left alone"))
                continue
            found = _match(p, as_card, the_set, group_id, products_of, prices_of, shadowless, siblings, promos, context)
            if found is None:
                results.append(_result(p, None, None, None, "no match"))
                continue
            product, printing_name, cents = found
            db.update("printings", {"id": eq(p["id"])}, {"tcgplayer_product": product["productId"],
                                                          "tcgplayer_printing": printing_name,
                                                          "tcgplayer_via": "auto"})
            linked += 1
            results.append(_result(p, product, printing_name, cents, "linked"))
    return {"group": {"groupId": group_id, "name": own.get("name")}, "linked": linked,
            "printings": len(results), "results": results}


def _match(p, card, the_set, group_id, products_of, prices_of, shadowless, siblings, promos, context):
    finish = p["finish"]
    edition = p["edition"]
    special = bool(p["pattern"] or p["stamps"] or p["error"])

    if p["error"] or (edition not in (None, "1st-edition", "shadowless")):
        return None

    if not special:
        if edition is None:
            return _plain(card, [group_id], UNLIMITED_NAMES.get(finish, ()), products_of, prices_of)
        if edition == "1st-edition":
            return _plain(card, [group_id] + [g["groupId"] for g in siblings],
                          FIRST_EDITION_NAMES.get(finish, ()), products_of, prices_of)
        return _plain(card, [g["groupId"] for g in shadowless], UNLIMITED_NAMES.get(finish, ()), products_of, prices_of)

    parts = [("stamp", STAMP_AS_TCGDEX.get(s, s)) for s in p["stamps"]]
    if p["pattern"]:
        parts.append(("foil", PATTERN_AS_TCGDEX.get(p["pattern"], p["pattern"])))
    wanted = {"parts": parts}
    names = (FIRST_EDITION_NAMES if edition == "1st-edition" else UNLIMITED_NAMES).get(finish, ())

    product = tcgplayer.special_product(wanted, card, products_of(group_id))
    if product is None:
        number = tcgplayer.normalise_local(card.get("localId"))
        elsewhere = [prod for g in promos for prod in products_of(g["groupId"]) if tcgplayer.card_number(prod) == number]
        total = str(the_set.get("printed_total") or "").lstrip("0") or None
        product = tcgplayer.special_product(wanted, card, elsewhere, set_name=the_set["name"], total=total, context=context)
    if product is None:
        return None
    quotes = prices_of(product["groupId"]).get(product["productId"], {})
    name = next((n for n in names if n in quotes), None) or (next(iter(quotes)) if len(quotes) == 1 else None)
    if name is None:
        return None
    return product, name, quotes.get(name)


def _plain(card, group_ids, names, products_of, prices_of):
    """The card's plain product in the first of these groups that quotes one of these printing names."""
    for gid in group_ids:
        product = tcgplayer.pick_for_card(products_of(gid), card)
        if product is None:
            continue
        quotes = prices_of(gid).get(product["productId"], {})
        for name in names:
            if name in quotes:
                return product, name, quotes[name]
    return None


def _as_card(card: dict, printings: list[dict]) -> dict:
    """A card in the shape tools/tcgplayer.py reads: TCGdex's, with the number as printed."""
    printed = (card.get("printed_number") or card.get("number") or "").split("/")[0].strip()
    return {
        "id": card["id"],
        "localId": printed,
        "name": card["name"],
        "variants_detailed": [
            {"type": p["finish"], "stamp": [STAMP_AS_TCGDEX.get(s, s) for s in p["stamps"] or []]
             + (["1st-edition"] if p["edition"] == "1st-edition" else [])}
            for p in printings
        ],
    }


def _result(p, product, printing_name, cents, status) -> dict:
    return {
        "printing": p["id"],
        "status": status,
        "productId": product["productId"] if product else p.get("tcgplayer_product"),
        "productName": product.get("name") if product else None,
        "printingName": printing_name or p.get("tcgplayer_printing"),
        "market": cents,
    }
