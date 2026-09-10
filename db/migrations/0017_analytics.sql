-- 0017: curated analytics schema for Superset (and any other BI reader).
--
-- Superset is a single-workspace tool: anyone who can open a dashboard can
-- open SQL Lab. So it must never see tenant-private rows (pursuits, bid
-- scenarios, settings, users, sessions, auth_events). The boundary is a
-- dedicated schema of views over the SHARED public-tender corpus only, read
-- by a role that has no grant on anything else.
--
-- Column names are stable snake_case (Superset refers to them in saved
-- charts); values stay Arabic. Human labels are set as dataset verbose names
-- by analytics/superset/provision.py.

CREATE SCHEMA IF NOT EXISTS analytics;

-- ---------------------------------------------------------------- tenders
CREATE OR REPLACE VIEW analytics.tenders AS
WITH med AS (
    SELECT tender_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY offer_value) AS m
    FROM offers WHERE offer_value > 0
    GROUP BY tender_id),
-- An offer under 5% or over 20x its tender's median is a unit price, a
-- placeholder (1 SAR bids exist) or a unit mistake. It stays in the data,
-- flagged, but never defines "lowest", the spread or a price ratio.
ov AS (
    SELECT o.tender_id, o.offer_value,
           (o.offer_value < 0.05 * med.m OR o.offer_value > 20 * med.m) AS outlier
    FROM offers o JOIN med USING (tender_id)
    WHERE o.offer_value > 0),
o AS (
    SELECT ov.tender_id,
           count(*)::int                                               AS offers_n,
           count(*) FILTER (WHERE outlier)::int                        AS outlier_offers_n,
           count(*) FILTER (WHERE NOT outlier)::int                    AS plausible_n,
           (min(offer_value) FILTER (WHERE NOT outlier))::float        AS lowest_offer,
           (max(offer_value) FILTER (WHERE NOT outlier))::float        AS highest_offer,
           max(med.m)::float                                           AS median_offer
    FROM ov JOIN med USING (tender_id)
    GROUP BY ov.tender_id),
a AS (
    SELECT DISTINCT ON (tender_id) tender_id, award_value, vendor_id
    FROM awards WHERE award_value IS NOT NULL
    ORDER BY tender_id, id)
SELECT t.id                                             AS tender_id,
       t.source,
       t.reference_number,
       t.name                                           AS tender_name,
       -- Forsah publishes only a publisher TYPE ("partner"/"business"),
       -- never the organisation, so it must not look like an agency name.
       CASE WHEN t.source = 'forsah'
            THEN 'فرصة · جهة غير مُعلنة (' || coalesce(t.agency_name_raw, '?') || ')'
            ELSE coalesce(ag.canonical_name, t.agency_name_raw, 'غير معروف') END AS agency,
       t.branch_name                                    AS branch,
       coalesce(nullif(t.activity_name_raw, ''), 'غير مصنف') AS activity,
       coalesce(t.tender_type_name, 'غير محدد')         AS tender_type,
       CASE WHEN a.tender_id IS NOT NULL THEN 'مُرسّاة'
            WHEN t.last_offer_date > now() THEN 'مفتوحة'
            ELSE 'مغلقة' END                            AS stage,
       t.inside_ksa,
       t.booklet_price::float                           AS booklet_price,
       t.financial_fees::float                          AS financial_fees,
       t.published_at,
       t.last_offer_date,
       t.offers_opening_date,
       coalesce(t.offers_opening_date, t.last_offer_date, t.published_at) AS event_date,
       (t.last_offer_date::date - current_date)         AS days_to_close,
       coalesce(o.offers_n, 0)                          AS offers_n,
       coalesce(o.outlier_offers_n, 0)                  AS outlier_offers_n,
       o.lowest_offer,
       o.highest_offer,
       o.median_offer,
       CASE WHEN o.plausible_n > 1 AND o.lowest_offer > 0
            THEN round(((o.highest_offer / o.lowest_offer - 1) * 100)::numeric, 1)::float END
                                                        AS spread_pct,
       a.award_value::float                             AS award_value,
       v.canonical_name                                 AS winner,
       CASE WHEN a.award_value IS NULL OR o.plausible_n IS NULL OR o.plausible_n < 2 THEN NULL
            ELSE a.award_value <= o.lowest_offer * 1.0001 END AS lowest_won,
       CASE WHEN a.award_value IS NULL THEN NULL
            WHEN a.award_value < 50000 THEN '1. أقل من 50 ألف'
            WHEN a.award_value < 250000 THEN '2. 50–250 ألف'
            WHEN a.award_value < 1000000 THEN '3. 250 ألف–1 مليون'
            ELSE '4. فوق مليون' END                    AS award_band
FROM tenders t
LEFT JOIN agencies ag ON ag.id = t.agency_id
LEFT JOIN o ON o.tender_id = t.id
LEFT JOIN a ON a.tender_id = t.id
LEFT JOIN vendors v ON v.id = a.vendor_id;

-- ----------------------------------------------------------------- offers
CREATE OR REPLACE VIEW analytics.offers AS
SELECT o.id                                              AS offer_id,
       o.tender_id,
       t.tender_name, t.agency, t.activity, t.tender_type, t.stage, t.event_date,
       coalesce(v.canonical_name, o.vendor_name_raw)     AS vendor,
       o.offer_value::float                              AS offer_value,
       o.is_winner,
       CASE WHEN o.is_winner THEN 'فائز' ELSE 'غير فائز' END AS outcome,
       o.technical_pass,
       o.rank                                            AS offer_rank,
       count(*) OVER (PARTITION BY o.tender_id)::int     AS bidders_n,
       (o.offer_value < 0.05 * t.median_offer OR o.offer_value > 20 * t.median_offer) AS is_outlier,
       CASE WHEN o.offer_value BETWEEN 0.05 * t.median_offer AND 20 * t.median_offer
            THEN round((o.offer_value / nullif(t.lowest_offer, 0))::numeric, 4)::float END
                                                         AS ratio_to_lowest,
       CASE WHEN o.offer_value BETWEEN 0.05 * t.median_offer AND 20 * t.median_offer
            THEN round((o.offer_value / nullif(t.median_offer, 0))::numeric, 4)::float END
                                                         AS ratio_to_median
FROM offers o
JOIN analytics.tenders t ON t.tender_id = o.tender_id
LEFT JOIN vendors v ON v.id = o.vendor_id
WHERE o.offer_value > 0;

-- ----------------------------------------------------------------- awards
CREATE OR REPLACE VIEW analytics.awards AS
SELECT w.id                                              AS award_id,
       w.tender_id,
       t.tender_name, t.agency, t.activity, t.tender_type, t.event_date,
       coalesce(v.canonical_name, 'غير معروف')           AS winner,
       w.award_value::float                              AS award_value,
       t.offers_n, t.lowest_offer, t.median_offer, t.spread_pct, t.lowest_won,
       t.award_band,
       CASE WHEN t.median_offer > 0
            THEN round(((1 - w.award_value / t.median_offer) * 100)::numeric, 1)::float END
                                                         AS discount_vs_median_pct
FROM awards w
JOIN analytics.tenders t ON t.tender_id = w.tender_id
LEFT JOIN vendors v ON v.id = w.vendor_id
WHERE w.award_value IS NOT NULL;

-- ------------------------------------------------------- vendor scorecard
CREATE OR REPLACE VIEW analytics.vendor_scorecard AS
SELECT vendor,
       count(*)::int                                     AS bids,
       count(*) FILTER (WHERE is_winner)::int            AS wins,
       round((100.0 * count(*) FILTER (WHERE is_winner) / count(*))::numeric, 1)::float AS win_rate_pct,
       coalesce(sum(offer_value) FILTER (WHERE is_winner), 0)::float AS awarded_value,
       round(avg(ratio_to_lowest)::numeric, 3)::float    AS avg_ratio_to_lowest,
       count(DISTINCT agency)::int                       AS agencies_n,
       count(DISTINCT activity)::int                     AS activities_n,
       min(event_date)                                   AS first_seen,
       max(event_date)                                   AS last_seen
FROM analytics.offers
GROUP BY vendor;

-- ------------------------------------------------------- agency scorecard
CREATE OR REPLACE VIEW analytics.agency_scorecard AS
SELECT agency,
       count(*)::int                                     AS tenders,
       count(*) FILTER (WHERE stage = 'مفتوحة')::int     AS open_now,
       count(*) FILTER (WHERE stage = 'مُرسّاة')::int     AS awarded,
       coalesce(sum(award_value), 0)::float              AS awarded_value,
       round(avg(offers_n) FILTER (WHERE offers_n > 0)::numeric, 2)::float AS avg_bidders,
       round((100.0 * count(*) FILTER (WHERE lowest_won)
              / nullif(count(*) FILTER (WHERE lowest_won IS NOT NULL), 0))::numeric, 1)::float
                                                         AS lowest_wins_pct,
       round(avg(spread_pct)::numeric, 1)::float         AS avg_spread_pct,
       count(DISTINCT activity)::int                     AS activities_n
FROM analytics.tenders
GROUP BY agency;

-- -------------------------------------------------------------- BoQ items
CREATE OR REPLACE VIEW analytics.boq_items AS
SELECT b.id AS item_id, b.tender_id, t.tender_name, t.agency, t.activity, t.event_date,
       b.item_no, b.description, b.unit, b.qty::float AS qty, b.confidence
FROM boq_items b
JOIN analytics.tenders t ON t.tender_id = b.tender_id;

-- ------------------------------------------------------- pipeline health
CREATE OR REPLACE VIEW analytics.ingest_runs AS
SELECT id AS run_id, connector, started_at, finished_at,
       extract(epoch FROM finished_at - started_at)::float AS duration_s,
       ok, CASE WHEN ok THEN 'ناجح' ELSE 'فاشل' END AS result,
       pages, items_seen, items_new, items_changed,
       left(error, 300) AS error
FROM ingest_runs;

-- ------------------------------------------------------------- the reader
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'superset_ro') THEN
        -- NOLOGIN here; bin/bootstrap-superset.sh sets LOGIN + a generated
        -- password so no secret ever lives in a migration file.
        CREATE ROLE superset_ro NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA analytics TO superset_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO superset_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO superset_ro;
REVOKE CREATE ON SCHEMA public FROM superset_ro;
ALTER ROLE superset_ro SET default_transaction_read_only = on;
ALTER ROLE superset_ro SET statement_timeout = '60s';
ALTER ROLE superset_ro SET search_path = analytics;

-- PUBLIC gets CONNECT and TEMPORARY on every database by default. Take both
-- back so only the owner and the analytics reader can connect, and nobody
-- but the owner can create even session-local tables here.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO superset_ro', current_database());
END $$;
