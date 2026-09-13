-- Who can reach what.
--
-- The database is private. Row-level security is on everywhere with no policies, and the
-- public roles are stripped of their grants as well, so without the admin key (service_role)
-- there is nothing to read and nothing to call. The app never uses the database at all: it
-- downloads published files from the public storage buckets below.

do $$
declare
  t text;
begin
  foreach t in array array['games', 'catalogs', 'series', 'sets', 'cards', 'variant_words',
                           'printings', 'terms', 'sources', 'source_records', 'images',
                           'publishes', 'change_log'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke execute on all functions in schema public from public, anon, authenticated;

alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke execute on functions from public, anon, authenticated;

grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant execute on all functions in schema public to service_role;

-- Public to read, admin-only to write. Pictures are WebP by the picture standard; the
-- originals bucket keeps whatever format a picture arrived in and is not public.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types) values
  ('catalog', 'catalog', true, 20971520, array['application/json', 'application/gzip']),
  ('images', 'images', true, 2097152, array['image/webp']),
  ('prices', 'prices', true, 20971520, array['application/json', 'application/gzip']),
  ('originals', 'originals', false, 20971520, array['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
on conflict (id) do nothing;
