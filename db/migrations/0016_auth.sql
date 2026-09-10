-- 0016: real authentication. Until now tenant identity was a client-supplied
-- X-Thaqip-Tenant header with a default fallback, i.e. anyone who could reach
-- the API was whichever tenant they claimed to be. Identity now comes from a
-- credential: a password-backed user session, or a service token.

CREATE TABLE IF NOT EXISTS users (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     bigint NOT NULL REFERENCES tenants(id),
  username      text NOT NULL,
  password_hash text NOT NULL,          -- scrypt$n$r$p$salt_b64$hash_b64
  role          text NOT NULL DEFAULT 'member',   -- admin | member | viewer
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz,
  UNIQUE (username)
);
CREATE INDEX IF NOT EXISTS users_tenant_idx ON users (tenant_id);

-- Server-side sessions so a session can actually be revoked. The cookie holds
-- an opaque token; only its hash is stored, so a database read does not hand
-- the reader a working session.
CREATE TABLE IF NOT EXISTS sessions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  user_agent  text,
  ip          text
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_live_idx ON sessions (token_hash)
  WHERE revoked_at IS NULL;

-- Audit trail for authentication events (readiness guide: privileged access
-- and export events must be logged).
CREATE TABLE IF NOT EXISTS auth_events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username   text,
  user_id    bigint,
  tenant_id  bigint,
  event      text NOT NULL,             -- login_ok | login_fail | logout | denied
  detail     text,
  ip         text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_events_created_idx ON auth_events (created_at DESC);
