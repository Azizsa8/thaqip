-- 0024: bid pipeline milestones (PRD "Thaqip for Contractors" §5.2, T-MILE-01).
--
-- Fixes a real pre-existing bug found while building this: `pursuits` has
-- carried a tenant_id since 0015_p2w_canonical.sql, but its uniqueness
-- constraint was never updated off the pre-multi-tenant `UNIQUE (tender_id)`.
-- Since tenders are shared platform-wide data (no tenant_id of their own —
-- see tenders in 0001_init.sql), that constraint means a SECOND tenant
-- trying to pursue a tender another tenant already pursues gets a raw
-- unique-violation 500 from POST /api/pursuits, not a normal per-tenant
-- pursuit. This was invisible with one tenant in production; it is
-- load-bearing now that multiple paying contractor tenants are the point.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pursuits_tender_id_key') THEN
        ALTER TABLE pursuits DROP CONSTRAINT pursuits_tender_id_key;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pursuits_tenant_tender_key') THEN
        ALTER TABLE pursuits ADD CONSTRAINT pursuits_tenant_tender_key UNIQUE (tenant_id, tender_id);
    END IF;
END $$;

-- ------------------------------------------------------------ pursuit_milestones
-- The fixed PRD sequence: booklet_purchased -> site_visit -> enquiries_deadline
-- -> addenda_received -> bond_issued -> submitted -> opened -> awarded/lost.
-- One row per (pursuit, milestone), seeded for every pursuit at creation time
-- (services/console app.py's create_pursuit) so the pipeline UI always shows
-- the full checklist, not just the milestones a contractor has touched.
CREATE TABLE IF NOT EXISTS pursuit_milestones (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pursuit_id    bigint      NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
    milestone     text        NOT NULL,
    due_at        timestamptz,             -- pre-filled from the tender's own dates when known; null otherwise
    completed_at  timestamptz,
    data          jsonb       NOT NULL DEFAULT '{}',  -- milestone-specific fields, see column comment
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pursuit_id, milestone)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pursuit_milestones_milestone_check') THEN
        ALTER TABLE pursuit_milestones ADD CONSTRAINT pursuit_milestones_milestone_check
            CHECK (milestone IN (
                'booklet_purchased', 'site_visit', 'enquiries_deadline', 'addenda_received',
                'bond_issued', 'submitted', 'opened', 'awarded', 'lost'
            ));
    END IF;
END $$;

COMMENT ON COLUMN pursuit_milestones.data IS
    'Milestone-specific fields, all optional and contractor-entered: site_visit {attendee}; addenda_received {count, last_date}; bond_issued {amount, bank, pct_of_bid}; opened {bid_rank}.';

CREATE INDEX IF NOT EXISTS pursuit_milestones_pursuit_idx ON pursuit_milestones (pursuit_id);
-- Reminder job scan: incomplete milestones with a due date, cheapest first.
CREATE INDEX IF NOT EXISTS pursuit_milestones_due_idx ON pursuit_milestones (due_at)
    WHERE completed_at IS NULL AND due_at IS NOT NULL;

-- --------------------------------------------------------------- notifications
-- Extend the existing alert-engine table (0003_alerts.sql) to also carry
-- milestone reminders, per the PRD's own verification text ("reminder rows
-- in notifications with milestone ids"), rather than building a parallel
-- table. profile_id becomes optional; exactly one source is ever set.
ALTER TABLE notifications ALTER COLUMN profile_id DROP NOT NULL;
ALTER TABLE notifications
    ADD COLUMN IF NOT EXISTS pursuit_milestone_id bigint REFERENCES pursuit_milestones(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS milestone_offset text;  -- 'T-3' | 'T-1'

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'notifications_source_check') THEN
        ALTER TABLE notifications ADD CONSTRAINT notifications_source_check CHECK (
            (profile_id IS NOT NULL AND pursuit_milestone_id IS NULL AND milestone_offset IS NULL)
            OR
            (profile_id IS NULL AND pursuit_milestone_id IS NOT NULL AND milestone_offset IN ('T-3', 'T-1'))
        );
    END IF;
END $$;

-- One T-3 and one T-1 reminder per milestone, ever — the reminder job's own
-- dedupe query backs this up, but the constraint is what actually prevents
-- a double-send under concurrent job runs.
CREATE UNIQUE INDEX IF NOT EXISTS notifications_milestone_dedupe_idx
    ON notifications (pursuit_milestone_id, milestone_offset)
    WHERE pursuit_milestone_id IS NOT NULL;
