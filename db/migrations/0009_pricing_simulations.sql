-- 0009: Dynamic Win-Probability & Pricing Intelligence Simulator (M5-2, M5-3)
CREATE TABLE IF NOT EXISTS pursuit_simulations (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pursuit_id     bigint NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
  proposed_price numeric(18,2) NOT NULL,
  win_pct        real NOT NULL,
  expected_value numeric(18,2),
  basis          text NOT NULL,                 -- activity_and_agency | activity_history | sector_fallback | live_bidders
  metadata       jsonb NOT NULL DEFAULT '{}',   -- benchmarks snapshot (median, p25, p75, gtpl flag)
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pursuit_sim_pursuit_idx ON pursuit_simulations (pursuit_id);
