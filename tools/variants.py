"""
The special printings of a card: the stamped and pattern-foil copies that sit beside its
normal, holo and reverse.

The catalog has always shipped a card's press runs as five flags -- normal, holo, reverse,
firstEdition, wPromo -- and that is the whole of what the app could offer. It cannot say
that MEP Tyrunt also exists with a Pokemon Center stamp, or that a Prismatic Evolutions
common has a Poke Ball and a Master Ball reverse. Those are different objects at very
different prices, and a collector holding one had nowhere to file it.

TCGdex already knows. Every card carries `variants_detailed`, one entry per printing, each
with a `type` (normal, holo, reverse) and whatever sets it apart: `stamp` (a list),
`foil` (the pattern) and `subtype` (shadowless, a copyright line, a known error). This
module reads that list and names the printings that are not the plain one, so pack.py can
ship them and pull_prices.py can price them, and the two cannot disagree about which
printings a card has.

What counts as special
----------------------
A stamp every printing of a card carries is part of the card, not a variation of it. The
MEP promos all bear the set logo and svp-045 is only ever stamped Worlds 2023, so those
come off first; what is left is what separates one printing from another.

Within a type, the printing with nothing left is the plain one and everything else is
special. Where a type has no plain printing, the least decorated one that carries no
stamp stands in for it: Mewtwo VSTAR's only holo is rainbow and Fossil Aerodactyl's is
galaxy, and calling those "special" would hand every card in the set a variant with
nothing plain beside it. A type whose every printing is stamped has no stand-in, because
a stamp is never the ordinary copy.

Stdlib only, because the editor imports it too.
"""

from __future__ import annotations

# The TCGdex types, in the order a picker lists them.
TYPES = ("normal", "holo", "reverse")

# How a stamp reads to a person. Anything missing from here is title-cased from its slug,
# which is right for the World Championships deck signatures ("jason-klaczynski") that make
# up most of the long tail, and legible for anything upstream adds next.
STAMP_LABELS = {
    "1st-edition": "1st Edition",
    "1st-edition-error": "1st Edition Error",
    "1st-edition-scratch-error": "1st Edition Scratch Error",
    "1st-movie": "1st Movie",
    "1st-movie-inverted": "1st Movie Inverted",
    "10th-anniversary": "10th Anniversary",
    "25th-celebration": "25th Celebration",
    "30th-pokeday": "30th Pokémon Day",
    "ace-trainer": "Ace Trainer",
    "asia-promo": "Asia Promo",
    "asia-2023-24": "Asia 2023-24",
    "champion": "Champion",
    "city-championships": "City Championships",
    "comic-con": "Comic-Con",
    "countdown-calendar": "Countdown Calendar Stamp",
    "d-edition-error": "D Edition Error",
    "distributor-meeting": "Distributor Meeting",
    "eb-games": "EB Games Stamp",
    "finalist": "Finalist",
    "fossil-museum": "Fossil Museum",
    "gamestop": "GameStop Stamp",
    "gen-con": "Gen Con",
    "grey-star": "Grey Star",
    "gym-challenge": "Gym Challenge",
    "illustration-contest-2022": "Illustration Contest 2022",
    "illustration-contest-2024": "Illustration Contest 2024",
    "international-championship-europe": "Europe International Championships",
    "international-championship-latin-america": "Latin America International Championships",
    "international-championship-north-america": "North America International Championships",
    "jr-stamp-rally": "Jr. Stamp Rally",
    "judge": "Judge Stamp",
    "master-ball-league": "Master Ball League",
    "mcdonalds": "McDonald's Stamp",
    "national-championships": "National Championships",
    "origins-2008": "Origins 2008",
    "player-rewards-program": "Player Rewards",
    "poke-ball-league": "Poké Ball League",
    "pokeball": "Poké Ball",
    "pokemon-4-ever": "Pokémon 4Ever",
    "pokemon-center": "Pokémon Center Stamp",
    "pokemon-center-ny": "Pokémon Center NY Stamp",
    "pokemon-day": "Pokémon Day Stamp",
    "pokemon-rocks-america": "Pokémon Rocks America",
    "pokemon-together": "Pokémon Together",
    "poketour-99": "PokéTour '99",
    "pop-tournament": "POP Tournament",
    "pre-release": "Prerelease Stamp",
    "professor-program": "Professor Program",
    "quarter-finalist": "Quarter-Finalist",
    "regional-championships": "Regional Championships",
    "semi-finalist": "Semi-Finalist",
    "set-logo": "Set Logo Stamp",
    "snowflake": "Snowflake Stamp",
    "stadium-challenge": "Stadium Challenge",
    "staff": "Staff Stamp",
    "state-championships": "State Championships",
    "thank-you": "Thank You Stamp",
    "top-eight": "Top 8",
    "top-sixteen": "Top 16",
    "top-thirty-two": "Top 32",
    "trick-or-trade": "Trick or Trade Stamp",
    "ultra-ball-league": "Ultra Ball League",
    "w-promo": "W Promo Stamp",
    "winner": "Winner Stamp",
    "wizard-world-chicago": "Wizard World Chicago",
    "wizard-world-philadelphia": "Wizard World Philadelphia",
    "wotc": "WotC",
}

FOIL_LABELS = {
    "cosmos": "Cosmos Holo",
    "cracked-ice": "Cracked Ice Holo",
    "duskball": "Dusk Ball Pattern",
    "energy": "Energy Pattern",
    "friendball": "Friend Ball Pattern",
    "galaxy": "Galaxy Holo",
    "gold": "Gold",
    "league": "League",
    "loveball": "Love Ball Pattern",
    "masterball": "Master Ball Pattern",
    "mirror": "Mirror Holo",
    "player-reward": "Player Rewards",
    "pokeball": "Poké Ball Pattern",
    "professor-program": "Professor Program",
    "quickball": "Quick Ball Pattern",
    "rainbow": "Rainbow",
    "starlight": "Starlight Holo",
    "team-rocket": "Team Rocket Pattern",
    "tinsel": "Tinsel Holo",
}

SUBTYPE_LABELS = {
    "1999-2000-copyright": "1999-2000 Copyright",
    "1999-copyright": "1999 Copyright",
    "no-e-reader": "No e-Reader",
    "unlimited": "Unlimited",
}

# A key is its parts joined with this. Chosen because it cannot occur in a slug, and
# because a variant id carries the key after a "~" and must still read back unambiguously.
JOIN = "+"


def _title(slug: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in slug.replace("_", "-").split("-") if w)


def part_label(kind: str, slug: str) -> str:
    table = {"stamp": STAMP_LABELS, "foil": FOIL_LABELS, "subtype": SUBTYPE_LABELS}[kind]
    return table.get(slug) or _title(slug)


def _standard(card: dict) -> list[dict]:
    # Jumbo cards are a different object to put in a binder, and the app has no pocket for
    # one, so they are left out rather than offered as a variation of the standard card.
    return [
        v for v in card.get("variants_detailed") or []
        if isinstance(v, dict) and v.get("type") and (v.get("size") or "standard") == "standard"
    ]


def common_stamps(card: dict) -> set[str]:
    """The stamps every printing of a card carries: part of the card, not a variation of it."""
    stamp_sets = [set(v.get("stamp") or []) for v in _standard(card)]
    return set.intersection(*stamp_sets) if stamp_sets else set()


def special_printings(card: dict) -> list[dict]:
    """
    Every printing of `card` beyond its plain normal, holo and reverse.

    Each comes back as `{"type", "key", "label", "parts"}`: the TCGdex type it was
    printed on, a stable key for the app's variant id and the price file, the name a
    person reads, and the (kind, slug) pairs the key was built from, which is what the
    TCGplayer match looks for in a product name. `parts` is not shipped.
    """
    entries = _standard(card)
    if len(entries) < 2:
        return []

    common = common_stamps(card)

    def own_stamps(v: dict) -> list[str]:
        return sorted(set(v.get("stamp") or []) - common)

    by_type: dict[str, list[dict]] = {}
    for v in entries:
        by_type.setdefault(v["type"], []).append(v)

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    order = [t for t in TYPES if t in by_type] + [t for t in by_type if t not in TYPES]
    for kind in order:
        group = by_type[kind]
        unstamped = [v for v in group if not own_stamps(v)]
        base = min(
            unstamped,
            key=lambda v: bool(v.get("foil")) + bool(v.get("subtype")),
            default=None,
        )
        for v in group:
            if v is base:
                continue
            parts = [("stamp", s) for s in own_stamps(v)]
            if v.get("foil") and v.get("foil") != (base or {}).get("foil"):
                parts.append(("foil", v["foil"]))
            if v.get("subtype") and v.get("subtype") != (base or {}).get("subtype"):
                parts.append(("subtype", v["subtype"]))
            if not parts:
                continue
            key = JOIN.join(slug for _, slug in parts)
            if (kind, key) in seen:
                continue
            seen.add((kind, key))
            out.append({
                "type": kind,
                "key": key,
                "label": " · ".join(part_label(k, s) for k, s in parts),
                "parts": parts,
            })
    return out


def shipped(card: dict) -> list[dict]:
    """What pack.py writes: the printings without the match hints."""
    return [{k: p[k] for k in ("type", "key", "label")} for p in special_printings(card)]


def price_key(kind: str, key: str) -> str:
    """How a special printing is addressed in the price file: `holo~pokemon-center`."""
    return f"{kind}~{key}"
