-- Every rule in the catalog migrations, exercised as the admin role the editor will use.
-- Any expectation that does not hold raises, and tools/test_database.py stops on it.

create function pg_temp.expect(ok boolean, what text) returns void
language plpgsql as $$
begin
  if ok is not true then
    raise exception 'expected: %', what;
  end if;
end $$;

create function pg_temp.expect_error(statement text, fragment text) returns void
language plpgsql as $$
begin
  begin
    execute statement;
  exception when others then
    if position(fragment in sqlerrm) = 0 then
      raise exception 'expected an error mentioning "%", got: %', fragment, sqlerrm;
    end if;
    return;
  end;
  raise exception 'expected an error mentioning "%" from: %', fragment, statement;
end $$;

grant execute on function pg_temp.expect(boolean, text), pg_temp.expect_error(text, text) to service_role;

set role service_role;

-- IDs are built from their parts ------------------------------------------------------------

insert into series (catalog_id, code, name) values ('ptcg-en', 'me', 'Mega Evolution');
insert into sets (series_id, code, name, printed_total) values ('ptcg-en-me', 'me5', 'Pitch Black', 84);
insert into cards (set_id, number, printed_number, name, category, subtypes, hp, types, rarity,
                   attacks, weaknesses, resistances, abilities)
values ('ptcg-en-me5', '062', '062/084', 'Bastiodon', 'pokemon', '{stage-2}', 160, '{metal}', 'rare',
        '[{"name": "Test Attack", "cost": ["metal", "colorless"], "damage": "120"}]',
        '[{"type": "fire", "value": "×2"}]',
        '[{"type": "grass", "value": "-30"}]',
        '[{"name": "Test Ability", "kind": "ability", "text": "Test."}]');
insert into printings (card_id, finish) values ('ptcg-en-me5-062', 'holo'), ('ptcg-en-me5-062', 'reverse');
insert into printings (card_id, edition, finish, stamps)
values ('ptcg-en-me5-062', '1st-edition', 'holo', '{staff,pre-release,staff}');

select pg_temp.expect(exists (select 1 from series where id = 'ptcg-en-me'), 'series ID built');
select pg_temp.expect(exists (select 1 from sets where id = 'ptcg-en-me5'), 'set ID built without the series');
select pg_temp.expect(exists (select 1 from cards where id = 'ptcg-en-me5-062'), 'card ID built');
select pg_temp.expect(exists (select 1 from printings where id = 'ptcg-en-me5-062_reverse'), 'printing ID built');
select pg_temp.expect(
  (select stamps from printings where id = 'ptcg-en-me5-062_1st-edition-holo-pre-release-staff') = '{pre-release,staff}',
  'variant parts in order, stamps sorted and each once');

-- A number that repeats in a set keeps its whole printed number.
insert into cards (set_id, number, printed_number, name, category)
values ('ptcg-en-me5', '15-102', '15/102', 'Venusaur', 'pokemon');
select pg_temp.expect_error(
  $$insert into cards (set_id, number, name, category) values ('ptcg-en-me5', '63/84', 'X', 'pokemon')$$,
  'cards_number_check');
select pg_temp.expect_error(
  $$insert into sets (series_id, code, name) values ('ptcg-en-me', 'me-06', 'X')$$, 'sets_code_check');
select pg_temp.expect_error(
  $$insert into printings (card_id, finish) values ('ptcg-en-me5-062', 'holo')$$, 'duplicate key');

-- Vocabulary -----------------------------------------------------------------------------------

select pg_temp.expect_error(
  $$insert into printings (card_id, finish) values ('ptcg-en-me5-062', 'pokeball')$$, 'is not a finish word');
select pg_temp.expect_error(
  $$insert into printings (card_id, finish, stamps) values ('ptcg-en-me5-062', 'normal', '{made-up}')$$,
  'is not a stamp word');
select pg_temp.expect_error(
  $$insert into cards (set_id, number, name, category, rarity) values ('ptcg-en-me5', '063', 'X', 'pokemon', 'rare-holo')$$,
  'unknown rarity: rare-holo');
select pg_temp.expect_error(
  $$insert into cards (set_id, number, name, category, attacks) values ('ptcg-en-me5', '063', 'X', 'pokemon', '[{"name": "A", "cost": ["Metal"]}]')$$,
  'unknown type: Metal');
select pg_temp.expect_error(
  $$insert into cards (set_id, number, name, category, abilities) values ('ptcg-en-me5', '063', 'X', 'pokemon', '[{"name": "A", "kind": "power"}]')$$,
  'unknown ability kind: power');
select pg_temp.expect_error(
  $$update terms set code = 'rare-card' where kind = 'rarity' and code = 'rare'$$, 'is used by cards');
select pg_temp.expect_error($$delete from terms where kind = 'type' and code = 'grass'$$, 'is used by cards');
select pg_temp.expect_error($$delete from variant_words where word = 'staff'$$, 'is used by printings');
update variant_words set label = 'Staff' where word = 'staff';
delete from variant_words where word = 'glossy';

-- Pictures -------------------------------------------------------------------------------------

insert into images (card_id, role, chosen, path) values ('ptcg-en-me5-062', 'front', true, 'cards/a.webp');
insert into images (card_id, role, source_url) values ('ptcg-en-me5-062', 'front', 'https://example.com/candidate.png');
select pg_temp.expect_error(
  $$insert into images (card_id, role, chosen, path) values ('ptcg-en-me5-062', 'front', true, 'cards/b.webp')$$,
  'images_chosen_card');
select pg_temp.expect_error(
  $$insert into images (set_id, role, path) values ('ptcg-en-me5', 'front', 'x.webp')$$, 'check constraint');
select pg_temp.expect_error(
  $$insert into images (card_id, role, chosen) values ('ptcg-en-me5-062', 'front', true)$$, 'check constraint');

-- Renaming a draft set's code renames everything under it --------------------------------------

update sets set code = 'me05' where id = 'ptcg-en-me5';
select pg_temp.expect(exists (select 1 from printings where id = 'ptcg-en-me05-062_reverse'), 'rename reaches printings');
select pg_temp.expect((select card_id from images where path = 'cards/a.webp') = 'ptcg-en-me05-062', 'rename reaches pictures');
select pg_temp.expect(not exists (select 1 from cards where set_id = 'ptcg-en-me5'), 'nothing left under the old code');

-- The publish gate -----------------------------------------------------------------------------

select pg_temp.expect(
  exists (select 1 from publish_problems('ptcg-en-me05') where subject = 'ptcg-en-me05-062' and problem = 'not reviewed'),
  'an unreviewed card blocks publishing');
select pg_temp.expect(
  exists (select 1 from publish_problems('ptcg-en-me05') where problem like 'the set has no logo%'),
  'a missing logo blocks publishing');
select pg_temp.expect(
  exists (select 1 from publish_problems('ptcg-en-me05') where subject = 'ptcg-en-me05-15-102' and problem like 'has no printings'),
  'a card with no printings blocks publishing');
select pg_temp.expect_error(
  $$select record_publish('ptcg-en-me05', 1, 'sets/x.json.gz', repeat('a', 64))$$, 'not ready to publish');

insert into printings (card_id, finish) values ('ptcg-en-me05-15-102', 'holo');
update cards set review = 'reviewed' where set_id = 'ptcg-en-me05';
update printings set review = 'reviewed' where card_id like 'ptcg-en-me05-%';
update sets set no_logo = true, no_symbol = true where id = 'ptcg-en-me05';
update cards set review = 'flagged', review_note = 'wrong HP?' where id = 'ptcg-en-me05-062';
select pg_temp.expect(
  exists (select 1 from publish_problems('ptcg-en-me05') where problem = 'flagged: wrong HP?'),
  'a flagged card blocks publishing, with its note');
update cards set review = 'reviewed', review_note = null where id = 'ptcg-en-me05-062';

-- A card with no picture is allowed, but only once the catalog has a card back.
update cards set no_image = true where id = 'ptcg-en-me05-15-102';
select pg_temp.expect(
  exists (select 1 from publish_problems('ptcg-en-me05') where problem like 'cards without a picture need a card back%'),
  'no picture needs a card back');
insert into images (catalog_id, role, chosen, path) values ('ptcg-en', 'back', true, 'backs/en.webp');
select pg_temp.expect(not exists (select 1 from publish_problems('ptcg-en-me05')), 'the set is ready');

-- Only publishing publishes ----------------------------------------------------------------------

select pg_temp.expect_error($$update sets set status = 'published' where id = 'ptcg-en-me05'$$, 'only publishing');
select pg_temp.expect_error($$update sets set version = 1 where id = 'ptcg-en-me05'$$, 'only publishing');
select pg_temp.expect_error($$update cards set locked = true where id = 'ptcg-en-me05-062'$$, 'only publishing');
select pg_temp.expect_error(
  $$insert into sets (series_id, code, name, status) values ('ptcg-en-me', 'me06', 'X', 'published')$$,
  'starts unpublished');
select pg_temp.expect_error(
  $$select record_publish('ptcg-en-me05', 2, 'sets/x.json.gz', repeat('a', 64))$$, 'the next publish is 1');

select record_publish('ptcg-en-me05', 1, 'sets/ptcg-en-me05.v1.json.gz', repeat('a', 64));

select pg_temp.expect((select status = 'published' and version = 1 and locked from sets where id = 'ptcg-en-me05'), 'set published');
select pg_temp.expect((select bool_and(locked) from cards where set_id = 'ptcg-en-me05'), 'its cards locked');
select pg_temp.expect((select bool_and(p.locked) from printings p join cards c on c.id = p.card_id where c.set_id = 'ptcg-en-me05'), 'its printings locked');
select pg_temp.expect((select status = 'published' and locked from series where id = 'ptcg-en-me'), 'its series published');
select pg_temp.expect((select cards = 2 and printings = 4 from publishes where set_id = 'ptcg-en-me05' and version = 1), 'publish recorded');

-- Published records keep their IDs and are withdrawn, not deleted -------------------------------

select pg_temp.expect_error($$update sets set code = 'me5' where id = 'ptcg-en-me05'$$, 'its ID cannot change');
select pg_temp.expect_error($$update series set code = 'mega' where id = 'ptcg-en-me'$$, 'its ID cannot change');
select pg_temp.expect_error($$update cards set number = '062a' where id = 'ptcg-en-me05-062'$$, 'its ID cannot change');
select pg_temp.expect_error($$update printings set finish = 'normal' where id = 'ptcg-en-me05-062_holo'$$, 'its ID cannot change');
select pg_temp.expect_error($$delete from printings where id = 'ptcg-en-me05-062_holo'$$, 'cannot be deleted');
select pg_temp.expect_error($$delete from sets where id = 'ptcg-en-me05'$$, 'cannot be deleted');
select pg_temp.expect_error($$update cards set locked = false where id = 'ptcg-en-me05-062'$$, 'only publishing');

-- Moving a set to another series in the same catalog does not change its ID.
insert into series (catalog_id, code, name) values ('ptcg-en', 'mep', 'Mega Evolution Promos');
update sets set series_id = 'ptcg-en-mep' where id = 'ptcg-en-me05';
update sets set series_id = 'ptcg-en-me' where id = 'ptcg-en-me05';

-- What needs a new publish ------------------------------------------------------------------------

select pg_temp.expect((select status from sets where id = 'ptcg-en-me05') = 'published_changed', 'moving series is a change');
select record_publish('ptcg-en-me05', 2, 'sets/ptcg-en-me05.v2.json.gz', repeat('b', 64));

update printings set tcgplayer_product = 123, tcgplayer_via = 'manual' where id = 'ptcg-en-me05-062_holo';
update sets set tcgplayer_group = 456, tcgplayer_via = 'auto' where id = 'ptcg-en-me05';
update cards set notes = 'checked against my copy' where id = 'ptcg-en-me05-062';
select pg_temp.expect((select status from sets where id = 'ptcg-en-me05') = 'published', 'price links and notes are not changes');

update cards set hp = 170 where id = 'ptcg-en-me05-062';
select pg_temp.expect((select status from sets where id = 'ptcg-en-me05') = 'published_changed', 'HP is a change');

-- Change log ---------------------------------------------------------------------------------------

select pg_temp.expect(
  exists (select 1 from change_log
          where table_name = 'cards' and row_id = 'ptcg-en-me05-062' and field = 'hp'
            and before = '160' and after = '170'),
  'the HP edit is in the change log');
select pg_temp.expect(
  exists (select 1 from change_log where table_name = 'sets' and row_id = 'ptcg-en-me05' and field = 'id'
            and before = '"ptcg-en-me5"' and after = '"ptcg-en-me05"'),
  'the code rename is in the change log');

-- A new card in a published set is free until it is published ---------------------------------

insert into cards (set_id, number, name, category, review, no_image)
values ('ptcg-en-me05', '999', 'Late Card', 'pokemon', 'reviewed', true);
update cards set number = '085' where id = 'ptcg-en-me05-999';
delete from cards where id = 'ptcg-en-me05-085';

update cards set withdrawn = true where id = 'ptcg-en-me05-15-102';
select record_publish('ptcg-en-me05', 3, 'sets/ptcg-en-me05.v3.json.gz', repeat('c', 64));
select pg_temp.expect((select cards = 1 and printings = 3 from publishes where set_id = 'ptcg-en-me05' and version = 3),
  'a withdrawn card is left out of the publish');

-- Source records notice when a source changes its mind -------------------------------------------

insert into source_records (source_id, language, kind, key, data)
values ('tcgdex', 'en', 'card', 'me05-062', '{"hp": 160}');
update source_records set matched = 'ptcg-en-me05-062', matched_hash = data_hash where key = 'me05-062';
select pg_temp.expect(not (select changed from source_records where key = 'me05-062'), 'a fresh match is unchanged');
update source_records set data = '{"hp": 170}' where key = 'me05-062';
select pg_temp.expect((select changed from source_records where key = 'me05-062'), 'a source changing its data is noticed');

-- Nobody but the admin key gets in -------------------------------------------------------------

reset role;
set role anon;
do $$
begin
  begin
    perform 1 from public.cards;
    raise exception 'anon could read cards';
  exception when insufficient_privilege then null;
  end;
  begin
    perform public.publish_problems('ptcg-en-me05');
    raise exception 'anon could call publish_problems';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into public.sets (series_id, code, name) values ('ptcg-en-me', 'me99', 'X');
    raise exception 'anon could insert a set';
  exception when insufficient_privilege then null;
  end;
end $$;

reset role;
set role authenticated;
do $$
begin
  begin
    perform 1 from public.change_log;
    raise exception 'authenticated could read the change log';
  exception when insufficient_privilege then null;
  end;
end $$;

reset role;
select pg_temp.expect((select count(*) from storage.buckets where public) = 3, 'three public buckets');
select pg_temp.expect((select not public from storage.buckets where id = 'originals'), 'originals stay private');
