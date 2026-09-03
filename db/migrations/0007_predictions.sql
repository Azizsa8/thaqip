-- 0007: prediction log (M5-4 groundwork) — record what we predicted at decision
-- time so outcome-vs-prediction calibration is possible from day one.
CREATE TABLE IF NOT EXISTS predictions (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pursuit_id bigint NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
  kind       text NOT NULL DEFAULT 'win_baseline',
  value      numeric(8,4) NOT NULL,          -- e.g. 0.5 = 50% baseline win chance
  basis      jsonb NOT NULL DEFAULT '{}',    -- inputs snapshot (bidders, n, source)
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS predictions_pursuit_idx ON predictions (pursuit_id);
