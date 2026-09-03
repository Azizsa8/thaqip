-- 0004: tag which lane first detected each tender, so the freshness SLO
-- counts only live poller detections (backfill/reconcile pollute the metric).
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS detected_by text NOT NULL DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS tenders_detected_by_idx ON tenders (detected_by);
