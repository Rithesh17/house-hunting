-- Poster's contact name from the CL reply panel (div.reply-contact-name), when shown.
-- Apply via the Supabase SQL editor / CLI, then sync.
alter table listings add column if not exists contact_name text;
