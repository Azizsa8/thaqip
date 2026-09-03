-- 0005: War Room v0 (PRD M3-1/M3-2) — pursuits and the compliance matrix
CREATE TABLE IF NOT EXISTS pursuits (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tender_id  bigint NOT NULL UNIQUE REFERENCES tenders(id),
  stage      text NOT NULL DEFAULT 'studying',   -- studying | pricing | writing | submitted | won | lost
  notes      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compliance_items (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pursuit_id  bigint NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
  requirement text NOT NULL,
  category    text NOT NULL DEFAULT 'general',   -- document | guarantee | qualification | deadline | technical | general
  source_ref  text NOT NULL,                     -- citation: clause/file/regulation the requirement comes from
  status      text NOT NULL DEFAULT 'missing',   -- missing | in_progress | met | n_a
  origin      text NOT NULL DEFAULT 'rule',      -- rule | llm | manual
  confidence  real NOT NULL DEFAULT 1.0,
  sort_order  int NOT NULL DEFAULT 100,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS compliance_pursuit_idx ON compliance_items (pursuit_id);
