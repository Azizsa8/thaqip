-- 0013: operator evidence note/link per compliance matrix row.
ALTER TABLE compliance_items
  ADD COLUMN IF NOT EXISTS evidence_ref text;
