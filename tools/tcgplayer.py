"""
How a catalog card is matched to a TCGplayer product, in one place.

Two things need this answer and they must never disagree about it. `pull_prices.py`
prices the catalog with it every night, and `editor/server.py` shows it to a person
deciding whether the match is right. If each carried its own copy of the rules, the
editor could tell you a card matched the plain printing while the nightly job quietly
priced the staff stamp -- and the tool for checking the match would be checking a
different match.

So the rules live here, stdlib only, because the editor is deliberately dependency-free
and imports this too.

The join, and where a person can overrule it
--------------------------------------------
Automatically: by set, then by printed number. `catalog/tcgplayer-groups.json` says
which TCGplayer group each set is, and within a group a card is found by its number --
and by its name as well, where the group holds more than one set (a two-deck Trainer Kit
numbers both decks from one). See pick_for_card().

A card's special printings (variants.py: its Pokemon Center stamp, its Poke Ball pattern)
are found by what TCGplayer writes in brackets after the name, first in the card's own
group and then in the promo groups stamped cards are filed under. See special_product().
resolve() is the whole answer for one card, and is what every caller uses.

By hand, at either level:

  catalog/tcgplayer-groups.json   an entry with `"via": "manual"` is a set someone linked
                                  in the editor. map_groups.py never re-derives it, not
                                  even with --recheck, and keeps what it would have said
                                  under `auto` so the link can be undone.
  catalog/tcgplayer-cards.json    one card linked to one product. Beats the number match
                                  outright, including when the product lives in another
                                  group -- the promo filed under a Trainer Kit, the card
                                  TCGplayer numbers differently. `productId: null` says
                                  the card has no product at all and should carry no
                                  price, which is not the same as not having looked.
                                  A key of `<card id>~<type>~<printing>` links one
                                  special printing instead (see variants.py), the same
                                  way.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path

import variants  # tools/ is not a package: whoever imports this has tools/ on the path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"
GROUPS = CATALOG / "tcgplayer-groups.json"
CARD_LINKS = CATALOG / "tcgplayer-cards.json"
CACHE = CATALOG / ".tcgcsv"

TCGCSV = "https://tcgcsv.com/tcgplayer"
POKEMON = 3

# A product photo at the size fill_gaps.py settled on. See the note on TCGPLAYER_IMAGE
# there: the 874 box is what lands a card at roughly TCGdex's own resolution.
PRODUCT_IMAGE = "https://product-images.tcgplayer.com/fit-in/874x874/{}.jpg"

CARD_LINKS_NOTE = (
    "Cards linked to a TCGplayer product by hand, from editor/. pull_prices.py prices a "
    "card listed here from its linked product instead of matching it by printed number. "
    "productId null means the card has no TCGplayer product and carries no price."
)


def normalise(name: str) -> str:
    """
    A set name with everything a catalog adds to it taken back off.

    Both sides decorate: TCGplayer prefixes a release code ("SV08: "), TCGdex sometimes
    appends "Base Set", and one of them writes Pokemon with an accent. What is left is the
    name a person would say out loud, which is the only part the two reliably agree on.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    # The release code comes off whether it is punctuated with a colon ("SV08: Surging
    # Sparks") or a dash ("SM - Ultra Prism"). Both forms are in use, and which one a set
    # got seems to be a matter of what year it was filed.
    text = re.sub(r"^[a-z]{1,7}[0-9]*(\.[0-9]+)?[a-z]?\s*[:-]\s*", "", text)
    # "&" and "and" are the same word. TCGdex writes "Black & White", TCGplayer writes
    # "Black and White", and that one character was hiding a 115-card set.
    text = text.replace("&", " and ")
    text = re.sub(r"\b(pokemon|tcg|the|base set|collection)\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def card_total(product: dict) -> str | None:
    """The set size printed after the slash ("217" of "039/217"), or None where there is none."""
    for entry in product.get("extendedData") or []:
        if entry.get("name") == "Number":
            value = (entry.get("value") or "").strip()
            if "/" in value:
                return value.split("/", 1)[1].strip().lstrip("0") or None
    return None


# Everything TCGplayer puts in brackets after a card's name: "[Staff]", "(Prerelease)",
# "(Pokemon Center Exclusive)", "(Poke Ball Pattern)".
DECORATION = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")


def words(text: str) -> str:
    """
    Text as space-padded plain lowercase words, for whole-phrase matching.

    "and" is dropped because the two catalogs disagree about it inside names the same way
    they do in set names -- "[Diamond & Pearl]" against "[Diamond and Pearl]".
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    kept = [w for w in re.sub(r"[^a-z0-9]+", " ", text).split() if w != "and"]
    return " " + " ".join(kept) + " "


def decoration(product: dict) -> str:
    """The bracketed parts of a product's name, as words()."""
    return words(" ".join(a or b for a, b in DECORATION.findall(product.get("name") or "")))


def base_name(product: dict) -> str:
    """A product's card name with its decoration and trailing number taken off, normalised."""
    name = DECORATION.sub(" ", product.get("name") or "")
    name = re.sub(r"\s-\s*[A-Za-z]*\d+[A-Za-z]*(/\w+)?\s*$", " ", name)
    return normalise(name)


def is_decorated(product: dict) -> bool:
    """
    A staff stamp, a prerelease stamp, a Pokemon Center exclusive -- anything TCGplayer
    names in brackets.

    Round brackets count as much as square ones. TCGplayer files "[Staff]" in square
    brackets but "(Pokemon Center Exclusive)" and "(Poke Ball Pattern)" in round ones, and
    reading only the first kind let a stamped Tyrunt compete with the plain one for its
    number on equal terms.
    """
    return decoration(product).strip() != ""


def names_agree(card: dict, product: dict) -> bool:
    """
    Whether a product is plausibly the card, by name.

    Containment rather than equality, because each side decorates differently ("Pokedex
    (HANDY910is)"), with a close spelling allowed for the typos TCGplayer's listings carry
    ("Imposter Professor Oak"). Abbreviated energies ("Unit Energy GRW") do not agree, and
    do not need to: the name is only decisive where the number cannot be.
    """
    a, b = normalise(card.get("name") or ""), base_name(product)
    if not a or not b:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


# How TCGplayer words a part of a special printing, where it does not simply use the words
# of TCGdex's slug. Each phrase is matched as whole words against words().
PART_PHRASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("stamp", "pre-release"): ("prerelease", "pre release"),
    ("stamp", "pokemon-center"): ("pokemon center",),
    ("stamp", "player-rewards-program"): ("player rewards", "player reward", "play pokemon", "prize pack"),
    ("stamp", "professor-program"): ("professor program",),
    ("stamp", "countdown-calendar"): ("countdown calendar", "holiday calendar"),
    ("stamp", "trick-or-trade"): ("trick or trade", "trick trade"),
    ("stamp", "top-eight"): ("top 8", "top eight"),
    ("stamp", "top-sixteen"): ("top 16", "top sixteen"),
    ("stamp", "top-thirty-two"): ("top 32", "top thirty two"),
    ("stamp", "semi-finalist"): ("semi finalist", "semifinalist"),
    ("stamp", "quarter-finalist"): ("quarter finalist", "quarterfinalist"),
    ("stamp", "eb-games"): ("eb games",),
    ("stamp", "gamestop"): ("gamestop", "game stop"),
    # TCGdex spells this player's name with a stray r; TCGplayer does not.
    ("stamp", "ross-cawthorn"): ("ross cawthon", "ross cawthorn"),
    ("stamp", "regional-championships"): ("regional championships", "regional championship"),
    ("stamp", "national-championships"): ("national championships", "national championship"),
    ("stamp", "state-championships"): ("state championships", "state championship"),
    ("stamp", "city-championships"): ("city championships", "city championship"),
    ("foil", "pokeball"): ("poke ball",),
    ("foil", "masterball"): ("master ball",),
    ("foil", "loveball"): ("love ball",),
    ("foil", "friendball"): ("friend ball",),
    ("foil", "quickball"): ("quick ball",),
    ("foil", "duskball"): ("dusk ball",),
    ("foil", "energy"): ("energy symbol", "energy pattern"),
    ("foil", "cosmos"): ("cosmos", "cosmo holo"),
    ("foil", "cracked-ice"): ("cracked ice",),
    ("foil", "player-reward"): ("player rewards", "player reward"),
    ("foil", "team-rocket"): ("team rocket",),
}


def part_phrases(kind: str, slug: str, set_name: str = "") -> tuple[str, ...]:
    if (kind, slug) == ("stamp", "set-logo"):
        # A set-logo stamp is written as the set's own name: "Lucario - 6/130 [Diamond & Pearl]"
        # in Burger King Promos. Only ever passed for a product outside the set's own group,
        # where the set's name in brackets can only be the stamp; inside it, "(Delta Species)"
        # is a card of the set called Delta Species.
        return tuple(p for p in ("set logo", words(set_name).strip()) if p)
    return PART_PHRASES.get((kind, slug)) or (words(slug).strip(),)


def _marks() -> tuple[str, ...]:
    """Every phrase that names a stamp: what a product must not carry beyond its own."""
    found = {p for slug in variants.STAMP_LABELS if slug != "set-logo" for p in part_phrases("stamp", slug)}
    found |= {p for (kind, _), phrases in PART_PHRASES.items() if kind == "stamp" for p in phrases}
    return tuple(sorted(found))


MARKS = _marks()


def special_product(special: dict, card: dict, products: list[dict], *, set_name: str = "",
                    total: str | None = None, context: dict[int, str] | None = None,
                    usable=lambda product: True) -> dict | None:
    """
    The TCGplayer product a special printing is, from `products`, or None.

    A product qualifies when it carries the card's printed number and name, and every part
    of the printing -- each stamp, the foil pattern -- is written into its bracketed
    decoration. It must not carry some *other* stamp besides: "(State Championships)
    [Staff]" is not the State Championships card, it is its staff copy, and when TCGplayer
    lists only the staff one the right answer is no product. Of what is left, the product
    with least else written in its brackets wins.

    Two things let this look outside the card's own group, where most stamped reprints are
    filed ("Countdown Calendar Promos", "World Championship Decks", "Nintendo Promos"):

      `context`  group id to that group's name as words(), for the groups no catalog set is
                 linked to. Searched along with the decoration, because some groups are the
                 stamp: a Countdown Calendar card is listed as plain "Riolu - 61/130".
      `total`    the card's set size. A product in some other set's group qualifies only if
                 it prints that same size after the slash ("52/130"), which is what keeps a
                 same-named, same-numbered card from another set out; one in a `context`
                 group qualifies unless it prints a different one.

    `set_name` is how a set-logo stamp is recognised, so it belongs only with those two.

    A 1st Edition is not matched here. TCGplayer does not sell it as a separate product but
    as a printing of the plain one; see first_edition_quotes().
    """
    number = normalise_local(card.get("localId"))
    parts = special["parts"]
    context = context or {}
    # A stamp every printing carries is not "another stamp" on a product either: a
    # Prerelease-only promo's staff copy is "(Prerelease) [Staff]".
    own = {p for slug in variants.common_stamps(card) for p in part_phrases("stamp", slug)}
    # And a staff card is, near enough always, a prerelease staff card. TCGdex records only
    # the staff stamp; TCGplayer writes "(Prerelease) [Staff]".
    if ("stamp", "staff") in parts:
        own.update(part_phrases("stamp", "pre-release"))
    best, best_size = None, 10 ** 6
    for product in products:
        if card_number(product) != number or not usable(product) or not names_agree(card, product):
            continue
        text = decoration(product)
        searched = text + context.get(product.get("groupId"), "")
        if not all(any(f" {p} " in searched for p in part_phrases(*part, set_name)) for part in parts):
            continue
        if total is not None:
            printed = card_total(product)
            if printed != total and (printed is not None or product.get("groupId") not in context):
                continue
        leftover = text
        for part in parts:
            for phrase in part_phrases(*part, set_name):
                leftover = leftover.replace(f" {phrase} ", " ")
        if any(f" {mark} " in leftover for mark in MARKS if mark not in own):
            continue
        size = len(leftover.split())
        if size < best_size:
            best, best_size = product, size
    return best


def first_edition_quotes(quotes: dict[str, int]) -> dict[str, int]:
    """
    A plain product's 1st Edition printings, keyed as the unmarked printing would be.

    TCGplayer prices WotC-era 1st Editions as a printing of the card rather than as a
    product of its own: "1st Edition Holofoil" beside "Unlimited Holofoil". A special
    printing keyed `1st-edition` is therefore priced by keeping those and dropping the
    prefix, so the app asks for "holofoil" whichever kind of card it is holding.
    """
    out = {}
    for key, cents in quotes.items():
        if key.startswith("1stEdition"):
            rest = key[len("1stEdition"):]
            out[(rest[:1].lower() + rest[1:]) or "normal"] = cents
    return out


def plain_quotes(quotes: dict[str, int]) -> dict[str, int]:
    """
    A product's quotes for the unmarked card: its 1st Edition printings taken out, and
    "Unlimited" read as the plain printing it is.

    The app asks for "holofoil" first and "1stEditionHolofoil" second, which is right for a
    modern card and was wrong for every WotC one: those groups have no "Holofoil" at all,
    only "Unlimited Holofoil" and "1st Edition Holofoil", so an Unlimited Jungle Clefable
    was quoted at its 1st Edition price, three times what it trades for. The 1st Edition
    figure is not lost; it prices the card's `1st-edition` printing instead.

    A product quoted *only* in 1st Edition keeps those figures, since one close price is a
    better answer than none, which is the rule the app already applies.
    """
    out: dict[str, int] = {}
    for key, cents in quotes.items():
        if key.startswith("1stEdition"):
            continue
        if key.startswith("unlimited"):
            rest = key[len("unlimited"):]
            key = (rest[:1].lower() + rest[1:]) or "normal"
        out.setdefault(key, cents)
    return out or dict(quotes)


# Words that mark a product as something other than the ordinary card, over and above the
# stamps: a misprint is a different object to the card it misprints.
FLAWS = ("misprint", "error")


def pick_for_card(products: list[dict], card: dict, *, specials: list[dict] = (),
                  shared: bool = False, usable=lambda product: True) -> dict | None:
    """
    The one product a card's plain printing is priced from, or None.

    A printed number can carry several products: the card, its staff stamp, its
    prerelease stamp. Those are different objects at different prices, so among the
    products with the card's number:

      - one that is a special printing of this card (its Pokemon Center stamp, its Poke
        Ball pattern) is never the plain card, even when it is all there is;
      - a name that agrees beats one that does not;
      - a product carrying the stamps the card itself always carries beats one that does
        not -- Celebratory Fanfare only exists stamped Ace Trainer, so "(Ace Trainer)" is
        its plain product;
      - an undecorated name beats a decorated one, and a decoration naming fewer stamps
        (or a misprint) beats one naming more. Past that, TCGplayer's own order decides,
        as it always has.

    A name that agrees is required when `shared` is true -- the group holds more than one
    catalog set, as every two-deck Trainer Kit does, so the number alone cannot say whose
    card it is. Elsewhere the number is enough, which keeps "Unit Energy GRW" priced.

    `usable` narrows the field first; the price pull passes "has a quote", so a plain
    product nobody has sold does not shadow a stamped one that has a price.
    """
    number = normalise_local(card.get("localId"))
    if not number:
        return None
    candidates = [p for p in products if card_number(p) == number and usable(p)]
    taken = {
        id(found) for special in specials
        if (found := special_product(special, card, candidates)) is not None
    }
    own = [part_phrases("stamp", slug) for slug in variants.common_stamps(card) if slug != "set-logo"]
    own_words = {phrase for phrases in own for phrase in phrases}

    ranked = []
    for index, product in enumerate(candidates):
        if id(product) in taken:
            continue
        agrees = names_agree(card, product)
        if shared and not agrees:
            continue
        text = decoration(product)
        carries_own = bool(own) and all(any(f" {p} " in text for p in phrases) for phrases in own)
        marks = sum(1 for mark in (*MARKS, *FLAWS) if mark not in own_words and f" {mark} " in text)
        rank = (not agrees, bool(own) and not carries_own, text.strip() != "", marks, index)
        ranked.append((rank, product))
    return min(ranked, key=lambda pair: pair[0])[1] if ranked else None


def shared_groups(mapping: dict[str, dict]) -> set[int]:
    """Groups more than one catalog set is linked to: where a number alone is ambiguous."""
    seen: dict[int, int] = {}
    for entry in mapping.values():
        if entry.get("groupId"):
            seen[entry["groupId"]] = seen.get(entry["groupId"], 0) + 1
    return {group for group, count in seen.items() if count > 1}


def link_key(card_id: str, special: dict | None = None) -> str:
    """Where a hand link is filed in tcgplayer-cards.json: the card, or one special printing."""
    return card_id if special is None else f"{card_id}~{special['type']}~{special['key']}"


def unlinked_context(groups: list[dict], mapping: dict[str, dict]) -> dict[int, str]:
    """
    Group id to name, as words(), for every group no catalog set is linked to.

    These are TCGplayer's own promo buckets -- "Countdown Calendar Promos", "World
    Championship Decks", "Base Set (Shadowless)" -- and the only places a stamped reprint
    can be found by the group it is in rather than by what is written after its name.
    """
    used = {entry.get("groupId") for entry in mapping.values() if entry.get("groupId")}
    return {g["groupId"]: words(g.get("name") or "") for g in groups if g.get("groupId") not in used}


def set_total(set_doc: dict) -> str | None:
    """The set size printed after a card's number, as card_total() writes it."""
    official = (set_doc.get("cardCount") or {}).get("official")
    if not official:
        return None
    return str(official).lstrip("0") or None


def resolve(card: dict, set_doc: dict, *, mapping: dict[str, dict], links: dict[str, dict],
            products, elsewhere, context: dict[int, str], shared: set[int],
            usable=lambda product: True) -> dict:
    """
    Which TCGplayer product a card is, and which each of its special printings is.

    `products(group_id)` is one group's product list; `elsewhere(number)` is every product
    with that printed number in any group. How those are got is the caller's business --
    the price pull fetches them, the editor reads its cache -- which is why they are
    passed in, and why the answer is the same either way.

    Returns `{"product", "via", "special": [...]}`. `via` is "link" (a person chose it;
    `product` is None for "not sold"), "number" (the automatic match) or None (nothing
    found). Each special printing comes back as variants.special_printings() gave it,
    plus its own `product` and `via`, where `via` may also be "match" (found by its
    decoration) or "first-edition" (priced as a printing of the plain product).
    """
    group_id = (mapping.get(set_doc.get("id")) or {}).get("groupId")
    own = products(group_id) if group_id else []
    specials = variants.special_printings(card)

    def linked(key: str) -> tuple[bool, dict | None]:
        link = links.get(key)
        if link is None:
            return False, None
        if not (link.get("productId") and link.get("groupId")):
            return True, None
        wanted = link["productId"]
        return True, next((p for p in products(link["groupId"]) if p.get("productId") == wanted), None)

    by_hand, plain = linked(card["id"])
    via = "link" if by_hand else None
    if not by_hand:
        plain = pick_for_card(own, card, specials=specials, shared=group_id in shared, usable=usable)
        via = "number" if plain else None

    out = []
    number = normalise_local(card.get("localId"))
    for special in specials:
        by_hand, product = linked(link_key(card["id"], special))
        found_via = "link" if by_hand else None
        if not by_hand:
            if special["parts"] == [("stamp", "1st-edition")]:
                product, found_via = plain, ("first-edition" if plain else None)
            else:
                product = special_product(special, card, own, usable=usable)
                if product is None and number:
                    product = special_product(
                        special, card, [p for p in elsewhere(number) if p.get("groupId") != group_id],
                        set_name=set_doc.get("name") or "", total=set_total(set_doc),
                        context=context, usable=usable,
                    )
                found_via = "match" if product else None
        out.append({**special, "product": product, "via": found_via})
    return {"product": plain, "via": via, "special": out}


def special_quotes(special: dict, product_quotes: dict[str, int] | None) -> dict[str, int]:
    """What a resolved special printing is quoted at, given its product's quotes."""
    if not product_quotes:
        return {}
    if special.get("via") == "first-edition":
        return first_edition_quotes(product_quotes)
    return dict(product_quotes)


def load_groups() -> dict[str, dict]:
    if not GROUPS.exists():
        return {}
    return json.loads(GROUPS.read_text(encoding="utf-8")).get("sets") or {}


def load_card_links() -> dict[str, dict]:
    if not CARD_LINKS.exists():
        return {}
    return json.loads(CARD_LINKS.read_text(encoding="utf-8")).get("cards") or {}
