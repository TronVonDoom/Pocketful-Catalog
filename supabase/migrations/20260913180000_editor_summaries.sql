-- How far along each set is, in one row per set, for the editor's lists.
--
-- Counting over the REST API would mean fetching every card of every set just to show a
-- sidebar, so the database counts instead. Withdrawn cards and printings are left out,
-- because they are not part of what a set publishes. security_invoker makes the view obey
-- the reader's own permissions rather than its owner's.

create view set_summaries with (security_invoker = true) as
select
  s.id as set_id,
  count(c.id) filter (where not c.withdrawn) as cards,
  count(c.id) filter (where not c.withdrawn and c.review = 'reviewed') as reviewed,
  count(c.id) filter (where not c.withdrawn and c.review = 'flagged') as flagged,
  count(c.id) filter (where not c.withdrawn and c.no_image) as marked_no_picture,
  count(c.id) filter (where not c.withdrawn and exists (
    select 1 from images i where i.card_id = c.id and i.role = 'front' and i.chosen)) as with_picture,
  (select count(*) from printings p join cards pc on pc.id = p.card_id
   where pc.set_id = s.id and not pc.withdrawn and not p.withdrawn) as printings,
  (select count(*) from printings p join cards pc on pc.id = p.card_id
   where pc.set_id = s.id and not pc.withdrawn and not p.withdrawn and p.review <> 'reviewed') as printings_to_review
from sets s
left join cards c on c.set_id = s.id
group by s.id;

revoke all on set_summaries from anon, authenticated;
grant select on set_summaries to service_role;
