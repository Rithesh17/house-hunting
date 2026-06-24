-- Reply contact email (CL relay) revealed via the chromerpc reply flow.
-- (phone already exists.) Apply with the Supabase CLI / SQL editor, then sync.
alter table listings add column if not exists reply_email text;
