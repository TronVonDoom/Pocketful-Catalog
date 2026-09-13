-- Everything the app downloads lives on Cloudflare R2 instead (tools/r2.py), decided before
-- a single file was written, so the storage buckets 20260913120200_catalog_access.sql
-- created are removed unused. Supabase now holds only the private database, and the app
-- has no reason to reach it at all.
--
-- Supabase refuses direct deletes from its storage tables unless storage.allow_delete_query
-- is set, because deleting a bucket that still holds files would orphan them. So this makes
-- sure the buckets are empty first, and only then sets it, for this transaction alone.

do $$
begin
  if exists (select 1 from storage.objects
             where bucket_id in ('catalog', 'images', 'prices', 'originals')) then
    raise exception 'the Supabase storage buckets still hold files; move them to R2 first';
  end if;

  perform set_config('storage.allow_delete_query', 'true', true);
  delete from storage.buckets where id in ('catalog', 'images', 'prices', 'originals');
end $$;
