-- The rules the tables keep for themselves, so no tool can get them wrong:
--
--   * IDs are built from their parts and never typed.
--   * A published record never changes its ID and is never deleted.
--   * Every variant part and every term is a word from its list.
--   * Editing a published set marks it as having unpublished changes.
--   * Publishing a set is refused while anything in it is unreviewed.
--   * Every edit is written to change_log.
--
-- BEFORE triggers run in name order, which is why they are numbered: an ID is built (t10)
-- before the vocabulary is checked (t20) and before the lock compares it with the old one (t30).

-- ---------------------------------------------------------------------------------------
-- Helpers

-- True while record_publish is running, so its own updates are not mistaken for edits.
create function publishing() returns boolean
language sql stable set search_path = public as $$
  select coalesce(current_setting('pocketful.publishing', true), '') = 'on'
$$;

-- The codes in `codes` that are not terms of `expected`, comma-separated, or null.
create function missing_terms(expected term_kind, codes text[]) returns text
language sql stable set search_path = public as $$
  select string_agg(distinct c, ', ')
  from unnest(codes) as c
  where c is not null
    and not exists (select 1 from terms t where t.kind = expected and t.code = c)
$$;

create function check_word(candidate text, expected word_kind) returns void
language plpgsql stable set search_path = public as $$
begin
  if candidate is not null
     and not exists (select 1 from variant_words w where w.word = candidate and w.kind = expected) then
    raise exception '"%" is not a % word', candidate, expected
      using hint = 'Add it to variant_words first.';
  end if;
end $$;

create function touch_updated_at() returns trigger
language plpgsql set search_path = public as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- ---------------------------------------------------------------------------------------
-- IDs

create function build_catalog_id() returns trigger
language plpgsql set search_path = public as $$
begin
  new.id := new.game_id || '-' || new.language;
  return new;
end $$;

create function build_series_id() returns trigger
language plpgsql set search_path = public as $$
begin
  new.id := new.catalog_id || '-' || new.code;
  return new;
end $$;

-- The series is deliberately not part of a set's ID, only its catalog.
create function build_set_id() returns trigger
language plpgsql set search_path = public as $$
declare
  catalog text;
begin
  select s.catalog_id into catalog from series s where s.id = new.series_id;
  if catalog is null then
    raise exception 'series "%" does not exist', new.series_id using errcode = 'foreign_key_violation';
  end if;
  new.id := catalog || '-' || new.code;
  return new;
end $$;

create function build_card_id() returns trigger
language plpgsql set search_path = public as $$
begin
  new.id := new.set_id || '-' || new.number;
  return new;
end $$;

-- edition → pattern → finish → stamps → error, stamps in word-list order.
create function build_printing_id() returns trigger
language plpgsql set search_path = public as $$
declare
  unknown text;
begin
  perform check_word(new.edition, 'edition');
  perform check_word(new.pattern, 'pattern');
  perform check_word(new.finish, 'finish');
  perform check_word(new.error, 'error');

  select string_agg(s, ', ') into unknown
  from unnest(new.stamps) as s
  where not exists (select 1 from variant_words w where w.word = s and w.kind = 'stamp');
  if unknown is not null then
    raise exception '"%" is not a stamp word', unknown using hint = 'Add it to variant_words first.';
  end if;

  select coalesce(array_agg(w.word order by w.sort, w.word), '{}') into new.stamps
  from variant_words w
  where w.kind = 'stamp' and w.word = any (new.stamps);

  new.variant := array_to_string(
    array[new.edition, new.pattern, new.finish] || new.stamps || array[new.error], '-');
  new.id := new.card_id || '_' || new.variant;
  return new;
end $$;

create trigger t10_build_id before insert or update on catalogs
  for each row execute function build_catalog_id();
create trigger t10_build_id before insert or update on series
  for each row execute function build_series_id();
create trigger t10_build_id before insert or update on sets
  for each row execute function build_set_id();
create trigger t10_build_id before insert or update on cards
  for each row execute function build_card_id();
create trigger t10_build_id before insert or update on printings
  for each row execute function build_printing_id();

-- ---------------------------------------------------------------------------------------
-- Vocabulary

create function check_card_terms() returns trigger
language plpgsql set search_path = public as $$
declare
  missing text;
begin
  missing := missing_terms('rarity', array[new.rarity]);
  if missing is not null then
    raise exception 'unknown rarity: %', missing using hint = 'Add it to terms first.';
  end if;

  missing := missing_terms('type',
    new.types
    || array(select jsonb_array_elements_text(coalesce(a -> 'cost', '[]'))
             from jsonb_array_elements(new.attacks) as a)
    || array(select w ->> 'type'
             from jsonb_array_elements(new.weaknesses || new.resistances) as w));
  if missing is not null then
    raise exception 'unknown type: %', missing using hint = 'Add it to terms first.';
  end if;

  missing := missing_terms('subtype', new.subtypes);
  if missing is not null then
    raise exception 'unknown subtype: %', missing using hint = 'Add it to terms first.';
  end if;

  missing := missing_terms('ability_kind',
    array(select a ->> 'kind' from jsonb_array_elements(new.abilities) as a));
  if missing is not null then
    raise exception 'unknown ability kind: %', missing using hint = 'Add it to terms first.';
  end if;

  return new;
end $$;

create trigger t20_check_terms before insert or update on cards
  for each row execute function check_card_terms();

-- A word or term that something already uses cannot be renamed or removed; its label can.
create function guard_word_in_use() returns trigger
language plpgsql set search_path = public as $$
begin
  if tg_op = 'UPDATE' and new.word = old.word and new.kind = old.kind then
    return new;
  end if;
  if exists (
    select 1 from printings p
    where old.word in (p.edition, p.pattern, p.finish, p.error) or old.word = any (p.stamps)
  ) then
    raise exception 'the word "%" is used by printings, so it cannot be renamed or removed', old.word;
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end $$;

create trigger t30_guard_in_use before update or delete on variant_words
  for each row execute function guard_word_in_use();

create function guard_term_in_use() returns trigger
language plpgsql set search_path = public as $$
declare
  used boolean;
begin
  if tg_op = 'UPDATE' and new.code = old.code and new.kind = old.kind then
    return new;
  end if;
  used := case old.kind
    when 'rarity' then exists (select 1 from cards where rarity = old.code)
    when 'subtype' then exists (select 1 from cards where old.code = any (subtypes))
    when 'ability_kind' then exists (
      select 1 from cards where abilities @> jsonb_build_array(jsonb_build_object('kind', old.code)))
    when 'type' then exists (
      select 1 from cards
      where old.code = any (types)
         or attacks @> jsonb_build_array(jsonb_build_object('cost', jsonb_build_array(old.code)))
         or weaknesses @> jsonb_build_array(jsonb_build_object('type', old.code))
         or resistances @> jsonb_build_array(jsonb_build_object('type', old.code)))
  end;
  if used then
    raise exception 'the % "%" is used by cards, so it cannot be renamed or removed', old.kind, old.code;
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end $$;

create trigger t30_guard_in_use before update or delete on terms
  for each row execute function guard_term_in_use();

-- ---------------------------------------------------------------------------------------
-- Locks

-- Published records keep their IDs forever and are withdrawn rather than deleted. Only
-- record_publish can lock a record, publish a set or change its version.
create function guard_locked() returns trigger
language plpgsql set search_path = public as $$
declare
  row_after jsonb;
  row_before jsonb := to_jsonb(old);
begin
  if tg_op = 'DELETE' then
    if old.locked then
      raise exception '% "%" has been published, so it cannot be deleted', tg_table_name, old.id
        using hint = 'Withdraw it instead.';
    end if;
    return old;
  end if;

  row_after := to_jsonb(new);

  if old.locked and new.id <> old.id then
    raise exception '% "%" has been published, so its ID cannot change', tg_table_name, old.id;
  end if;

  if not publishing() then
    if new.locked <> old.locked then
      raise exception '% "%": only publishing can lock or unlock a record', tg_table_name, old.id;
    end if;
    if row_after ->> 'status' = 'published' and row_before ->> 'status' <> 'published' then
      raise exception '% "%": only publishing can mark a record published', tg_table_name, old.id;
    end if;
    if (row_after -> 'version') is distinct from (row_before -> 'version')
       or (row_after -> 'published_at') is distinct from (row_before -> 'published_at') then
      raise exception '% "%": only publishing can change its version', tg_table_name, old.id;
    end if;
  end if;

  return new;
end $$;

create trigger t30_guard_locked before update or delete on series
  for each row execute function guard_locked();
create trigger t30_guard_locked before update or delete on sets
  for each row execute function guard_locked();
create trigger t30_guard_locked before update or delete on cards
  for each row execute function guard_locked();
create trigger t30_guard_locked before update or delete on printings
  for each row execute function guard_locked();

-- A new record cannot arrive already locked or published either.
create function guard_new_record() returns trigger
language plpgsql set search_path = public as $$
begin
  if not publishing() and (new.locked or to_jsonb(new) ->> 'status' in ('published', 'published_changed')) then
    raise exception '% "%": a new record starts unpublished', tg_table_name, new.id;
  end if;
  return new;
end $$;

create trigger t30_guard_new before insert on series
  for each row execute function guard_new_record();
create trigger t30_guard_new before insert on sets
  for each row execute function guard_new_record();
create trigger t30_guard_new before insert on cards
  for each row execute function guard_new_record();
create trigger t30_guard_new before insert on printings
  for each row execute function guard_new_record();

-- ---------------------------------------------------------------------------------------
-- Unpublished changes

-- Fields that never reach the app, so changing them does not need a new publish.
create function unpublished_fields() returns text[]
language sql immutable set search_path = public as $$
  select array['notes', 'created_at', 'updated_at', 'locked', 'status', 'version', 'published_at',
               'tcgplayer_group', 'tcgplayer_via', 'tcgplayer_product', 'tcgplayer_printing']
$$;

create function mark_set_changed(target text) returns void
language sql set search_path = public as $$
  update sets set status = 'published_changed' where id = target and status = 'published'
$$;

create function note_set_change() returns trigger
language plpgsql set search_path = public as $$
begin
  if old.status = 'published' and new.status = 'published' and not publishing()
     and to_jsonb(new) - unpublished_fields() <> to_jsonb(old) - unpublished_fields() then
    new.status := 'published_changed';
  end if;
  return new;
end $$;

create trigger t40_note_change before update on sets
  for each row execute function note_set_change();

-- Cards, printings and pictures mark the set they belong to.
create function note_child_change() returns trigger
language plpgsql set search_path = public as $$
declare
  row_before jsonb := case when tg_op <> 'INSERT' then to_jsonb(old) end;
  row_after jsonb := case when tg_op <> 'DELETE' then to_jsonb(new) end;
  affected text[];
begin
  if publishing() then
    return null;
  end if;
  if tg_op = 'UPDATE' and row_after - unpublished_fields() = row_before - unpublished_fields() then
    return null;
  end if;

  affected := case tg_table_name
    when 'cards' then array[row_before ->> 'set_id', row_after ->> 'set_id']
    when 'printings' then array(
      select c.set_id from cards c
      where c.id in (row_before ->> 'card_id', row_after ->> 'card_id'))
    when 'images' then array(
      select coalesce(r ->> 'set_id',
                      (select c.set_id from cards c where c.id = r ->> 'card_id'),
                      (select c.set_id from printings p join cards c on c.id = p.card_id
                       where p.id = r ->> 'printing_id'))
      from (values (row_before), (row_after)) as v (r)
      where r is not null)
  end;

  perform mark_set_changed(s) from (select distinct unnest(affected) as s) as d where s is not null;
  return null;
end $$;

create trigger t50_note_change after insert or update or delete on cards
  for each row execute function note_child_change();
create trigger t50_note_change after insert or update or delete on printings
  for each row execute function note_child_change();
create trigger t50_note_change after insert or update or delete on images
  for each row execute function note_child_change();

-- ---------------------------------------------------------------------------------------
-- Publishing

-- Everything standing between a set and the app. Empty means it can be published.
create function publish_problems(target text)
returns table (subject text, problem text)
language plpgsql stable set search_path = public as $$
declare
  the_set sets;
begin
  select * into the_set from sets where id = target;
  if not found then
    raise exception 'set "%" does not exist', target;
  end if;

  return query
  select target, 'the set has no cards'
  where not exists (select 1 from cards where set_id = target and not withdrawn);

  return query
  select target, 'the set has no logo; choose one or mark it as not existing'
  where not the_set.no_logo
    and not exists (select 1 from images where set_id = target and role = 'logo' and chosen);

  return query
  select target, 'the set has no symbol; choose one or mark it as not existing'
  where not the_set.no_symbol
    and not exists (select 1 from images where set_id = target and role = 'symbol' and chosen);

  return query
  select c.id, case c.review
      when 'unreviewed' then 'not reviewed'
      else 'flagged: ' || coalesce(c.review_note, 'no note') end
  from cards c
  where c.set_id = target and not c.withdrawn and c.review <> 'reviewed';

  return query
  select c.id, 'has no printings'
  from cards c
  where c.set_id = target and not c.withdrawn
    and not exists (select 1 from printings p where p.card_id = c.id and not p.withdrawn);

  return query
  select c.id, 'has no picture; choose one or mark it as having none'
  from cards c
  where c.set_id = target and not c.withdrawn and not c.no_image
    and not exists (select 1 from images i where i.card_id = c.id and i.role = 'front' and i.chosen);

  return query
  select p.id, case p.review
      when 'unreviewed' then 'not reviewed'
      else 'flagged: ' || coalesce(p.review_note, 'no note') end
  from printings p join cards c on c.id = p.card_id
  where c.set_id = target and not c.withdrawn and not p.withdrawn and p.review <> 'reviewed';

  -- A card with no picture is drawn as the card back, so there has to be one.
  return query
  select se.catalog_id, 'cards without a picture need a card back for this catalog or series'
  from series se
  where se.id = the_set.series_id
    and exists (select 1 from cards where set_id = target and not withdrawn and no_image)
    and not exists (
      select 1 from images i
      where i.role = 'back' and i.chosen and (i.series_id = se.id or i.catalog_id = se.catalog_id));
end $$;

-- Called after the set file is in storage. Locks everything that went out with it.
create function record_publish(target text, next_version integer, file_path text, file_sha256 text)
returns publishes
language plpgsql set search_path = public as $$
declare
  the_set sets;
  problem_count integer;
  result publishes;
begin
  select * into the_set from sets where id = target for update;
  if not found then
    raise exception 'set "%" does not exist', target;
  end if;

  select count(*) into problem_count from publish_problems(target);
  if problem_count > 0 then
    raise exception 'set "%" is not ready to publish: % problem(s)', target, problem_count
      using hint = format('select * from publish_problems(%L)', target);
  end if;

  if next_version <> the_set.version + 1 then
    raise exception 'set "%" is at version %, so the next publish is %, not %',
      target, the_set.version, the_set.version + 1, next_version;
  end if;

  perform set_config('pocketful.publishing', 'on', true);

  update cards set locked = true
  where set_id = target and not withdrawn and not locked;

  update printings p set locked = true
  from cards c
  where c.id = p.card_id and c.set_id = target and not c.withdrawn and not p.withdrawn and not p.locked;

  update series set locked = true, status = 'published'
  where id = the_set.series_id;

  update sets set locked = true, status = 'published', version = next_version, published_at = now()
  where id = target;

  insert into publishes (set_id, version, cards, printings, file, sha256)
  values (
    target,
    next_version,
    (select count(*) from cards where set_id = target and not withdrawn),
    (select count(*) from printings p join cards c on c.id = p.card_id
     where c.set_id = target and not c.withdrawn and not p.withdrawn),
    file_path,
    file_sha256)
  returning * into result;

  perform set_config('pocketful.publishing', 'off', true);
  return result;
end $$;

-- ---------------------------------------------------------------------------------------
-- Change log

create function log_change() returns trigger
language plpgsql set search_path = public as $$
declare
  row_before jsonb := case when tg_op <> 'INSERT' then to_jsonb(old) end;
  row_after jsonb := case when tg_op <> 'DELETE' then to_jsonb(new) end;
  r jsonb := coalesce(row_after, row_before);
  row_key text := coalesce(r ->> 'id', r ->> 'word', (r ->> 'kind') || ':' || (r ->> 'code'));
begin
  if tg_op = 'INSERT' then
    insert into change_log (table_name, row_id, action, after)
    values (tg_table_name, row_key, 'insert', row_after);
  elsif tg_op = 'DELETE' then
    insert into change_log (table_name, row_id, action, before)
    values (tg_table_name, row_key, 'delete', row_before);
  else
    insert into change_log (table_name, row_id, action, field, before, after)
    select tg_table_name, row_key, 'update', k, row_before -> k, row_after -> k
    from jsonb_object_keys(row_after) as k
    where k not in ('updated_at', 'data_hash', 'changed')
      and (row_before -> k) is distinct from (row_after -> k);
  end if;
  return null;
end $$;

do $$
declare
  t text;
begin
  foreach t in array array['games', 'catalogs', 'series', 'sets', 'cards', 'printings',
                           'variant_words', 'terms', 'sources', 'images'] loop
    execute format('create trigger t90_touch before update on %I
                    for each row execute function touch_updated_at()', t);
    execute format('create trigger t60_log after insert or update or delete on %I
                    for each row execute function log_change()', t);
  end loop;
end $$;
