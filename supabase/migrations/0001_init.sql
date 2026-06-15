-- Minimal cloud schema for the SF house-hunting dashboard.
-- Philosophy: keep the cloud row SMALL. Photos stay remote (image_urls), the
-- verbatim post body is NOT stored (the dossier links out to the source post).
-- Only identity, display essentials, and Claude's verdict live here. One row
-- per deduped unit (the cluster primary); the other source posts are embedded
-- in `sources`. Written by the local sync (service_role); read publicly (anon).

create table if not exists public.listings (
    id              text primary key,
    source          text,
    url             text,
    title           text,
    price           integer,
    room_type       text,           -- 'studio' | '1br' | '2br_plus' | 'unknown'
    bedrooms        real,
    bathrooms       real,
    sqft            integer,
    area            text,
    neighborhood    text,
    address         text,
    lat             double precision,
    lng             double precision,
    image_urls      jsonb default '[]'::jsonb,   -- remote photo URLs
    phone           text,
    -- Claude's verdict (the value-add we keep) --
    legit_score     integer,
    legit_label     text,           -- 'likely-legit' | 'unverified-amateur' | 'likely-scam'
    red_flags       jsonb default '[]'::jsonb,
    fit_score       integer,
    verdict_summary text,
    recommendation  text,
    status          text,
    -- dedup: one row per unit; duplicate source posts embedded here --
    dup_count       integer default 1,
    sources         jsonb default '[]'::jsonb,   -- [{url,source,price,fit_score,legit_score,legit_label,area}]
    first_seen_at   timestamptz,
    updated_at      timestamptz default now()
);

create index if not exists idx_listings_fit on public.listings(fit_score);
create index if not exists idx_listings_status on public.listings(status);

-- Public dashboard is READ-ONLY: anon may SELECT, nothing else. The local sync
-- uses the service_role key, which bypasses RLS, to write.
alter table public.listings enable row level security;

drop policy if exists "public read" on public.listings;
create policy "public read" on public.listings for select to anon using (true);
