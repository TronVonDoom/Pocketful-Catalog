# Card editor

A local app for fixing the catalog by hand: correct what a card or a set says, give a card
a picture it does not have, and tell the price pull which TCGplayer product a card or a
set really is. When you are done, **Publish** commits and pushes, and GitHub rebuilds what
the app downloads.

## Opening it

On Windows, once:

```powershell
powershell -ExecutionPolicy Bypass -File editor\install-shortcut.ps1
```

That puts **Pocketful Editor** in the Start menu and on the desktop. It opens in a window
of its own (Edge's app mode: no tabs, no address bar) with no console behind it, and the
server stops when you close the window. `-NoDesktop` skips the desktop icon; `-Remove`
takes both away again. Run it again if you move the repository or reinstall Python, since a
shortcut holds absolute paths to both.

Without installing anything, double-click `editor\Pocketful Editor.cmd`, or from a terminal:

```bash
python editor/server.py          # a browser tab at http://127.0.0.1:8766, Ctrl-C to stop
python editor/server.py --app    # its own window, exits when the window closes
```

No dependencies. It is `http.server` and one HTML file, for the same reason the rest of the
tooling is stdlib-only: a tool for fixing one Pokémon's name should not need a package
manager. Opening it a second time brings up another window onto the editor that is
already running rather than failing on the port. When it runs without a console, it logs
to `editor/editor.log`.

## The thing to understand first

**`catalog/sets/*.json` is output, not source.** It is what `pull_catalog.py --static`
wrote, and the weekly Catalog workflow re-runs that over every set and opens a PR with the
result. Anything typed directly into those files disappears the next time upstream is
pulled: not flagged, not conflicted, just replaced by whatever TCGdex said that morning.

So this editor never touches them. Everything it writes goes beside them:

```
catalog/sets/base1.json           what TCGdex says               (rewritten by every pull)
catalog/overrides.json            what you say instead           (yours, survives every pull)
catalog/art/                      pictures you supplied          (yours)
catalog/tcgplayer-groups.json     which TCGplayer group a set is (derived; "manual" entries are yours)
catalog/tcgplayer-cards.json      which product a card is        (yours)
dist/catalog-v1.json.gz           the merge                      (what the app downloads)
```

The pull stays a faithful copy of upstream; an override stays a deliberate statement that
upstream is wrong about one specific thing. Neither can quietly eat the other.

## Finding your way around

The left column starts with the work to do, then your own edits, then every set grouped by
series (newest first). A purple dot marks a set you have edited; an amber one, a set with
no TCGplayer group. The filter box at the top narrows the list.

Pokémon TCG Pocket comes last, under its own heading, and is left out of both to-do lists
and sorted below printed cards in search. It is the phone game: nothing in it has a
TCGplayer product or a second source of art, and because its sets are the newest in the
catalog it used to sit above everything else.

- **Cards without art**: every card with no picture from any source.
- **Sets not on TCGplayer**: sets whose cards get no price because no group was found.
- **Corrected cards**, **Cards linked by hand**, **Sets you changed**: what you have done.
- Clicking a **series name** lists its sets with their TCGplayer links, so a whole era can
  be linked in one sitting.

Opening a set shows its cards, with chips to narrow them to the ones without art, the
corrected ones, or the linked ones. **Edit set** opens the set itself in the right-hand
panel.

## Giving a card a picture

Select the card, then any of:

- **Drop** a file, or an image dragged out of another browser window, on the picture.
- **Paste** with Ctrl+V: a copied image, or a snip from **Win+Shift+S**, which is also the
  easiest way to crop a picture down to just the card.
- **Choose file…**, or paste a picture's **web address** and press Get.
- **TCGplayer photo**: the product photo of whichever TCGplayer product the card is priced
  from.

The *Find one* links search Google Images, TCGplayer, Bulbapedia and PkmnCards for that card.

Usually none of that is needed. **Linking a card** to a TCGplayer product gives it that
product's photo in the same step, if it has no picture yet. And when **Cards without art**
holds cards that are already matched to a product with the same name, a button over the
list takes all of their photos at once. Both write the product photo's address as
`imageAltSource: tcgplayer` — no file is committed — which is what `fill_gaps.py` writes
for the same cards, so the next refresh agrees with them rather than flagging them.

The picture is resized in the page, to fit 734×1024 at most (the size of the best scans
already in the catalog), and saved as WebP. You get a warning if it is smaller than
TCGdex's 600×825, since it would look soft full-size next to every other card, or if it is
not card-shaped. Nothing is written until you press **Save**.

On Save the file goes into `catalog/art/cards/`, named after the card and a hash of its
bytes, and the override points `imageAlt` at the address it will have on
raw.githubusercontent.com once pushed, marked `imageAltSource: manual`. A hashed name means
a replaced picture is a new URL, so nothing can go on serving the old one from cache. A file
no override refers to any more is deleted on the next save, so a picture you replaced
twice before publishing never reaches the repository.

**If the card already has TCGdex art**, your picture replaces it: the app draws a TCGdex
stem in preference to any fallback, so saving also overrides `image` to nothing.

**Hide wrong picture** is for art that shows the wrong card when there is nothing better to
put in its place. The wrong picture in someone's binder is worse than none. **Back to
upstream picture** undoes either.

One limit worth knowing: the app only ever *fills in* missing art on cards already in a
binder, so a replaced picture shows on cards added after the catalog updates, while copies
already filed keep the art they had.

## Correcting a card or a set

Fields that differ from upstream are highlighted, with a `was: …` link that puts the
original value back. **Revert to upstream** deletes the whole entry.

Card fields: `name`, `localId`, `rarity`, `illustrator`, `category`, `hp`, `types`,
`description` and the five press-run flags, with the three artwork fields under *Advanced*.
Set fields: `name`, `releaseDate`, `symbol` and `logo`. A logo can be dropped or pasted like
card art and is kept as PNG, because a logo's transparent background matters and the app
asks for `.png`.

That is exactly what `pack.py` ships (`CARD_FIELDS`, `SET_OVERRIDABLE`), and it is enforced
server-side, so an override on anything else is refused rather than accepted and silently
dropped at pack time. The tree holds more per card (attacks, abilities, weaknesses), but
the app never draws it, so editing it here would change nothing. A set's card count,
series and abbreviation are structure the pull derives and the app joins on, so they are
not open to a hand edit.

Two conveniences:

- An edit that ends up **agreeing** with upstream is not stored. Type a correction, change
  your mind, type the original back, and the entry is removed rather than kept as a no-op,
  so the override count stays a true measure of how far the shipped catalog departs from
  the pull.
- An empty box means "no opinion", never "this card has no illustrator". The only fields
  that can be overridden to *nothing* are the artwork ones, via Hide wrong picture.

## Linking to TCGplayer

A card is priced automatically by set, then by printed number: the set's TCGplayer group
comes from `tcgplayer-groups.json`, and within that group the product with the same number
wins, preferring the plain printing over a stamped one. The rules live in
`tools/tcgplayer.py`, which both this editor and `pull_prices.py` import, so the match the
editor shows you is the match the nightly job makes.

**A set.** *Change…* on a set, or *Link…* in any set table, lists every TCGplayer group,
ranked by how many of the set's cards it sells under the same number and name. That finds
pairs no name comparison can: TCGdex's *Sun & Moon* is TCGplayer's *SM Base Set*, and 80% of
its cards sit there at the same numbers. Groups already linked to another set are flagged.
**Not sold on TCGplayer** records that there is deliberately no group. The link is written as
`"via": "manual"`, with the automatic answer kept under `auto`, and `map_groups.py` never
re-derives it, not even with `--recheck`. **Use automatic match** puts the automatic answer back.

**A card.** The card's panel shows the product it is priced from, with today's market
prices. *Change product…* browses the set's group, with the same printed number first
since the right product is usually the stamped sibling beside the wrong one, or searches
all of TCGplayer ("charizard 4" narrows by number). A card link beats the number match
outright, even into another group. **Not sold** means the card carries no price at all,
which is different from not having looked. Linking a card to the product it already
matches stores nothing.

**A special printing.** Under the card's own product, the panel lists its stamped and
pattern printings — a Pokémon Center stamp, a Poké Ball reverse, a 1st Edition — each with
the product it is priced from and the same *Change product…*, **Not sold** and **Use
automatic match**. Those links go in `tcgplayer-cards.json` too, keyed
`<card id>~<type>~<printing>`. See the main README's *Special printings*.

Product lists come from the `catalog/.tcgcsv/` cache `map_groups.py` keeps. What is missing,
recent sets that are still filling in, and prices older than 20 hours are fetched from
tcgcsv.com and cached too.

## Publishing

**Publish** (with a count of unpublished changes) shows what changed in plain terms and
suggests a commit message. It then:

1. commits **only** the files above, so a half-finished change to a tool in the same
   working tree stays out of a commit that says it is catalog edits;
2. `git pull --rebase`, because the nightly price job commits newly mapped sets to main and
   this clone may be behind;
3. `git push`.

The push starts the Catalog workflow, which audits and republishes `catalog-v1.json.gz`.
Phones pick that up at their next catalog refresh (at most six days). A change to a
TCGplayer link also starts the Prices workflow.

If GitHub has changed the same lines you did, the rebase is aborted rather than left
half-done. A half-done rebase leaves conflict markers inside the very JSON files the editor
reads, which would break the editor. Your commit stays on this computer, nothing is pushed,
and the output says so. If git is already in the middle of a rebase or merge, Publish refuses
to start, and the header says what is unfinished.

Publish runs Git for Windows (`Program Files\Git\cmd\git.exe`) when it is installed, not
the first `git` on PATH. A shortcut gets the machine's PATH, where a toolchain's bundled
MSYS2 git can come first, and that git has neither line-ending conversion nor your GitHub
credentials. The repository's `.gitattributes` keeps line endings right under any git
regardless.

**Repack** runs `tools/pack.py --static` locally, the same script the workflow runs, so you
can check what would ship without publishing.

## Overrides that have gone off

Each override records what upstream said when it was written, which makes two conditions
detectable. Both are shown in the UI and counted in the header:

| | what it means | what to do |
|---|---|---|
| **upstream moved** | TCGdex has changed the field since you overrode it; possibly they fixed the very thing you were working around | Look. If they fixed it, revert. |
| **not in catalog** | No card with that id exists any more: a renamed set id, or a card upstream withdrew | Revert, or leave it parked until the card returns. |

Neither is acted on automatically. "Upstream changed" is not the same as "upstream is now
right", and an editor that silently dropped your corrections when TCGdex moved would be
solving the wrong problem.

## Safety

The server listens on 127.0.0.1 only. Because any web page open in the same browser can
still aim a request at localhost, every API call must come from the editor's own origin
(checked on `Host` and `Origin`), and everything that writes must be JSON, which a browser
will not send cross-site without a preflight this server never answers. That matters
because a POST here can commit and push.

## Committing edits by hand

Every file in the listing under *The thing to understand first*, apart from `catalog/sets/`
and `dist/`, is source. It is small, it is readable, and its diff is
the entire history of every correction anyone has made to the catalog, which is precisely
what editing `catalog/sets/` directly would have thrown away. Publish is a convenience,
not a requirement; committing those files any other way works the same.
