-- 0020: official Saudi price indices (GASTAT, via the DataSaudi public API).
--
-- Replaces the pricing engine's flat 2%/yr placeholder with measured price
-- levels. One row per series per month. Values are GASTAT's published index
-- levels as served on the date in fetched_at; a later revision overwrites
-- the value and moves fetched_at.

CREATE TABLE IF NOT EXISTS price_indices (
    series         text        NOT NULL,   -- e.g. cpi.general, wpi.metal_machinery
    period         date        NOT NULL,   -- first day of the month the level describes
    value          numeric(14,6) NOT NULL CHECK (value > 0),
    source         text        NOT NULL DEFAULT 'gastat/datasaudi',
    source_series  text        NOT NULL,   -- cube + member it came from, for audit
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (series, period)
);

-- Shared public data, safe for the analytics reader.
CREATE OR REPLACE VIEW analytics.price_indices AS
SELECT series, period, value::float AS value, source, source_series, fetched_at
FROM price_indices;
