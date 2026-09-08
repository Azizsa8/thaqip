-- 0010: pricing prediction accuracy loop (Phase 1 M5)
CREATE TABLE IF NOT EXISTS pricing_prediction_results (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  simulation_id       bigint NOT NULL UNIQUE REFERENCES pursuit_simulations(id) ON DELETE CASCADE,
  pursuit_id          bigint NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
  actual_award_value  numeric(18,2) NOT NULL,
  predicted_price     numeric(18,2) NOT NULL,
  absolute_error      numeric(18,2) NOT NULL,
  percentage_error    real NOT NULL,
  measured_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pricing_prediction_results_pursuit_idx
  ON pricing_prediction_results (pursuit_id);
CREATE INDEX IF NOT EXISTS pricing_prediction_results_measured_idx
  ON pricing_prediction_results (measured_at DESC);
