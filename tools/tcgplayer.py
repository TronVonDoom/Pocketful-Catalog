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
which TCGplayer group each set is, and within a group a card is found by its number.

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
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

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


def is_decorated(product: dict) -> bool:
    """A staff stamp, a prerelease stamp -- anything TCGplayer names in square brackets."""
    return bool(re.search(r"\[[^\]]+\]", product.get("name") or ""))


def pick_by_number(products: list[dict], usable=lambda product: True) -> dict[str, dict]:
    """
    Printed number to the one product a card with that number is priced from.

    A printed number can carry several products: the card, its staff stamp, its
    prerelease stamp. Those are genuinely different objects that trade at genuinely
    different prices, and the catalog only knows about the plain one -- so the plain one
    is what gets the number, and a decorated name only fills in where no plain product
    exists. `usable` narrows the field first; the price pull passes "has a quote", so a
    plain product nobody has sold does not shadow a stamped one that has a price.
    """
    plain: dict[str, dict] = {}
    decorated: dict[str, dict] = {}
    for product in products:
        number = card_number(product)
        if not number or not usable(product):
            continue
        bucket = decorated if is_decorated(product) else plain
        bucket.setdefault(number, product)
    return {**decorated, **plain}


def load_groups() -> dict[str, dict]:
    if not GROUPS.exists():
        return {}
    return json.loads(GROUPS.read_text(encoding="utf-8")).get("sets") or {}


def load_card_links() -> dict[str, dict]:
    if not CARD_LINKS.exists():
        return {}
    return json.loads(CARD_LINKS.read_text(encoding="utf-8")).get("cards") or {}
