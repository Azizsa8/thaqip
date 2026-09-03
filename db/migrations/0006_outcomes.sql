-- 0006: outcome logging (PRD M5-1) — the moat's data-capture point
CREATE TABLE IF NOT EXISTS outcomes (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pursuit_id      bigint NOT NULL UNIQUE REFERENCES pursuits(id) ON DELETE CASCADE,
  result          text NOT NULL,                 -- won | lost
  submitted_value numeric(18,2),                 -- what we bid (user-entered)
  award_value     numeric(18,2),                 -- winning value (auto-reconciled from awards when known)
  competitor_count int,                          -- auto from offers when known
  notes           text,
  logged_at       timestamptz NOT NULL DEFAULT now()
);
