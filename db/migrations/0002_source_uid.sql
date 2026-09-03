-- 0002: support non-numeric source ids (Forsah uses UUIDs) — ticket B6
ALTER TABLE tenders ALTER COLUMN source_tender_id DROP NOT NULL;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS source_uid text;
CREATE UNIQUE INDEX IF NOT EXISTS tenders_source_uid_uq
  ON tenders (source, source_uid) WHERE source_uid IS NOT NULL;
-- Forsah competition-intensity counters (the data Forsah exposes per opportunity)
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS bids_count int;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS submitted_bids_count int;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS external_bids_count int;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS draft_bids_count int;
