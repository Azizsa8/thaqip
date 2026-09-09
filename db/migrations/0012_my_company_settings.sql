-- 0012: operator company baseline used by vendor comparison drill-downs.
ALTER TABLE user_calculator_prefs
  ADD COLUMN IF NOT EXISTS my_company_name text NOT NULL DEFAULT 'شركتي',
  ADD COLUMN IF NOT EXISTS target_win_rate_pct numeric(5,2) NOT NULL DEFAULT 25.00,
  ADD COLUMN IF NOT EXISTS cost_advantage_pct numeric(5,2) NOT NULL DEFAULT 0.00;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'user_calculator_prefs_target_win_rate_pct_check'
  ) THEN
    ALTER TABLE user_calculator_prefs
      ADD CONSTRAINT user_calculator_prefs_target_win_rate_pct_check
      CHECK (target_win_rate_pct BETWEEN 0 AND 100) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'user_calculator_prefs_cost_advantage_pct_check'
  ) THEN
    ALTER TABLE user_calculator_prefs
      ADD CONSTRAINT user_calculator_prefs_cost_advantage_pct_check
      CHECK (cost_advantage_pct BETWEEN -50 AND 50) NOT VALID;
  END IF;
END $$;
