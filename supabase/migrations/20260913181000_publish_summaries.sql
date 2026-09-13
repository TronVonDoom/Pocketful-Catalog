-- What a set looked like in the index at the moment it was published.
--
-- The index the app reads lists every published set with its name, dates and counts. Built
-- from the sets table, it would show edits made since the last publish -- a renamed set
-- whose file still carries the old name. So each publish keeps the set's index entry as it
-- went out, and the index is built from those.

alter table publishes add column summary jsonb;
