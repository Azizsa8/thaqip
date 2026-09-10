-- 0015: canonical Price-to-Win schema.
--
-- Establishes the four things the P2W spec treats as hard gates and that the
-- pre-existing schema had no representation for at all:
--   1. TENANCY  - private operator data (pursuits/outcomes/predictions/follows/
--      alert_profiles/user_calculator_prefs) is tenant-scoped. Shared observed
--      market facts (tenders/offers/awards/vendors/agencies) are deliberately
--      NOT tenant-scoped: they are public record and shared across tenants.
--   2. VERSIONED user scenarios - never overwritten (US-03).
--   3. PREDICTION RECORDS carrying evidence/confidence/suppression, plus
--      feedback rows so calibration is measurable rather than asserted.
--   4. PROVENANCE - source_registry + source_lineage, so every derived fact can
--      be traced to the endpoint, access class and parser version that made it.
--
-- Every object is created IF NOT EXISTS so the migration runner can re-apply it
-- safely and so it tolerates objects created out-of-band.

-- ---------------------------------------------------------------------------
-- 1. Tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
  id          bigserial PRIMARY KEY,
  slug        text NOT NULL UNIQUE,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- The default tenant is pinned to id 1 so the tenant_id column defaults below
-- are stable and every pre-existing private row lands in it.
INSERT INTO tenants (id, slug, name)
VALUES (1, 'default', 'Default Tenant')
ON CONFLICT (slug) DO NOTHING;

SELECT setval(
  pg_get_serial_sequence('tenants', 'id'),
  GREATEST((SELECT COALESCE(max(id), 1) FROM tenants), 1)
);

ALTER TABLE pursuits
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE outcomes
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE predictions
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE follows
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE alert_profiles
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE user_calculator_prefs
  ADD COLUMN IF NOT EXISTS tenant_id bigint NOT NULL DEFAULT 1 REFERENCES tenants(id);

CREATE INDEX IF NOT EXISTS pursuits_tenant_idx              ON pursuits (tenant_id);
CREATE INDEX IF NOT EXISTS outcomes_tenant_idx              ON outcomes (tenant_id);
CREATE INDEX IF NOT EXISTS predictions_tenant_idx           ON predictions (tenant_id);
CREATE INDEX IF NOT EXISTS follows_tenant_idx               ON follows (tenant_id);
CREATE INDEX IF NOT EXISTS alert_profiles_tenant_idx        ON alert_profiles (tenant_id);
CREATE INDEX IF NOT EXISTS user_calculator_prefs_tenant_idx ON user_calculator_prefs (tenant_id);

-- ---------------------------------------------------------------------------
-- 2. Versioned user bid scenarios (US-03: versions, never overwrites)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_bid_scenarios (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id       bigint NOT NULL DEFAULT 1 REFERENCES tenants(id),
  pursuit_id      bigint REFERENCES pursuits(id) ON DELETE CASCADE,
  tender_id       bigint REFERENCES tenders(id) ON DELETE CASCADE,
  name            text NOT NULL DEFAULT '',
  estimated_cost  numeric(18,2),
  min_margin_pct  numeric(6,2),
  target_win_pct  numeric(5,2),
  proposed_bid    numeric(18,2),
  risk_reserve    numeric(18,2),
  version         int NOT NULL DEFAULT 1,
  seed            bigint,
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  superseded_by   bigint REFERENCES user_bid_scenarios(id)
);

CREATE INDEX IF NOT EXISTS user_bid_scenarios_tenant_idx  ON user_bid_scenarios (tenant_id);
CREATE INDEX IF NOT EXISTS user_bid_scenarios_pursuit_idx ON user_bid_scenarios (pursuit_id, version DESC);
CREATE INDEX IF NOT EXISTS user_bid_scenarios_tender_idx  ON user_bid_scenarios (tender_id, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS user_bid_scenarios_version_uq
  ON user_bid_scenarios (tenant_id, pursuit_id, version)
  WHERE pursuit_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Price predictions (model output contract)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS price_predictions (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id             bigint NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
  prediction_scope      text NOT NULL
                        CHECK (prediction_scope IN ('MARKET', 'COMPETITOR', 'USER_OPTIMIZER')),
  subject_vendor_id     bigint REFERENCES vendors(id),
  p10                   numeric(18,2),
  p50                   numeric(18,2),
  p90                   numeric(18,2),
  expected_value        numeric(18,2),
  win_probability       numeric(6,4),
  confidence_score      int,
  similarity_confidence int,
  data_freshness_score  int,
  evidence_count        int,
  evidence_tier         text,
  model_version         text NOT NULL,
  feature_snapshot_id   text,
  seed                  bigint,
  suppression_reason    text,
  explanation_factors   jsonb NOT NULL DEFAULT '[]'::jsonb,
  generated_at          timestamptz NOT NULL DEFAULT now()
);

-- The suppression invariant is enforced in the DB as well as in contracts.py:
-- a suppressed prediction may not carry numbers, an unsuppressed one must.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'price_predictions_suppression_check'
  ) THEN
    ALTER TABLE price_predictions
      ADD CONSTRAINT price_predictions_suppression_check
      CHECK (
        (suppression_reason IS NOT NULL
           AND p10 IS NULL AND p50 IS NULL AND p90 IS NULL AND win_probability IS NULL)
        OR
        (suppression_reason IS NULL
           AND p10 IS NOT NULL AND p50 IS NOT NULL AND p90 IS NOT NULL
           AND p10 <= p50 AND p50 <= p90)
      ) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'price_predictions_evidence_tier_check'
  ) THEN
    ALTER TABLE price_predictions
      ADD CONSTRAINT price_predictions_evidence_tier_check
      CHECK (evidence_tier IS NULL OR evidence_tier IN ('A', 'B', 'C', 'D')) NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS price_predictions_tender_idx
  ON price_predictions (tender_id, prediction_scope, generated_at DESC);
CREATE INDEX IF NOT EXISTS price_predictions_subject_idx
  ON price_predictions (subject_vendor_id) WHERE subject_vendor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS price_predictions_model_idx
  ON price_predictions (model_version, generated_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Prediction feedback (calibration evidence)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_feedback (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  prediction_id           bigint NOT NULL REFERENCES price_predictions(id) ON DELETE CASCADE,
  actual_award_value      numeric(18,2),
  actual_winner_vendor_id bigint REFERENCES vendors(id),
  actual_bidder_count     int,
  interval_hit            boolean,
  abs_pct_error           numeric(8,4),
  user_feedback           text,
  recorded_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS prediction_feedback_prediction_idx
  ON prediction_feedback (prediction_id);

-- ---------------------------------------------------------------------------
-- 5. Provenance: source registry + per-fact lineage
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_registry (
  source_id              text PRIMARY KEY,
  name                   text NOT NULL,
  access_class           text NOT NULL,
  legal_basis            text,
  redistribution_policy  text,
  auth_method            text,
  refresh_sla_minutes    int,
  rate_limit_per_sec     numeric(8,3),
  owner                  text,
  schema_version         text
);

INSERT INTO source_registry (
  source_id, name, access_class, legal_basis, redistribution_policy,
  auth_method, refresh_sla_minutes, rate_limit_per_sec, owner, schema_version
) VALUES
  ('etimad_visitor_api', 'Etimad public visitor listing API', 'PUBLIC_OPEN',
   'Publicly accessible listing endpoint', 'derived_only', 'none', 5, 1.0, 'data', 'v1'),
  ('forsah_public_api', 'Forsah public opportunities API', 'PUBLIC_OPEN',
   'Publicly accessible JSON API', 'derived_only', 'none', 60, 0.7, 'data', 'v1')
ON CONFLICT (source_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS source_lineage (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fact_table        text NOT NULL,
  fact_id           bigint NOT NULL,
  source_id         text REFERENCES source_registry(source_id),
  source_object_ref text,
  content_hash      text,
  parser_version    text,
  access_class      text,
  retrieved_at      timestamptz,
  recorded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS source_lineage_fact_idx   ON source_lineage (fact_table, fact_id);
CREATE INDEX IF NOT EXISTS source_lineage_source_idx ON source_lineage (source_id, retrieved_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Versioned aggregate profiles
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS competitor_profiles (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vendor_id           bigint NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  activity_id         bigint,
  profile_version     int NOT NULL DEFAULT 1,
  observation_count   int NOT NULL DEFAULT 0,
  median_bid_ratio    numeric(10,6),
  p10_bid_ratio       numeric(10,6),
  p90_bid_ratio       numeric(10,6),
  win_rate            numeric(6,4),
  technical_pass_rate numeric(6,4),
  last_seen_at        timestamptz,
  computed_at         timestamptz NOT NULL DEFAULT now(),
  feature_snapshot_id text
);

-- activity_id is nullable (an all-activity profile), so the uniqueness of
-- (vendor, activity, version) needs two partial indexes to cover NULL.
CREATE UNIQUE INDEX IF NOT EXISTS competitor_profiles_uq
  ON competitor_profiles (vendor_id, activity_id, profile_version)
  WHERE activity_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS competitor_profiles_all_activity_uq
  ON competitor_profiles (vendor_id, profile_version)
  WHERE activity_id IS NULL;
CREATE INDEX IF NOT EXISTS competitor_profiles_vendor_idx
  ON competitor_profiles (vendor_id, profile_version DESC);

CREATE TABLE IF NOT EXISTS agency_profiles (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agency_id                bigint NOT NULL REFERENCES agencies(id) ON DELETE CASCADE,
  activity_id              bigint,
  profile_version          int NOT NULL DEFAULT 1,
  tender_count             int NOT NULL DEFAULT 0,
  lowest_qualified_win_rate numeric(6,4),
  median_bidder_count      numeric(8,3),
  award_spread_pct         numeric(8,4),
  cancellation_rate        numeric(6,4),
  computed_at              timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS agency_profiles_uq
  ON agency_profiles (agency_id, activity_id, profile_version)
  WHERE activity_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS agency_profiles_all_activity_uq
  ON agency_profiles (agency_id, profile_version)
  WHERE activity_id IS NULL;
CREATE INDEX IF NOT EXISTS agency_profiles_agency_idx
  ON agency_profiles (agency_id, profile_version DESC);

-- ---------------------------------------------------------------------------
-- 7. Tender similarity (retrieval layer, versioned)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tender_similarity (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id         bigint NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
  similar_tender_id bigint NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
  total_score       numeric(8,6),
  components        jsonb NOT NULL DEFAULT '{}'::jsonb,
  exclusion_reason  text,
  retrieval_version text NOT NULL DEFAULT 'v1',
  computed_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tender_id, similar_tender_id, retrieval_version)
);

CREATE INDEX IF NOT EXISTS tender_similarity_lookup_idx
  ON tender_similarity (tender_id, retrieval_version, total_score DESC);
