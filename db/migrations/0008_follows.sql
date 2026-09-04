-- 0008: light-follow (M2-3 "tender follow") — update alerts without a full pursuit
CREATE TABLE IF NOT EXISTS follows (
  tender_id  bigint PRIMARY KEY REFERENCES tenders(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now()
);
