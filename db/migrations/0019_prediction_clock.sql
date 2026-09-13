-- 0019: the prediction clock (accuracy Stage 1).
--
-- Accuracy can only be proven with predictions that were written down while
-- the answer was still unknown. Two things made that impossible before:
--
-- 1. Market predictions were only recorded when someone opened a tender page
--    (or a test ran), so most tenders never had one before their award.
-- 2. awards_harvest deletes and re-inserts a tender's awards on every pass, so
--    awards.created_at is "last harvested", not "first seen". A prediction made
--    after an award was public could look as if it had been made blind.
--
-- This migration adds a daily snapshot origin on price_predictions, a durable
-- first-seen timestamp per awarded tender, and one scorecard view that applies
-- the blindness rule in a single place.

-- --------------------------------------------------- where a prediction came from
ALTER TABLE price_predictions
    ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'interactive',
    ADD COLUMN IF NOT EXISTS snapshot_date date;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'price_predictions_origin_check') THEN
        ALTER TABLE price_predictions ADD CONSTRAINT price_predictions_origin_check
            CHECK (origin IN ('interactive', 'daily_snapshot'));
    END IF;
END $$;

-- One market snapshot per tender per day, however often the worker runs.
CREATE UNIQUE INDEX IF NOT EXISTS price_predictions_daily_snapshot_uq
    ON price_predictions (tender_id, snapshot_date)
    WHERE origin = 'daily_snapshot' AND prediction_scope = 'MARKET';

-- --------------------------------------------------- when an award first became known
CREATE TABLE IF NOT EXISTS tender_award_first_seen (
    tender_id   bigint PRIMARY KEY REFERENCES tenders(id) ON DELETE CASCADE,
    first_seen  timestamptz NOT NULL,
    source      text NOT NULL DEFAULT 'awards_harvest'
);

-- Backfill from the outbox: awards_harvest emits tender.awarded exactly once,
-- the first time it finds awards for a tender, so that event time is the
-- earliest moment the award was visible to us.
INSERT INTO tender_award_first_seen (tender_id, first_seen, source)
SELECT entity_id, min(created_at), 'backfill:ingest_events'
FROM ingest_events
WHERE event_type = 'tender.awarded' AND entity_type = 'tender'
GROUP BY entity_id
ON CONFLICT (tender_id) DO UPDATE
    SET first_seen = LEAST(tender_award_first_seen.first_seen, EXCLUDED.first_seen);

-- Any awarded tender the outbox missed: the current row time is the best we
-- have, and it can only be later than the truth, which makes scoring stricter.
INSERT INTO tender_award_first_seen (tender_id, first_seen, source)
SELECT tender_id, min(created_at), 'backfill:awards.created_at'
FROM awards
GROUP BY tender_id
ON CONFLICT (tender_id) DO NOTHING;

-- --------------------------------------------------- the scorecard
-- A prediction is BLIND if it was generated before the earlier of
--   (a) offers opening on Etimad (competitor prices can become public), and
--   (b) the first time we saw the award.
-- Only the latest blind market prediction per tender is scored, so a tender
-- that was snapshotted 30 times still counts once. Tenders with more than one
-- awardee are excluded: their total is not comparable to one contract value.
CREATE OR REPLACE VIEW prediction_scorecard AS
WITH award_totals AS (
    SELECT tender_id, count(*) AS awardees, sum(award_value) AS award_value
    FROM awards
    WHERE award_value IS NOT NULL AND award_value > 0
    GROUP BY tender_id),
cutoffs AS (
    SELECT t.id AS tender_id,
           LEAST(fs.first_seen, coalesce(t.offers_opening_date, fs.first_seen)) AS blind_until,
           fs.first_seen AS award_first_seen,
           t.offers_opening_date
    FROM tenders t
    JOIN tender_award_first_seen fs ON fs.tender_id = t.id),
latest_blind AS (
    SELECT DISTINCT ON (p.tender_id)
           p.*, c.blind_until, c.award_first_seen, c.offers_opening_date
    FROM price_predictions p
    JOIN cutoffs c ON c.tender_id = p.tender_id
    WHERE p.prediction_scope = 'MARKET' AND p.generated_at < c.blind_until
    ORDER BY p.tender_id, p.generated_at DESC, p.id DESC)
SELECT lb.id                                   AS prediction_id,
       lb.tender_id,
       lb.origin,
       lb.model_version,
       lb.evidence_tier,
       lb.suppression_reason,
       lb.suppression_reason IS NOT NULL       AS suppressed,
       lb.generated_at,
       lb.blind_until,
       lb.award_first_seen,
       round((extract(epoch FROM lb.blind_until - lb.generated_at) / 86400.0)::numeric, 1)
                                               AS lead_days,
       lb.p10, lb.p50, lb.p90,
       a.award_value,
       a.awardees,
       CASE WHEN lb.suppression_reason IS NULL AND a.awardees = 1
            THEN a.award_value BETWEEN lb.p10 AND lb.p90 END          AS interval_hit,
       CASE WHEN lb.suppression_reason IS NULL AND a.awardees = 1
            THEN round((abs(lb.p50 - a.award_value) / a.award_value)::numeric, 4) END
                                               AS abs_pct_error,
       (lb.suppression_reason IS NULL AND a.awardees = 1)             AS scorable
FROM latest_blind lb
JOIN award_totals a ON a.tender_id = lb.tender_id;

COMMENT ON VIEW prediction_scorecard IS
    'Latest blind MARKET prediction per awarded tender. Blind = generated before '
    'min(offers_opening_date, award first seen). p10..p90 is a nominal 80% interval.';
