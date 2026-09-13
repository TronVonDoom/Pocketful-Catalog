# Pocketful Catalog

**The card data behind [Pocketful](https://github.com/TronVonDoom/Pocketful), built once
instead of fetched forever.**

---

## Why this is a separate repository

Pocketful used to ask [TCGdex](https://tcgdex.dev) every question at runtime: the set
index on every launch, a set's contents every time one was opened, and one HTTP request
per card in the collection on every sync. That works for one user and scales badly —
a thousand people with five-hundred-card collections is roughly seventeen requests a
second against a free, volunteer-run API that sets `Cache-Control: no-store` and so has
nothing absorbing repeats.

None of that data changes. A set has not changed since the day it was printed, so
re-fetching Base Set is paying a round trip for an answer that was already true in 1999.

So it gets built here, once, and published as a release asset the app downloads and
keeps. This repository is deliberately not the app: the catalog moves when a set is
released, the app moves when someone writes a feature, and two clocks in one repository
means every catalog refresh dirties the app's history. It also keeps ~20 MB of JSON out
of every clone of the app, and stops catalog releases from colliding with the APK
releases Pocketful's in-app updater reads.

## Two documents, two lifetimes

This repository builds two files, and keeping them apart is the single most important
thing about it.

**The catalog** is what a card **is**: name, number, rarity, illustrator, artwork, HP,
types, flavour text, press runs. Every bit of it was fixed the day the card was printed, so
the file has no expiry date — which is exactly what lets the app download it once and
simply keep it, asking again only to learn whether a *new set* exists.

**The prices** are what a card is **worth**, which is true for about a day.

They never share a file, a release tag or a TTL. Put a price inside the catalog and the
whole thing inherits the shortest lifetime in it: "downloaded once and kept" becomes
"re-downloaded nightly", and the 1.1MB of card data starts expiring at the speed of a
number that moves.

| | `catalog/sets/` | prices |
|---|---|---|
| what it answers | what is this card | what is it worth today |
| changes | when a set is printed | nightly |
| release tag | `catalog` | `prices` |
| app refreshes it | every six days | every twenty hours |
| built from | TCGdex | TCGplayer, via [tcgcsv.com](https://tcgcsv.com) |
| size | ~1.1 MB gzipped | ~0.12 MB gzipped |

### Why prices are built here rather than fetched by the app

An earlier version of this repository had no price file at all, on the reasoning that a
price is not a printed property of a card and should be read live from the source. The
reasoning was right and the arrangement did not survive contact with the data.

TCGdex carries a `pricing.tcgplayer` field that is simply **empty** for a large part of the
catalog — every promo not sold as an English single, most Japanese printings, the Trainer
Kits. For those cards the app had a choice between showing nothing and converting the
European price from euros, and the converted figure turned out to be badly wrong: MEP
Ceruledge converts to about $24 and actually trades at **$14**. Scarcity in Europe says
nothing about scarcity here.

TCGplayer's own catalog has all of them, and [tcgcsv.com](https://tcgcsv.com) publishes it.
What that service does not permit is being asked per user — its terms are one pull a day,
ten thousand requests, and an explicit *"design your integration to ingest the data into
your own database or cache rather than"* querying it live. One nightly job here is exactly
the arrangement it asks for, and it is the same bargain the catalog already strikes: one
client asks, everybody downloads the answer.

The result covers **20,064 of 23,548 cards**. The remainder is Pokémon TCG Pocket, which is
a phone game whose cards do not exist as objects, and Trainer Kits, which were never sold
as singles. Neither has a market price to miss.

### How a card finds its price

By set, then by printed number. `catalog/tcgplayer-groups.json` records which TCGplayer
group each set is; `map_groups.py` works that out once and leaves the answer in the tree
where a person can read it. It asks three ways, cheapest first — the normalised name, the
abbreviation either side carries, and failing both, one card's TCGplayer product id from
TCGdex, since every product id belongs to exactly one group and one card therefore settles
the whole set.

187 of 218 sets map. Of the 31 that do not, 15 are Pocket and most of the rest are Trainer
Kits — sets TCGplayer has no group for because it does not sell them.

A number alone decides only inside a group that is one set. A two-deck Trainer Kit numbers
both decks from one, so there the product has to agree on the name too, or the card carries
no price — which is better than the other deck's price, and that is what 170 of them used
to carry. Where a number holds both a card and its stamped copy, the plain one wins, and
a stamp is recognised whether TCGplayer writes it `[Staff]` or `(Pokemon Center Exclusive)`.

WotC-era groups quote "Unlimited Holofoil" and "1st Edition Holofoil" and no plain
"Holofoil" at all, so the price file writes the unlimited figure under the plain key and
keeps the 1st Edition one for the card's 1st Edition printing. Before that, an Unlimited
Jungle Clefable was quoted at its 1st Edition price, three times what it trades for.

## Price history

`pull_prices.py --history` records each night's figures into one small file per set
(`tools/price_history.py`), published on the `price-history` release tag as
`history-<set id>.json.gz`: every day for the last five weeks and one day per week before
that, back to February 2024. Every series is aligned with a `dates` list and holds exactly
what the price file carried that day, so a chart can never disagree with the price above
it. The app downloads the sets it needs -- one for a card's page, the collection's sets for
the portfolio chart -- and keeps them for most of a day.

The price file itself gains `previous`, every figure from the last recorded day before
this one, which is how a price tag shows its daily move without any history downloaded.

The past was filled in once from TCGCSV's daily archives (`price_history.py --backfill`,
run from the Prices workflow's manual trigger with *backfill* ticked), priced through the
current product match. Re-running it rebuilds the whole history; it is not needed nightly.

## Special printings

MEP Tyrunt is a holo promo, and it is also the same holo promo with a Pokémon Center stamp
at ten times the price. A Prismatic Evolutions common has a Poké Ball and a Master Ball
reverse. The five `variants` flags cannot say either, so the app had nowhere to file them.

TCGdex can: every card carries `variants_detailed`, one entry per printing with its stamps,
foil pattern and subtype. `tools/variants.py` reads that and names the printings that are
not the plain normal, holo or reverse — a stamp every printing shares (the set logo on MEP
promos) is part of the card, not a variation of it. About 4,600 printings across 3,300
cards come out of it.

`pack.py` ships them on each card as `special: [{type, key, label}]`, and the app files
each as a variant of its own. `pull_prices.py` prices them under `special` in the price
file, keyed `<type>~<key>` — kept out of `cards` so an older app never reads a stamped
copy's price as the plain one's. A printing is found by what TCGplayer writes after the
name (`(Poke Ball Pattern)`, `[Staff]`), in the card's own group first and then in the
promo groups stamped cards are filed under — Countdown Calendar Promos, World
Championship Decks, Base Set (Shadowless) — which is why the price pull reads every group
rather than only the linked ones. A 1st Edition is a printing of the plain product rather
than a product of its own, and is priced that way. Roughly 2,900 of the 4,600 find a
product; most of the rest are set-logo stamps TCGplayer does not list separately.

The editor shows each printing's product and can link any of them by hand, exactly like
the card itself.

Where the automatic answer is wrong, or missing, a person can overrule it at either level
from the editor. A set linked by hand is recorded in `tcgplayer-groups.json` as
`"via": "manual"`, which `map_groups.py` never re-derives. A card linked by hand to one
product goes in `catalog/tcgplayer-cards.json` and beats the number match outright, even
into another group. The matching rules live in `tools/tcgplayer.py`, shared by the price
pull and the editor, so the two cannot disagree about which product a card is.

## Holes

About 7% of the catalog — roughly 1,700 cards across 67 sets — has no artwork upstream.
Whole Trainer Kits, Shining Fates' Shiny Vault, Crown Zenith's Galarian Gallery, the
McDonald's sets, Ancient Mew. TCGdex derives a card's image from whether the asset
exists on their CDN, and for these it does not, in any language.

That is not the bug. The bug was that **a card with no artwork and a card that was never
fetched look identical once they are on disk**, so nobody could tell a real absence from
a failed pull. Every hole is now either written down in `catalog/holes.json` with a
reason, or it fails the audit.

`fill_gaps.py` closes most of them, in three tiers:

1. **pokemontcg.io** — proper scans, covering the big whole-set holes. Its set ids
   differ from TCGdex's (`sma` against `swsh4.5sv`), which is what `SET_ALIASES` is for.
2. **TCGplayer product photos**, keyed by the id TCGdex hands out in
   `variants_detailed[].thirdParty.tcgplayer`. Lower fidelity than a scan, but it
   reaches the oddities the card databases never filed — Ancient Mew has no
   pokemontcg.io entry at all, and is filled from TCGplayer product `108589`. TCGdex no
   longer hands those ids out for new cards, so failing one, the photo of the product the
   card is *priced* from is used: a link made in the editor, or the automatic match when
   its name agrees with the card's.
3. **Nothing**, recorded as a hole with a reason. Mostly Trainer Kits, which are not
   sold as singles, so no product photo exists either.

Filled art is written to `imageAlt` beside `image` rather than into it, so a later
upstream pull that *does* have the scan wins automatically and nobody has to remember
which stems were invented here.

## The tools

```bash
python tools/pull_catalog.py --static --all    # every card, GraphQL-batched
python tools/fill_gaps.py                      # resolve missing artwork
python tools/audit.py                          # check it, exit 1 on a new hole
python tools/audit.py --accept                 # record today's holes as the baseline
python tools/pack.py --static                  # build what the app downloads
python tools/map_groups.py                     # match sets to TCGplayer groups
python tools/pull_prices.py                    # build the nightly price file
python editor/server.py                        # the editor: fix cards, add art, link TCGplayer
```

## Correcting a card

`catalog/sets/` is output. The weekly refresh re-pulls every set over the top of it, so
an edit made there survives until the next Monday and no longer.

Corrections therefore live in `catalog/overrides.json`, which `pack.py` lays over the
pulled data on its way into the shipped file — the pull stays an honest copy of upstream,
the override stays an explicit disagreement with it, and neither eats the other. Each
entry also records what upstream said at the time, so the editor can tell you when TCGdex
has since changed a field you were working around.

The editor is where all of it happens by hand. On Windows,
`editor\install-shortcut.ps1` adds **Pocketful Editor** to the Start menu and the desktop,
and it opens in a window of its own. From a terminal, `python editor/server.py` does the same
in a browser tab. In it you can:

- correct the fields the app draws, on a card or on a set;
- give a card with no picture one, by dropping, pasting or choosing a file, or taking the
  TCGplayer product photo. It is committed under `catalog/art/` and pointed at by the
  override;
- link a set to its TCGplayer group, or a card to its exact product, where the automatic
  match is wrong;
- **Publish**, which commits exactly those files and pushes them, so GitHub rebuilds what the
  app downloads.

See [editor/README.md](editor/README.md).

`series_report.py` walks the catalog era by era, oldest first, and answers the two
questions `audit.py` deliberately does not: **is every card's picture as good as every
other card's**, and **does every card carry the same kind of information as its
neighbours**. Neither is a hole, so neither fails the audit, and both are exactly what
someone notices when they open a binder.

Every finding it prints raises the same question -- did the pull drop this, or was it
never there? `--check-upstream N` samples N of them and asks TCGdex directly, so that is
a command rather than an afternoon.

```bash
python tools/series_report.py --serie base --check-upstream 20
```

Every card in the catalog now carries a picture at 600x825 or better, or none at all;
there is no middle tier. That was not true at first -- the TCGplayer fallback was
fetching a 437px box, which renders ~310x437, roughly half the linear resolution of
every other card. Fine in a grid tile and visibly soft the moment anyone opened one full
size. The box is 874 now, which lands at ~620x874, slightly larger than TCGdex's own.

There is also a viewer, for the checks a script cannot make — whether the art on a card
is *the right art*:

```bash
python tools/serve.py          # http://127.0.0.1:8765/tools/viewer/index.html
```

Series in the sidebar, sets under them, every card as a thumbnail, and the raw JSON
behind any card you click. Each era carries its own health -- card count, holes, soft
images -- and each set a coloured dot for the worst thing true of it, so "where is the
catalog weak" is answered by looking rather than by opening twenty sets. Open a set and
it shows the mix of renditions its pictures come from, and which fields some of its
cards carry and others do not.

Those rollups come from `catalog/summary.json`, written by
`series_report.py --write-summary`. It is derived and regenerable, but committed anyway:
a weekly refresh that quietly drops a set's artwork shows up there as a few changed
numbers, which is reviewable in a way that 23,000 changed card records is not. Cards filled from a second source are badged with which one,
and cards with no art anywhere are badged as holes, so the two are never confused on
screen either. It reads the working tree directly and caches nothing, which means it can
be left open while a pull runs.

`audit.py` touches no network. It reads what the pull wrote, so it costs TCGdex nothing
to run on every commit, and a failure means the catalog is wrong rather than that the
API was having an afternoon.

## Being a good guest

Everything here identifies itself, backs off, and batches where batching exists.

Running the price pull once a day and serving the result is *kinder* to TCGdex than the
app asking them directly, and by a wide margin: one client with a User-Agent and a
retry schedule, once a day, instead of one request per card per user per sync. Above
about one user, centralising the fetch is the polite option, not the greedy one.

If you are from TCGdex and this is causing you trouble, open an issue — that is what
the User-Agent is for.

## License

MIT for the code here. The card data is TCGdex's, itself MIT-licensed at
[tcgdex/cards-database](https://github.com/tcgdex/cards-database).

Card names, artwork and set data are the property of their respective owners. Pokémon
and the Pokémon TCG are trademarks of Nintendo, Creatures Inc. and GAME FREAK Inc. This
project is not affiliated with any of them.
