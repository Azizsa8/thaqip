-- 0025: BoQ catalogue matching (PRD "Thaqip for Contractors" §5.3, T-MATCH-01).
--
-- boq_lines already carries catalogue_item_id/match_confidence/
-- match_confirmed_by/match_confirmed_at (0022_boq_workbench.sql): the
-- "currently selected" match and its confirmation state. What was missing
-- is somewhere to hold the PRD's required "two nearest catalogue
-- candidates" for display before a human picks one — this adds exactly
-- that, as a small jsonb array rather than a new join table, since it is
-- read-only display data, never queried by catalogue_item_id itself
-- (confirmed matches still live in the indexed catalogue_item_id column).
ALTER TABLE boq_lines
    ADD COLUMN IF NOT EXISTS match_candidates jsonb NOT NULL DEFAULT '[]';

COMMENT ON COLUMN boq_lines.match_candidates IS
    'Up to 2 nearest catalogue_items candidates from thaqip_ingestion.catalogue_match, each {kind:"suggested", catalogue_item_id, code, name_ar, unit, score}. Empty when the catalogue has fewer than 2 unit-compatible items or nothing clears the confidence floor — a suggestion is never forced (T-MATCH-01: every suggestion needs >=2 candidates, so anything short of that is no suggestion at all, not a weak one).';
