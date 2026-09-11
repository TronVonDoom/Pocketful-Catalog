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

## What belongs here, and what does not

This repository catalogs what a card **is**. Everything it carries was fixed the day the
card was printed: name, number, rarity, illustrator, artwork, HP, types, flavour text,
and which press runs it exists in.

It does not carry what a card is **worth**. That is a different fact with a different
lifetime — it changes daily, it comes from whoever is quoting it, and it is nobody's idea
of a printed property of the card. The app reads prices live from the source, and only
for the cards in your collection.

Keeping the line there is what lets the rest of this work. A document with no expiry can
be downloaded once and simply had; the app treats a catalog it already has as correct
forever and asks again only to learn whether a *new set* exists. Put a price in it and the
whole file inherits the shortest lifetime in it — the immutable half would start expiring
at the speed of the volatile half, and "downloaded once and kept" becomes "re-downloaded
on a TTL".

| | `catalog/sets/` | prices |
|---|---|---|
| what it answers | what is this card | what is it worth today |
| changes | when a set is printed | constantly |
| lives | here, shipped to the app | nowhere here; fetched live by the app |
| whole catalog costs | ~810 GraphQL requests | one REST request per card |

That cost asymmetry is worth knowing about: TCGdex's GraphQL schema exposes `image`,
`rarity` and `variants` but carries no `pricing` field and no `thirdParty` ids — both are
REST-only. So the data this repository wants is the cheap kind, and `fill_gaps.py` is the
only thing here that pays the REST cost, for the ~1,700 cards missing artwork.

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
   pokemontcg.io entry at all, and is filled from TCGplayer product `108589`.
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
python editor/server.py                        # fix a card by hand
```

## Correcting a card

`catalog/sets/` is output. The weekly refresh re-pulls every set over the top of it, so
an edit made there survives until the next Monday and no longer.

Corrections therefore live in `catalog/overrides.json`, which `pack.py` lays over the
pulled data on its way into the shipped file — the pull stays an honest copy of upstream,
the override stays an explicit disagreement with it, and neither eats the other. Each
entry also records what upstream said at the time, so the editor can tell you when TCGdex
has since changed a field you were working around.

`python editor/server.py` opens a local editor over all of it: browse or search, edit the
fields the app actually draws, and repack. See [editor/README.md](editor/README.md).

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
