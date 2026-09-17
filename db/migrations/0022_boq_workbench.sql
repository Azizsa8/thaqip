-- 0022: the BoQ workbench (PRD "Thaqip for Contractors" §5.3/§5.4/§6).
--
-- Contractors upload priced bills-of-quantities for deterministic review
-- (arithmetic, unrounded prices, concentration risk, capacity inversions,
-- ...) and, only with explicit per-upload consent, let their line items feed
-- a pooled item-price benchmark. Two things this schema must enforce, not
-- just document:
--
-- 1. Privacy by default. A tenant's own documents/lines/findings are never
--    readable by another tenant (enforced at the API layer; here we just
--    make tenant_id/document_id mandatory and indexed so that check is
--    cheap). Consent is opt-in, per upload, timestamped and revocable.
-- 2. The "never fewer than 5" rule (PRD §5.4, §8, T-BENCH-01): a benchmark
--    with n_contributors < 5 must be impossible to store, not just
--    impossible to display. That is a CHECK constraint on item_benchmarks,
--    not application logic.
--
-- catalogue_items is created before boq_lines so boq_lines.catalogue_item_id
-- can reference it directly, no follow-up ALTER TABLE needed.

-- --------------------------------------------------------- catalogue_items
-- The canonical item list ("باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", EA)
-- that BoQ lines get matched against. Matching is AI-assisted but only a
-- human-confirmed match (boq_lines.match_confirmed_by) ever feeds a
-- benchmark.
CREATE TABLE IF NOT EXISTS catalogue_items (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code         text        UNIQUE,             -- short slug, e.g. 'door-fire-90min-single'
    name_ar      text        NOT NULL,
    unit         text        NOT NULL,            -- normalized: EA, M2, M3, MT, LOT, ...
    family       text,                            -- e.g. 'doors','electrical','hvac'
    capacity_key text,                             -- e.g. 'kva','hp','mm' for capacity comparison
    attributes   jsonb       NOT NULL DEFAULT '{}',
    created_by   bigint      REFERENCES users(id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE catalogue_items IS
    'Canonical priced items that BoQ lines are matched to. Shared across all tenants (no tenant_id): the catalogue itself carries no company-specific price.';
COMMENT ON COLUMN catalogue_items.capacity_key IS
    'Attribute name used for capacity-inversion checks (bigger capacity priced lower => flag), e.g. kva/hp/mm/amps/tons.';

-- ---------------------------------------------------------------- boq_documents
CREATE TABLE IF NOT EXISTS boq_documents (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       bigint      NOT NULL REFERENCES tenants(id),
    tender_id       bigint      REFERENCES tenders(id),   -- nullable: not every BoQ ties to an ingested tender
    filename        text        NOT NULL,
    storage_key     text        NOT NULL,                  -- MinIO object key, tenant/<id>/boq/<uuid>
    sha256          text,
    scan_status     text        NOT NULL DEFAULT 'pending',
    uploaded_by     bigint      REFERENCES users(id) ON DELETE SET NULL,
    consent_pool    boolean     NOT NULL DEFAULT false,     -- contractor consents to pooling this doc's lines
    consent_at      timestamptz,
    total_declared  numeric(18,4),                          -- total the BoQ file itself claims
    total_computed  numeric(18,4),                           -- sum of our computed line totals
    created_at      timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'boq_documents_scan_status_check') THEN
        ALTER TABLE boq_documents ADD CONSTRAINT boq_documents_scan_status_check
            CHECK (scan_status IN ('pending', 'clean', 'infected', 'unscanned'));
    END IF;
END $$;

COMMENT ON TABLE boq_documents IS
    'One uploaded priced BoQ file per row. Private to tenant_id unless consent_pool is set for the upload.';
COMMENT ON COLUMN boq_documents.consent_pool IS
    'Whether the contractor consented, at upload time, to this document''s confirmed lines feeding the shared item-price pool. Per-upload, plain-Arabic checkbox (PRD §5.3); withdrawal is tracked in boq_consents, not by flipping this flag.';
COMMENT ON COLUMN boq_documents.scan_status IS
    'pending (queued) | clean | infected | unscanned (explicitly labelled, not blocked on AV). Never served to a client while infected.';

CREATE INDEX IF NOT EXISTS boq_documents_tenant_idx ON boq_documents (tenant_id);

-- ---------------------------------------------------------------- boq_lines
CREATE TABLE IF NOT EXISTS boq_lines (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id         bigint      NOT NULL REFERENCES boq_documents(id) ON DELETE CASCADE,
    line_no             int,
    category            text,
    item                text,
    description         text,
    spec                text,
    unit_raw            text,                     -- unit exactly as it appeared in the source file
    unit                text,                     -- normalized: EA, M2, M3, MT, LOT, ...
    qty                 numeric(18,4),
    unit_price          numeric(18,4),
    total               numeric(18,4),
    text_numbers        boolean     NOT NULL DEFAULT false,  -- qty/unit_price were stored as text in the source
    catalogue_item_id   bigint      REFERENCES catalogue_items(id),
    match_confidence    numeric(6,4),
    match_confirmed_by  bigint      REFERENCES users(id) ON DELETE SET NULL,
    match_confirmed_at  timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE boq_lines IS
    'Parsed BoQ line items. Only lines with match_confirmed_by set (a human confirmed the catalogue match) may ever be read into item_benchmarks (PRD T-MATCH-01).';
COMMENT ON COLUMN boq_lines.text_numbers IS
    'True if qty or unit_price were stored as text (not numeric) cells in the source spreadsheet — itself a BoQ quality signal (see rule text_number in boq_findings).';

CREATE INDEX IF NOT EXISTS boq_lines_document_idx ON boq_lines (document_id);
CREATE INDEX IF NOT EXISTS boq_lines_catalogue_item_idx ON boq_lines (catalogue_item_id)
    WHERE catalogue_item_id IS NOT NULL;

-- ------------------------------------------------------------- boq_findings
CREATE TABLE IF NOT EXISTS boq_findings (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id    bigint      NOT NULL REFERENCES boq_documents(id) ON DELETE CASCADE,
    line_id        bigint      REFERENCES boq_lines(id) ON DELETE CASCADE,  -- null: document-level finding
    rule           text        NOT NULL,
    severity       text        NOT NULL,
    message_ar     text        NOT NULL,
    suggestion_ar  text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'boq_findings_severity_check') THEN
        ALTER TABLE boq_findings ADD CONSTRAINT boq_findings_severity_check
            CHECK (severity IN ('info', 'warning', 'critical'));
    END IF;
END $$;

COMMENT ON TABLE boq_findings IS
    'Deterministic (non-AI) review output. rule is a machine-readable code, e.g. arithmetic_mismatch | unrounded_unit_price | duplicate_description | capacity_inversion | concentration_risk | missing_spec | text_number. line_id is null for document-level findings such as concentration_risk.';

CREATE INDEX IF NOT EXISTS boq_findings_document_idx ON boq_findings (document_id);

-- ---------------------------------------------------------------- item_benchmarks
-- Materialised (nightly job) pooled statistics. n_contributors >= 5 is the
-- product's core privacy/trust rule (PRD §5.4, §8): a row backed by fewer
-- than 5 distinct tenants must never exist, not merely never be displayed.
CREATE TABLE IF NOT EXISTS item_benchmarks (
    catalogue_item_id  bigint      NOT NULL REFERENCES catalogue_items(id),
    period_start       date        NOT NULL,
    period_end         date        NOT NULL,
    n_contributors     int         NOT NULL,
    n_lines            int         NOT NULL,
    p25                numeric(18,4),
    p50                numeric(18,4),
    p75                numeric(18,4),
    index_used         text,                      -- price_indices.series used to time-adjust, if any
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalogue_item_id, period_start, period_end)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'item_benchmarks_n_contributors_check') THEN
        ALTER TABLE item_benchmarks ADD CONSTRAINT item_benchmarks_n_contributors_check
            CHECK (n_contributors >= 5);
    END IF;
END $$;

COMMENT ON TABLE item_benchmarks IS
    'Pooled p25/p50/p75 per catalogue item per period, from consented+confirmed BoQ lines only. n_contributors >= 5 is a hard DB constraint (T-BENCH-01): a benchmark backed by fewer than 5 distinct tenants must never be storable, let alone displayed.';
COMMENT ON COLUMN item_benchmarks.index_used IS
    'price_indices.series used to time-adjust contributing lines onto a common period, if any (see 0020_price_indices.sql).';

-- ---------------------------------------------------------------- boq_consents
-- Consent is recorded independently of boq_documents.consent_pool so that
-- withdrawal has its own auditable timestamp and scope, and so a single
-- document could in principle carry more than one consent scope later.
CREATE TABLE IF NOT EXISTS boq_consents (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     bigint      NOT NULL REFERENCES tenants(id),
    document_id   bigint      NOT NULL REFERENCES boq_documents(id) ON DELETE CASCADE,
    scope         text        NOT NULL,          -- e.g. 'item_pool'
    granted_at    timestamptz NOT NULL DEFAULT now(),
    withdrawn_at  timestamptz
);

COMMENT ON TABLE boq_consents IS
    'Per-upload, per-scope consent grants/withdrawals (PRD §5.3, §9). Withdrawal stops future pooling within 24h; already-published aggregates are not recomputed retroactively.';

CREATE INDEX IF NOT EXISTS boq_consents_document_idx ON boq_consents (document_id);

-- ----------------------------------------------------- analytics.item_benchmarks
-- Aggregate-only view for Superset (see 0017_analytics.sql for the pattern
-- and the superset_ro role). Never expose tenant_id, document_id or any
-- per-company row through the analytics schema — item_benchmarks already
-- carries no such column, but we still select columns explicitly rather
-- than SELECT * so a future column addition here doesn't leak silently.
CREATE OR REPLACE VIEW analytics.item_benchmarks AS
SELECT b.catalogue_item_id,
       c.name_ar,
       c.unit,
       c.family,
       b.period_start,
       b.period_end,
       b.n_contributors,
       b.n_lines,
       b.p25::float AS p25,
       b.p50::float AS p50,
       b.p75::float AS p75,
       b.index_used
FROM item_benchmarks b
JOIN catalogue_items c ON c.id = b.catalogue_item_id;

GRANT SELECT ON analytics.item_benchmarks TO superset_ro;
