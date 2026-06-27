-- Verbatim revealed call/text instructions from the CL contact reveal (masked relay
-- number + extension/code, e.g. "Call (415) 943-0693 x 46" / "Text 46 to (415)
-- 943-0693"). Wording varies, so it is stored as-is and shown on the dashboard.
-- Apply via the Supabase SQL editor / CLI, then sync.
alter table listings add column if not exists contact_details text;
