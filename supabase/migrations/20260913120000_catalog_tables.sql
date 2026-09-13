-- The catalog's own tables. docs/database.md says what each one is for; the rules that
-- build IDs, lock published records and keep the change log are in the next migration.

create type set_kind as enum ('expansion', 'special', 'promo', 'deck', 'kit', 'other');
create type publish_status as enum ('draft', 'published', 'published_changed');
create type review_state as enum ('unreviewed', 'reviewed', 'flagged');
create type card_category as enum ('pokemon', 'trainer', 'energy');
create type link_via as enum ('auto', 'manual');
create type word_kind as enum ('edition', 'pattern', 'finish', 'stamp', 'error');
create type term_kind as enum ('rarity', 'type', 'subtype', 'ability_kind');
create type image_role as enum ('front', 'back', 'logo', 'symbol');
create type record_kind as enum ('series', 'set', 'card');

create table games (
  id text primary key check (id ~ '^[a-z0-9]+$'),
  name text not null,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- One per game and language. The ID is built from both.
create table catalogs (
  id text primary key,
  game_id text not null references games (id) on update restrict on delete restrict,
  language text not null check (language ~ '^[a-z]+$'),
  name text not null,
  native_name text,
  sort integer not null default 0,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (game_id, language)
);

create table series (
  id text primary key,
  catalog_id text not null references catalogs (id) on update restrict on delete restrict,
  code text not null check (code ~ '^[a-z0-9]+$'),
  name text not null,
  name_en text,
  sort integer not null default 0,
  -- A series is published once any of its sets is, and has nothing of its own to change.
  status publish_status not null default 'draft' check (status <> 'published_changed'),
  locked boolean not null default false,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table sets (
  id text primary key,
  series_id text not null references series (id) on update cascade on delete restrict,
  code text not null check (code ~ '^[a-z0-9]+(\.[a-z0-9]+)?$'),
  name text not null,
  name_en text,
  kind set_kind not null default 'expansion',
  release_date date,
  -- The number after the slash on the main set's cards.
  printed_total integer check (printed_total > 0),
  -- The set code printed on recent cards (PBL). Information only, never part of an ID.
  abbreviation text,
  sort integer not null default 0,
  no_logo boolean not null default false,
  no_symbol boolean not null default false,
  tcgplayer_group integer,
  tcgplayer_via link_via,
  status publish_status not null default 'draft',
  version integer not null default 0 check (version >= 0),
  published_at timestamptz,
  locked boolean not null default false,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((tcgplayer_group is null) = (tcgplayer_via is null))
);
create index sets_series on sets (series_id);

-- What every printing of a card shares.
create table cards (
  id text primary key,
  set_id text not null references sets (id) on update cascade on delete cascade,
  number text not null check (number ~ '^[a-z0-9]+(-[a-z0-9]+)?$'),
  printed_number text,
  number_assigned boolean not null default false,
  -- The subset a card belongs to inside its set (Trainer Gallery), or null for the main set.
  section text,
  sort integer,
  name text not null,
  name_en text,
  category card_category not null,
  subtypes text[] not null default '{}',
  hp integer check (hp > 0),
  types text[] not null default '{}',
  evolves_from text,
  abilities jsonb not null default '[]' check (jsonb_typeof(abilities) = 'array'),
  attacks jsonb not null default '[]' check (jsonb_typeof(attacks) = 'array'),
  weaknesses jsonb not null default '[]' check (jsonb_typeof(weaknesses) = 'array'),
  resistances jsonb not null default '[]' check (jsonb_typeof(resistances) = 'array'),
  retreat integer check (retreat >= 0),
  rules text[] not null default '{}',
  flavor_text text,
  illustrator text,
  rarity text,
  regulation_mark text check (regulation_mark ~ '^[A-Z]$'),
  dex_numbers integer[] not null default '{}',
  -- Confirmed that no picture exists anywhere yet. The app shows the card back instead.
  no_image boolean not null default false,
  -- Cards sharing this value are the same card in different languages.
  same_as uuid,
  review review_state not null default 'unreviewed',
  review_note text,
  reviewed_at timestamptz,
  withdrawn boolean not null default false,
  locked boolean not null default false,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index cards_set on cards (set_id);
create index cards_same_as on cards (same_as) where same_as is not null;
create index cards_missing_image on cards (set_id) where no_image;

create table variant_words (
  word text primary key check (word ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  kind word_kind not null,
  label text not null,
  description text,
  sort integer not null default 0,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table printings (
  id text primary key,
  card_id text not null references cards (id) on update cascade on delete cascade,
  variant text not null,
  edition text references variant_words (word) on update restrict on delete restrict,
  pattern text references variant_words (word) on update restrict on delete restrict,
  finish text not null references variant_words (word) on update restrict on delete restrict,
  stamps text[] not null default '{}',
  error text references variant_words (word) on update restrict on delete restrict,
  tcgplayer_product integer,
  tcgplayer_printing text,
  tcgplayer_via link_via,
  -- How to tell this printing apart from its siblings, in words the app can show.
  identify text,
  review review_state not null default 'unreviewed',
  review_note text,
  reviewed_at timestamptz,
  withdrawn boolean not null default false,
  locked boolean not null default false,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((tcgplayer_product is null) = (tcgplayer_via is null))
);
create index printings_card on printings (card_id);

-- One spelling for every fixed choice: rarities, energy types, subtypes, ability kinds.
create table terms (
  kind term_kind not null,
  code text not null check (code ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  -- How it is written in each catalog, keyed by language: {"en": "Holo Rare"}.
  labels jsonb not null default '{}' check (jsonb_typeof(labels) = 'object'),
  sort integer not null default 0,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (kind, code)
);

create table sources (
  id text primary key check (id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name text not null,
  url text,
  -- What its license or terms allow, in your own words.
  terms text,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Everything a source said, untouched, and which of your records it describes.
create table source_records (
  id bigint generated always as identity primary key,
  source_id text not null references sources (id) on update cascade on delete cascade,
  language text not null,
  kind record_kind not null,
  key text not null,
  data jsonb not null,
  data_hash text generated always as (md5(data::text)) stored,
  fetched_at timestamptz not null default now(),
  matched text,
  -- data_hash at the moment the match was reviewed.
  matched_hash text,
  changed boolean generated always as (matched is not null and matched_hash is distinct from md5(data::text)) stored,
  unique (source_id, language, kind, key)
);
create index source_records_matched on source_records (matched) where matched is not null;

-- Every picture, chosen or not, with where it came from.
create table images (
  id uuid primary key default gen_random_uuid(),
  catalog_id text references catalogs (id) on update restrict on delete restrict,
  series_id text references series (id) on update cascade on delete cascade,
  set_id text references sets (id) on update cascade on delete cascade,
  card_id text references cards (id) on update cascade on delete cascade,
  printing_id text references printings (id) on update cascade on delete cascade,
  role image_role not null,
  chosen boolean not null default false,
  path text unique,
  thumb_path text unique,
  width integer check (width > 0),
  height integer check (height > 0),
  bytes bigint check (bytes > 0),
  sha256 text check (sha256 ~ '^[0-9a-f]{64}$'),
  below_standard boolean not null default false,
  source_id text references sources (id) on update cascade on delete restrict,
  source_url text,
  -- The untouched original, kept only when it cannot be downloaded again.
  original_path text,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (num_nonnulls(catalog_id, series_id, set_id, card_id, printing_id) = 1),
  check (case role
    when 'back' then catalog_id is not null or series_id is not null
    when 'logo' then series_id is not null or set_id is not null
    when 'symbol' then set_id is not null
    when 'front' then card_id is not null or printing_id is not null
  end),
  -- A candidate can be just an address; the picture in use has to be in storage.
  check (not chosen or path is not null)
);
create unique index images_chosen_catalog on images (catalog_id, role) where chosen and catalog_id is not null;
create unique index images_chosen_series on images (series_id, role) where chosen and series_id is not null;
create unique index images_chosen_set on images (set_id, role) where chosen and set_id is not null;
create unique index images_chosen_card on images (card_id, role) where chosen and card_id is not null;
create unique index images_chosen_printing on images (printing_id, role) where chosen and printing_id is not null;

create table publishes (
  id bigint generated always as identity primary key,
  set_id text not null references sets (id) on update cascade on delete restrict,
  version integer not null check (version > 0),
  published_at timestamptz not null default now(),
  cards integer not null,
  printings integer not null,
  file text not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  unique (set_id, version)
);

-- Every edit to every table, written by the database itself.
create table change_log (
  id bigint generated always as identity primary key,
  at timestamptz not null default now(),
  table_name text not null,
  row_id text not null,
  action text not null check (action in ('insert', 'update', 'delete')),
  field text,
  before jsonb,
  after jsonb
);
create index change_log_row on change_log (table_name, row_id, at);
