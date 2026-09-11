# Card editor

A local web app for correcting what the catalog says about a card.

```bash
python editor/server.py
```

Opens `http://127.0.0.1:8766`. No dependencies — it is `http.server` and one HTML file,
for the same reason the rest of the tooling is stdlib-only: a tool for fixing one
Pokémon's name should not need a package manager.

## The thing to understand first

**`catalog/sets/*.json` is output, not source.** It is what `pull_catalog.py --static`
wrote, and the weekly Catalog workflow re-runs that over every set and opens a PR with
the result. Anything typed directly into those files disappears the next time upstream is
pulled — not flagged, not conflicted, just replaced by whatever TCGdex said that morning.

So this editor never touches them. Edits go to **`catalog/overrides.json`**, and
`pack.py` lays them over the pulled data on its way into `dist/catalog-v1.json.gz`. The
pull stays a faithful copy of upstream; the override stays a deliberate statement that
upstream is wrong about one specific thing. Neither can quietly eat the other.

```
catalog/sets/base1.json     what TCGdex says        (rewritten by every pull)
catalog/overrides.json      what you say instead    (yours, survives every pull)
dist/catalog-v1.json.gz     the two, merged         (what the app downloads)
```

## Using it

The left column is every set, grouped by era; a dot marks a set you have edited. The
middle is the cards. The right is the form.

Fields that differ from upstream are highlighted, with a `was: …` link that puts the
original value back. **Save override** writes the entry; **Revert** deletes it and
returns the card to whatever upstream says.

Two conveniences worth knowing:

- An edit that ends up **agreeing** with upstream is not stored. Type a correction, change
  your mind, type the original back, and the entry is removed rather than kept as a
  no-op — so the override count stays a true measure of how far the shipped catalog
  departs from the pull.
- **Repack** runs `tools/pack.py --static`, the same script the publish workflow runs, so
  you can check what actually ships without a second terminal.

## Overrides that have gone off

Each entry records what upstream said at the time it was written. That makes two
conditions detectable, and both are surfaced in the UI and counted in the header:

| | what it means | what to do |
|---|---|---|
| **upstream moved** | TCGdex has changed the field since you overrode it — possibly they fixed the very thing you were working around | Look. If they fixed it, revert. |
| **not in catalog** | No card with that id exists any more — a renamed set id, or a card upstream withdrew | Revert, or leave it parked until the card returns. |

Neither is acted on automatically. "Upstream changed" is not the same as "upstream is now
right", and an editor that silently dropped your corrections when TCGdex moved would be
solving the wrong problem.

## What can be edited

`name`, `localId`, `rarity`, `illustrator`, `category`, `hp`, `types`, `description`,
`image`, `imageAlt`, `imageAltSource`, and the five press-run flags.

That list is exactly what `pack.py` ships, and it is enforced server-side in
`FIELDS` — an override on anything else is refused rather than accepted and silently
dropped at pack time. The tree under `catalog/sets/` holds more per card (attacks,
abilities, weaknesses, the detailed variant breakdown), but the app never draws it, so
editing it here would change nothing.

Prices are not editable, and not missing by oversight: the catalog does not carry them at
all. It records what a card *is*; what it is *worth* is fetched live by the app. See the
repository README.

## Committing edits

`catalog/overrides.json` is source. Commit it. It is small, it is readable, and its diff
is the entire history of every correction anyone has made to the catalog — which is
precisely what editing `catalog/sets/` directly would have thrown away.
