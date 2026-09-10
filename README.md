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
released and when prices move, the app moves when someone writes a feature, and putting
two clocks in one repository means a nightly price refresh dirties the app's history
every single day. It also keeps ~20 MB of churning JSON out of every clone of the app,
and stops catalog releases from colliding with the APK releases Pocketful's in-app
updater reads.

## The two halves

The split runs through everything here, because the halves have opposite properties.

| | `catalog/sets/` | `catalog/prices/` |
|---|---|---|
| changes | when a set is printed | constantly |
| fetched by | GraphQL, 40 cards per request | REST, one request per card |
| whole catalog costs | ~810 requests | ~23,500 requests |
| shipped as | bundled with the app | downloaded, on a TTL |

That asymmetry is not a choice. TCGdex's GraphQL schema exposes `image`, `rarity` and
`variants`, but carries no `pricing` field and no `thirdParty` ids — both are REST-only.
So the half that almost never changes is cheap to refresh, and the half that changes
daily is expensive, which is exactly backwards and is why they run on separate
schedules.

Mixing them into one document is the mistake this layout exists to avoid: it would make
the immutable half expire at the speed of the volatile half.

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
python tools/pull_catalog.py --static --all    # the immutable half, GraphQL-batched
python tools/pull_catalog.py --prices --all    # the volatile half, REST
python tools/fill_gaps.py                      # resolve missing artwork
python tools/audit.py                          # check it, exit 1 on a new hole
python tools/audit.py --accept                 # record today's holes as the baseline
```

There is also a viewer, for the checks a script cannot make — whether the art on a card
is *the right art*:

```bash
python tools/serve.py          # http://127.0.0.1:8765/tools/viewer/index.html
```

Series in the sidebar, sets under them, every card as a thumbnail, and the raw JSON
behind any card you click. Cards filled from a second source are badged with which one,
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
