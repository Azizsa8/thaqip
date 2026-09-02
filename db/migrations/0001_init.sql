-- Thaqip schema v1 (ticket C1)
-- Postgres 16 + pgvector. Arabic-safe: UTF-8 database, ICU collation recommended at CREATE DATABASE time.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agencies (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  canonical_name text NOT NULL,
  name_variants  text[] NOT NULL DEFAULT '{}',
  agency_code    text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (canonical_name)
);

CREATE TABLE IF NOT EXISTS activities (
  id        bigint PRIMARY KEY,          -- Etimad tenderActivityId
  name_ar   text NOT NULL,
  name_en   text,
  parent_id bigint REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS tenders (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source                 text NOT NULL DEFAULT 'etimad',      -- etimad | forsah | ...
  source_tender_id       bigint NOT NULL,                     -- Etimad tenderId
  source_id_string       text,                                -- encrypted tenderIdString (detail-page key)
  reference_number       text NOT NULL,
  tender_number          text,
  name                   text NOT NULL,
  agency_id              bigint REFERENCES agencies(id),
  agency_name_raw        text,
  branch_name            text,
  activity_id            bigint,
  activity_name_raw      text,
  tender_type_id         int,
  tender_type_name       text,
  status_id              int,
  status_name            text,
  booklet_price          numeric(14,2),
  financial_fees         numeric(14,2),
  buying_cost            numeric(14,2),
  invitation_cost        numeric(14,2),
  submission_date        timestamptz,
  last_enquiries_date    timestamptz,
  last_offer_date        timestamptz,
  offers_opening_date    timestamptz,
  last_enquiries_date_hijri text,
  last_offer_date_hijri  text,
  offers_opening_date_hijri text,
  inside_ksa             boolean,
  published_at           timestamptz,                          -- best-known official publish time
  detected_at            timestamptz NOT NULL DEFAULT now(),   -- when Thaqip first saw it (freshness SLO)
  payload                jsonb NOT NULL,                       -- raw source row, always retained
  content_hash           text NOT NULL,                        -- hash of normalized payload for diffing
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_tender_id)
);
CREATE INDEX IF NOT EXISTS tenders_status_idx     ON tenders (status_id);
CREATE INDEX IF NOT EXISTS tenders_activity_idx   ON tenders (activity_id);
CREATE INDEX IF NOT EXISTS tenders_agency_idx     ON tenders (agency_id);
CREATE INDEX IF NOT EXISTS tenders_lastoffer_idx  ON tenders (last_offer_date);
CREATE INDEX IF NOT EXISTS tenders_detected_idx   ON tenders (detected_at);
CREATE INDEX IF NOT EXISTS tenders_payload_gin    ON tenders USING gin (payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS vendors (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  canonical_name text NOT NULL,
  name_variants  text[] NOT NULL DEFAULT '{}',
  cr_number      text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (canonical_name)
);

CREATE TABLE IF NOT EXISTS offers (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id    bigint NOT NULL REFERENCES tenders(id),
  vendor_id    bigint REFERENCES vendors(id),
  vendor_name_raw text,
  offer_value  numeric(18,2),
  is_winner    boolean,
  technical_pass boolean,
  rank         int,
  payload      jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS offers_tender_idx ON offers (tender_id);
CREATE INDEX IF NOT EXISTS offers_vendor_idx ON offers (vendor_id);

CREATE TABLE IF NOT EXISTS awards (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id    bigint NOT NULL REFERENCES tenders(id),
  vendor_id    bigint REFERENCES vendors(id),
  award_value  numeric(18,2),
  awarded_at   timestamptz,
  payload      jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS awards_tender_idx ON awards (tender_id);

CREATE TABLE IF NOT EXISTS documents (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id     bigint NOT NULL REFERENCES tenders(id),
  kind          text NOT NULL,           -- tsd | boq | attachment | clarification | other
  file_name     text,
  mime_type     text,
  size_bytes    bigint,
  sha256        text NOT NULL,
  storage_key   text NOT NULL,           -- object-storage path
  text_extracted boolean NOT NULL DEFAULT false,
  scan_status   text NOT NULL DEFAULT 'pending',  -- pending | clean | flagged
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tender_id, sha256)
);

CREATE TABLE IF NOT EXISTS doc_chunks (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES documents(id),
  chunk_no    int NOT NULL,
  content     text NOT NULL,
  embedding   vector(1024),
  UNIQUE (document_id, chunk_no)
);

CREATE TABLE IF NOT EXISTS boq_items (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id   bigint NOT NULL REFERENCES tenders(id),
  document_id bigint REFERENCES documents(id),
  item_no     text,
  description text NOT NULL,
  unit        text,
  qty         numeric(18,3),
  confidence  real NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS boq_tender_idx ON boq_items (tender_id);

-- Transactional outbox (ticket D1). Relay ships rows to Redis Streams.
CREATE TABLE IF NOT EXISTS ingest_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_type  text NOT NULL,             -- tender.created | tender.updated | tender.awarded | tender.cancelled | tender.extended | document.stored | ...
  entity_type text NOT NULL,
  entity_id   bigint NOT NULL,
  data        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  relayed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS ingest_events_unrelayed_idx ON ingest_events (id) WHERE relayed_at IS NULL;

CREATE TABLE IF NOT EXISTS ingest_runs (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  connector    text NOT NULL,            -- etimad.listing | etimad.details | etimad.awards | ...
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  ok           boolean,
  pages        int NOT NULL DEFAULT 0,
  items_seen   int NOT NULL DEFAULT 0,
  items_new    int NOT NULL DEFAULT 0,
  items_changed int NOT NULL DEFAULT 0,
  error        text,
  checkpoint   jsonb                     -- resumable cursor (ticket C4)
);
