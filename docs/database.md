# The Pocketful catalog database

**Status: approved 2026-09-13.** The schema is in [`supabase/migrations/`](../supabase/migrations)
and is tested by `python tools/test_database.py`. Changes to the design are made here
first, then in a new migration.

---

## What changes

Today the catalog **is** TCGdex. It is pulled every week, `overrides.json` is laid over
the top, and the result is published as one file the app downloads. A correction is an
exception to someone else's data.

From here on the catalog is **yours**. It lives in a database that only you write to.
TCGdex, pokemontcg.io and TCGplayer stop being the truth and become *sources*: they
suggest what a series, set, card or picture should be, and you decide. **Nothing reaches
the app until you have reviewed a set and published it.** Until the first set is
published, the app's catalog is empty, and that is expected.

## Three places, three jobs

```
  ┌────────────┐   reads/writes   ┌──────────────────────┐
  │   Editor   │ ───────────────▶ │  Database (private)  │ ◀── importers (TCGdex, …)
  │ (your PC)  │                  │  Supabase Postgres   │ ◀── nightly price job
  └─────┬──────┘                  └──────────────────────┘
        │ Publish
        ▼
  ┌──────────────────────┐   downloads   ┌───────┐
  │  Storage (public,    │ ────────────▶ │  App  │
  │  read-only) R2       │               └───────┘
  └──────────────────────┘
```

- **The database** is the source of truth. Every series, set, card, printing, picture
  record, TCGplayer link and review mark is here. It is private: only the editor and the
  tools can reach it, using an admin key that lives on your PC. It is in Supabase.
- **Storage** holds what the app downloads: an index of published sets, one file per
  published set, the pictures, and the prices. Anyone can read it and only your R2 token
  can write to it. It is on Cloudflare R2, so the app never depends on Supabase: a paused
  Supabase project stops the editor, never the app.
- **The app** never talks to the database and carries no key. It downloads files and
  keeps them, just as it does today. This keeps it working offline and fast, and a
  thousand users cost almost nothing.

## Scope

| | |
|---|---|
| Game | Pokémon TCG, `ptcg`. Pokémon TCG Pocket would be a second game later. |
| Catalogs | English `en`, Japanese `jp`, Traditional Chinese `cht`, Simplified Chinese `chs` |
| Cards | Every physical card: main sets, promos, subsets, decks, kits, jumbo cards |

Each language is its own catalog with its own series and sets, because the sets do not
line up. One English set is often two Japanese sets combined, and Japan and China both
have sets that were never printed in English. A card can be *linked* to the same card in
another language, but it is never the same record.

---

## IDs

An ID is built from the thing's parts by the database, never typed by hand. It can change
freely until the set it belongs to is published, and changing a draft set's code renames
every card, printing and picture under it. After publishing, an ID never changes and is
never deleted. Something published by mistake is **withdrawn** (hidden from the app),
because someone's collection may already hold it.

| What | ID | Built from |
|---|---|---|
| Catalog | `ptcg-en` | game, language |
| Series | `ptcg-en-me` | catalog, series code |
| Set | `ptcg-en-me05` | catalog, set code |
| Card | `ptcg-en-me05-062` | set, printed number |
| Printing | `ptcg-en-me05-062_reverse` | card, `_`, variant name |

The series is **not** in the set or card ID. A series is a grouping you might rearrange
(moving a promo set, splitting an era), and rearranging should not rename every card in it.

### Set codes

A set code follows its series: the series code plus the set's place in that series,
two digits, in release order. Pitch Black is the fifth Mega Evolution set, so it is
`me05`. A half set takes `.5`, as in `me02.5`. A series' promo set is the series code
plus `p`, as in `mep`. Codes are lowercase `a–z`, `0–9` and `.`, and unique within a
catalog. The code printed on recent cards (PBL) is kept as the set's `abbreviation`, but
never used in an ID, because older cards print none.

### Subsets

Trainer Gallery, Galarian Gallery, Shiny Vault, Radiant Collection and the like stay
**inside their parent set**, as printed. Their numbers are already distinct (`tg01`,
`gg01`, `sv001`), and each card carries the subset's name in `section` so the app can
show it as a heading within the set.

### Card numbers

The number is what is printed before the slash, lowercased, with its leading zeros kept:

| Printed | In the ID |
|---|---|
| 062/084 | `062` |
| 4/102 | `4` |
| SWSH050 | `swsh050` |
| TG01/TG30 | `tg01` |
| 25a | `25a` |

Three rules cover the cards that do not fit:

- **A number that repeats in one set keeps its whole printed number.** Celebrations'
  Classic Collection reprints Venusaur 15/102, Here Comes Team Rocket! 15/82, Rocket's
  Zapdos 15/132 and Claydol 15/106, so those become `15-102`, `15-82`, `15-132` and
  `15-106`.
- **A symbol is spelled out.** Unown ? and Unown ! become `question` and `exclamation`.
- **A card with no printed number gets one assigned in the editor**, and is marked as
  assigned so the app never presents it as printed. Early Japanese cards need this.

### Printing names

Every printing has a variant name, including the plain one. `ptcg-en-me05-062` is the card
(its name, attacks and artwork), and anything with an `_` is a specific printing someone
can own. Collections always store printings.

The name is assembled from a fixed word list (the `variant_words` table), always in this
order:

**edition → pattern → finish → stamps → error**

| Part | Written when | Words, for example |
|---|---|---|
| edition | not the normal unlimited print | `1st-edition`, `shadowless` |
| pattern | the foil has a pattern | `cosmos`, `cracked-ice`, `pokeball`, `masterball` |
| finish | always | `normal`, `holo`, `reverse` |
| stamps | stamped, in word-list order | `staff`, `pre-release`, `pokemon-center` |
| error | a known misprint | `no-holo-error` |

| Printing | ID |
|---|---|
| Pitch Black Bastiodon, reverse holo | `ptcg-en-me05-062_reverse` |
| Base Set Charizard, unlimited | `ptcg-en-base01-4_holo` |
| Base Set Charizard, shadowless | `ptcg-en-base01-4_shadowless-holo` |
| Base Set Charizard, 1st Edition | `ptcg-en-base01-4_1st-edition-holo` |
| A Poké Ball pattern reverse | `…_pokeball-reverse` |
| A Cosmos holo promo with a staff stamp | `…_cosmos-holo-staff` |

Because the unlimited edition is left out rather than written, adding a new variation to a
card later never renames one that already exists. A word that is not in the list cannot be
used until you add it in the editor, so two printings of the same kind can never end up
with two different spellings. A word in use can have its label changed but cannot be
renamed or removed.

---

## Tables

Field names are what the database calls them. Every table except the logs also has
`created_at`, `updated_at` and a free-text `notes`.

### `games` and `catalogs`

| Field | Example | Meaning |
|---|---|---|
| `games.id` | `ptcg` | |
| `games.name` | Pokémon TCG | |
| `catalogs.id` | `ptcg-en` | |
| `catalogs.game_id` | `ptcg` | |
| `catalogs.language` | `en` | |
| `catalogs.name`, `native_name` | English | |
| `catalogs.sort` | 1 | order the app lists catalogs in |

A catalog's **card back** is a picture (role `back`) in `images`.

### `series`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me` | |
| `catalog_id` | `ptcg-en` | |
| `code` | `me` | |
| `name` | Mega Evolution | as printed in that language |
| `name_en` | | English name, for series in other languages, so they can be searched in English |
| `sort` | 23 | order within the catalog |
| `status` | `draft` / `published` | published once any of its sets is |

Its logo, and a card back of its own where its cards' back differs from the catalog's
(vintage Japanese cards, for one), are pictures in `images`.

### `sets`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05` | |
| `series_id` | `ptcg-en-me` | |
| `code` | `me05` | |
| `name` | Pitch Black | as printed |
| `name_en` | | English name, for sets in other languages |
| `kind` | `expansion` | `expansion`, `special`, `promo`, `deck`, `kit`, `other` |
| `release_date` | 2026-07-17 | |
| `printed_total` | 84 | the number after the slash on the main set's cards |
| `abbreviation` | PBL | the printed set code, where one exists; never part of an ID |
| `sort` | 5 | order within the series |
| `no_logo`, `no_symbol` | false | confirmed that the set has no logo or symbol to show |
| `tcgplayer_group`, `tcgplayer_via` | | TCGplayer group ID, and whether it was matched `auto` or `manual` |
| `status` | `draft` | `draft`, `published`, `published_changed` (published, with edits not yet published) |
| `version` | 3 | bumped by every publish |
| `published_at` | | |

Its logo and symbol are pictures in `images`. How many cards and printings a set has is
counted from the cards, not stored.

### `cards`

What every printing of a card shares.

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05-062` | |
| `set_id` | `ptcg-en-me05` | |
| `number` | `062` | the ID form |
| `printed_number` | 062/084 | exactly as printed, for display |
| `number_assigned` | false | true when the card has no printed number |
| `section` | | the subset it belongs to inside the set (Trainer Gallery), or empty |
| `sort` | 62 | order within the set, so `tg01` sorts after the main set |
| `name` | Bastiodon | as printed |
| `name_en` | | English name, for cards in other languages |
| `category` | `pokemon` | `pokemon`, `trainer`, `energy` |
| `subtypes` | `stage-2` | stage, trainer kind, mechanic (ex, V, Mega…), each a term |
| `hp` | 160 | |
| `types` | `metal` | energy types, each a term |
| `evolves_from` | Shieldon | |
| `abilities` | | list of name, kind (a term: Ability, Poké-Power…), text |
| `attacks` | | list of name, cost (types), damage, text |
| `weaknesses`, `resistances` | fire ×2 / grass −30 | list of type and value |
| `retreat` | 4 | |
| `rules` | | rule-box text |
| `flavor_text` | | |
| `illustrator` | Kinu Nishimura | |
| `rarity` | `rare` | a term |
| `regulation_mark` | J | |
| `dex_numbers` | 411 | |
| `no_image` | false | confirmed that no picture exists anywhere yet; the app shows the card back |
| `same_as` | | cards sharing this value are the same card in different languages |
| `review` | `unreviewed` | `unreviewed`, `reviewed`, `flagged` |
| `review_note`, `reviewed_at` | | why it is flagged; when it was reviewed |
| `withdrawn` | false | hidden from the app; published cards are never deleted |

Its picture is in `images`.

### `printings`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05-062_reverse` | |
| `card_id` | `ptcg-en-me05-062` | |
| `variant` | `reverse` | the assembled name |
| `edition`, `pattern`, `finish`, `stamps`, `error` | `reverse` | its parts, each a word from `variant_words` |
| `tcgplayer_product`, `tcgplayer_printing`, `tcgplayer_via` | | TCGplayer product ID, its printing (Reverse Holofoil), and how it was matched |
| `identify` | | how to tell this printing apart, shown in the app |
| `review`, `review_note`, `reviewed_at` | | as on cards |
| `withdrawn` | false | as on cards |

A printing that looks different from its card has a picture of its own in `images`; the
rest use the card's.

### `variant_words`

| Field | Example | Meaning |
|---|---|---|
| `word` | `pokeball` | what goes in the ID |
| `kind` | `pattern` | `edition`, `pattern`, `finish`, `stamp`, `error` |
| `label` | Poké Ball Pattern | what the app shows |
| `description` | | how to recognise it |
| `sort` | | its position among words of its kind, which decides stamp order |

Seeded with 139 words from what today's catalog already uses. Two words TCGdex uses for
both a stamp and a foil were given one meaning each: `pokeball` is the pattern and
`pokeball-stamp` the stamp; `professor-program` is the stamp and `professor-program-foil`
the foil. World Championships player signatures were left out until those decks are
decided on (see the end of this document).

### `terms`

One spelling for everything that is a fixed choice: rarities, energy types, subtypes and
ability kinds. TCGdex writes both "Holo Rare" and "Rare Holo", and both "Illustration
rare" and "Illustration Rare"; a card picks a term instead of typing text, and a term in
use cannot be renamed or removed.

| Field | Example | Meaning |
|---|---|---|
| `kind` | `rarity` | `rarity`, `type`, `subtype`, `ability_kind` |
| `code` | `special-illustration-rare` | |
| `labels` | `{"en": "Special Illustration Rare"}` | how it is written in each catalog |
| `sort` | | |

Seeded with 71 terms, English labels only. Japanese and Chinese labels are added from a
real source when those catalogs start, not guessed.

### `images`

Every picture, chosen or not, with where it came from.

| Field | Example | Meaning |
|---|---|---|
| `id` | | |
| `catalog_id`, `series_id`, `set_id`, `card_id`, `printing_id` | a card | what it is a picture of; exactly one is filled |
| `role` | `front` | `front` (card, printing), `back` (catalog, series), `logo` (series, set), `symbol` (set) |
| `chosen` | true | the one in use, one per subject and role; the rest are candidates |
| `path`, `thumb_path` | `images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.webp` | where it is in the public R2 bucket |
| `width`, `height`, `bytes`, `sha256` | | |
| `below_standard` | false | smaller than the standard size |
| `source_id` | `tcgdex` | a row in `sources` |
| `source_url` | | where it was taken from |
| `original_path` | | the untouched original in the private R2 bucket, kept only when it cannot be downloaded again |

A candidate can be just a `source_url`; a chosen picture has to be in R2.

### `sources` and `source_records`

A source is a place data can come from: `tcgdex`, `pokemontcg-io`, `tcgplayer`,
`upload`, and `pocketful-2026`, the TCGdex-built catalog as it stood before this database.
**Nothing arrives from any source unless you import it** (see [Imports](#imports)).

| Field | Example | Meaning |
|---|---|---|
| `sources.id` | `tcgdex` | |
| `sources.name`, `url` | | |
| `sources.terms` | | what its license or terms allow, in your words |
| `source_records.source_id` | `tcgdex` | |
| `source_records.language` | `en` | |
| `source_records.kind` | `card` | `series`, `set`, `card` |
| `source_records.key` | `me05-062` | the source's own ID |
| `source_records.data` | | everything the source said, untouched |
| `source_records.fetched_at` | | |
| `source_records.matched` | `ptcg-en-me05-062` | which of your records it describes, once decided |
| `source_records.matched_hash` | | the data as it was when you reviewed the match |
| `source_records.changed` | false | worked out by the database: a later import of the set brought different data from what you reviewed |

This is what lets the editor say "TCGdex now says 170 HP; you say 160", and what lets it
point at fields where two sources disagree. For Japanese and Chinese cards you cannot
proofread, agreement between sources is what the text is trusted on.

### `publishes`

| Field | Meaning |
|---|---|
| `set_id`, `version`, `published_at` | |
| `cards`, `printings` | how many went out |
| `file`, `sha256` | the set file that was written |

### `change_log`

Every edit to every table except the source records, recorded by the database itself:
table, ID, field, value before, value after, when. Git gave the catalog a full history,
and moving to a database must not lose that.

---

## Reviewing and publishing

### Imports

Nothing enters the database by itself. There is no bulk import, no scheduled pull and no
background refresh. An import is always **one set, from one named source, started by you**
in the editor ("get Base Set from TCGdex"). It fills `source_records` for that set only
and shows you what arrived.

What an import brings is still only a suggestion: a series, set, card or picture exists
only once you create or accept it. Nothing imported before this database carries over.
Your old corrections, pictures and TCGplayer links are the `pocketful-2026` source, and
they come in the same way, one set at a time, if and when you choose.

### A set's life

1. **Create** the series, if it is new, and the set: code, name, dates, logo, symbol.
   If you have imported the set, the form is pre-filled from that import, and you accept
   or change each field.
2. **Pick the cards.** The editor lists the cards your import brought as candidates. You
   take the ones that belong, and can add a card no import has.
3. **Review each card:** picture beside fields, fields that sources disagree on
   highlighted. Accept, fix, or flag, then move to the next with the keyboard.
4. **Review its printings:** which ones exist, a picture for any that looks different,
   and the TCGplayer product (suggested automatically, the way prices are matched today).
5. **Publish.**

### What Publish requires

The database refuses to publish a set (`publish_problems` lists why) until:

- it has at least one card;
- every card and printing is reviewed, and none is flagged;
- every card has at least one printing;
- every card has a chosen picture, **or** is marked `no_image`;
- the set has a logo and a symbol, or is marked as having none;
- if any card is marked `no_image`, the catalog or the series has a card back.

A card published without a picture is drawn as its card back in the app. The editor keeps
those cards in a **Published without a picture** list, so each one can be given a picture
later. Adding one is an ordinary edit: the set becomes `published_changed`, and the next
publish sends the picture out.

### What Publish does

1. Uploads the set's chosen pictures and writes `catalog/sets/ptcg-en-me05.v3.json.gz`
   to the public R2 bucket.
2. Rewrites `catalog/index.json` there from the database: every published catalog, series
   and set, with names, logos, card backs and versions.
3. Calls `record_publish`, which checks the set again, bumps its version, marks it and its
   series published, locks every card and printing that went out, and records the publish.

Only `record_publish` can lock a record, mark it published or change a set's version.
Any edit afterwards to something the app shows sets the status to `published_changed`;
notes and TCGplayer links do not, because neither is in the set file. Publishing again
writes a new version, and the app downloads only that set again.

---

## What the app downloads

Everything is in one public R2 bucket, `pocketful`, under a folder per kind of file:

| Path | What |
|---|---|
| `catalog/index.json` | every published catalog, series and set, with names, logos, card backs, versions, and the public address every other path is relative to |
| `catalog/sets/ptcg-en-me05.v3.json.gz` | one published set |
| `images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.webp` | a card picture |
| `images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.thumb.webp` | its thumbnail |
| `images/sets/ptcg-en/me05/logo.81bd40.webp` | a set logo |
| `images/backs/ptcg-en.5e0a11.webp` | a card back |
| `prices/prices.json.gz` | today's prices, by printing ID |
| `prices/history/ptcg-en-me05.json.gz` | one set's price history |

Untouched originals go in a second bucket, `pocketful-originals`, which is private and
never downloaded by the app.

The only address built into the app is that of `catalog/index.json`. The index names the
public address every other path is relative to, so pictures, set files and prices can move
by changing one value in it. Moving the index itself, to your own domain before launch,
takes one app update, which a launch is anyway.

A set file, trimmed:

```json
{
  "schema": 2,
  "id": "ptcg-en-me05",
  "version": 3,
  "series": "ptcg-en-me",
  "code": "me05",
  "name": "Pitch Black",
  "releaseDate": "2026-07-17",
  "printedTotal": 84,
  "logo": "images/sets/ptcg-en/me05/logo.81bd40.webp",
  "cards": [
    {
      "id": "ptcg-en-me05-062",
      "number": "062",
      "printedNumber": "062/084",
      "name": "Bastiodon",
      "category": "pokemon",
      "hp": 160,
      "types": ["metal"],
      "rarity": "rare",
      "illustrator": "Kinu Nishimura",
      "image": "images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.webp",
      "printings": [
        { "id": "ptcg-en-me05-062_holo", "finish": "holo" },
        { "id": "ptcg-en-me05-062_reverse", "finish": "reverse" }
      ]
    }
  ]
}
```

A card with no picture simply has no `image`, and the app draws the card back the index
gives for its series or catalog. The file carries what the app shows today plus printings,
sections, English names and labels. Attack and ability text stays in the database until
the app has a screen for it, at which point it joins the file under a new `schema` number.
The schema number is in the format so that an older build never misreads a newer file.

A picture's file name ends in a short hash of its contents, so a replaced picture has a new
address and nothing can go on showing the old one from a cache.

## Prices

The nightly job stays in this repository and keeps reading TCGCSV. What changes is
that it reads the TCGplayer links from the database instead of from JSON files, and keys
the price file by **printing ID**. Only published printings are priced. The matching
rules in `tools/tcgplayer.py` survive as the suggestion the editor makes during review.
The link itself is confirmed by you, not guessed at publish time.

Japanese prices can come from TCGplayer's Japanese category later. Chinese cards will
mostly have none, and will show none rather than a converted guess.

## Pictures

- **Card-shaped (63:88) and cropped to the card's edge.**
- **WebP, at most 734×1024**, the size of the best scans already in the catalog, with a
  thumbnail at 245×342.
- **Never enlarged.** A smaller source is kept at its own size and marked
  `below_standard`, so the editor can list every picture worth replacing.
- **Where each one came from is always recorded.** If a site ever asks for its pictures to
  be removed, every one of them can be found and replaced.
- **An original is kept (privately) only when it cannot be fetched again**, meaning an
  upload, a paste or a scan. A picture taken from a web address keeps the address and a
  hash instead, which saves storage.

## Security

- **Row-level security is on for every table, with no access rules**, and the public
  roles have had every grant on tables and functions taken away, so the database cannot
  be read, written or called without the admin key. `tools/test_database.py` checks this.
- **The admin key lives at `C:\Users\TronVonDoom\keystores\pocketful-supabase.json`**,
  beside the release signing key, and as a GitHub secret for the nightly price job. It is
  never in either repository and never in the app. The file holds the project `url`, the
  `secret_key` (for the REST API, which is all the editor and the nightly jobs use from
  Supabase) and the `database_password` (only for applying migrations). `tools/supabase_config.py`
  reads it.
- **Migrations connect to PostgreSQL directly**, which on Supabase is reachable over IPv6
  only. This PC has IPv6. GitHub's runners do not, which is one more reason the nightly
  jobs use the REST API rather than the database connection.
- **R2 credentials live at `C:\Users\TronVonDoom\keystores\pocketful-r2.json`**: the S3
  endpoint, an access key pair, the two bucket names and the public address. The token
  behind them has Object Read & Write on the two buckets and nothing else. `tools/r2.py`
  reads it.
- **The public bucket `pocketful`** can be read by anyone and written only with that
  token. **The private bucket `pocketful-originals`** cannot be read publicly at all.
- **Supabase storage is not used.** Its buckets were removed before anything was written.

## Costs

**Supabase** holds only the database. At the time of writing, its free plan includes a
500 MB database, which the catalog's text fits in comfortably, and pauses a project after a
week with no activity. A pause only stops the editor until the project is resumed from the
dashboard; the app does not use Supabase.

**Cloudflare R2** holds everything the app downloads. At the time of writing, its free
tier includes 10 GB of storage and millions of reads and writes a month, and downloads cost
nothing. English pictures and thumbnails come to roughly 2–3 GB, so all of English fits
free. Cloudflare needs a payment method on file to enable R2 even within the free tier.

The public address is Cloudflare's `r2.dev` one for now. Cloudflare rate-limits it and says
it is for development, so before launch the bucket gets your own domain (about $10 a year).

Check [supabase.com/pricing](https://supabase.com/pricing) and
[Cloudflare's R2 pricing](https://developers.cloudflare.com/r2/pricing/) before relying
on any of these numbers.

## Testing and applying the schema

```bash
python tools/test_database.py      # every migration and rule, on a throwaway local PostgreSQL
python tools/migrate.py            # what the Supabase project has applied, and what it has not
python tools/migrate.py --apply    # apply the rest, each in one transaction
```

A schema change is a new file in `supabase/migrations/`, never an edit to one already
applied. `migrate.py` records what has run in the same table the Supabase CLI uses.

`test_database.py` builds a throwaway PostgreSQL in a temporary folder, stands in for the parts of
Supabase the migrations rely on (its roles, default grants and storage schema), applies
every migration, runs `supabase/tests/`, and deletes it all again. Nothing touches
Supabase or any other database. It needs PostgreSQL's command-line programs, which are
installed on this PC.

---

## What happens to what exists today

| Today | Becomes |
|---|---|
| `pull_catalog.py`, `fill_gaps.py` | reworked into the per-set import, run only when you start it |
| `catalog/sets/`, `overrides.json`, `catalog/art/` | the `pocketful-2026` source, imported one set at a time only if you choose |
| `tcgplayer-groups.json`, `tcgplayer-cards.json` | part of the same source, on the same terms |
| The editor | reworked to read and write the database; still a Windows app, still no dependencies |
| The `catalog` release and weekly workflow | retired once the app reads the new catalog |
| The `prices` workflow | keyed by printing ID and fed from the database |
| The app's catalog download | reads `catalog/index.json` and set files from R2; collections store printing IDs; existing local data is cleared on the upgrade |

## Build order

1. **Schema.** *Done.* Tested locally and applied to the Supabase project on 2026-09-13
   with `python tools/migrate.py --apply`.
2. **Per-set import:** one set from one named source into `source_records`, run only when
   you start it. Nothing is created as a series, set or card; that is your job in the editor.
3. **Editor:** create series and sets, pick cards, review, printings, pictures, TCGplayer
   links, the Published-without-a-picture list.
4. **Publish** to R2.
5. **App:** read the new catalog, store printing IDs, draw card backs, start fresh.
6. **Prices** by printing ID.
7. **Base Set pilot**, start to finish, timed.

## Decisions

Made on 2026-09-13:

1. **Set codes** follow the series: series code plus two digits in release order (`me05`).
2. **Subsets** stay inside their parent set, with a `section`.
3. **Pictures** are WebP at most 734×1024, never enlarged.
4. **A set can publish with cards that have no picture**, marked `no_image`. The app shows
   the card back, and the editor lists them for correcting later.
5. **Nothing is imported without your authorization.** Imports are per set, from a named
   source, started by you, and nothing imported before this database carries over.
6. **The starting vocabulary is kept**: 139 variant words, 71 terms and 5 sources,
   re-approved after that rule was set.
7. **Everything the app downloads is on Cloudflare R2**, from the start, so nothing has to
   move later and the app never depends on Supabase. Supabase is the private database only.
   GitHub was considered and ruled out: repository size limits, rate limits on serving
   files, and a copyright takedown there could reach the account that ships app updates.

To decide when they come up:

- **World Championships deck cards.** TCGdex files them as signature-stamped printings of
  the original cards. They are sold as their own decks with a different back and cannot be
  played in tournaments, so they may be better as sets of their own.
- **Jumbo cards.** TCGdex files them as a size of an existing printing. The app has no
  pocket for one yet, so whether they are printings or cards of their own is open.
