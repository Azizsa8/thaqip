-- 0023: eligibility & fit scoring (PRD "Thaqip for Contractors" §5.1, T-ELIG-*).
--
-- Two things this schema exists to support honestly, not just document:
--
-- 1. The fit score must never assume eligibility when the classification
--    requirement is unknown. `tender_details` is filled in by a real fetch
--    of Etimad's detail-page components (services/ingestion/src/
--    thaqip_ingestion/etimad/details.py, already live for pursued tenders in
--    award_watch.py; this migration lets the same fetcher's output be stored
--    for ANY tender, not just pursued ones). Until a tender has a row here,
--    application code must treat classification/region as unknown — there is
--    no "assume compliant" default anywhere in this schema.
-- 2. A company's fit profile (self-declared activities/regions/classification
--    grades) is private to its tenant, same as every other tenant-scoped
--    table in this project.

-- ------------------------------------------------------------ company_profiles
-- One row per tenant. Self-declared (PRD §5.1: "classification fields and
-- grade (self-declared, validated later against booklet requirements)") —
-- this table never claims to be a verified license record.
CREATE TABLE IF NOT EXISTS company_profiles (
    tenant_id             bigint      PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    activity_ids          bigint[]    NOT NULL DEFAULT '{}',  -- tenders.activity_id values (Etimad tenderActivityId)
    regions               text[]      NOT NULL DEFAULT '{}',  -- free-text region names, matched against tender_details.execution_location
    classification_grades text[]      NOT NULL DEFAULT '{}',  -- self-declared classification tokens, e.g. 'أعمال الميكانيكية', 'التكييف المركزي'
    certifications        text[]      NOT NULL DEFAULT '{}',  -- ISO / civil-defence / medical-gas etc, informational only (not yet scored — no verified requirement source)
    min_project_value     numeric(14,2),
    max_project_value     numeric(14,2),
    updated_by            bigint      REFERENCES users(id) ON DELETE SET NULL,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE company_profiles IS
    'Self-declared contractor fit profile, one row per tenant. Feeds the deterministic fit score (thaqip_ingestion.fit_score); never itself treated as a verified license or classification record.';

-- ------------------------------------------------------------- tender_details
-- Structured fields parsed from Etimad's detail-page view-components
-- (GetRelationsDetailsViewComponenet / GetTenderDatesViewComponenet). One row
-- per tender, written once fetched; a missing row means "not fetched yet",
-- not "no requirement" — application code must distinguish the two.
CREATE TABLE IF NOT EXISTS tender_details (
    tender_id                bigint      PRIMARY KEY REFERENCES tenders(id) ON DELETE CASCADE,
    classification_required  boolean,               -- derived: relations['مجال التصنيف'] not 'غير مطلوب'
    classification_text      text,                  -- raw value of 'مجال التصنيف'
    execution_location       text,                  -- raw value of 'مكان التنفيذ'
    activity_name_detail     text,                  -- raw value of 'نشاط المنافسة' (cross-check against tenders.activity_name_raw)
    enquiries_deadline       timestamptz,           -- 'آخر موعد لإستلام الإستفسارات'
    stop_period_days         int,                   -- 'فترة التوقف'
    expected_award_date      date,                  -- 'التاريخ المتوقع للترسية'
    work_start_date          date,                  -- 'تاريخ بدء الأعمال / الخدمات'
    site_visit_date          timestamptz,           -- present on some tenders only; null when the source has no such row
    relations_raw            jsonb       NOT NULL DEFAULT '{}',  -- full label->value dict, lossless retention
    dates_raw                jsonb       NOT NULL DEFAULT '{}',  -- full label->value dict, lossless retention
    fetch_error              text,                  -- non-null if the last fetch attempt failed (never silently absent)
    fetched_at                timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tender_details IS
    'Structured detail-page fields per tender (PRD §5.1, T-ELIG-01). Absence of a row means "not fetched yet" and must never be read as "no classification requirement" — check classification_required, which is itself null until a successful fetch.';
COMMENT ON COLUMN tender_details.classification_required IS
    'null = not yet determined (fetch missing or failed). Only relations_raw is authoritative; this column is a cached, honestly-nullable derivation of it.';

CREATE INDEX IF NOT EXISTS tender_details_fetched_idx ON tender_details (fetched_at);
