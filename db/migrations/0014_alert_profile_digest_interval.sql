-- 0014: per-profile alert delivery cadence.
ALTER TABLE alert_profiles
  ADD COLUMN IF NOT EXISTS digest_interval text NOT NULL DEFAULT 'instant';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'alert_profiles_digest_interval_check'
  ) THEN
    ALTER TABLE alert_profiles
      ADD CONSTRAINT alert_profiles_digest_interval_check
      CHECK (digest_interval IN ('instant','hourly','daily')) NOT VALID;
  END IF;
END $$;
