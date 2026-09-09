-- 0011: operator calculator defaults for the Settings tab.
CREATE TABLE IF NOT EXISTS user_calculator_prefs (
  id                    smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  default_markup_pct    numeric(6,2) NOT NULL DEFAULT 12.00,
  risk_tolerance        text NOT NULL DEFAULT 'balanced' CHECK (risk_tolerance IN ('low','balanced','high')),
  default_agency_id     bigint REFERENCES agencies(id),
  alert_frequency       text NOT NULL DEFAULT 'instant' CHECK (alert_frequency IN ('instant','hourly','daily')),
  retention_days        int NOT NULL DEFAULT 180 CHECK (retention_days BETWEEN 30 AND 3650),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

INSERT INTO user_calculator_prefs (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
