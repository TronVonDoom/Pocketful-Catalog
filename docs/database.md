# The Pocketful catalog database

**Status: draft for approval, 2026-09-13.** Nothing in here exists yet. Once this is
agreed, it is what gets built, and changes to it get made here first.

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
  │  read-only) Supabase │               └───────┘
  └──────────────────────┘
```

- **The database** is the source of truth. Every series, set, card, printing, picture,
  TCGplayer link and review mark is here. It is private: only the editor and the
  tools can reach it, using an admin key that lives on your PC.
- **Storage** holds what the app downloads: an index of published sets, one file per
  published set, the pictures, and the prices. Anyone can read it and only the admin key
  can write to it.
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

An ID is built from the thing's parts, never typed by hand. It can change freely until the
set it belongs to is published, and after that it never changes and is never deleted.
Something published by mistake is **withdrawn** (hidden from the app), because someone's
collection may already hold it.

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
two digits, in release order. Examples: `me05`, `sv08`, `swsh12`. A half set takes `.5`,
as in `me02.5`. A series' promo set is the series code plus `p`, as in `mep`. The editor
suggests the code and you can change it until the set is published. Codes are lowercase
`a–z`, `0–9` and `.`, and unique within a catalog.

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
| error | a known misprint | `no-damage-error` |

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
with two different spellings.

---

## Tables

Field names are what the database will call them. Every table also has `created_at`,
`updated_at` and a free-text `notes`.

### `games` and `catalogs`

| Field | Example | Meaning |
|---|---|---|
| `games.id` | `ptcg` | |
| `games.name` | Pokémon TCG | |
| `catalogs.id` | `ptcg-en` | |
| `catalogs.game` | `ptcg` | |
| `catalogs.language` | `en` | |
| `catalogs.name` | English | |
| `catalogs.sort` | 1 | order the app lists catalogs in |

### `series`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me` | |
| `catalog` | `ptcg-en` | |
| `code` | `me` | |
| `name` | Mega Evolution | as printed in that language |
| `name_en` | | English name, for series in other languages, so they can be searched in English |
| `logo` | image | |
| `sort` | 23 | order within the catalog |
| `status` | `draft` / `published` | published once any of its sets is |

### `sets`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05` | |
| `series` | `ptcg-en-me` | |
| `code` | `me05` | |
| `name` | Pitch Black | as printed |
| `name_en` | | English name, for sets in other languages |
| `kind` | `expansion` | `expansion`, `special`, `promo`, `subset`, `deck`, `kit`, `other` |
| `parent` | | the set a split-out subset belongs to |
| `release_date` | 2026-07-17 | |
| `printed_total` | 84 | the number after the slash |
| `abbreviation` | PBL | the printed set code, where one exists; information only, never part of an ID |
| `logo`, `symbol` | images | |
| `sort` | 5 | order within the series |
| `tcgplayer_group` | | TCGplayer group ID, plus `tcgplayer_via`: `auto` or `manual` |
| `status` | `draft` | `draft`, `published`, `published-with-changes` |
| `version` | 3 | bumped by every publish |
| `published_at` | | |

How many cards and printings a set has is counted from the cards, not stored.

### `cards`

What every printing of a card shares.

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05-062` | |
| `set` | `ptcg-en-me05` | |
| `number` | `062` | the ID form |
| `printed_number` | 062/084 | exactly as printed, for display |
| `number_assigned` | false | true when the card has no printed number |
| `sort` | 62 | order within the set, so `tg01` sorts after the main set |
| `name` | Bastiodon | as printed |
| `name_en` | | English name, for cards in other languages |
| `category` | `pokemon` | `pokemon`, `trainer`, `energy` |
| `subtypes` | Stage 2 | stage, trainer kind (Item, Supporter…), mechanic (ex, V, Mega…) |
| `hp` | 160 | |
| `types` | Metal | energy types |
| `evolves_from` | Shieldon | |
| `abilities` | | list of name, kind (Ability, Poké-Power, Poké-Body…), text |
| `attacks` | | list of name, cost, damage, text |
| `weaknesses`, `resistances` | Fire ×2 / Grass −30 | list of type and value |
| `retreat` | 4 | |
| `rules` | | rule-box text |
| `flavor_text` | | |
| `illustrator` | Kinu Nishimura | |
| `rarity` | `rare` | a word from the `terms` table |
| `regulation_mark` | J | |
| `dex_numbers` | 411 | |
| `image` | image | the card's picture, used by every printing without its own |
| `same_as` | | link group for the same card in other languages |
| `review` | `unreviewed` | `unreviewed`, `reviewed`, `flagged` |
| `review_note`, `reviewed_at` | | why it is flagged; when it was reviewed |

### `printings`

| Field | Example | Meaning |
|---|---|---|
| `id` | `ptcg-en-me05-062_reverse` | |
| `card` | `ptcg-en-me05-062` | |
| `variant` | `reverse` | the assembled name |
| `edition`, `pattern`, `finish`, `stamps`, `error` | `reverse` | its parts, each a word from `variant_words` |
| `image` | image | its own picture, when it looks different from the card's |
| `tcgplayer_product` | | TCGplayer product ID, plus `tcgplayer_printing` (e.g. Reverse Holofoil) and `tcgplayer_via` |
| `identify` | | how to tell this printing apart, shown in the app |
| `review`, `review_note`, `reviewed_at` | | as on cards |
| `withdrawn` | false | hidden from the app; published printings are never deleted |

### `variant_words`

| Field | Example | Meaning |
|---|---|---|
| `word` | `pokeball` | what goes in the ID |
| `kind` | `pattern` | `edition`, `pattern`, `finish`, `stamp`, `error` |
| `label` | Poké Ball Pattern | what the app shows |
| `description` | | how to recognise it |
| `sort` | | its position among words of its kind, which decides stamp order |

Seeded from the roughly forty words today's catalog already uses.

### `terms`

One spelling for everything that is a fixed choice: rarities, energy types, subtypes and
ability kinds. TCGdex today writes both "Holo Rare" and "Rare Holo", and both
"Illustration rare" and "Illustration Rare"; with a terms table, a card picks one entry
instead of typing text.

| Field | Example | Meaning |
|---|---|---|
| `kind` | `rarity` | |
| `code` | `special-illustration-rare` | |
| `labels` | English, Japanese, Chinese | how it is written in each catalog |
| `symbol` | image | the printed rarity symbol, where there is one |
| `sort` | | |

### `images`

Every picture, chosen or not, with where it came from.

| Field | Example | Meaning |
|---|---|---|
| `id` | | |
| `subject` | a card | exactly one series, set, card or printing |
| `role` | `front` | `front`, `logo`, `symbol` |
| `chosen` | true | the one in use; the rest are candidates you decided against |
| `path` | `images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.webp` | where it is in storage |
| `thumb_path` | | the small version |
| `width`, `height`, `bytes`, `sha256` | | |
| `below_standard` | false | smaller than the standard size |
| `source` | `tcgdex` | a row in `sources` |
| `source_url` | | where it was taken from |
| `original_path` | | the untouched original, kept only when it cannot be downloaded again (an upload, a paste, a scan) |

### `sources` and `source_records`

A source is anywhere a suggestion comes from: `tcgdex`, `pokemontcg-io`, `tcgplayer`,
`upload`, and `pocketful-2026`, which is today's catalog including your 18 corrections and
the pictures you added.

| Field | Example | Meaning |
|---|---|---|
| `sources.id` | `tcgdex` | |
| `sources.name`, `url` | | |
| `sources.terms` | | what its license or terms allow, in your words |
| `source_records.source` | `tcgdex` | |
| `source_records.kind` | `card` | `series`, `set`, `card` |
| `source_records.key` | `me05-062` | the source's own ID |
| `source_records.data` | | everything the source said, untouched |
| `source_records.fetched_at` | | |
| `source_records.matched` | `ptcg-en-me05-062` | which of your records it describes, once decided |
| `source_records.changed` | false | the source has said something new since you reviewed the match |

This is what lets the editor say "TCGdex now says 170 HP; you say 160", and what lets it
point at fields where two sources disagree. For Japanese and Chinese cards you cannot
proofread, agreement between sources is what the text is trusted on.

### `publishes`

| Field | Meaning |
|---|---|
| `set`, `version`, `published_at` | |
| `cards`, `printings` | how many went out |
| `file`, `sha256` | the set file that was written |

### `change_log`

Every edit to every table, recorded by the database itself: table, ID, field, value
before, value after, when. Git gave the catalog a full history, and moving to a database
must not lose that.

---

## Reviewing and publishing

### A set's life

1. **Create** the series, if it is new, and the set: code, name, dates, logo, symbol.
   The form is pre-filled from whichever source has that set, and you accept or change
   each field.
2. **Pick the cards.** The editor lists every card the sources have for the set as
   candidates. You take the ones that belong, and can add a card no source has.
3. **Review each card:** picture beside fields, fields that sources disagree on
   highlighted. Accept, fix, or flag, then move to the next with the keyboard.
4. **Review its printings:** which ones exist, a picture for any that looks different,
   and the TCGplayer product (suggested automatically, the way prices are matched today).
5. **Publish.**

### What Publish requires

The editor refuses to publish a set until:

- every card and printing is reviewed, and none is flagged;
- every card has a chosen picture, or is explicitly marked as having none anywhere;
- the set has its logo and symbol, or they are marked as not existing;
- every ID is valid and unique.

### What Publish does

1. Writes `catalog/sets/ptcg-en-me05.v3.json.gz` to storage.
2. Updates `catalog/index.json`, the list of every published series and set with its
   version.
3. Bumps the set's `version`, sets its status to `published`, and records the publish.

A correction after publishing sets the status to `published-with-changes`. Publishing
again writes a new version, and the app downloads only that set again.

---

## What the app downloads

```
catalog/index.json                                   every published catalog, series, set, version
catalog/sets/ptcg-en-me05.v3.json.gz                 one published set
images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.webp        a card picture
images/cards/ptcg-en/me05/ptcg-en-me05-062.3f9a2c.thumb.webp  its thumbnail
images/sets/ptcg-en/me05/logo.81bd40.webp            a set logo
prices/prices.json.gz                                today's prices, by printing ID
prices/history/ptcg-en-me05.json.gz                  one set's price history
```

A set file, trimmed:

```json
{
  "schema": 2,
  "id": "ptcg-en-me05",
  "version": 3,
  "series": { "id": "ptcg-en-me", "name": "Mega Evolution" },
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

It carries what the app shows today plus printings, English names and labels. Attack and
ability text stays in the database until the app has a screen for it, at which point it
joins the file under a new `schema` number. The schema number is in the format so that an
older build never misreads a newer file.

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

- **Row-level security is on for every table, with no public access rules**, so the
  database cannot be read or written without the admin key.
- **The admin key lives at `C:\Users\TronVonDoom\keystores\pocketful-supabase.json`**,
  beside the release signing key, and as a GitHub secret for the nightly price job. It is
  never in either repository and never in the app.
- **Storage is public to read and admin-only to write.**

## Supabase plan

At the time of writing, the free plan includes a 500 MB database, 1 GB of file storage and
5 GB of downloads a month, and pauses a project after a week with no activity. The Base Set
pilot fits easily. English pictures and thumbnails will come to roughly 2–3 GB, so the Pro
plan (about $25 a month) will be needed partway through English. Check
[supabase.com/pricing](https://supabase.com/pricing) before relying on these numbers.

---

## What happens to what exists today

| Today | Becomes |
|---|---|
| `pull_catalog.py`, `fill_gaps.py` | importers that fill `source_records` and candidate pictures |
| `catalog/sets/`, `overrides.json`, `catalog/art/` | imported once as the `pocketful-2026` source, so your past corrections and pictures are suggestions too |
| `tcgplayer-groups.json`, `tcgplayer-cards.json` | imported as suggested TCGplayer links |
| The editor | reworked to read and write the database; still a Windows app, still no dependencies |
| The `catalog` release and weekly workflow | retired once the app reads the new catalog |
| The `prices` workflow | keyed by printing ID and fed from the database |
| The app's catalog download | reads `catalog/index.json` and set files from storage; collections store printing IDs; existing local data is cleared on the upgrade |

## Build order

1. **You create the Supabase project.** I write the schema as SQL migrations in
   `supabase/migrations/` and seed the game, catalogs, variant words and terms.
2. **Importers:** TCGdex English and today's catalog into `source_records`. Nothing is
   created as a series, set or card; that is your job in the editor.
3. **Editor:** create series and sets, pick cards, review, printings, pictures, TCGplayer
   links.
4. **Publish** to storage.
5. **App:** read the new catalog, store printing IDs, start fresh.
6. **Prices** by printing ID.
7. **Base Set pilot**, start to finish, timed.

## Decisions still open

1. **Set code pattern.** Series code plus two digits in release order (`base01`, `me05`,
   `swsh12`), or something else?
2. **Subsets** like Trainer Gallery, Galarian Gallery and Shiny Vault. Their numbers are
   already distinct (`tg01`, `gg01`, `sv001`), so the recommendation is to keep them
   inside their parent set, as printed. Splitting them out into sets of their own is the
   alternative.
3. **Picture standard.** 734×1024 WebP, never enlarged?
4. **Publish gate.** Must every card have a picture, or may a set publish with cards
   marked "no picture exists anywhere"?
