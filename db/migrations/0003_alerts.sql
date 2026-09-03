-- 0003: Phase 1 alert engine (PRD M2-3)
CREATE TABLE IF NOT EXISTS alert_profiles (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         text NOT NULL,
  channel      text NOT NULL DEFAULT 'telegram',   -- telegram | email | log
  target       text NOT NULL,                      -- chat id / email address
  keywords     text[] NOT NULL DEFAULT '{}',       -- OR-matched against tender name+activity
  activity_ids bigint[] NOT NULL DEFAULT '{}',
  agency_ids   bigint[] NOT NULL DEFAULT '{}',
  sources      text[] NOT NULL DEFAULT '{etimad,forsah}',
  event_types  text[] NOT NULL DEFAULT '{tender.created,tender.extended,tender.awarded}',
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES alert_profiles(id),
  event_id    bigint,                                -- outbox id (dedupe key with profile)
  tender_id   bigint REFERENCES tenders(id),
  event_type  text NOT NULL,
  channel     text NOT NULL,
  target      text NOT NULL,
  title       text NOT NULL,
  body        text NOT NULL,
  status      text NOT NULL DEFAULT 'pending',       -- pending | sent | failed
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  sent_at     timestamptz,
  UNIQUE (profile_id, event_id)
);
CREATE INDEX IF NOT EXISTS notifications_status_idx ON notifications (status) WHERE status='pending';
