-- Stage-2 cross-check outcome for a listing (DRE / ownership / price / duplicates).
-- Written by the local sync once a listing has been vetted under the two-stage
-- flow; the dashboard can surface it as verification badges. Nullable.
alter table public.listings
    add column if not exists verification jsonb;
